# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Support log API -- status, the enable toggle, and the download archive.

Endpoints: /api/support-logs, /api/support-logs/download
"""

import glob
import io
import logging
import os
import socket
import subprocess
import tarfile
import threading
import time

from flask import Blueprint, Response, jsonify, request

import support_log

log = logging.getLogger(__name__)
support_logs_bp = Blueprint("support_logs", __name__)

# One archive at a time. Each in-flight download holds a gzip window and a
# member buffer, and the board has better things to do with its memory.
_building = threading.Lock()


@support_logs_bp.get("/api/support-logs")
def api_support_logs():
    return jsonify({"ok": True, **support_log.summary()})


@support_logs_bp.post("/api/support-logs")
def api_support_logs_set():
    data = request.get_json(silent=True) or {}
    if "enabled" not in data:
        return jsonify({"ok": False, "error": "Provide 'enabled'"}), 400
    support_log.set_enabled(bool(data["enabled"]))
    return jsonify({"ok": True, **support_log.summary()})


def _context_report() -> str:
    """Board identity, so a log can be matched to the unit that produced it."""
    from helpers import (find_installed_app, get_bios_info, get_rauc_version,
                         read_hw_info)

    rows = [("Generated", time.strftime("%Y-%m-%d %H:%M:%S %Z")),
            ("Hostname", socket.gethostname()),
            ("Menu version", get_rauc_version() or "unknown")]
    try:
        bios = get_bios_info() or {}
        rows.append(("BIOS", f"{bios.get('vendor', '')} {bios.get('version', '')}".strip()
                     or "unknown"))
    except Exception:
        rows.append(("BIOS", "unavailable"))
    try:
        info = read_hw_info() or {}
        for label, value in info.get("summary") or []:
            rows.append((label, str(value).replace("\n", " ")))
    except Exception:
        rows.append(("Hardware", "unavailable"))
    try:
        app_info = find_installed_app() or {}
        rows.append(("Installed app",
                     f"{app_info.get('id', 'none')} {app_info.get('version', '')}".strip()))
    except Exception:
        rows.append(("Installed app", "unavailable"))

    status = support_log.summary()
    storage = "degraded" if status.get("degraded") else "mounted"
    reason = _storage_status().get("reason", "")
    if status.get("degraded") and reason:
        storage += f" ({reason})"
    if not os.path.ismount(support_log.STORAGE_MOUNT):
        if not status.get("degraded"):
            storage += " (boot status; not mounted now)"
    elif status.get("degraded"):
        storage += " (boot status; mounted now)"
    rows.append(("Persistent storage", storage))
    rows.append(("Log entries", status.get("entries", 0)))
    if status.get("broken"):
        rows.append(("Logging", f"stopped: {status['broken']}"))

    return "".join(f"{label}: {value}\n" for label, value in rows)


def _storage_status() -> dict:
    """Parse the key=value file written by setup_bulk_storage.sh."""
    data = {}
    try:
        text = support_log.STORAGE_STATUS.read_text(encoding="utf-8")
    except OSError:
        return data
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            data[key.strip()] = value.strip()
    return data


def _run(argv, timeout: int = 10) -> str:
    """Run a read-only diagnostic command; never raise."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              errors="replace", timeout=timeout, check=False)
    except FileNotFoundError:
        return f"({argv[0]} not installed)\n"
    except subprocess.TimeoutExpired:
        return f"(timed out after {timeout}s)\n"
    except Exception as exc:
        return f"(failed: {exc})\n"
    out = proc.stdout + proc.stderr
    if proc.returncode:
        out += f"(exit {proc.returncode})\n"
    return out or "(no output)\n"


def _storage_diagnosis(status: dict, mounted: bool, label: str, fs_type: str,
                       marker: bool) -> str:
    """One line naming the likely cause, from what setup_bulk_storage.sh saw
    at boot and what is mounted and on the partition now."""
    if mounted:
        return "Bulk storage is mounted."
    if status.get("state") == "mounted":
        return ("The boot check mounted bulk storage but it is no longer "
                "mounted (unmounted after boot). Reboot to remount.")
    if label == "rave-data" and marker:
        return ("The partition is initialized now but was not when the boot "
                "check ran (typically converted by a system update during "
                "this boot). Reboot to finish.")
    if label == "rave-data":
        return ("Labelled rave-data but the .rave-storage-v1 marker is "
                "missing: initialization did not complete.")
    if label == "app-b":
        return ("Legacy app-b partition: the next system update converts it "
                "to rave-data.")
    if not fs_type:
        return "No filesystem on the partition: it was never initialized."
    return (f"Unexpected filesystem ({fs_type}, label '{label or 'none'}'): "
            "left untouched by updates; needs a manual reinitialize.")


def _storage_report() -> str:
    """Why bulk storage is or is not mounted.

    Collects what setup_bulk_storage.sh checks -- the p5 device node, its
    label, the marker and the bind mounts -- plus the disk layout, since a
    missing or mislabelled p5 usually means an unexpected partition table.
    The status file is only written at boot, so its age and the partition's
    current contents are both reported: a system update can initialize p5
    after the check ran.
    """
    sections = []

    def section(title: str, body: str):
        sections.append(f"--- {title} ---\n{body.rstrip()}\n")

    status = _storage_status()
    device = status.get("device") or os.environ.get("RAVE_STORAGE_DEVICE",
                                                    "/dev/nvme0n1p5")
    present = os.path.exists(device)
    label = fs_type = ""
    listing = "(device missing)\n"
    if present:
        label = _run(["blkid", "-o", "value", "-s", "LABEL", device]).strip()
        fs_type = _run(["blkid", "-o", "value", "-s", "TYPE", device]).strip()
        # _run reports failures inline; a failed probe means "no value".
        label = "" if label.startswith("(") else label
        fs_type = "" if fs_type.startswith("(") else fs_type
        # debugfs opens read-only, so this sees the marker without mounting.
        listing = (_run(["debugfs", "-R", "ls -l /", device])
                   if fs_type.startswith("ext") else "(not ext*)\n")
    marker = ".rave-storage-v1" in listing
    mounted = os.path.ismount(support_log.STORAGE_MOUNT)

    lines = [_storage_diagnosis(status, mounted, label, fs_type, marker), ""]
    try:
        raw = support_log.STORAGE_STATUS.read_text(encoding="utf-8")
        written = time.strftime(
            "%Y-%m-%d %H:%M:%S %Z",
            time.localtime(support_log.STORAGE_STATUS.stat().st_mtime))
    except OSError as exc:
        raw, written = f"(unreadable: {exc})\n", "unknown"
    try:
        with open("/proc/uptime", encoding="ascii") as uptime:
            booted = time.strftime(
                "%Y-%m-%d %H:%M:%S %Z",
                time.localtime(time.time() - float(uptime.read().split()[0])))
    except (OSError, ValueError, IndexError):
        booted = "unknown"
    lines += [f"Booted: {booted}", f"Status written: {written}", raw.rstrip(),
              f"Mounted now: {'yes' if mounted else 'no'} "
              f"({support_log.STORAGE_MOUNT})",
              f"Device now: label={label or 'none'} type={fs_type or 'none'} "
              f"marker={'present' if marker else 'absent'}"]
    section("Diagnosis", "\n".join(lines))
    section(f"Root of {device} (debugfs, read-only)", listing)

    section("Block devices",
            _run(["lsblk", "-e", "1,7", "-o",
                  "NAME,SIZE,TYPE,FSTYPE,LABEL,PARTLABEL,UUID,MOUNTPOINTS,MODEL"]))
    nvme = sorted(glob.glob("/dev/nvme*n*"))
    section("blkid", _run(["blkid", *nvme]) if nvme else "(no NVMe devices)\n")
    disks = sorted(d for d in nvme if "p" not in d.rsplit("n", 1)[-1])
    for disk in disks:
        section(f"Partition table {disk}", _run(["sfdisk", "-l", disk]))

    mounts = _run(["findmnt", "-rn", "-o", "TARGET,SOURCE,FSROOT,FSTYPE"])
    section("Mounts under /var/lib/rave-menu",
            "\n".join(m for m in mounts.splitlines()
                      if m.startswith("/var/lib/rave-menu"))
            or "(none)\n")
    section("System update slots", _run(["rauc", "status"]))
    section("Free space",
            _run(["df", "-Pm", "/", "/var/lib/rave-menu/updates",
                  "/var/lib/rave-menu/uploads", str(support_log.STORAGE_MOUNT)]))
    section("rave-storage.service",
            _run(["journalctl", "-b", "-u", "rave-storage.service",
                  "--no-pager", "-n", "200"]))
    section("Kernel NVMe messages",
            _run(["sh", "-c", "dmesg 2>&1 | grep -i nvme | tail -n 50"]))
    return "\n".join(sections)


def _best_effort(label: str, producer) -> bytes:
    """Run *producer*, substituting a note on failure.

    A hardware report that hangs or throws must not turn the download into a
    500 -- the logs are the point of the archive, the reports are a bonus.
    """
    try:
        return (producer() or "").encode("utf-8", "replace")
    except Exception as exc:
        log.warning("support archive: %s unavailable: %s", label, exc)
        return f"({label} unavailable: {exc})\n".encode("utf-8")


class _Sink:
    """A write target that hands each chunk straight to the response."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, data) -> int:
        self.buf += data
        return len(data)

    def drain(self) -> bytes:
        chunk = bytes(self.buf)
        del self.buf[:]
        return chunk


def _archive(stem: str):
    """Yield a .tar.gz of the logs and reports, a member at a time.

    Streamed rather than assembled in memory: the logs can run to a couple of
    hundred megabytes and this runs on the board.

    Releasing ``_building`` is the caller's job, not this generator's: a HEAD
    request never starts the body, so a ``finally`` here would not run.
    """
    sink = _Sink()
    with tarfile.open(fileobj=sink, mode="w|gz") as tar:
        def add_bytes(name: str, payload: bytes):
            info = tarfile.TarInfo(f"{stem}/{name}")
            info.size = len(payload)
            info.mtime = int(time.time())
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(payload))
            return sink.drain()

        from helpers import read_displays, read_hw_info

        yield add_bytes("context.txt",
                        _best_effort("context", _context_report))
        yield add_bytes("hardware-report.txt",
                        _best_effort("hardware report",
                                     lambda: (read_hw_info() or {}).get("full", "")))
        yield add_bytes("video-report.txt",
                        _best_effort("video report",
                                     lambda: (read_displays() or {}).get("full", "")))
        yield add_bytes("storage-report.txt",
                        _best_effort("storage report", _storage_report))

        for path in support_log.log_files():
            # Logs written while rave-data was unmounted share a filename
            # with the real ones, so keep the directory in the arcname.
            arcname = f"{stem}/{path.parent.name}/{path.name}"
            try:
                handle = open(path, "rb")
            except OSError as exc:
                log.warning("support archive: skipped %s: %s", path, exc)
                continue
            # Size is taken from the open handle, not the path: a rotation
            # between the two would pair this header with a different, shorter
            # file, and tar would abort part way through writing the member.
            with handle:
                info = tar.gettarinfo(fileobj=handle, arcname=arcname)
                tar.addfile(info, handle)
            yield sink.drain()
    yield sink.drain()


@support_logs_bp.get("/api/support-logs/download")
def api_support_logs_download():
    if not _building.acquire(blocking=False):
        return jsonify({"ok": False,
                        "error": "another support archive is being built"}), 429
    # Never request.host: that is caller-controlled and ends up in a header.
    stem = f"buddy-support-{socket.gethostname()}-{time.strftime('%Y%m%d-%H%M%S')}"
    response = Response(
        _archive(stem),
        mimetype="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{stem}.tar.gz"'},
    )
    # Runs whether the body was streamed, abandoned, or never started at all,
    # which a HEAD request does not do.
    response.call_on_close(_building.release)
    return response
