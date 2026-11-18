# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""HW Server (Vivado JTAG) and ChipScope Server start/stop/status API.

Several Vivado releases can be installed side by side under
/opt/tools/<version>/. The server runs the globally selected release (see
vivado_installer.selected_version); programming follows the same choice, so
client and server always come from one tree.

hw_server (:3121) and cs_server (:3042) are started and stopped together from
one release, so a ChipScope session and a JTAG session always agree on the
toolchain. cs_server is best-effort: if it fails to start, hw_server is left
running and the failure is reported in cs_error.

Endpoints:
    GET  /api/hw-server/status
    POST /api/hw-server/start
    POST /api/hw-server/stop
"""

import logging
import os
import re
import signal
import subprocess
import threading
from pathlib import Path

from flask import Blueprint, jsonify, request

import support_log
from helpers import IP_CMD, BulkOperationBusy, acquire_bulk_lock
import vivado_installer as vi

log = logging.getLogger(__name__)
hw_server_bp = Blueprint("hw_server", __name__)

HW_SERVER_PORT = vi.HW_SERVER_PORT
CS_SERVER_PORT = vi.CS_SERVER_PORT
LOG_PATH = Path("/var/log/rave-menu-hw-server.log")
CS_LOG_PATH = Path("/var/log/rave-menu-cs-server.log")

_state_lock = threading.Lock()
_proc: "subprocess.Popen | None" = None
_proc_version: "str | None" = None
_cs_proc: "subprocess.Popen | None" = None
_cs_proc_version: "str | None" = None


def bulk_operation_running() -> bool:
    """True when a programming job holds the bulk lock.

    Stopping hw_server mid-job would tear the server out from under a live
    xsdb, so both this module and the toolchain selector refuse while held.
    """
    try:
        lock = acquire_bulk_lock()
    except BulkOperationBusy:
        return True
    except OSError:
        return False
    lock.close()
    return False


def _preferred_version() -> "str | None":
    """Version the toggle starts: the global selection, else newest."""
    installed = vi.installed_versions()
    chosen = vi.selected_version()
    if chosen and chosen in installed:
        return chosen
    return installed[0] if installed else None


def _is_running() -> bool:
    """Whether a server is up, reconciling with the on-disk marker.

    hw_server is started with start_new_session=True, so it outlives a
    rave-menu restart that would otherwise lose _proc entirely.
    """
    global _proc, _proc_version
    if _proc is not None:
        if _proc.poll() is None:
            return True
        _proc = None
        _proc_version = None
    server = vi.running_server()
    if server:
        _proc_version = server.get("version")
        return True
    _proc_version = None
    return False


def _cs_is_running() -> bool:
    """Whether cs_server is up, reconciling with its own marker."""
    global _cs_proc, _cs_proc_version
    if _cs_proc is not None:
        if _cs_proc.poll() is None:
            return True
        _cs_proc = None
        _cs_proc_version = None
    server = vi.running_cs_server()
    if server:
        _cs_proc_version = server.get("version")
        return True
    _cs_proc_version = None
    return False


def _list_ipv4_addresses():
    """Return non-loopback IPv4 addresses as a list of {iface, addr}."""
    out = []
    try:
        result = subprocess.run(
            [IP_CMD, "-4", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            # e.g. "2: eth0    inet 192.168.0.146/24 brd ..."
            m = re.match(r"\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/", line)
            if not m:
                continue
            iface, addr = m.group(1), m.group(2)
            if iface == "lo" or addr.startswith("127."):
                continue
            out.append({"iface": iface, "addr": addr})
    except Exception as exc:
        log.warning("ip addr enumeration failed: %s", exc)
    return out


def _status_payload():
    running = _is_running()
    server = vi.running_server() if running else None
    pid = (_proc.pid if (running and _proc) else None) or (server or {}).get("pid")
    version = (_proc_version if running else None) or _preferred_version()
    cs_running = _cs_is_running()
    cs_server = vi.running_cs_server() if cs_running else None
    cs_pid = (_cs_proc.pid if (cs_running and _cs_proc) else None) or (cs_server or {}).get("pid")
    effective = vi.effective_status()
    return {
        "running": running,
        "pid": pid,
        "port": HW_SERVER_PORT,
        "cs_running": cs_running,
        "cs_pid": cs_pid,
        "cs_port": CS_SERVER_PORT,
        "cs_binary": vi.cs_server_binary(version) if version else None,
        "cs_log": str(CS_LOG_PATH),
        "version": version,
        "binary": vi.hw_server_binary(version) if version else None,
        "base_version": vi.BASE_VERSION,
        "selected_version": effective["selected_version"],
        "effective_version": effective["effective_version"],
        "effective_source": effective["effective_source"],
        "warnings": effective["warnings"],
        "installed_versions": vi.installed_versions(),
        "versions": vi.version_status(),
        "ips": _list_ipv4_addresses() if running else [],
        "log": str(LOG_PATH),
    }


@hw_server_bp.get("/api/hw-server/status")
def status():
    with _state_lock:
        return jsonify(_status_payload())


@hw_server_bp.post("/api/hw-server/start")
def start():
    """Start hw_server from the globally selected release.

    Optional JSON body: {"version": "2026.1"} overrides the selection for
    this call. A version that isn't installed yet returns 409 with error
    "not_installed" so the UI can offer to install it.
    """
    global _proc, _proc_version
    data = request.get_json(silent=True) or {}
    requested = data.get("version") or request.args.get("version") or ""

    with _state_lock:
        if _is_running():
            return jsonify({"ok": True, **_status_payload(), "message": "already running"})

        version = requested or _preferred_version() or ""
        if requested and not vi.valid_version(requested):
            return jsonify({"ok": False, "error": "invalid version"}), 400
        if not version:
            return jsonify({
                **_status_payload(),
                "ok": False,
                "error": "not_installed",
                "version": None,
                "message": f"No Vivado HW Server found under {vi.INSTALL_PREFIX}.",
            }), 409

        binary = vi.hw_server_binary(version)
        if not binary:
            return jsonify({
                **_status_payload(),
                "ok": False,
                "error": "not_installed",
                "version": version,
                "message": f"Vivado HW Server {version} is not installed.",
            }), 409

        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            log_fp = open(LOG_PATH, "ab", buffering=0)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"cannot open log file: {exc}"}), 500

        env = os.environ.copy()
        env.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")

        try:
            _proc = subprocess.Popen(
                [binary],
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            _proc_version = version
            vi.marker_write(version, _proc.pid, binary)
        except Exception as exc:
            log_fp.close()
            return jsonify({"ok": False, "error": f"failed to start hw_server: {exc}"}), 500
        finally:
            try:
                log_fp.close()
            except Exception:
                pass

        cs_error = _start_cs_server(version, env)
        if cs_error:
            log.warning("cs_server did not start: %s", cs_error)
        message = f"hw_server {version} started"
        message += ", cs_server too" if not cs_error else f" (cs_server failed: {cs_error})"

        # What a remote Vivado then does over JTAG is invisible to the menu,
        # so record the window during which that was possible.
        support_log.record("hw-server.start", {"version": version}, message, 0)

        return jsonify({
            "ok": True,
            **_status_payload(),
            "cs_error": cs_error,
            "message": message,
        })


def _start_cs_server(version: str, env: dict) -> "str | None":
    """Start cs_server for `version`. Returns an error string, or None.

    Best-effort: a ChipScope failure must not take down a working hw_server,
    so the caller reports this rather than failing the request.
    """
    global _cs_proc, _cs_proc_version
    if _cs_is_running():
        return None

    binary = vi.cs_server_binary(version)
    if not binary:
        return f"cs_server is not present in {version}"

    try:
        CS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_fp = open(CS_LOG_PATH, "ab", buffering=0)
    except Exception as exc:
        return f"cannot open cs_server log file: {exc}"

    try:
        # Port is passed explicitly so the reported cs_port stays correct
        # even if a future release changes the default. Not -d: daemon mode
        # would detach and leave us without a pid to track.
        _cs_proc = subprocess.Popen(
            [binary, "-s", f"TCP::{CS_SERVER_PORT}"],
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        _cs_proc_version = version
        vi.cs_marker_write(version, _cs_proc.pid, binary)
    except Exception as exc:
        return f"failed to start cs_server: {exc}"
    finally:
        try:
            log_fp.close()
        except Exception:
            pass
    return None


def _terminate(pid, proc):
    """SIGTERM the process group, escalating to SIGKILL after 5s."""
    if not pid:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            if proc is not None:
                proc.terminate()
        except Exception:
            pass
    try:
        if proc is not None:
            proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            if proc is not None:
                proc.wait(timeout=2)
        except Exception:
            pass


def stop_cs_server() -> dict:
    """Stop cs_server. Returns what was stopped, or {}."""
    global _cs_proc, _cs_proc_version
    if not _cs_is_running():
        _cs_proc = None
        _cs_proc_version = None
        vi.cs_marker_clear()
        return {}

    pid = _cs_proc.pid if _cs_proc is not None else (vi.running_cs_server() or {}).get("pid")
    stopped = {"version": _cs_proc_version, "pid": pid}
    _terminate(pid, _cs_proc)
    _cs_proc = None
    _cs_proc_version = None
    vi.cs_marker_clear()
    return stopped


def stop_server() -> dict:
    """Stop both servers. Returns what hw_server was, or {}.

    Shared with the toolchain selector, which stops the servers when the
    selection changes -- they only ever serve the previously selected
    release. Callers must hold _state_lock.
    """
    global _proc, _proc_version
    stop_cs_server()
    if not _is_running():
        _proc = None
        _proc_version = None
        vi.marker_clear()
        return {}

    stopped = {"version": _proc_version}
    pid = _proc.pid if _proc is not None else (vi.running_server() or {}).get("pid")
    stopped["pid"] = pid

    _terminate(pid, _proc)

    _proc = None
    _proc_version = None
    vi.marker_clear()
    return stopped


@hw_server_bp.post("/api/hw-server/stop")
def stop():
    # Refusing while a programming job runs keeps the server alive under a
    # live xsdb connection.
    if bulk_operation_running():
        return jsonify({
            "ok": False,
            "error": "busy",
            "message": "a programming operation is running; "
                       "stopping the hardware server now would interrupt it",
        }), 409
    with _state_lock:
        stopped = stop_server()
        message = "hw_server stopped" if stopped else "not running"
        if stopped:
            support_log.record("hw-server.stop", {}, message, 0)
        return jsonify({"ok": True, **_status_payload(),
                        "stopped_server": stopped, "message": message})