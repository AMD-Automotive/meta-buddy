# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Versal UFS partition layout scanning for the Flash Program advanced panel.

Endpoint: /api/versal/layout/scan
"""

import logging
import os
import subprocess

from flask import Blueprint, jsonify, request

import support_log
import versal_partitions as vp
from helpers import SCRIPTS_DIR, BulkOperationBusy, acquire_bulk_lock

log = logging.getLogger(__name__)
versal_layout_bp = Blueprint("versal_layout", __name__)

SCAN_SCRIPT = SCRIPTS_DIR / "scan_versal_layout.sh"
SCAN_OUTPUT = "/run/rave-menu/versal-gpt.bin"
SCAN_TIMEOUT_S = 600


@versal_layout_bp.post("/api/versal/layout/scan")
def scan_layout():
    payload = request.get_json(silent=True) or {}
    wic = (payload.get("wic") or "").strip()
    bmap = (payload.get("bmap") or "").strip()

    if not wic:
        return jsonify({"error": "a Rootfs WIC image is required to compare layouts"}), 400
    if not SCAN_SCRIPT.exists() or not os.access(SCAN_SCRIPT, os.X_OK):
        return jsonify({"error": f"scan script missing or not executable: {SCAN_SCRIPT}"}), 500

    try:
        lock_file = acquire_bulk_lock()
    except BulkOperationBusy as exc:
        return jsonify({"error": str(exc)}), 409

    try:
        os.makedirs(os.path.dirname(SCAN_OUTPUT), exist_ok=True)
        try:
            result = subprocess.run(
                [str(SCAN_SCRIPT), SCAN_OUTPUT],
                cwd=str(SCRIPTS_DIR), capture_output=True, text=True,
                timeout=SCAN_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            support_log.record("versal.layout-scan", {"wic": wic, "bmap": bmap},
                               "timed out reading the partition table",
                               None, status="timeout")
            return jsonify({"error": "timed out reading the partition table from the target"}), 504

        if result.returncode != 0:
            log.warning("versal layout scan failed: %s", result.stderr[-2000:])
            support_log.record("versal.layout-scan", {"wic": wic, "bmap": bmap},
                               result.stdout + result.stderr, result.returncode)
            return jsonify({
                "error": "could not read the partition table from the target",
                "log": (result.stdout + result.stderr)[-4000:],
            }), 500

        with open(SCAN_OUTPUT, "rb") as handle:
            raw = handle.read()

        target = vp.parse_gpt(raw)
        image = vp.parse_gpt(vp.read_image_head(wic))
        plan = vp.build_plan(image, target, bool(bmap))
    except vp.LayoutError as exc:
        return jsonify({"error": str(exc)}), 422
    except OSError as exc:
        return jsonify({"error": f"layout scan failed: {exc}"}), 500
    finally:
        lock_file.close()

    return jsonify({"ok": True, "target": target, "image": image, "plan": plan})
