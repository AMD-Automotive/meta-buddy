# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""ADAU1860 Codec routes -- hardware codec control.

Endpoints:
    GET  /api/adau1860/snapshot      -- full codec state
    GET  /api/adau1860/status        -- basic status (PLL, power)
    POST /api/adau1860/volume        -- {"db": float} or {"percent": int}
    POST /api/adau1860/mute          -- {"mute": bool}
    POST /api/adau1860/output_mode   -- {"mode": "headphone"|"lineout"}
    POST /api/adau1860/asrc          -- {"bypass": bool}
    POST /api/adau1860/reinit        -- reinitialize codec
    GET  /api/adau1860/dmic          -- DMIC status
    POST /api/adau1860/register      -- {"addr": int, "value": int} for raw access
    GET  /api/adau1860/register/<addr> -- read raw register
"""

from flask import Blueprint, jsonify, request

import adau1860

adau1860_bp = Blueprint("adau1860", __name__)


@adau1860_bp.get("/api/adau1860/snapshot")
def snapshot():
    return jsonify(adau1860.codec_snapshot())


@adau1860_bp.get("/api/adau1860/status")
def status():
    return jsonify(adau1860.get_codec_status())


@adau1860_bp.post("/api/adau1860/volume")
def set_volume():
    data = request.get_json(silent=True) or {}
    if "db" in data:
        try:
            db = float(data["db"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid db value"}), 400
        ok, msg = adau1860.set_dac_volume_db(db)
    elif "percent" in data:
        try:
            pct = int(data["percent"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid percent value"}), 400
        ok, msg = adau1860.set_dac_volume_percent(pct)
    else:
        return jsonify({"ok": False, "error": "missing 'db' or 'percent'"}), 400
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


@adau1860_bp.post("/api/adau1860/mute")
def set_mute():
    data = request.get_json(silent=True) or {}
    mute = bool(data.get("mute", False))
    ok, msg = adau1860.set_dac_mute(mute)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


@adau1860_bp.post("/api/adau1860/output_mode")
def set_output_mode():
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).lower()
    if mode not in ("headphone", "lineout"):
        return jsonify({"ok": False, "error": "mode must be 'headphone' or 'lineout'"}), 400
    ok, msg = adau1860.set_output_mode(mode)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


@adau1860_bp.post("/api/adau1860/asrc")
def set_asrc():
    data = request.get_json(silent=True) or {}
    bypass = bool(data.get("bypass", True))
    ok, msg = adau1860.set_asrc_bypass(bypass)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


@adau1860_bp.post("/api/adau1860/reinit")
def reinit():
    ok, msg = adau1860.reinit_codec()
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


@adau1860_bp.get("/api/adau1860/dmic")
def get_dmic():
    return jsonify(adau1860.get_dmic_levels())


@adau1860_bp.get("/api/adau1860/register/<addr>")
def read_register(addr):
    try:
        addr_int = int(addr, 0)  # supports 0x prefix
    except ValueError:
        return jsonify({"ok": False, "error": "invalid address"}), 400
    ok, value, msg = adau1860.read_register(addr_int)
    return jsonify({"ok": ok, "value": value, "message": msg}), 200 if ok else 500


@adau1860_bp.post("/api/adau1860/register")
def write_register():
    data = request.get_json(silent=True) or {}
    try:
        addr = int(data.get("addr", 0))
        value = int(data.get("value", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid addr or value"}), 400
    ok, msg = adau1860.write_register(addr, value)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


@adau1860_bp.post("/api/adau1860/dmic_meter/start")
def dmic_meter_start():
    """Start the streaming DMIC level meter."""
    return jsonify(adau1860.start_dmic_metering())


@adau1860_bp.post("/api/adau1860/dmic_meter/stop")
def dmic_meter_stop():
    """Stop the streaming DMIC level meter."""
    return jsonify(adau1860.stop_dmic_metering())


@adau1860_bp.get("/api/adau1860/dmic_levels")
def dmic_levels():
    """Get current DMIC levels from streaming meter."""
    return jsonify(adau1860.get_dmic_levels())


@adau1860_bp.post("/api/adau1860/loopback/start")
def loopback_start():
    """Start a 5-second record + playback loopback test."""
    return jsonify(adau1860.start_loopback_test())


@adau1860_bp.get("/api/adau1860/loopback/status")
def loopback_status():
    """Get the current loopback test status."""
    return jsonify(adau1860.get_loopback_status())
