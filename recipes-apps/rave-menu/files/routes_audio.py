# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Audio routes -- unified audio state and control.

New unified API:
    GET  /api/audio/state          -- complete audio state (sinks + inputs)
    POST /api/audio/set_default    -- {"id": "<sink_id>"}
    POST /api/audio/set_volume     -- {"id": "<sink_id>", "percent": 0-100}
    POST /api/audio/set_mute       -- {"id": "<sink_id>", "mute": bool}
    POST /api/audio/mic_control    -- {"name": "pga_gain"|"boost", "value": int|bool}
    POST /api/audio/dmic/start_metering
    POST /api/audio/dmic/stop_metering
    GET  /api/audio/dmic/levels
    POST /api/audio/dmic/test
    GET  /api/audio/dmic/test/status

Legacy endpoints preserved for backward compat:
    GET  /api/audio/snapshot
    POST /api/audio/default_sink
    POST /api/audio/sink_volume
    POST /api/audio/sink_mute
"""

from flask import Blueprint, jsonify, request

import pipewire
import dmic_meter
import nau8822_meter
import audio_targets

audio_bp = Blueprint("audio", __name__)


# -- New Unified API --

@audio_bp.get("/api/audio/state")
def audio_state():
    """Complete audio state — polled every 2s by the user page."""
    return jsonify(pipewire.audio_state())


@audio_bp.post("/api/audio/set_default")
def set_default():
    data = request.get_json(silent=True) or {}
    sink_id = str(data.get("id", "")).strip()
    if not sink_id:
        return jsonify({"ok": False, "error": "missing id"}), 400
    ok, msg = pipewire.set_default_sink(sink_id)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


@audio_bp.post("/api/audio/set_volume")
def set_volume():
    data = request.get_json(silent=True) or {}
    sink_id = str(data.get("id", "")).strip()
    try:
        pct = int(data.get("percent", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid percent"}), 400
    if not sink_id:
        return jsonify({"ok": False, "error": "missing id"}), 400

    # Check if this is the ADAU1860 hardware DAC
    if sink_id == "adau1860_lineout":
        try:
            import adau1860
            ok, msg = adau1860.set_dac_volume_percent(pct)
            if ok:
                pipewire.save_volume_state(pct, None)
            return jsonify({"ok": ok, "message": msg}), 200 if ok else 500
        except ImportError:
            return jsonify({"ok": False, "error": "adau1860 module not available"}), 500

    ok, msg = pipewire.set_sink_volume(sink_id, pct)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


@audio_bp.post("/api/audio/set_mute")
def set_mute():
    data = request.get_json(silent=True) or {}
    sink_id = str(data.get("id", "")).strip()
    mute = bool(data.get("mute", False))
    if not sink_id:
        return jsonify({"ok": False, "error": "missing id"}), 400

    # ADAU1860 hardware mute
    if sink_id == "adau1860_lineout":
        try:
            import adau1860
            ok, msg = adau1860.set_dac_mute(mute)
            return jsonify({"ok": ok, "message": msg}), 200 if ok else 500
        except (ImportError, AttributeError):
            return jsonify({"ok": False, "error": "adau1860 mute not available"}), 500

    ok, msg = pipewire.set_sink_mute(sink_id, mute)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


@audio_bp.post("/api/audio/play_chime")
def play_chime():
    """Play a short chime on a specific sink to confirm audio output."""
    data = request.get_json(silent=True) or {}
    sink_id = str(data.get("id", "")).strip()
    if not sink_id:
        return jsonify({"ok": False, "error": "missing id"}), 400
    ok, msg = pipewire.play_chime(sink_id)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


# -- Mic Input Controls --

@audio_bp.post("/api/audio/mic_control")
def mic_control():
    """Set NAU8822 mic control."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or data.get("control") or "").strip()
    value = data.get("value")
    if not name:
        return jsonify({"ok": False, "error": "missing name"}), 400

    # Map friendly names to ALSA control names
    control_map = {"pga_gain": "PGA Volume", "boost": "PGA Boost", "boost_20db": "PGA Boost"}
    alsa_name = control_map.get(name, name)

    if name in ("boost", "boost_20db"):
        value = 1 if value else 0

    try:
        value = int(value)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid value"}), 400

    ok, msg = pipewire.set_mic_control(alsa_name, value)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


# -- DMIC Metering --

@audio_bp.post("/api/audio/dmic/start_metering")
def dmic_start():
    return jsonify(dmic_meter.start_metering())


@audio_bp.post("/api/audio/dmic/stop_metering")
def dmic_stop():
    return jsonify(dmic_meter.stop_metering())


@audio_bp.get("/api/audio/dmic/levels")
def dmic_levels():
    return jsonify(dmic_meter.get_levels())


@audio_bp.post("/api/audio/dmic/test")
def dmic_test():
    return jsonify(dmic_meter.start_loopback())


@audio_bp.get("/api/audio/dmic/test/status")
def dmic_test_status():
    return jsonify(dmic_meter.get_loopback_status())


# -- NAU8822 Mic Metering --

@audio_bp.post("/api/audio/nau8822/start_metering")
def nau_start():
    return jsonify(nau8822_meter.start_metering())


@audio_bp.post("/api/audio/nau8822/stop_metering")
def nau_stop():
    return jsonify(nau8822_meter.stop_metering())


@audio_bp.get("/api/audio/nau8822/levels")
def nau_levels():
    return jsonify(nau8822_meter.get_levels())


@audio_bp.post("/api/audio/nau8822/test")
def nau_test():
    return jsonify(nau8822_meter.start_loopback())


@audio_bp.get("/api/audio/nau8822/test/status")
def nau_test_status():
    return jsonify(nau8822_meter.get_loopback_status())


# -- Generic Input Metering / Loopback (registry-driven) --
#
# Works for any target registered in audio_targets.TARGETS, so new audio
# inputs need no new routes — just a registry entry.

@audio_bp.post("/api/audio/input/<key>/start_metering")
def input_start_metering(key):
    return jsonify(audio_targets.start_metering(key))


@audio_bp.post("/api/audio/input/<key>/stop_metering")
def input_stop_metering(key):
    return jsonify(audio_targets.stop_metering(key))


@audio_bp.get("/api/audio/input/<key>/levels")
def input_levels(key):
    return jsonify(audio_targets.get_levels(key))


@audio_bp.post("/api/audio/input/<key>/test")
def input_test(key):
    data = request.get_json(silent=True) or {}
    pair = data.get("pair")
    if pair is not None:
        try:
            pair = [int(pair[0]), int(pair[1])]
        except (TypeError, ValueError, IndexError):
            return jsonify({"ok": False, "error": "invalid pair"}), 400
    return jsonify(audio_targets.start_loopback(key, pair))


@audio_bp.get("/api/audio/input/<key>/test/status")
def input_test_status(key):
    return jsonify(audio_targets.get_loopback_status(key))


# -- Legacy Endpoints (backward compat) --

@audio_bp.get("/api/audio/snapshot")
def snapshot():
    """Legacy: returns old-format audio snapshot."""
    state = pipewire.audio_state()
    # Convert to old format for existing frontend
    sinks = []
    for s in state.get("sinks", []):
        sinks.append({
            "name": s.get("id", ""),
            "description": s.get("name", ""),
            "mute": s.get("muted", False),
            "volume_percent": s.get("volume_percent", 0),
            "state": "RUNNING" if s.get("available") else "unavailable",
            "available": s.get("available", False),
            "bus_path": s.get("bus_path", ""),
            "card_name": s.get("card_name", ""),
        })
    return jsonify({"sinks": sinks, "default_sink": pipewire.get_default_sink()})


@audio_bp.post("/api/audio/default_sink")
def default_sink_legacy():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"ok": False, "error": "missing name"}), 400
    ok, msg = pipewire.set_default_sink(name)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


@audio_bp.post("/api/audio/sink_volume")
def sink_volume_legacy():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    try:
        pct = int(data.get("percent", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid percent"}), 400
    if not name:
        return jsonify({"ok": False, "error": "missing name"}), 400
    ok, msg = pipewire.set_sink_volume(name, pct)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


@audio_bp.post("/api/audio/sink_mute")
def sink_mute_legacy():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    mute = bool(data.get("mute", False))
    if not name:
        return jsonify({"ok": False, "error": "missing name"}), 400
    ok, msg = pipewire.set_sink_mute(name, mute)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


# -- Legacy Mic Controls --

@audio_bp.get("/api/audio/mic_controls")
def mic_controls_legacy():
    controls = pipewire.get_all_mic_controls()
    return jsonify({"ok": True, "controls": controls})


# -- ACPI SSDT Loading (for audio codec devices) --

import acpi_ssdt


@audio_bp.get("/api/audio/ssdt/status")
def ssdt_status():
    """Get status of SSDT loading capability and available tables."""
    return jsonify(acpi_ssdt.get_ssdt_status())


@audio_bp.post("/api/audio/ssdt/load")
def ssdt_load():
    """Load an SSDT via ACPI ConfigFS.
    
    Request body: {"name": "adau1860-ssdt"}
    """
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"ok": False, "error": "missing name"}), 400
    ok, msg = acpi_ssdt.load_ssdt(name)
    return (jsonify({"ok": ok, "message": msg}), 200 if ok else 500)


@audio_bp.post("/api/audio/ssdt/unload")
def ssdt_unload():
    """Unload an SSDT.
    
    Request body: {"name": "adau1860-ssdt"}
    """
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"ok": False, "error": "missing name"}), 400
    ok, msg = acpi_ssdt.unload_ssdt(name)
    return (jsonify({"ok": ok, "message": msg}), 200 if ok else 500)


@audio_bp.post("/api/audio/adau1860/enable")
def adau1860_enable():
    """Enable or disable ADAU1860 for next boot.
    
    Request body: {"enabled": true/false}
    """
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", False))
    ok, msg = acpi_ssdt.set_adau1860_enabled(enabled)
    return (jsonify({"ok": ok, "message": msg, "enabled": enabled}), 200 if ok else 500)


@audio_bp.get("/api/audio/adau1860/status")
def adau1860_status():
    """Get ADAU1860 enable status."""
    enabled = acpi_ssdt.is_adau1860_enabled()
    # Also check if currently loaded
    ssdts = acpi_ssdt.list_available_ssdts()
    loaded = any((s.table_name == "adau1860" or s.name in ("adau1860", "adau1860-ssdt")) and s.loaded for s in ssdts)
    return jsonify({
        "enabled": enabled,
        "loaded": loaded,
        "message": "ADAU1860 is " + ("enabled" if enabled else "disabled") + 
                   " and " + ("loaded" if loaded else "not loaded")
    })


