# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Board-test routes -- start/status/rerun/log for the hardware test runner.

These endpoints delegate to board_test_runner which manages serial
communication with the Versal device under test.
"""

from flask import Blueprint, jsonify, request
import board_test_runner

board_test = Blueprint("board_test", __name__)


@board_test.post("/api/board-test/discover")
def board_test_discover():
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force", False))
    snap = board_test_runner.discover(force=force)
    return jsonify(snap)


@board_test.post("/api/board-test/start")
def board_test_start():
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force", False))
    iperf = data.get("iperf") if isinstance(data.get("iperf"), dict) else None
    iperf10g = data.get("iperf10g") if isinstance(data.get("iperf10g"), dict) else None
    ok, message, snap = board_test_runner.start_all(force=force, iperf_options=iperf, iperf10g_options=iperf10g)
    status = 200 if ok else 409
    return jsonify({"ok": ok, "message": message, "snapshot": snap}), status


@board_test.get("/api/board-test/status")
def board_test_status():
    return jsonify(board_test_runner.snapshot())


@board_test.post("/api/board-test/rerun/<test_id>")
def board_test_rerun(test_id: str):
    data = request.get_json(silent=True) or {}
    iperf = data.get("iperf") if isinstance(data.get("iperf"), dict) else None
    iperf10g = data.get("iperf10g") if isinstance(data.get("iperf10g"), dict) else None
    ok, message, snap = board_test_runner.rerun_one(test_id, iperf_options=iperf, iperf10g_options=iperf10g)
    if message.startswith("unknown test"):
        return jsonify({"ok": False, "error": message, "snapshot": snap}), 404
    status = 200 if ok else 409
    return jsonify({"ok": ok, "message": message, "snapshot": snap}), status


@board_test.get("/api/board-test/log/<test_id>")
def board_test_log(test_id: str):
    output = board_test_runner.get_test_output(test_id)
    if output is None:
        return jsonify({"ok": False, "error": f"unknown test: {test_id}"}), 404
    return jsonify({"ok": True, "test_id": test_id, "output": output})
