# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Action routes -- script execution, SSE streaming, file browser, power, updates.

Endpoints: /api/run/<action>, /api/stream/<job_id>, /api/browse,
           /api/install-nvme, /api/poweroff, /api/reboot,
           /api/check-update, /api/install-update, /api/usb-bundles
"""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from flask import Blueprint, Response, abort, jsonify, request, stream_with_context
from helpers import (ACTIONS, APP_ROOT, JOBS, SCRIPTS_DIR,
                     UPDATE_MANIFEST_URL, BulkOperationBusy, acquire_bulk_lock,
                     clear_bios_update_pending, find_usb_bundles,
                     get_bios_info, get_bios_update_pending, get_rauc_version,
                     get_nvme_free_mib, get_region_capacity, list_browse_sources,
                     register_job, resolve_browse_source, run_cmd)
import support_log

log = logging.getLogger(__name__)
actions = Blueprint("actions", __name__)

DOWNLOAD_DIR = "/var/lib/rave-menu/updates"
UPLOAD_DIR = "/var/lib/rave-menu/uploads"
STORAGE_STATUS_FILE = "/run/rave-menu/storage-status"
BULK_ACTIONS = {"board-test", "versal-jtag", "install-app", "bios-update"}

# SSE heartbeat interval. A bare ":\n\n" comment line keeps the socket from
# being culled by intermediaries during long silent periods (e.g. flashcp on
# a 128 MB OSPI image takes ~100s with no host-side stdout).
SSE_HEARTBEAT_S = 10.0

# Support-log event name per action. Naming them here rather than at the call
# sites keeps the log vocabulary stable even as the UI changes.
JOB_EVENTS = {
    "board-test": "versal.program",
    "versal-jtag": "versal.jtag",
    "app-store": "app-store.launch",
    "reconfigure-versal": "versal.reconfigure",
    "install-ubuntu": "ubuntu.install",
    "uninstall-ubuntu": "ubuntu.uninstall",
    "install-app": "app.install",
    "bios-update": "bios.update",
}


def _job_event(action: str, args: list) -> str:
    # Reprogramming the Versal from an installed app reuses the installer
    # script, so the action id alone cannot tell the two apart.
    if action == "install-app" and "--recovery" in args:
        return "app.reprogram-versal"
    return JOB_EVENTS.get(action, f"action.{action}")



def _is_mounted(path: str) -> bool:
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as mounts:
            for line in mounts:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == path:
                    return True
    except Exception:
        pass
    return os.path.ismount(path)


def _script_for(action: str) -> Path:
    p = ACTIONS.get(action)
    if not p:
        abort(404, f"Unknown action '{action}'")
    if not p.exists():
        abort(500, f"Script missing: {p}")
    if not os.access(p, os.X_OK):
        abort(500, f"Script not executable: {p}")
    return p


@actions.post("/api/run/<action>")
def run_action(action: str):
    args = request.json.get("args", []) if request.is_json else []
    if not isinstance(args, list):
        return jsonify({"error": "args must be a list"}), 400

    job_id, cmd = start_job(action, [str(a) for a in args])
    return jsonify({"job_id": job_id, "cmd": cmd})


def start_job(action: str, args: list) -> tuple:
    """Start an ACTIONS script as a streaming job.

    Spawns the script registered under ``action`` with ``args`` (argv list,
    never a shell string) and registers it as a job for SSE streaming via
    ``/api/stream/<job_id>``. Returns ``(job_id, cmd)``. Raises (via ``abort``)
    if the action is unknown or its script is missing/not executable.

    Shared by ``/api/run/<action>`` and the App Store install endpoints so the
    job runner is defined in exactly one place.
    """
    script = _script_for(action)

    env = os.environ.copy()
    env.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
    env["RAVE_ACTION"] = action
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [str(script)] + [str(a) for a in args]

    # Opened before the lock is taken so that a refusal is recorded too: "I
    # pressed the button and it said busy" is itself a support question.
    entry = support_log.begin(_job_event(action, args),
                              {"action": action, "args": list(args)})

    lock_file = None
    try:
        if action in BULK_ACTIONS:
            lock_file = acquire_bulk_lock()
        proc = subprocess.Popen(
            cmd,
            cwd=str(SCRIPTS_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            # A script emitting one undecodable byte would otherwise raise in
            # the drain thread and strand the job.
            encoding="utf-8",
            errors="replace",
            env=env,
            pass_fds=(lock_file.fileno(),) if lock_file else (),
        )
    except BulkOperationBusy as exc:
        if entry:
            entry.line(str(exc))
            entry.finish(None, "rejected-busy")
        abort(409, str(exc))
    except OSError as exc:
        if entry:
            entry.line(str(exc))
            entry.finish(None, "launch-failed")
        abort(500, f"could not start {action}: {exc}")
    finally:
        if lock_file:
            lock_file.close()

    return register_job(proc, entry), cmd


@actions.get("/api/stream/<job_id>")
def stream_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "unknown job id"}), 404

    @stream_with_context
    def gen():
        # Replay what the job has buffered, then follow it live. The job's own
        # thread does the reading, so several clients can watch one job and a
        # client may attach after the script has already exited.
        cursor = 0
        try:
            while True:
                lines, cursor, dropped = job.since(cursor)
                if dropped:
                    yield f"data: [... {dropped} earlier lines not buffered ...]\n\n"
                for line in lines:
                    yield f"data: {line}\n\n"
                if lines:
                    continue
                with job.cond:
                    if job.seq > cursor:
                        continue
                    if job.rc is not None:
                        break
                    signalled = job.cond.wait(SSE_HEARTBEAT_S)
                if not signalled:
                    # An SSE comment line, ignored by EventSource clients but
                    # still a write on the socket, so it defeats idle timeouts.
                    yield ": keepalive\n\n"
            yield f"event: status\ndata: exited:{job.rc}\n\n"
        except GeneratorExit:
            # Browser disconnected. The job keeps draining in the background
            # so the shell runs to completion (e.g. reconfigure_versal).
            return
        except Exception as exc:
            yield f"event: status\ndata: error:{str(exc)}\n\n"

    return Response(gen(), mimetype="text/event-stream")


@actions.post("/api/install-nvme")
def api_install_nvme():
    from helpers import running_from_nvme

    if running_from_nvme():
        return jsonify({"ok": False, "error": "Already running from NVMe"}), 400

    # On VFAT, /boot/EFI and /boot/efi collide case-insensitively. When
    # /boot/efi is mounted from NVMe, writing /boot/EFI/BOOT/grub.cfg updates
    # the wrong ESP. Unmount /boot/efi so we always edit the USB boot config.
    if _is_mounted("/boot/efi"):
        umount_result = run_cmd(["/bin/umount", "/boot/efi"], timeout=5)
        if umount_result.returncode not in (0,) and _is_mounted("/boot/efi"):
            err = (umount_result.stderr or umount_result.stdout or "failed to unmount /boot/efi").strip()
            return jsonify({"ok": False, "error": err}), 500

    grub_cfg = Path("/boot/EFI/BOOT/grub.cfg")
    if not grub_cfg.exists():
        return jsonify({"ok": False, "error": "Cannot find grub.cfg at " + str(grub_cfg)}), 500
    try:
        text = grub_cfg.read_text()
        updated = text
        updated = updated.replace("set default='RAVE2 Buddy'", "set default='Install to NVME'")
        # Legacy fallbacks for older grub configs
        updated = updated.replace('set default="xen"', 'set default="install"')
        updated = updated.replace('set default="boot"', 'set default="install"')
        updated = updated.replace('set default=2', 'set default="install"')
        updated = updated.replace('default=xen', 'default=install')
        updated = updated.replace('default=boot', 'default=install')
        text = updated
        grub_cfg.write_text(text)
    except Exception as exc:
        return jsonify({"ok": False, "error": "Failed to update grub.cfg: " + str(exc)}), 500
    support_log.record("nvme.install", {}, "grub.cfg set to the NVMe installer", 0,
                       status="reboot-requested")
    cmd = ["/bin/sh", "-lc", "(sleep 1; systemctl reboot || /sbin/reboot || reboot) >/dev/null 2>&1 &"]
    run_cmd(cmd, timeout=3)
    return jsonify({"ok": True, "message": "Rebooting into NVMe installer..."})


@actions.post("/api/poweroff")
def api_poweroff():
    cmd = ["/bin/sh", "-lc", "(sleep 1; systemctl poweroff || /sbin/poweroff || poweroff) >/dev/null 2>&1 &"]
    result = run_cmd(cmd, timeout=3)
    if result.returncode not in (0,):
        return jsonify({"ok": False, "error": (result.stderr or result.stdout or "poweroff failed").strip()}), 500
    return jsonify({"ok": True, "message": "poweroff requested"})


@actions.post("/api/reboot")
def api_reboot():
    cmd = ["/bin/sh", "-lc", "(sleep 1; systemctl reboot || /sbin/reboot || reboot) >/dev/null 2>&1 &"]
    result = run_cmd(cmd, timeout=3)
    if result.returncode not in (0,):
        return jsonify({"ok": False, "error": (result.stderr or result.stdout or "reboot failed").strip()}), 500
    return jsonify({"ok": True, "message": "reboot requested"})


@actions.post("/api/reboot-to")
def api_reboot_to():
    """Arm a one-shot GRUB boot into another entry, then reboot.

    Body: {"entry": "menu"|"ubuntu"|"ubuntu-prev"|"ubuntu-recovery"|"app-a"}.
    Only that single boot uses ``entry``; the normal default takes over again
    on the boot after that, whether or not the one-shot target came up.
    """
    from helpers import get_boot_default, set_next_boot_entry

    entry = request.json.get("entry") if request.is_json else None
    if entry not in ("menu", "ubuntu", "ubuntu-prev", "ubuntu-recovery", "app-a"):
        return jsonify({"ok": False, "error": "invalid entry"}), 400

    state = get_boot_default()
    if entry.startswith("ubuntu") and not state.get("installed"):
        return jsonify({"ok": False, "error": "Ubuntu is not installed"}), 400
    if entry == "app-a" and not state.get("app_installed"):
        return jsonify({"ok": False, "error": "No application is installed"}), 400

    ok, err = set_next_boot_entry(entry)
    if not ok:
        return jsonify({"ok": False, "error": err}), 500

    support_log.record("boot.next-entry", {"entry": entry}, "", 0,
                       status="reboot-requested")
    cmd = ["/bin/sh", "-lc", "(sleep 1; systemctl reboot || /sbin/reboot || reboot) >/dev/null 2>&1 &"]
    result = run_cmd(cmd, timeout=3)
    if result.returncode not in (0,):
        return jsonify({"ok": False, "error": (result.stderr or result.stdout or "reboot failed").strip()}), 500
    return jsonify({"ok": True, "message": f"rebooting once into {entry}"})


@actions.get("/api/boot-default")
def api_get_boot_default():
    """Return which OS boots by default and which targets are installed.

    Adds ``app_name`` (the installed app's display name) so the boot-default
    chooser can label the app row by its catalog title rather than the raw slot.
    """
    from helpers import find_installed_app, get_boot_default, get_grub_timeout

    data = get_boot_default()
    data["timeout"] = get_grub_timeout()
    if data.get("app_installed"):
        # Prefer the on-slot manifest name; fall back to the grubenv app_title
        # (always readable even when the app slot is not mounted), then generic.
        info = find_installed_app()
        data["app_name"] = (
            info.get("name")
            or info.get("id")
            or data.get("app_title")
            or "Application"
        )
    return jsonify(data)


@actions.post("/api/boot-default")
def api_set_boot_default():
    """Set the default boot OS. Body: {"target": "buddy"|"ubuntu"|"app"}."""
    from helpers import get_boot_default, set_boot_default

    target = request.json.get("target") if request.is_json else None
    if target not in ("ubuntu", "buddy", "app"):
        return jsonify({"ok": False, "error": "target must be 'buddy', 'ubuntu' or 'app'"}), 400

    state = get_boot_default()
    if target == "ubuntu" and not state.get("installed"):
        return jsonify({"ok": False, "error": "Ubuntu is not installed"}), 400
    if target == "app" and not state.get("app_installed"):
        return jsonify({"ok": False, "error": "No application is installed"}), 400

    ok, err = set_boot_default(target)
    if not ok:
        return jsonify({"ok": False, "error": err}), 500
    support_log.record("boot.default", {"target": target}, "", 0)
    return jsonify({"ok": True, "default": target})


@actions.post("/api/boot-timeout")
def api_set_boot_timeout():
    """Set the GRUB menu countdown. Body: {"timeout": <seconds>}."""
    from helpers import set_grub_timeout

    seconds = request.json.get("timeout") if request.is_json else None
    if seconds is None:
        return jsonify({"ok": False, "error": "timeout is required"}), 400
    ok, err = set_grub_timeout(seconds)
    if not ok:
        return jsonify({"ok": False, "error": err}), 400
    support_log.record("boot.timeout", {"timeout": int(seconds)}, "", 0)
    return jsonify({"ok": True, "timeout": int(seconds)})


@actions.get("/api/browse-sources")
def browse_sources():
    """List the browsable file sources (USB media, an installed Ubuntu)."""
    return jsonify({"ok": True, "sources": list_browse_sources()})


@actions.get("/api/browse")
def browse_files():
    rel = request.args.get("path", "")
    source = request.args.get("source", "usb")
    root, err = resolve_browse_source(source)
    if err:
        return jsonify({"error": err}), 400
    base = os.path.realpath(root)
    target = os.path.realpath(os.path.join(base, rel))
    if target != base and not target.startswith(base + os.sep):
        return jsonify({"error": "invalid path"}), 400
    if not os.path.isdir(target):
        if target == base:
            # Nothing mounted yet (e.g. no USB stick): show an empty listing
            # rather than an error.
            return jsonify({"source": source, "root": base, "path": "",
                            "abs_path": target, "entries": []})
        return jsonify({"error": "not a directory"}), 404
    entries = []
    try:
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            entries.append({
                "name": name,
                "is_dir": os.path.isdir(full),
            })
    except PermissionError:
        return jsonify({"error": "permission denied"}), 403
    display = os.path.relpath(target, base)
    if display == ".":
        display = ""
    return jsonify({"source": source, "root": base, "path": display,
                    "abs_path": target, "entries": entries})


# ── Browser-side file upload (drag/drop into Versal payload inputs) ──
#
# The client streams the raw file body via PUT (binary, no multipart) and
# passes the original filename in the X-Filename header. We sanitise the
# filename to a basename and write it under UPLOAD_DIR. On success we return
# the absolute saved path so the client can drop it straight into the
# matching text input. UPLOAD_DIR is intentionally separate from
# payload-cache (which the programming scripts wipe via trap on exit), so an
# uploaded file survives between selection and "Program/Configure" click.

import re


def _safe_upload_name(raw_name: str) -> str:
    name = os.path.basename((raw_name or "").strip())
    name = name.replace("\x00", "")
    # Keep alnum, dot, dash, underscore. Anything else collapses to '_'.
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    name = name.strip("._-") or "upload.bin"
    return name[:200]


@actions.put("/api/upload-payload")
def upload_payload():
    raw_name = request.headers.get("X-Filename", "")
    # Client sends percent-encoded UTF-8 so non-ASCII filenames survive
    # an HTTP header. Decode here, then sanitise.
    try:
        from urllib.parse import unquote
        raw_name = unquote(raw_name)
    except Exception:
        pass
    fname = _safe_upload_name(raw_name)
    if not fname:
        return jsonify({"ok": False, "error": "missing filename"}), 400

    try:
        lock_file = acquire_bulk_lock()
    except BulkOperationBusy as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409

    try:
        try:
            os.makedirs(UPLOAD_DIR, exist_ok=True)
        except OSError as exc:
            return jsonify({"ok": False, "error": f"cannot create upload dir: {exc}"}), 500

        dest = os.path.join(UPLOAD_DIR, fname)
        tmp = dest + ".part"
        try:
            # Stream straight from the wsgi input to disk in 1 MiB chunks so we
            # don't materialise multi-GiB uploads in memory.
            stream = request.stream
            with open(tmp, "wb") as fh:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            os.replace(tmp, dest)
        except Exception as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return jsonify({"ok": False, "error": f"upload failed: {exc}"}), 500

        try:
            size = os.path.getsize(dest)
        except OSError:
            size = 0
        return jsonify({"ok": True, "path": dest, "name": fname, "size": size})
    finally:
        lock_file.close()


@actions.get("/api/storage-status")
def storage_status():
    data = {"state": "degraded", "reason": "status-unavailable"}
    try:
        with open(STORAGE_STATUS_FILE, "r", encoding="utf-8") as status:
            for line in status:
                key, separator, value = line.rstrip("\n").partition("=")
                if separator and key:
                    data[key] = value
    except OSError:
        pass

    path = UPLOAD_DIR if os.path.isdir(UPLOAD_DIR) else "/var/lib/rave-menu"
    try:
        stat = os.statvfs(path)
        data["free"] = stat.f_bavail * stat.f_frsize
        data["total"] = stat.f_blocks * stat.f_frsize
    except OSError:
        data["free"] = None
        data["total"] = None
    return jsonify({"ok": True, **data})


@actions.get("/api/uploads")
def list_uploads():
    """List previously uploaded payload files (most recent first)."""
    items = []
    try:
        with os.scandir(UPLOAD_DIR) as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if entry.name.endswith(".part"):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                items.append({
                    "name": entry.name,
                    "path": os.path.join(UPLOAD_DIR, entry.name),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                })
    except FileNotFoundError:
        pass
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    items.sort(key=lambda e: e["mtime"], reverse=True)
    free = None
    total = None
    try:
        st = os.statvfs(UPLOAD_DIR if os.path.isdir(UPLOAD_DIR) else "/var/lib")
        free = st.f_bavail * st.f_frsize
        total = st.f_blocks * st.f_frsize
    except OSError:
        pass
    return jsonify({"ok": True, "items": items, "free": free, "total": total})


@actions.post("/api/uploads/clear")
def clear_uploads():
    """Delete every regular file under UPLOAD_DIR to reclaim space."""
    try:
        lock_file = acquire_bulk_lock()
    except BulkOperationBusy as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409

    removed = 0
    freed = 0
    errors = []
    try:
        entries = list(os.scandir(UPLOAD_DIR))
    except FileNotFoundError:
        lock_file.close()
        return jsonify({"ok": True, "removed": 0, "freed": 0})
    except OSError as exc:
        lock_file.close()
        return jsonify({"ok": False, "error": str(exc)}), 500
    try:
        for entry in entries:
            try:
                if not (entry.is_file(follow_symlinks=False) or entry.is_symlink()):
                    continue
                try:
                    freed += entry.stat().st_size
                except OSError:
                    pass
                os.unlink(entry.path)
                removed += 1
            except OSError as exc:
                errors.append(f"{entry.name}: {exc}")
        resp = {"ok": not errors, "removed": removed, "freed": freed}
        if errors:
            resp["errors"] = errors
        return jsonify(resp)
    finally:
        lock_file.close()


@actions.post("/api/uploads/delete")
def delete_upload():
    """Delete a single file from UPLOAD_DIR by name."""
    data = request.get_json(silent=True) or {}
    raw = data.get("name") or ""
    name = _safe_upload_name(raw)
    if not name or name != os.path.basename(raw or ""):
        return jsonify({"ok": False, "error": "invalid filename"}), 400
    target = os.path.join(UPLOAD_DIR, name)
    # Prevent escaping UPLOAD_DIR via symlinks / odd inputs.
    real_upload = os.path.realpath(UPLOAD_DIR)
    real_target = os.path.realpath(target)
    if os.path.dirname(real_target) != real_upload:
        return jsonify({"ok": False, "error": "path outside upload dir"}), 400
    try:
        lock_file = acquire_bulk_lock()
    except BulkOperationBusy as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    try:
        try:
            size = os.path.getsize(target)
        except OSError:
            size = 0
        try:
            os.unlink(target)
        except FileNotFoundError:
            return jsonify({"ok": False, "error": "not found"}), 404
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        return jsonify({"ok": True, "name": name, "freed": size})
    finally:
        lock_file.close()


# ── System Update endpoints ──────────────────────────────────────────

RAVE2_COMPATIBLE = "rave2-menu"


@actions.get("/api/check-update")
def api_check_update():
    """Fetch the remote manifest and return compatible updates."""
    import urllib.request
    import urllib.error

    current = get_rauc_version()

    try:
        req = urllib.request.Request(UPDATE_MANIFEST_URL, method="GET",
                                     headers={"User-Agent": "RAVE2-BUDDY/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            manifest = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError) as exc:
        return jsonify({"ok": False, "error": f"Cannot reach update server: {exc}"}), 502
    except (json.JSONDecodeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid manifest format"}), 502

    entries = manifest.get("updates", [])
    compatible = [
        {
            "version": e.get("version", ""),
            "description": e.get("description", ""),
            "url": e.get("url", ""),
            "experimental": bool(e.get("experimental", False)),
        }
        for e in entries
        if e.get("compatible") == RAVE2_COMPATIBLE
    ]

    return jsonify({
        "ok": True,
        "current_version": current,
        "updates": compatible,
    })


@actions.get("/api/usb-bundles")
def api_usb_bundles():
    """List .raucb files found on mounted USB media."""
    return jsonify({"ok": True, "bundles": find_usb_bundles()})


# ── BIOS update endpoints (see docs/bios-update-plan.md) ─────────────────

@actions.get("/api/bios-status")
def api_bios_status():
    """Return detected BIOS vendor/product line/version and any pending result."""
    info = get_bios_info()
    pending = get_bios_update_pending()
    return jsonify({"ok": True, **info, "pending": pending or None})


@actions.post("/api/bios-pending/clear")
def api_bios_pending_clear():
    """Acknowledge/clear the pending-update record after the user has seen it."""
    clear_bios_update_pending()
    return jsonify({"ok": True})


@actions.get("/api/nvme-free-space")
def api_nvme_free_space():
    """Return the free NVMe space (MiB) available for Ubuntu + Jumbo apps."""
    free_mib = get_nvme_free_mib()
    if free_mib is None:
        return jsonify({"ok": False, "error": "Could not read the NVMe partition table."}), 500
    # An installed jumbo app already holds real partitions, excluded from
    # free_mib. Report its size so the install page can show what is already
    # committed rather than reserving for it a second time.
    capacity = get_region_capacity() or {}
    jumbo_app_mib = 0
    if capacity.get("jumbo_installed"):
        jumbo_app_mib = max(capacity.get("app_tail_end_mib", 0)
                            - capacity.get("jumbo_first_mib", 0), 0)
    return jsonify({"ok": True, "free_mib": free_mib,
                    "jumbo_installed": bool(capacity.get("jumbo_installed")),
                    "jumbo_app_mib": jumbo_app_mib})


@actions.get("/api/rauc-status")
def api_rauc_status():
    """Return RAUC slot status with per-slot version strings."""
    result = run_cmd(["rauc", "status", "--detailed", "--output-format=json"], timeout=10)
    if result.returncode != 0:
        return jsonify({"ok": False, "error": (result.stderr or result.stdout or "rauc status failed").strip()}), 500
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return jsonify({"ok": False, "error": "Failed to parse rauc status"}), 500

    # Read /etc/rauc/version from each slot
    import tempfile, shutil
    for slot_entry in data.get("slots", []):
        for name, info in slot_entry.items():
            if info.get("state") == "booted":
                # Active slot — read directly
                info["version"] = get_rauc_version()
            else:
                # Inactive slot — mount read-only, peek, unmount
                dev = info.get("device")
                if not dev:
                    continue
                mnt = tempfile.mkdtemp(prefix="slot-peek-")
                try:
                    r = run_cmd(["mount", "-o", "ro", dev, mnt], timeout=10)
                    if r.returncode == 0:
                        vfile = os.path.join(mnt, "etc", "rauc", "version")
                        try:
                            with open(vfile, "r") as f:
                                info["version"] = f.read().strip()
                        except Exception:
                            info["version"] = ""
                    run_cmd(["umount", mnt], timeout=10)
                finally:
                    shutil.rmtree(mnt, ignore_errors=True)

    return jsonify({"ok": True, "status": data})


@actions.post("/api/install-update")
def api_install_update():
    """Install a RAUC bundle from a URL or a local path.

    Accepts JSON body:
      {"url": "https://..."} — remote bundle (downloaded first)
      {"path": "/run/media/.../file.raucb"} — local file on USB

    For remote URLs a background shell script will:
      1. Expand the rootfs if needed (resize2fs)
      2. HEAD the URL to learn the bundle size
    3. Verify enough free space on update staging filesystem
    4. Download the bundle
    5. Run rauc install on the local file
    6. Resize the updated (inactive) slot filesystem
    7. Clean up the downloaded file

    All steps stream progress to the SSE /api/stream/<job_id> endpoint.
    """
    if not request.is_json:
        return jsonify({"ok": False, "error": "JSON body required"}), 400

    data = request.get_json(silent=True) or {}
    source = data.get("url") or data.get("path") or ""

    if not source:
        return jsonify({"ok": False, "error": "Provide 'url' or 'path'"}), 400

    if not source.startswith(("http://", "https://")):
        # --- Local file (USB) — install directly ---
        real = os.path.realpath(source)
        if not real.startswith("/run/media/"):
            return jsonify({"ok": False, "error": "Local path must be under /run/media/"}), 400
        if not os.path.isfile(real):
            return jsonify({"ok": False, "error": f"File not found: {source}"}), 404
        source = real
        script = _build_local_install_script(source)
    else:
        # --- Remote URL — expand, check, download, install ---
        source = _normalize_download_url(source)
        parsed = urlparse(source)
        if parsed.scheme not in ("http", "https"):
            return jsonify({"ok": False, "error": "Only http/https URLs supported"}), 400

        filename = os.path.basename(parsed.path) or "bundle.raucb"
        dest = os.path.join(DOWNLOAD_DIR, filename)

        # Build a shell script that streams progress to stdout
        script = _build_remote_install_script(source, dest)

    env = os.environ.copy()
    env.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
    env["PYTHONUNBUFFERED"] = "1"

    entry = support_log.begin("system-update.install", {"source": source})

    try:
        lock_file = acquire_bulk_lock()
    except BulkOperationBusy as exc:
        if entry:
            entry.line(str(exc))
            entry.finish(None, "rejected-busy")
        return jsonify({"ok": False, "error": str(exc)}), 409
    try:
        proc = subprocess.Popen(
            ["sh", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            env=env,
            pass_fds=(lock_file.fileno(),),
        )
    finally:
        lock_file.close()

    return jsonify({"ok": True, "job_id": register_job(proc, entry)})


def _normalize_download_url(url: str) -> str:
    """Convert Dropbox share links to direct downloads and remove fragments."""
    parsed = urlparse(url)
    if parsed.hostname in {"dropbox.com", "www.dropbox.com"}:
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key not in {"dl", "raw"}]
        query.append(("dl", "1"))
        parsed = parsed._replace(query=urlencode(query))
    return urlunparse(parsed._replace(fragment=""))


def _build_remote_install_script(url: str, dest: str) -> str:
    """Return a shell script that expands rootfs, downloads, installs, and cleans up.

    Every step echoes progress so the SSE stream keeps the user informed.
    """
    # Shell-escape the URL and dest path
    import shlex
    q_url = shlex.quote(url)
    q_dest = shlex.quote(dest)
    q_download_dir = shlex.quote(DOWNLOAD_DIR)
    is_dropbox = urlparse(url).hostname in {"dropbox.com", "www.dropbox.com"}

    if is_dropbox:
        size_command = f'''python3 - {q_url} <<'PY' 2>/dev/null || true
import sys
import urllib.request

request = urllib.request.Request(sys.argv[1], method="HEAD", headers={{"User-Agent": "RAVE2-Update-Downloader/1"}})
with urllib.request.urlopen(request) as response:
    print(response.headers.get("Content-Length", ""))
PY
'''
        download_command = f'''python3 - {q_url} "$DEST_PATH" <<'PY'
import sys
import urllib.request

request = urllib.request.Request(sys.argv[1], headers={{"User-Agent": "RAVE2-Update-Downloader/1"}})
with urllib.request.urlopen(request) as response, open(sys.argv[2], "wb") as output:
    total = int(response.headers.get("Content-Length") or 0)
    copied = 0
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        output.write(chunk)
        copied += len(chunk)
        if copied % (64 * 1024 * 1024) < len(chunk):
            if total:
                print(f"Downloaded {{copied // 1048576}} of {{total // 1048576}} MiB ({{copied * 100 // total}}%)", flush=True)
            else:
                print(f"Downloaded {{copied // 1048576}} MiB", flush=True)
PY'''
    else:
        size_command = f"wget --spider -S -q {q_url} 2>&1 | grep -i content-length | awk '{{print $2}}' | tr -d '\\r' | tail -n1"
        download_command = f'wget -O "$DEST_PATH" {q_url} 2>&1'

    return f'''
set -e

DOWNLOAD_DIR={q_download_dir}
DEST_PATH={q_dest}
mkdir -p "$DOWNLOAD_DIR"

cleanup_bundle() {{
    if [ -n "${{DEST_PATH}}" ] && [ -f "${{DEST_PATH}}" ]; then
        rm -f "${{DEST_PATH}}"
        echo "Cleaned up staged bundle: ${{DEST_PATH}}"
    fi
}}
trap cleanup_bundle EXIT

# --- Step 1: Expand rootfs ---
echo "Expanding root filesystem..."
DEV=$(findmnt -n -o SOURCE /)
if [ -z "$DEV" ]; then
    echo "ERROR: Could not determine root device"
    exit 1
fi
resize2fs "$DEV" 2>&1 || true
FREE_MB=$(df -Pm "$DOWNLOAD_DIR" | awk 'NR==2 {{print $4}}')
echo "Update staging filesystem ($DOWNLOAD_DIR): ${{FREE_MB}} MiB free"

# --- Step 2: Check remote bundle size ---
echo "Checking remote bundle size..."
CONTENT_LENGTH=$({size_command})
if [ -n "$CONTENT_LENGTH" ] && [ "$CONTENT_LENGTH" -gt 0 ] 2>/dev/null; then
    SIZE_MB=$((CONTENT_LENGTH / 1048576))
    echo "Bundle size: ${{SIZE_MB}} MiB"

    # Need bundle + 10% headroom
    NEEDED_MB=$(( (CONTENT_LENGTH * 11 / 10) / 1048576 ))
    if [ "$FREE_MB" -lt "$NEEDED_MB" ]; then
        echo "ERROR: Not enough space on update staging filesystem — need ${{NEEDED_MB}} MiB but only ${{FREE_MB}} MiB free"
        exit 1
    fi
    echo "Space check passed on update staging filesystem (${{FREE_MB}} MiB free, need ${{NEEDED_MB}} MiB)"
else
    echo "Could not determine bundle size, proceeding anyway"
fi

# --- Step 3: Download ---
echo "Downloading bundle..."
{download_command}
if [ ! -f "$DEST_PATH" ]; then
    echo "ERROR: Download produced no file"
    exit 1
fi
DL_SIZE=$(ls -lh "$DEST_PATH" | awk '{{print $5}}')
echo "Download complete: ${{DL_SIZE}}"

# --- Step 4: Install ---
echo "Starting RAUC install..."
rauc install "$DEST_PATH"

# --- Step 5: Resize updated slot ---
{_build_post_install_resize_snippet()}

# --- Step 6: Clean up ---
cleanup_bundle
DEST_PATH=""

exit 0
'''


def _build_local_install_script(bundle_path: str) -> str:
    """Return a shell script that installs a local bundle and resizes target slot."""
    import shlex
    q_bundle = shlex.quote(bundle_path)

    return f'''
set -e

# --- Step 1: Install ---
echo "Starting RAUC install..."
rauc install {q_bundle}

# --- Step 2: Resize updated slot ---
{_build_post_install_resize_snippet()}

exit 0
'''


def _build_post_install_resize_snippet() -> str:
    """Return a shell snippet that expands the inactive rootfs filesystem."""
    return '''
echo "Determining updated slot device..."
TARGET_DEV=$(python3 - <<'PY'
import json
import subprocess

try:
    p = subprocess.run(
        ["rauc", "status", "--detailed", "--output-format=json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if p.returncode != 0:
        print("")
        raise SystemExit(0)

    data = json.loads(p.stdout)
    chosen = ""
    for entry in data.get("slots", []):
        for _name, info in entry.items():
            if info.get("state") == "booted":
                continue
            dev = info.get("device") or ""
            if dev.startswith("/dev/"):
                chosen = dev
                break
        if chosen:
            break
    print(chosen)
except Exception:
    print("")
PY
)

if [ -z "$TARGET_DEV" ]; then
    echo "ERROR: Could not determine updated slot device"
    exit 1
fi

echo "Resizing filesystem on $TARGET_DEV to fill its current partition..."
echo "Running filesystem check on $TARGET_DEV..."
if e2fsck -f -p "$TARGET_DEV"; then
    echo "Filesystem check completed"
else
    E2FSCK_RC=$?
    if [ "$E2FSCK_RC" -le 1 ]; then
        echo "Filesystem check completed with minor corrections"
    else
        echo "WARNING: Filesystem check failed on $TARGET_DEV (exit $E2FSCK_RC); skipping resize"
        exit 0
    fi
fi

if resize2fs "$TARGET_DEV"; then
    echo "Target slot resize complete"
else
    echo "WARNING: resize2fs failed on $TARGET_DEV; continuing without resize"
fi
'''
