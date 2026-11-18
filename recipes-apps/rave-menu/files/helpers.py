# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Shared utilities used across the rave-menu application.

Provides subprocess wrappers, sysfs helpers, and path constants.
No Flask dependency -- safe to import from anywhere.
"""

import collections
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
import fcntl
from pathlib import Path

log = logging.getLogger(__name__)

# -- Paths --

APP_ROOT = Path("/opt/rave-menu").resolve()
SCRIPTS_DIR = APP_ROOT / "scripts"

# -- External command paths (overridable via env) --

CONNMANCTL = os.environ.get("CONNMANCTL", "connmanctl")
IP_CMD = os.environ.get("IP_CMD", "/sbin/ip")
LSPCI_CMD = os.environ.get("LSPCI_CMD", "lspci")
IW_CMD = os.environ.get("IW_CMD", "iw")

# -- Script registry (action name -> script path) --

ACTIONS = {
    "board-test": SCRIPTS_DIR / "program_remote_versal_payload.sh",
    "versal-jtag": SCRIPTS_DIR / "program_jtag_versal.sh",
    "app-store": SCRIPTS_DIR / "launch_app_store.sh",
    "reconfigure-versal": SCRIPTS_DIR / "reconfigure_versal.sh",
    "install-ubuntu": SCRIPTS_DIR / "install_ubuntu.sh",
    "uninstall-ubuntu": SCRIPTS_DIR / "uninstall_ubuntu.sh",
    "install-app": SCRIPTS_DIR / "install_app_bundle.sh",
    "bios-update": SCRIPTS_DIR / "program_bios_update.sh",
}

# -- In-memory job store for running subprocesses --

# Output lines kept per job for an SSE client that attaches late or reconnects.
# Bounded because a large application install prints far more than a browser
# needs, and nothing drains the buffer when no client is attached.
JOB_RING_LINES = 2000

# How long a finished job stays retrievable. The client may attach after a
# short-lived script has already exited, so the record outlives the process.
JOB_RETENTION_S = 600.0


class Job:
    """A running script plus a bounded ring of the output it has produced."""

    def __init__(self, proc, support_record=None):
        self.proc = proc
        self.support_record = support_record
        self.lines = collections.deque(maxlen=JOB_RING_LINES)
        self.seq = 0            # total lines ever produced, including dropped
        self.rc = None
        self.ended = None
        self.cond = threading.Condition()

    def append(self, text: str) -> None:
        with self.cond:
            self.lines.append(text)
            self.seq += 1
            self.cond.notify_all()

    def finish(self, rc) -> None:
        with self.cond:
            self.rc = rc
            self.ended = time.monotonic()
            self.cond.notify_all()

    def since(self, cursor: int) -> tuple:
        """Return ``(lines, new_cursor, dropped)`` for everything after *cursor*."""
        with self.cond:
            oldest = self.seq - len(self.lines)
            dropped = max(0, oldest - cursor)
            start = max(cursor, oldest)
            return list(self.lines)[start - oldest:], self.seq, dropped


JOBS: dict = {}
JOBS_LOCK = threading.Lock()


def register_job(proc, support_record=None) -> str:
    """Track *proc* for SSE streaming and start draining its output.

    Draining starts here rather than when a client connects, so the transcript
    is captured even if no browser ever attaches and the child is always
    reaped.
    """
    job = Job(proc, support_record)
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        _reap_finished_jobs()
        JOBS[job_id] = job
    threading.Thread(target=_drain_job, args=(job,), daemon=True).start()
    return job_id


def _reap_finished_jobs() -> None:
    """Drop long-finished jobs. Caller must hold ``JOBS_LOCK``."""
    cutoff = time.monotonic() - JOB_RETENTION_S
    stale = [key for key, job in JOBS.items()
             if job.ended is not None and job.ended < cutoff]
    for key in stale:
        JOBS.pop(key, None)


def _drain_job(job: Job) -> None:
    """Read a job's output to completion and record it.

    This thread must never die early: the SSE generator waits on the job's
    condition, and the child holds the inherited bulk-operation lock until it
    is reaped.
    """
    try:
        if job.proc.stdout:
            for line in job.proc.stdout:
                text = line.rstrip()
                job.append(text)
                if job.support_record is not None:
                    try:
                        job.support_record.line(text)
                    except Exception:
                        log.exception("support log write failed")
    except Exception:
        log.exception("reading job output failed")
    finally:
        try:
            rc = job.proc.wait()
        except Exception:
            log.exception("waiting for job failed")
            rc = -1
        job.finish(rc)
        if job.support_record is not None:
            try:
                job.support_record.finish(rc)
            except Exception:
                log.exception("support log finish failed")

BULK_LOCK_FILE = os.environ.get(
    "RAVE_BULK_LOCK_FILE", "/run/lock/rave-bulk-operation.lock")


class BulkOperationBusy(Exception):
    pass


def acquire_bulk_lock():
    """Acquire the global bulk-operation lock without waiting."""
    os.makedirs(os.path.dirname(BULK_LOCK_FILE), exist_ok=True)
    lock_file = open(BULK_LOCK_FILE, "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise BulkOperationBusy("another bulk operation is already running") from exc
    return lock_file

# -- File browser roots --

BROWSE_ROOT = "/run/media"

# An installed Ubuntu lives on its own NVMe partition and is never mounted
# while the menu runs, so browsing it means mounting it read-only on demand.
UBUNTU_BROWSE_MOUNT = os.environ.get(
    "UBUNTU_BROWSE_MOUNT", "/run/rave-menu/ubuntu-root")
UBUNTU_FS_LABEL = "ubuntu"

RAUC_VERSION_FILE = "/etc/rauc/version"
UPDATE_MANIFEST_URL = "https://rave2.cicdstorage.com/updates/manifest.json"

# BIOS update (see docs/bios-update-plan.md) -- state left by
# scripts/program_bios_update.sh for the menu to pick up across a reboot.
BIOS_UPDATE_PENDING_FILE = "/var/lib/rave-menu/bios-update-pending.json"

# Hardware info cache, generated once at boot by rave-hwinfo.service.
HW_INFO_CACHE = os.environ.get("HW_INFO_CACHE", "/run/rave-menu/hw_info.txt")

# -- App Store (Phase 4) --

# Remote catalog (the "strategy" layer: outer metadata + whole-file sha256).
# Online only; there is no on-device fallback catalog. Overridable via env.
APP_CATALOG_URL = os.environ.get(
    "APP_CATALOG_URL", "https://rave2.cicdstorage.com/updates/appstore.json")
# Where downloaded .rave2app bundles are cached during install.
APP_CACHE_DIR = os.environ.get("APP_CACHE_DIR", "/var/lib/rave-menu/app-cache")

# Additional, user-added catalog roots. The built-in ``APP_CATALOG_URL`` above
# is always present and is *not* stored here; this file only holds the extra
# stores a user adds on-device, so it survives reboots (it lives on the
# persistent ``/var/lib`` partition). Each entry is a "root" the store logic
# resolves to a catalog with ``catalog_url_for`` (see below).
APP_STORES_FILE = os.environ.get(
    "APP_STORES_FILE", "/var/lib/rave-menu/app-stores.json")


def catalog_url_for(root: str) -> str:
    """Resolve a store *root* to the catalog URL to fetch.

    A root that already points at a ``.json`` document is used verbatim;
    otherwise it is treated as a directory and ``appstore.json`` is appended.
    Returns ``""`` for an empty/blank root.
    """
    root = (root or "").strip()
    if not root:
        return ""
    if root.lower().rstrip("/").endswith(".json"):
        return root
    return root.rstrip("/") + "/appstore.json"


def load_app_stores() -> list:
    """Return the user-added store roots as an ordered, de-duplicated list."""
    try:
        with open(APP_STORES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    raw = data.get("stores") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw:
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, dict):
            url = str(item.get("url", "")).strip()
        else:
            url = ""
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def save_app_stores(stores: list) -> bool:
    """Atomically persist the list of store roots. Returns ``True`` on success."""
    payload = {"stores": [s for s in stores if isinstance(s, str) and s.strip()]}
    try:
        os.makedirs(os.path.dirname(APP_STORES_FILE) or ".", exist_ok=True)
        tmp = APP_STORES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, APP_STORES_FILE)
        return True
    except OSError:
        return False


def run_cmd(cmd, timeout=20, input_text=None):
    """Run a command and return a CompletedProcess, never raising."""
    env = os.environ.copy()
    env.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
    try:
        return subprocess.run(
            cmd,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"command not found: {cmd[0]}")
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, exc.stdout or "", exc.stderr or "command timed out")
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def sysfs_read(path: str) -> str:
    """Read a single sysfs/procfs value, returning '' on any error."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except Exception:
        return ""


def get_rauc_version() -> str:
    """Return the RAUC menu version string, or '' if unavailable."""
    try:
        with open(RAUC_VERSION_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def get_bios_info() -> dict:
    """Detect BIOS vendor, product line, and installed version via dmidecode.

    Returns {"vendor": "ami"|"coreboot"|"unknown",
             "product_line": "rave2-core"|"rave2-pro"|"rave2-max"|"unknown",
             "version": <BIOS Version string, or "">}.
    See docs/bios-update-plan.md §4a.
    """
    info = {"vendor": "unknown", "product_line": "unknown", "version": ""}

    bios = run_cmd(["dmidecode", "-t", "bios"], timeout=10)
    if bios.returncode == 0:
        vendor_match = re.search(r"Vendor:\s*(.+)", bios.stdout)
        vendor = (vendor_match.group(1).strip().lower() if vendor_match else "")
        if "coreboot" in vendor:
            info["vendor"] = "coreboot"
        elif "megatrends" in vendor:
            info["vendor"] = "ami"
        version_match = re.search(r"^\s*Version:\s*(.+)$", bios.stdout, re.MULTILINE)
        if version_match:
            info["version"] = version_match.group(1).strip()

    system = run_cmd(["dmidecode", "-t", "system"], timeout=10)
    if system.returncode == 0:
        product_match = re.search(r"Product Name:\s*(.+)", system.stdout)
        product = (product_match.group(1).strip().lower() if product_match else "")
        if product in ("rave2-core", "rave2-pro", "rave2-max"):
            info["product_line"] = product

    return info


def get_bios_update_pending() -> dict:
    """Return the pending-update record left by program_bios_update.sh, or {}."""
    try:
        with open(BIOS_UPDATE_PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def clear_bios_update_pending() -> None:
    try:
        os.remove(BIOS_UPDATE_PENDING_FILE)
    except OSError:
        pass


def _parse_hw_info(text: str) -> dict:
    """Parse the report_hw_info.sh cache into {section: [(label, value), ...]}."""
    sections: dict = {}
    current = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        header = re.match(r"^---\s*(.*?)\s*---$", stripped)
        if header:
            current = header.group(1)
            sections.setdefault(current, [])
            continue
        if current is None or ":" not in stripped:
            continue
        label, _, value = stripped.partition(":")
        sections[current].append((label.strip(), value.strip()))
    return sections


def _section_value(sections: dict, section: str, label: str) -> str:
    """Return the first value for a (section, label) pair, or ''."""
    for lbl, val in sections.get(section, []):
        if lbl == label:
            return val
    return ""


def _memory_summary(sections: dict) -> str:
    """Return total installed memory with its type (e.g. '64 GB LPDDR5')."""
    total, mtype = 0, ""
    for _loc, desc in sections.get("Memory", []):
        parts = desc.split()
        if len(parts) >= 2 and parts[1].upper() == "GB":
            try:
                total += int(parts[0])
            except ValueError:
                pass
        if len(parts) >= 3 and not mtype:
            mtype = parts[2]
    if not total:
        return ""
    return f"{total} GB {mtype}".strip()


def read_hw_info() -> dict:
    """Return the cached hardware report as an ordered About-menu summary.

    Reads the fast, boot-generated cache (never runs dmidecode itself). Returns
    a dict with:
      available - bool, whether the cache was present and parsed
      summary   - list of (label, value) in the order the About menu leads with
      full      - the raw report text (for a "full report" dump)
    """
    try:
        with open(HW_INFO_CACHE, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return {"available": False, "summary": [], "full": ""}

    s = _parse_hw_info(text)
    cpu = _section_value(s, "Processor", "Model")
    cores = _section_value(s, "Processor", "Core Count")
    threads = _section_value(s, "Processor", "Thread Count")
    if cpu and cores and threads:
        cpu += f" ({cores}C/{threads}T)"

    rows = [
        ("Product", " ".join(v for v in (
            _section_value(s, "Baseboard", "Manufacturer"),
            _section_value(s, "System", "Product Name"),
            _section_value(s, "Baseboard", "Product Name"),
            _section_value(s, "Baseboard", "Version"),
        ) if v)),
        ("Processor", cpu),
        ("BIOS", (
            _section_value(s, "BIOS / Firmware", "Vendor")
            + "\n"
            + " ".join(v for v in (
                _section_value(s, "BIOS / Firmware", "Build ID"),
                _section_value(s, "BIOS / Firmware", "Release Date"),
            ) if v)
        ).strip()),
        ("Memory", _memory_summary(s)),
    ]
    summary = [(label, value) for label, value in rows if value]
    return {"available": bool(summary), "summary": summary, "full": text}


# -- Display / video outputs (DRM) --

_HDMI_OUIS = {0x000C03, 0xC45DD8}  # HDMI LLC (1.4), HDMI Forum (2.x)


def _decode_edid(path: str):
    """Return (sink_type, display_name) from an EDID blob, or (None, '')."""
    try:
        with open(path, "rb") as f:
            edid = f.read()
    except OSError:
        return None, ""
    if len(edid) < 128 or edid[0:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None, ""

    # Monitor name: descriptor tag 0xFC in the four 18-byte descriptors.
    name = ""
    for base in (54, 72, 90, 108):
        d = edid[base:base + 18]
        if len(d) == 18 and d[0:2] == b"\x00\x00" and d[2] == 0 and d[3] == 0xFC:
            name = d[5:18].split(b"\n")[0].decode("ascii", "replace").strip()
            break

    # Sink type: an HDMI IEEE OUI in a CTA-861 vendor-specific data block => HDMI.
    sink = "DisplayPort"
    for ext_off in range(128, len(edid), 128):
        ext = edid[ext_off:ext_off + 128]
        if len(ext) < 128 or ext[0] != 0x02:  # CTA-861 tag
            continue
        dtd_off = ext[2]
        i = 4
        while i < dtd_off and i < 128:
            tag = ext[i] >> 5
            length = ext[i] & 0x1F
            if tag == 3 and length >= 3:  # vendor-specific data block
                oui = ext[i + 1] | (ext[i + 2] << 8) | (ext[i + 3] << 16)
                if oui in _HDMI_OUIS:
                    sink = "HDMI"
            i += 1 + length
    return sink, name


def _drm_active_modes() -> dict:
    """Map connector name -> 'RESxRES @ HZ Hz' from the kernel atomic state.

    Reads /sys/kernel/debug/dri/*/state (root-only; the menu runs as root).
    Returns {} if debugfs is unavailable.
    """
    import glob

    conn_to_crtc: dict = {}
    crtc_to_mode: dict = {}
    for state in glob.glob("/sys/kernel/debug/dri/*/state"):
        try:
            with open(state) as f:
                text = f.read()
        except OSError:
            continue

        cur = None
        for line in text.splitlines():
            m = re.match(r"connector\[\d+\]:\s+(\S+)", line)
            if m:
                cur = m.group(1)
                continue
            m = re.match(r"\s+crtc=(crtc-\d+)", line)
            if m and cur:
                conn_to_crtc[cur] = m.group(1)
                cur = None

        cur = None
        for line in text.splitlines():
            m = re.match(r"crtc\[\d+\]:\s+(crtc-\d+)", line)
            if m:
                cur = m.group(1)
                continue
            m = re.search(r'mode:\s+"([^"]+)":\s+(\d+)', line)
            if m and cur:
                crtc_to_mode[cur] = f"{m.group(1)} @ {m.group(2)} Hz"
                cur = None

    return {c: crtc_to_mode.get(crtc) for c, crtc in conn_to_crtc.items()
            if crtc_to_mode.get(crtc)}


def read_displays() -> dict:
    """Return connected display outputs for the About menu.

    Each entry: {port, sink, name, mode}. 'sink' is 'HDMI' or 'DisplayPort'
    (decoded from the EDID); 'mode' is the active resolution + refresh, or the
    connector's preferred mode as a fallback. 'full' is a detailed text report
    covering every connector, for a collapsible "full video report" dump.
    """
    import glob

    active = _drm_active_modes()
    displays = []
    for path in sorted(glob.glob("/sys/class/drm/card*-*")):
        port = os.path.basename(path).split("-", 1)[1]  # card0-DP-1 -> DP-1
        if port.startswith("Writeback"):
            continue
        if sysfs_read(os.path.join(path, "status")) != "connected":
            continue

        sink, name = _decode_edid(os.path.join(path, "edid"))
        mode = active.get(port)
        if not mode:
            pref = sysfs_read(os.path.join(path, "modes")).splitlines()
            mode = pref[0] if pref else ""

        displays.append({
            "port": port,
            "sink": sink or "DisplayPort",
            "name": name,
            "mode": mode,
        })

    return {
        "available": bool(displays),
        "displays": displays,
        "full": _video_full_report(active),
    }


def _edid_details(path: str) -> dict:
    """Return a detailed dict of EDID fields, or {} if unreadable/invalid."""
    try:
        with open(path, "rb") as f:
            edid = f.read()
    except OSError:
        return {}
    if len(edid) < 128 or edid[0:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return {}

    # Manufacturer PNP ID: three 5-bit letters packed into bytes 8-9.
    m = (edid[8] << 8) | edid[9]
    mfr = "".join(chr(((m >> s) & 0x1F) + 0x40) for s in (10, 5, 0))
    product = edid[10] | (edid[11] << 8)
    serial = edid[12] | (edid[13] << 8) | (edid[14] << 16) | (edid[15] << 24)
    week, year = edid[16], edid[17] + 1990
    ver = f"{edid[18]}.{edid[19]}"

    sink, name = _decode_edid(path)
    serial_str = ""
    # Descriptor tag 0xFF carries an ASCII serial string if present.
    for base in (54, 72, 90, 108):
        d = edid[base:base + 18]
        if len(d) == 18 and d[0:2] == b"\x00\x00" and d[2] == 0 and d[3] == 0xFF:
            serial_str = d[5:18].split(b"\n")[0].decode("ascii", "replace").strip()
            break

    return {
        "manufacturer": mfr,
        "product_code": f"0x{product:04X}",
        "serial": serial_str or (str(serial) if serial else ""),
        "mfg_date": f"week {week} {year}" if 1 <= week <= 53 else str(year),
        "edid_version": ver,
        "sink": sink or "DisplayPort",
        "name": name,
    }


def _video_full_report(active: dict = None) -> str:
    """Build a detailed text report of every DRM connector."""
    import glob

    if active is None:
        active = _drm_active_modes()

    lines = ["--- Video / Display Outputs ---"]
    for path in sorted(glob.glob("/sys/class/drm/card*-*")):
        port = os.path.basename(path).split("-", 1)[1]
        if port.startswith("Writeback"):
            continue
        status = sysfs_read(os.path.join(path, "status")) or "unknown"
        if status != "connected":
            lines.append(f"{port}: {status}")
            continue

        d = _edid_details(os.path.join(path, "edid"))
        mode = active.get(port)
        modes = sysfs_read(os.path.join(path, "modes")).splitlines()
        enabled = sysfs_read(os.path.join(path, "enabled"))
        dpms = sysfs_read(os.path.join(path, "dpms"))

        lines.append(f"{port}: connected")
        lines.append(f"    Sink type    : {d.get('sink', 'DisplayPort')}")
        if d.get("name"):
            lines.append(f"    Display name : {d['name']}")
        if d.get("manufacturer"):
            lines.append(f"    Manufacturer : {d['manufacturer']}")
        if d.get("product_code"):
            lines.append(f"    Product code : {d['product_code']}")
        if d.get("serial"):
            lines.append(f"    Serial       : {d['serial']}")
        if d.get("mfg_date"):
            lines.append(f"    Manufactured : {d['mfg_date']}")
        if d.get("edid_version"):
            lines.append(f"    EDID version : {d['edid_version']}")
        lines.append(f"    Active mode  : {mode or '(none)'}")
        if enabled:
            lines.append(f"    Enabled      : {enabled}")
        if dpms:
            lines.append(f"    DPMS         : {dpms}")
        if modes:
            lines.append(f"    Available    : {modes[0]} (preferred)")
            for extra in modes[1:8]:
                lines.append(f"                   {extra}")
            if len(modes) > 8:
                lines.append(f"                   ... {len(modes) - 8} more")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def running_from_nvme() -> bool:
    """Return True if the root filesystem is on an NVMe device."""
    # Method 1: /dev/root symlink
    try:
        root_dev = os.path.realpath("/dev/root")
        if "nvme" in root_dev:
            return True
    except Exception:
        pass
    # Method 2: check /proc/mounts for the "/" mount
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "/":
                    dev = parts[0]
                    # Resolve symlinks (e.g. /dev/disk/by-partuuid/... -> /dev/nvme0n1p2)
                    try:
                        dev = os.path.realpath(dev)
                    except Exception:
                        pass
                    if "nvme" in dev:
                        return True
    except Exception:
        pass
    # Method 3: resolve the root device via major:minor from mountinfo
    try:
        with open("/proc/self/mountinfo", "r") as f:
            for line in f:
                fields = line.split()
                # field 4 is mount point, field 2 is major:minor
                if len(fields) >= 5 and fields[4] == "/":
                    maj_min = fields[2]  # e.g. "259:2"
                    dev_path = f"/sys/dev/block/{maj_min}/device"
                    # Walk up to find the block device name
                    uevent = f"/sys/dev/block/{maj_min}/uevent"
                    if os.path.exists(uevent):
                        with open(uevent) as uf:
                            for ul in uf:
                                if ul.startswith("DEVNAME=") and "nvme" in ul:
                                    return True
    except Exception:
        pass
    return False


# -- NVMe free-space reporting (for the Ubuntu install page) --

NVME_DISK = os.environ.get("NVME_DISK", "/dev/nvme0n1")
APP_REGION_TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "app_region.py")


def get_region_capacity():
    """app_region.py's view of both app regions, or ``None`` if unreadable."""
    result = run_cmd(["python3", APP_REGION_TOOL, "capacity"], timeout=20)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def get_jumbo_free_mib():
    """Space a jumbo application could claim now, in MiB (``None`` if unknown).

    This is the whole tail after rave-data, including any jumbo app already
    installed there: installing over it reclaims its space.
    """
    cap = get_region_capacity()
    if not cap or cap.get("error"):
        return None
    return cap.get("jumbo_mib") or 0


def get_nvme_free_mib():
    """Return the free NVMe region available to Ubuntu, in MiB.

    Mirrors the free-space discovery in scripts/install_ubuntu.sh::partition():
    everything after partition 5 (rave-data) to the end of the disk, minus any
    jumbo application installed at the front of it — Ubuntu starts after that,
    so its partitions are not free space. Returns ``None`` if the disk or
    partition 5 can't be read.
    """
    cap = get_region_capacity()
    if cap and not cap.get("error") and cap.get("disk_end_mib"):
        free = cap["disk_end_mib"] - (cap.get("app_tail_end_mib")
                                      or cap.get("jumbo_first_mib") or 0)
        if free > 0:
            return free

    result = run_cmd(["parted", "-sm", NVME_DISK, "unit", "MiB", "print"], timeout=10)
    if result.returncode != 0:
        return None
    free_start = free_end = None
    for line in result.stdout.splitlines():
        fields = line.split(":")
        if line.startswith("5:") and len(fields) > 3:
            try:
                free_start = int(float(fields[2].rstrip("MiB"))) + 1
            except ValueError:
                pass
        elif line.startswith("/dev/") and len(fields) > 2:
            try:
                free_end = int(float(fields[1].rstrip("MiB")))
            except ValueError:
                pass
    if free_start is None or free_end is None or free_end <= free_start:
        return None
    return free_end - free_start


# -- Ubuntu boot-default control (grubenv on the ESP) --

# Candidate locations for the live ESP GRUB config, in priority order. The
# grubenv lives alongside grub.cfg. Mirrors the discovery logic in
# scripts/install_ubuntu.sh::configure_grub().
ESP_GRUB_CFG_CANDIDATES = (
    "/boot/efi/EFI/BOOT/grub.cfg",
    "/boot/efi/EFI/rave2/grub.cfg",
    "/boot/EFI/BOOT/grub.cfg",
)

GRUB_EDITENV = os.environ.get("GRUB_EDITENV", "grub-editenv")


def find_esp_grubenv() -> str:
    """Return the path to the ESP grubenv file, or '' if not found."""
    for cfg in ESP_GRUB_CFG_CANDIDATES:
        if os.path.isfile(cfg):
            return os.path.join(os.path.dirname(cfg), "grubenv")
    return ""


def _read_grubenv() -> dict:
    """Return the ESP grubenv as a dict, or {} if unavailable."""
    grubenv = find_esp_grubenv()
    if not grubenv or not os.path.isfile(grubenv):
        return {}
    result = run_cmd([GRUB_EDITENV, grubenv, "list"], timeout=10)
    if result.returncode != 0:
        return {}
    env = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def get_boot_default() -> dict:
    """Return boot status across the rave2 menu, Ubuntu and an installed app.

    Keys:
      installed     - bool, whether Ubuntu is installed (legacy key, kept)
      default       - 'ubuntu'|'buddy', the legacy Ubuntu/menu selection
      app_installed - bool, whether an application image has been installed
      boot_default  - 'menu'|'ubuntu'|'app'|'' raw grubenv selector (newer
                      GRUB templates honour this; '' means unset)

    Ubuntu is the default unless grubenv explicitly sets ubuntu_default=0,
    matching the legacy GRUB template logic.
    """
    env = _read_grubenv()
    installed = env.get("ubuntu_installed") == "1"
    default = "buddy" if env.get("ubuntu_default") == "0" else "ubuntu"
    return {
        "installed": installed,
        "default": default,
        "app_installed": env.get("app_installed") == "1",
        "boot_default": env.get("boot_default", ""),
        "app_title": env.get("app_title", ""),
    }


def set_boot_default(target: str) -> tuple:
    """Set the default boot OS. target is 'ubuntu', 'buddy', 'menu' or 'app'.

    Writes the newer ``boot_default`` selector and keeps the legacy
    ``ubuntu_default`` flag in sync so both old and new GRUB templates agree.

    Returns (ok: bool, error: str).
    """
    if target not in ("ubuntu", "buddy", "menu", "app"):
        return False, "target must be 'ubuntu', 'buddy', 'menu' or 'app'"
    grubenv = find_esp_grubenv()
    if not grubenv:
        return False, "Could not locate the ESP grubenv."
    if not os.path.isfile(grubenv):
        create = run_cmd([GRUB_EDITENV, grubenv, "create"], timeout=10)
        if create.returncode != 0:
            return False, (create.stderr or create.stdout or "failed to create grubenv").strip()

    # boot_default is the authoritative selector for newer templates. 'buddy'
    # maps to the rave2 'menu' entry.
    boot_default = "menu" if target == "buddy" else target
    sets = [f"boot_default={boot_default}"]
    # Keep the legacy ubuntu_default flag consistent for older templates.
    if target in ("ubuntu", "buddy", "menu"):
        sets.append("ubuntu_default=" + ("1" if target == "ubuntu" else "0"))

    for kv in sets:
        result = run_cmd([GRUB_EDITENV, grubenv, "set", kv], timeout=10)
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "grub-editenv failed").strip()
    return True, ""


# GRUB menuentry --id values from grub-nvme-rauc.cfg's `next_entry` handling.
NEXT_BOOT_ENTRIES = ("menu", "ubuntu", "ubuntu-prev", "ubuntu-recovery", "app-a")


def set_next_boot_entry(entry_id: str) -> tuple:
    """Arm a one-shot boot into *entry_id*, without changing the default.

    Writes grubenv ``next_entry``; the template boots it exactly once (as
    ``$default`` for that boot only) and clears the variable itself before
    booting, so a hung/failed one-shot target does not get retried and the
    normal default (``boot_default``/``ubuntu_default``) takes over again on
    the following boot regardless of outcome.

    Returns (ok: bool, error: str).
    """
    if entry_id not in NEXT_BOOT_ENTRIES:
        return False, f"Unknown boot entry '{entry_id}'"
    grubenv = find_esp_grubenv()
    if not grubenv:
        return False, "Could not locate the ESP grubenv."
    if not os.path.isfile(grubenv):
        create = run_cmd([GRUB_EDITENV, grubenv, "create"], timeout=10)
        if create.returncode != 0:
            return False, (create.stderr or create.stdout or "failed to create grubenv").strip()
    result = run_cmd([GRUB_EDITENV, grubenv, "set", f"next_entry={entry_id}"], timeout=10)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "grub-editenv failed").strip()
    return True, ""


def get_grub_timeout() -> int:
    """Return the GRUB menu countdown in seconds.

    Prefers the grubenv ``menu_timeout`` override (written by the Boot settings
    UI); falls back to parsing the ``set timeout=`` line of the live ESP
    grub.cfg, and finally to 5.
    """
    env = _read_grubenv()
    raw = env.get("menu_timeout", "")
    if raw.isdigit():
        return int(raw)
    for cfg in ESP_GRUB_CFG_CANDIDATES:
        try:
            with open(cfg, "r", encoding="utf-8") as fh:
                for line in fh:
                    s = line.strip()
                    if s.startswith("set timeout="):
                        val = s.split("=", 1)[1].strip().strip('"').strip("'")
                        if val.isdigit():
                            return int(val)
        except Exception:
            continue
    return 5


def set_grub_timeout(seconds) -> tuple:
    """Persist the GRUB menu countdown via the grubenv ``menu_timeout`` var.

    Stored in grubenv (not grub.cfg) so it survives App Store grub.cfg
    re-renders. Returns (ok: bool, error: str).
    """
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return False, "timeout must be an integer number of seconds"
    # Zero boots the default entry with no menu, leaving no way back in from
    # the console.
    if seconds < 1 or seconds > 3600:
        return False, "timeout must be between 1 and 3600 seconds"
    grubenv = find_esp_grubenv()
    if not grubenv:
        return False, "Could not locate the ESP grubenv."
    if not os.path.isfile(grubenv):
        create = run_cmd([GRUB_EDITENV, grubenv, "create"], timeout=10)
        if create.returncode != 0:
            return False, (create.stderr or create.stdout or "failed to create grubenv").strip()
    result = run_cmd([GRUB_EDITENV, grubenv, "set", f"menu_timeout={seconds}"], timeout=10)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "grub-editenv failed").strip()
    return True, ""


def rauc_status() -> dict:
    """Return parsed RAUC slot status as a dict, or empty on failure."""
    result = run_cmd(["rauc", "status", "--output-format=json"], timeout=10)
    if result.returncode != 0:
        return {}
    import json
    try:
        return json.loads(result.stdout)
    except Exception:
        return {}


def find_usb_bundles() -> list:
    """Scan /run/media for .raucb files on removable storage."""
    bundles = []
    base = Path(BROWSE_ROOT)
    if not base.is_dir():
        return bundles
    for mount in base.iterdir():
        if not mount.is_dir():
            continue
        for f in mount.iterdir():
            if f.suffix == ".raucb" and f.is_file():
                bundles.append(str(f))
    return sorted(bundles)


# -- File browser sources (USB media + an installed Ubuntu) --

def find_ubuntu_root_device() -> str:
    """Return the device node of the installed Ubuntu root partition, or ''."""
    # The GPT partition name is checked first: it is set by install_ubuntu.sh,
    # whereas the filesystem label could also match an unrelated USB stick.
    candidates = ["/dev/disk/by-partlabel/" + UBUNTU_FS_LABEL]
    result = run_cmd(["blkid", "-L", UBUNTU_FS_LABEL], timeout=10)
    if result.returncode == 0 and result.stdout.strip():
        candidates.append(result.stdout.strip())
    for cand in candidates:
        try:
            dev = os.path.realpath(cand)
        except OSError:
            continue
        if os.path.exists(dev):
            return dev
    return ""


def _mount_point_for_device(dev: str) -> str:
    """Return where *dev* is currently mounted, or '' if it is not mounted."""
    try:
        real = os.path.realpath(dev)
    except OSError:
        return ""
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                try:
                    src = os.path.realpath(parts[0])
                except OSError:
                    src = parts[0]
                if src == real:
                    # /proc/mounts octal-escapes spaces and friends.
                    return parts[1].encode().decode("unicode_escape")
    except OSError:
        pass
    return ""


def ensure_ubuntu_mounted() -> tuple:
    """Mount the installed Ubuntu root read-only. Returns (path, error)."""
    dev = find_ubuntu_root_device()
    if not dev:
        return "", "No installed Ubuntu was found."
    existing = _mount_point_for_device(dev)
    if existing:
        return existing, ""
    try:
        os.makedirs(UBUNTU_BROWSE_MOUNT, exist_ok=True)
    except OSError as exc:
        return "", f"Could not create {UBUNTU_BROWSE_MOUNT}: {exc}"
    result = run_cmd(
        ["mount", "-o", "ro,nosuid,nodev,noexec", dev, UBUNTU_BROWSE_MOUNT],
        timeout=30)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "mount failed").strip()
        return "", f"Could not mount {dev}: {err}"
    return UBUNTU_BROWSE_MOUNT, ""


def list_browse_sources() -> list:
    """Return the file-browser sources with their availability."""
    ubuntu_dev = find_ubuntu_root_device()
    return [
        {"id": "usb", "label": "USB Media", "available": os.path.isdir(BROWSE_ROOT)},
        {"id": "ubuntu", "label": "Ubuntu", "available": bool(ubuntu_dev),
         "device": ubuntu_dev},
    ]


def resolve_browse_source(source: str) -> tuple:
    """Return (root_path, error) for a file-browser source id."""
    if source in ("", "usb"):
        return BROWSE_ROOT, ""
    if source == "ubuntu":
        return ensure_ubuntu_mounted()
    return "", f"Unknown browse source '{source}'"


# -- Installed application manifest (installed.json on the app slot) --

def find_installed_app() -> dict:
    """Return the installed app manifest, or {} if no app is installed.

    The installer writes ``installed.json`` to ``<app-slot>/lib/versal/`` and a
    copy to rave-data. The rave-data copy is checked first: a disk-image app
    lives on its own partitions rather than the legacy p4, and nothing
    automounts them, so its manifest is otherwise unreadable. This does not
    mount anything itself, so it is safe to call from a GET handler.
    """
    import json

    try:
        with open("/var/lib/rave-menu/storage/installed-app.json", "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    try:
        from app_slots import APP_SLOT
        dev = APP_SLOT["device"]
    except Exception:
        dev = "/dev/nvme0n1p4"

    mounts = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    src = parts[0]
                    try:
                        src = os.path.realpath(src)
                    except Exception:
                        pass
                    if src == dev:
                        mounts.append(parts[1])
    except Exception:
        pass
    # Fall back to the conventional udev automount path for app-a.
    mounts.append("/run/media/app-a-nvme0n1p4")

    for mnt in mounts:
        path = os.path.join(mnt, "lib", "versal", "installed.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def free_bytes(path: str) -> int:
    """Return free bytes available at ``path``, or -1 if it cannot be read."""
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except Exception:
        return -1
