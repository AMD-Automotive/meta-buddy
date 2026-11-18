# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Global Vivado toolchain selection API.

One choice drives everything: the release the Versal programming flows use
*and* the release the hardware server serves. Keeping them as a single
setting is what makes an unsupported client/server pairing unreachable.

The choice is persisted (see vivado_installer.SELECTION_PATH) so it survives
a reboot, and the RAUC bundle hook carries it to the new slot on update.

Endpoints:
    GET  /api/tools/version
    POST /api/tools/version   {"version": "2026.1"}
"""

import logging

from flask import Blueprint, jsonify, request

import vivado_installer as vi
from routes_hw_server import _state_lock, bulk_operation_running, stop_server

log = logging.getLogger(__name__)
tools_bp = Blueprint("tools", __name__)


def _payload():
    status = vi.effective_status()
    return {
        "versions": vi.version_status(),
        "installed_versions": vi.installed_versions(),
        "selected": status["selected_version"],
        "effective": status["effective_version"],
        "effective_source": status["effective_source"],
        "programming_qualified": status["programming_qualified"],
        "programming_versions": status["programming_versions"],
        "base_version": vi.BASE_VERSION,
        "running": status["running"],
        "warnings": status["warnings"],
    }


@tools_bp.get("/api/tools/version")
def get_version():
    return jsonify(_payload())


@tools_bp.post("/api/tools/version")
def set_version():
    """Persist the global toolchain choice and stop any running server.

    A server only ever serves the previously selected release, so leaving it
    up would keep the board in the mismatched state this endpoint exists to
    prevent. The caller is expected to have confirmed with the user first.
    """
    data = request.get_json(silent=True) or {}
    version = str(data.get("version") or "")

    if not vi.valid_version(version):
        return jsonify({"ok": False, "error": "invalid version"}), 400

    if not vi.is_installed(version):
        return jsonify({
            **_payload(),
            "ok": False,
            "error": "not_installed",
            "version": version,
            "message": f"Vivado HW Server {version} is not installed.",
        }), 409

    # Re-picking the current selection is only a no-op when that selection is
    # actually in effect. If a different release is serving, it is overriding
    # the selection, and stopping it is the only way to honour the click.
    server = vi.running_server() or {}
    mismatch = bool(server.get("version")) and server["version"] != version
    if version == vi.selected_version() and not mismatch:
        return jsonify({**_payload(), "ok": True, "stopped_server": {},
                        "message": f"{version} already selected"})

    if bulk_operation_running():
        return jsonify({
            **_payload(),
            "ok": False,
            "error": "busy",
            "message": "a programming operation is running; "
                       "changing the toolchain now would interrupt it",
        }), 409

    with _state_lock:
        stopped = stop_server()
        if not vi.set_selected_version(version):
            return jsonify({**_payload(), "ok": False,
                            "error": "could not persist selection"}), 500
        message = f"programming tools set to {version}"
        if stopped:
            message += f"; {stopped.get('version') or 'hardware'} server stopped"
        return jsonify({**_payload(), "ok": True,
                        "stopped_server": stopped, "message": message})
