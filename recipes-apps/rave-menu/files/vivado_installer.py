# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Vivado HW Server (Lab Tools) installer helpers.

Pure helpers; no Flask. Imported by routes_vivado_install.py,
routes_hw_server.py and routes_tools.py.

Manifest is shipped by the `vivado-installer-assets` recipe as a single
JSON file at /usr/share/vivado-installer/manifest.json. The recipe owns the
catalog of selectable releases (filename, URL, expected SHA-256, QR image)
plus the install prefix, so adding a release is a one-recipe change.

Several releases can be installed side by side under
<install_prefix>/<version>/. Intermixing releases is not supported: the
`xsdb` used for programming must come from the same tree as the `hw_server`
it connects to. `resolve_version()` is the single place that decides which
tree every operation uses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths & manifest
# ---------------------------------------------------------------------------

ASSET_DIR = Path("/usr/share/vivado-installer")
# Overridable so the shell wrapper and the tests can point both halves at the
# same manifest instead of the installed one.
MANIFEST_PATH = Path(os.environ.get("RAVE_VIVADO_MANIFEST")
                     or ASSET_DIR / "manifest.json")

# Persistent state lives outside the install prefix so a wiped reinstall is
# recoverable.
VIVADO_STATE_DIR = Path(os.environ.get("RAVE_VIVADO_STATE_DIR")
                        or "/var/lib/rave-menu/vivado")
UPLOADS_DIR = VIVADO_STATE_DIR / "uploads"

# Locations to scan for an already-downloaded tar (browser on target, USB).
# Cog/Chromium runs as the `weston` user on this image, so its default
# Downloads dir is /home/weston/Downloads. amd-edf and root are kept for
# manual scp / desktop-session debugging.
DOWNLOAD_SCAN_ROOTS = [
    Path("/home/weston/Downloads"),
    Path("/home/amd-edf/Downloads"),
    Path("/root/Downloads"),
]
USB_ROOT = Path("/run/media")

# Layouts xsetup may produce under <prefix>/<version>/, most specific first.
HW_SERVER_SUBPATHS = (
    "HWSRVR/bin/hw_server",
    "Vivado_HW_Server/bin/hw_server",
    "Vivado_Lab/bin/hw_server",
    "Vivado/bin/hw_server",
)

# ChipScope server, shipped in the same bin/ as hw_server. It listens on its
# own port, so the two run side by side from one release.
CS_SERVER_SUBPATHS = tuple(
    sub.replace("hw_server", "cs_server") for sub in HW_SERVER_SUBPATHS
)

# Tool-tree roots and the settings script each may carry. Observed installs
# ship HWSRVR/bin/setupEnv.sh; the rest are tolerated layouts.
TOOL_ROOT_SUBDIRS = ("HWSRVR", "Vivado_Lab", "Vivado_HW_Server", "Vivado")
SETTINGS_NAMES = ("settings64.sh", "bin/setupEnv.sh")

# Written by /api/hw-server/start, removed by /api/hw-server/stop. On tmpfs so
# it cannot survive a reboot and go stale, and is not carried across a RAUC
# update.
RUNTIME_DIR = Path("/run/rave-menu")
MARKER_PATH = RUNTIME_DIR / "hw-server.json"
CS_MARKER_PATH = RUNTIME_DIR / "cs-server.json"

# The user's global toolchain choice. On persistent storage so it survives a
# reboot; the RAUC bundle hook copies it to the new slot.
SELECTION_PATH = VIVADO_STATE_DIR / "selected-version.json"

HW_SERVER_PORT = 3121
CS_SERVER_PORT = 3042

# Versions are used as path components and are accepted from HTTP requests.
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")


def valid_version(version: str) -> bool:
    return bool(version) and ".." not in version and bool(_VERSION_RE.match(version))


def _load_manifest() -> dict:
    try:
        with MANIFEST_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.error("vivado installer manifest missing at %s", MANIFEST_PATH)
        return {}
    except (OSError, ValueError) as exc:
        log.error("failed to read manifest %s: %s", MANIFEST_PATH, exc)
        return {}


def _parse_catalog(manifest: dict) -> "dict[str, dict]":
    out: dict[str, dict] = {}
    for raw in manifest.get("versions") or []:
        version = str(raw.get("version") or "")
        if not valid_version(version):
            log.error("manifest lists unusable version %r; skipping", version)
            continue
        out[version] = {
            "version": version,
            "filename": str(raw.get("filename") or ""),
            "url": str(raw.get("url") or ""),
            "sha256": str(raw.get("sha256") or "").lower(),
            "qr": str(raw.get("qr") or ""),
        }
    return out


_MANIFEST = _load_manifest()
INSTALL_PREFIX: str = _MANIFEST.get("install_prefix") or "/opt/tools"
CATALOG: "dict[str, dict]" = _parse_catalog(_MANIFEST)


def _pick(key: str) -> str:
    value = str(_MANIFEST.get(key) or "")
    if value in CATALOG:
        return value
    return next(iter(CATALOG), "")


# The release the Versal programming scripts are qualified against.
BASE_VERSION: str = _pick("base_version")
# The release pre-selected in the installer UI.
DEFAULT_VERSION: str = _pick("default_version") or BASE_VERSION


def _parse_programming_versions(manifest: dict) -> "tuple[str, ...]":
    """Releases validated for Versal programming; defaults to the catalog."""
    raw = manifest.get("programming_versions")
    if isinstance(raw, str):
        raw = raw.split()
    if not isinstance(raw, (list, tuple)) or not raw:
        return tuple(CATALOG)
    return tuple(str(v) for v in raw if valid_version(str(v)))


PROGRAMMING_VERSIONS: "tuple[str, ...]" = _parse_programming_versions(_MANIFEST)


def is_programming_qualified(version: str) -> bool:
    """False only when the manifest explicitly excludes this release."""
    return version in PROGRAMMING_VERSIONS if PROGRAMMING_VERSIONS else True

_SHA_TO_VERSION = {e["sha256"]: v for v, e in CATALOG.items() if e["sha256"]}


def version_sort_key(version: str) -> tuple:
    """Numeric sort key so 2026.1 sorts above 2025.2 (and above 2025.10)."""
    return tuple(int(n) for n in re.findall(r"\d+", version))


def catalog_versions() -> "list[str]":
    """Catalog versions, newest first."""
    return sorted(CATALOG, key=version_sort_key, reverse=True)


def entry_for(version: str) -> "dict | None":
    return CATALOG.get(version)


def install_dir(version: str) -> "Path | None":
    if not valid_version(version):
        return None
    return Path(INSTALL_PREFIX) / version


def qr_path(version: str) -> "Path | None":
    entry = CATALOG.get(version)
    if not entry or not entry["qr"]:
        return None
    # Basename comes from the manifest, never from the request.
    return ASSET_DIR / os.path.basename(entry["qr"])


# ---------------------------------------------------------------------------
# State files (one per installed version)
# ---------------------------------------------------------------------------

# Pre-multi-version images wrote a single state.json for the base release.
LEGACY_STATE_PATH = VIVADO_STATE_DIR / "state.json"


def state_path(version: str) -> Path:
    return VIVADO_STATE_DIR / f"state-{version}.json"


def tar_cache_path(version: str) -> Path:
    return VIVADO_STATE_DIR / f"installer-{version}.tar"


def state_read(version: str) -> dict:
    paths = [state_path(version)]
    if version == BASE_VERSION:
        paths.append(LEGACY_STATE_PATH)
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


# ---------------------------------------------------------------------------
# Install detection
# ---------------------------------------------------------------------------

def hw_server_binary(version: "str | None" = None) -> "str | None":
    """Path to a usable hw_server, for `version` or the newest installed."""
    return _server_binary(HW_SERVER_SUBPATHS, version, hw_server_binary)


def cs_server_binary(version: "str | None" = None) -> "str | None":
    """Path to a usable cs_server, for `version` or the newest installed."""
    return _server_binary(CS_SERVER_SUBPATHS, version, cs_server_binary)


def _server_binary(subpaths, version, recurse):
    if version is None:
        for candidate in installed_versions():
            found = recurse(candidate)
            if found:
                return found
        return None
    target = install_dir(version)
    if target is None:
        return None
    for sub in subpaths:
        path = target / sub
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def installed_versions() -> "list[str]":
    """Versions with a usable hw_server on disk, newest first.

    Scans the prefix rather than the catalog so a release installed by hand
    (or one dropped from the catalog) is still offered.
    """
    found: list[str] = []
    try:
        children = list(Path(INSTALL_PREFIX).iterdir())
    except OSError:
        children = []
    for child in children:
        if not child.is_dir() or not valid_version(child.name):
            continue
        if hw_server_binary(child.name):
            found.append(child.name)
    return sorted(found, key=version_sort_key, reverse=True)


def is_installed(version: "str | None" = None) -> bool:
    """True if a usable hw_server is present (for `version`, or any)."""
    return hw_server_binary(version) is not None


def settings_script(version: str) -> "str | None":
    """Path to the settings script to source for `version`, or None."""
    target = install_dir(version)
    if target is None:
        return None
    for subdir in TOOL_ROOT_SUBDIRS:
        for name in SETTINGS_NAMES:
            path = target / subdir / name
            if path.is_file():
                return str(path)
    return None


# ---------------------------------------------------------------------------
# Global toolchain selection (persistent, survives reboot and RAUC update)
# ---------------------------------------------------------------------------

def selected_version() -> "str | None":
    """The user's chosen release, or None if never set / unusable."""
    try:
        with SELECTION_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("version") or "")
    return version if valid_version(version) else None


def set_selected_version(version: str) -> bool:
    """Persist the global choice. Atomic so a crash cannot truncate it."""
    if not valid_version(version):
        return False
    payload = {"version": version, "set": now_iso()}
    try:
        SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SELECTION_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SELECTION_PATH)
    except OSError as exc:
        log.error("could not persist selected version %s: %s", version, exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Running hw_server detection
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    # /proc rather than kill(pid, 0): signalling a process owned by another
    # user raises EPERM, which means alive, not dead.
    return Path(f"/proc/{pid}").is_dir()


def _version_from_path(path: str) -> "str | None":
    """Map any path inside a tool tree back to its release."""
    prefix = str(INSTALL_PREFIX).rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    candidate = path[len(prefix):].split("/", 1)[0]
    return candidate if valid_version(candidate) else None


def _proc_field(pid: int, name: str) -> str:
    try:
        with open(f"/proc/{pid}/{name}", "rb") as f:
            return f.read().decode("utf-8", "replace").replace("\0", " ")
    except OSError:
        return ""


def _version_of_pid(pid: int) -> "str | None":
    """Release a pid belongs to, via cmdline then exe.

    The pid the menu records is a `bash` wrapper whose /proc/<pid>/exe is
    /usr/bin/bash.bash, so cmdline has to be consulted first. The real
    binary (the port owner) lives at .../bin/unwrapped/lnx64.o/hw_server,
    which is why this prefix-matches rather than comparing known subpaths.
    """
    for token in _proc_field(pid, "cmdline").split():
        version = _version_from_path(token)
        if version:
            return version
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None
    return _version_from_path(exe)


def _marker_read(path: "Path | None" = None) -> "dict | None":
    path = path or MARKER_PATH
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def marker_write(version: str, pid: int, binary: "str | None",
                 path: "Path | None" = None, port: "int | None" = None) -> None:
    path = path or MARKER_PATH
    port = HW_SERVER_PORT if port is None else port
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump({
                "version": version,
                "pid": pid,
                "binary": binary,
                "port": port,
                "started": now_iso(),
            }, f, indent=2, sort_keys=True)
            f.write("\n")
    except OSError as exc:
        log.warning("could not write server marker %s: %s", path, exc)


def marker_clear(path: "Path | None" = None) -> None:
    path = path or MARKER_PATH
    try:
        path.unlink()
    except OSError:
        pass


def cs_marker_write(version: str, pid: int, binary: "str | None") -> None:
    marker_write(version, pid, binary, CS_MARKER_PATH, CS_SERVER_PORT)


def cs_marker_clear() -> None:
    marker_clear(CS_MARKER_PATH)


def _port_owner_pid(port: int = HW_SERVER_PORT) -> "int | None":
    """Pid listening on `port`. `ss` is absent from the image; use netstat."""
    try:
        out = subprocess.run(["netstat", "-ltnp"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        parts = line.split()
        # proto recvq sendq local foreign state pid/prog
        if len(parts) < 7 or parts[5] != "LISTEN":
            continue
        if not parts[3].endswith(f":{port}"):
            continue
        owner = parts[6].split("/", 1)[0]
        if owner.isdigit():
            return int(owner)

    try:
        out = subprocess.run(["lsof", "-t", f"-i:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for token in out.split():
        if token.isdigit():
            return int(token)
    return None


def running_server() -> "dict | None":
    """The hw_server currently serving, or None.

    Signal A is the marker written at start; signal B is whoever actually
    owns :3121, which covers a server the menu did not start.
    """
    return _running_server(MARKER_PATH, HW_SERVER_PORT, hw_server_binary)


def running_cs_server() -> "dict | None":
    """The cs_server currently serving, or None."""
    return _running_server(CS_MARKER_PATH, CS_SERVER_PORT, cs_server_binary)


def _running_server(marker_path, port, binary_fn) -> "dict | None":
    marker = _marker_read(marker_path)
    if marker:
        pid = marker.get("pid")
        version = str(marker.get("version") or "")
        if isinstance(pid, int) and _pid_alive(pid):
            actual = _version_of_pid(pid)
            if actual and (not version or actual == version):
                return {"version": actual, "pid": pid,
                        "binary": marker.get("binary"), "source": "marker"}

    pid = _port_owner_pid(port)
    if pid and _pid_alive(pid):
        version = _version_of_pid(pid)
        if version:
            return {"version": version, "pid": pid,
                    "binary": binary_fn(version), "source": "port"}
        return {"version": None, "pid": pid, "binary": None, "source": "port"}
    return None


# ---------------------------------------------------------------------------
# The single precedence decision
# ---------------------------------------------------------------------------

def resolve_version(requested: "str | None" = None) -> tuple:
    """Decide which release every operation should use.

    Returns (version_or_None, source, warnings). Precedence:
      1. explicit request / RAVE_VIVADO_VERSION
      2. the running hw_server -- intermixing releases is unsupported
      3. the user's global selection
      4. manifest base_version
      5. newest installed
    """
    warnings: list[str] = []
    installed = installed_versions()

    override = requested or os.environ.get("RAVE_VIVADO_VERSION") or ""
    if override:
        if not valid_version(override):
            warnings.append(f"ignoring invalid requested version {override!r}")
        elif override not in installed:
            warnings.append(
                f"requested version {override} is not installed; ignoring")
        else:
            return override, "override", warnings

    chosen = selected_version()
    if chosen and chosen not in installed:
        warnings.append(
            f"selected version {chosen} is no longer installed; ignoring it")
        chosen = None

    server = running_server()
    if server:
        version = server.get("version")
        if version and version in installed:
            if chosen and chosen != version:
                warnings.append(
                    f"a {version} hardware server is running; programming will "
                    f"use {version} until it is stopped")
            return version, "running", warnings
        warnings.append(
            "something is listening on port %d but could not be matched to an "
            "installed release; ignoring it" % HW_SERVER_PORT)

    if chosen:
        return chosen, "selection", warnings

    if BASE_VERSION and BASE_VERSION in installed:
        return BASE_VERSION, "base", warnings

    if installed:
        return installed[0], "newest", warnings

    return None, "none", warnings


def effective_status(requested: "str | None" = None) -> dict:
    """resolve_version() plus everything the UI and logs want to show."""
    version, source, warnings = resolve_version(requested)
    if version and not is_programming_qualified(version):
        warnings.append(
            f"{version} is not listed as qualified for Versal programming on "
            f"this hardware; proceeding anyway")
    server = running_server()
    return {
        "effective_version": version,
        "effective_source": source,
        "selected_version": selected_version(),
        "base_version": BASE_VERSION,
        "settings": settings_script(version) if version else None,
        "programming_qualified": is_programming_qualified(version) if version else False,
        "programming_versions": list(PROGRAMMING_VERSIONS),
        "running": server,
        "warnings": warnings,
    }



# ---------------------------------------------------------------------------
# SHA-256 verification (with a small mtime/size cache)
# ---------------------------------------------------------------------------

# Cap files we hash to avoid scanning a 50 GB blob someone parked in Downloads.
MAX_HASH_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB

# Cache: path -> (mtime, size, sha256)
_HASH_CACHE: dict[str, tuple[float, int, str]] = {}


def _sha256_of(path: Path) -> "str | None":
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size > MAX_HASH_BYTES:
        return None
    key = str(path)
    cached = _HASH_CACHE.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as exc:
        log.warning("hash failed for %s: %s", path, exc)
        return None
    digest = h.hexdigest()
    _HASH_CACHE[key] = (st.st_mtime, st.st_size, digest)
    return digest


def verify_tar(path: "str | Path", version: "str | None" = None) -> tuple[bool, "str | None"]:
    """Return (matches, computed_sha256_or_None).

    With `version`, "matches" means the tar is that catalog release. Without
    it, any catalog release counts -- used by discovery, which doesn't know
    yet which one the user wants.
    """
    digest = _sha256_of(Path(path))
    if digest is None:
        return False, None
    if version is None:
        return digest in _SHA_TO_VERSION, digest
    entry = CATALOG.get(version)
    return bool(entry and entry["sha256"] == digest), digest


def version_for_sha(digest: "str | None") -> "str | None":
    """Which catalog release a computed digest belongs to, if any."""
    if not digest:
        return None
    return _SHA_TO_VERSION.get(digest.lower())


# ---------------------------------------------------------------------------
# Discovery: find candidate tars the user may have produced
# ---------------------------------------------------------------------------

# Chromium can rename downloads:
#   *.crdownload                -> in-progress / aborted
#   "Vivado... (1).tar"         -> duplicate suffix
#   "Vivado....tar.duplicate"   -> rare collision suffix
# Everything that *might* be the installer is surfaced and SHA-checked.
_TAR_GLOBS = ("*.tar", "*.TAR", "*.tar.crdownload", "*.tar.part", "*.tar.*")

# A loose prefix used to surface partial / browser-renamed downloads,
# e.g. "Vivado_HW_Server_Lin_2026.1" out of
# Vivado_HW_Server_Lin_2026.1_0616_1700.tar.
_FILENAME_FUZZY_PREFIXES = {}
for _version, _entry in CATALOG.items():
    _m = re.match(r"([A-Za-z0-9_]+?_\d+\.\d+)_", _entry["filename"])
    if _m:
        _FILENAME_FUZZY_PREFIXES[_version] = _m.group(1)


def _fuzzy_version(name: str) -> "str | None":
    """Guess the release a filename refers to, ignoring its checksum."""
    for version, prefix in _FILENAME_FUZZY_PREFIXES.items():
        if prefix in name:
            return version
    return None


def _is_candidate(name: str) -> bool:
    n = name.lower()
    if n.endswith(".tar") or n.endswith(".tar.crdownload") or n.endswith(".tar.part"):
        return True
    # numeric duplicate suffix: "...tar (1)" / "...tar.1" etc.
    return n.endswith(".tar") or ".tar." in n


def _scan_dir(root: Path, max_depth: int = 2) -> Iterable[Path]:
    try:
        if not root.is_dir():
            return []
    except OSError:
        return []
    out: list[Path] = []
    root_str = str(root)
    base_depth = root_str.count(os.sep)
    for dirpath, _dirs, files in os.walk(root_str, followlinks=False):
        depth = dirpath.count(os.sep) - base_depth
        if depth > max_depth:
            continue
        for name in files:
            if _is_candidate(name):
                out.append(Path(dirpath) / name)
    return out


def _classify(path: Path) -> dict:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    sha_ok, digest = verify_tar(path)
    matched = version_for_sha(digest)
    name = path.name
    guess = _fuzzy_version(name)
    hint = ""
    if name.endswith(".crdownload") or name.endswith(".part"):
        hint = "Looks like a partial / aborted download."
    elif not sha_ok and guess:
        hint = f"Filename looks like {guess} but the checksum does not match."
    elif matched and CATALOG[matched]["filename"] != name:
        hint = f"Renamed by browser, but the checksum matches {matched}."
    return {
        "path": str(path),
        "name": name,
        "size": size,
        "sha256": digest,
        "sha_ok": bool(sha_ok),
        "version": matched,
        "version_guess": guess,
        "hint": hint,
    }


def discover_local_tars() -> "list[dict]":
    """Scan USB media + Downloads dirs + cache + uploads for candidate tars.

    Each entry carries the catalog `version` its checksum matches, or None.
    """
    found: list[dict] = []
    seen: set[str] = set()

    def add(p: Path, kind: str):
        try:
            real = str(p.resolve())
        except OSError:
            return
        if real in seen:
            return
        seen.add(real)
        info = _classify(p)
        info["kind"] = kind
        found.append(info)

    # Cached / staged. installer-<ver>.tar is the current naming; plain
    # installer.tar is what pre-multi-version images left behind.
    for cached in (*VIVADO_STATE_DIR.glob("installer-*.tar"),
                   VIVADO_STATE_DIR / "installer.tar"):
        if cached.is_file():
            add(cached, "cache")
    if UPLOADS_DIR.is_dir():
        for p in UPLOADS_DIR.iterdir():
            if p.is_file() and _is_candidate(p.name):
                add(p, "upload")

    # Target-side browser Downloads
    for root in DOWNLOAD_SCAN_ROOTS:
        for p in _scan_dir(root, max_depth=2):
            add(p, "download")

    # USB media -- only the immediate top of each mount, no recursion.
    # Scanning into a rootfs-shaped USB stick would walk thousands of files
    # and surface noise like /usr/bin/tar.tar. On this image udisks/systemd
    # mounts removable media as /run/media/<volume>/, so the installer tar
    # is expected at the very top of the stick.
    if USB_ROOT.is_dir():
        try:
            mounts = list(USB_ROOT.iterdir())
        except OSError:
            mounts = []
        for mount in mounts:
            if not mount.is_dir():
                continue
            try:
                for p in mount.iterdir():
                    if p.is_file() and _is_candidate(p.name):
                        add(p, "usb")
            except OSError:
                continue

    # Sort: matching SHA first, then larger files (more likely complete).
    found.sort(key=lambda d: (not d["sha_ok"], -(d.get("size") or 0)))
    return found


# ---------------------------------------------------------------------------
# Helpers used by the install route
# ---------------------------------------------------------------------------

def version_status() -> "list[dict]":
    """Catalog entries plus install state, newest first.

    Versions installed on disk but absent from the catalog are appended so
    the UI never hides a working hw_server.
    """
    out: list[dict] = []
    on_disk = installed_versions()
    chosen = selected_version()
    for version in sorted(set(CATALOG) | set(on_disk),
                          key=version_sort_key, reverse=True):
        entry = CATALOG.get(version, {"version": version, "filename": "",
                                      "url": "", "sha256": "", "qr": ""})
        binary = hw_server_binary(version)
        out.append({
            **entry,
            "installed": binary is not None,
            "hw_server": binary,
            "install_dir": str(Path(INSTALL_PREFIX) / version),
            "in_catalog": version in CATALOG,
            "is_base": version == BASE_VERSION,
            "is_selected": version == chosen,
            "programming_qualified": is_programming_qualified(version),
            "state": state_read(version),
        })
    return out


def ensure_dirs() -> None:
    for d in (VIVADO_STATE_DIR, UPLOADS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# CLI -- so vivado_env.sh asks exactly the same question the web UI does
# ---------------------------------------------------------------------------

def _main(argv: "list[str]") -> int:
    command = argv[1] if len(argv) > 1 else ""
    requested = argv[2] if len(argv) > 2 else None

    if command == "resolve-settings":
        version, _source, _warnings = resolve_version(requested)
        path = settings_script(version) if version else None
        if not path:
            return 1
        print(path)
        return 0

    if command == "resolve-version":
        version, source, _warnings = resolve_version(requested)
        if not version:
            return 1
        print(f"{version}\t{source}")
        return 0

    if command == "describe":
        # One human-readable line for job logs, plus any warnings.
        status = effective_status(requested)
        version = status["effective_version"]
        if not version:
            print("no Vivado HW Server install found")
            return 1
        server = status["running"] or {}
        reason = {
            "override": "explicit override",
            "running": f"matching running hw_server, pid {server.get('pid')}",
            "selection": "your selection",
            "base": "qualified default",
            "newest": "newest installed",
        }.get(status["effective_source"], status["effective_source"])
        print(f"using {version} ({reason})")
        for warning in status["warnings"]:
            print(f"WARNING: {warning}")
        return 0

    if command == "status":
        print(json.dumps(effective_status(requested), indent=2, sort_keys=True))
        return 0

    print("usage: vivado_installer.py "
          "{resolve-settings|resolve-version|describe|status} [version]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
