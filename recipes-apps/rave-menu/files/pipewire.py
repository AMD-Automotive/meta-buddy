# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""PipeWire/PulseAudio audio backend abstraction.

Auto-detects whether PipeWire (pw-dump/wpctl) or PulseAudio (pactl) is
available and uses the appropriate commands. This allows deploying the
same code on systems being migrated from PA → PipeWire.

The audio session runs under the `weston` user, so all commands are
impersonated via sudo.
"""

import json
import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

# -- Configuration --

AUDIO_USER = os.environ.get("RAVE_AUDIO_USER", "weston")
SUDO = os.environ.get("SUDO", "/usr/bin/sudo")

# Persistence for default sink (PulseAudio fallback)
PULSE_DROPIN_DIR = Path("/etc/pulse/default.pa.d")
PULSE_DROPIN_FILE = PULSE_DROPIN_DIR / "99-rave-default-sink.pa"

# Volume persistence for hardware DAC (ADAU1860)
VOLUME_STATE_DIR = Path("/var/lib/rave-menu")
VOLUME_STATE_FILE = VOLUME_STATE_DIR / "adau1860_volume.json"


# -- Backend Detection --

def _has_command(name: str) -> bool:
    return shutil.which(name) is not None


def _detect_backend() -> str:
    """Detect whether PipeWire or PulseAudio is the active audio server."""
    # Check if wpctl is available (PipeWire's control tool)
    if _has_command("wpctl"):
        # Verify PipeWire is actually running for our audio user
        res = _run_as_user(["wpctl", "status"], timeout=3)
        if res.returncode == 0 and "Audio" in (res.stdout or ""):
            return "pipewire"
    # Fall back to PulseAudio
    if _has_command("pactl"):
        return "pulseaudio"
    return "none"


# Cache backend detection
_backend = None


def get_backend() -> str:
    global _backend
    if _backend is None:
        _backend = _detect_backend()
    return _backend


def reset_backend_cache():
    """Force re-detection (useful after installing PipeWire)."""
    global _backend
    _backend = None


# -- Utilities --

def _safe_name(name: str) -> bool:
    """Return True if name matches safe sink name characters."""
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-+=/]+", name or ""))


def _get_user_info() -> Optional[tuple]:
    """Return (username, uid) for the audio session user, or None."""
    name = AUDIO_USER
    try:
        pw = pwd.getpwnam(name)
        return name, pw.pw_uid
    except KeyError:
        return None


def _run(cmd, timeout=10, env=None) -> subprocess.CompletedProcess:
    """Run a command with error handling."""
    run_env = env or os.environ.copy()
    run_env.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
    try:
        return subprocess.run(
            cmd, text=True, capture_output=True, timeout=timeout, env=run_env,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"command not found: {cmd[0]}")
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, exc.stdout or "", "timeout")
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def _run_as_user(args, timeout=10) -> subprocess.CompletedProcess:
    """Run a command as the audio session user."""
    info = _get_user_info()
    if not info:
        return subprocess.CompletedProcess(args, 1, "", "audio user not found")
    user, uid = info
    cmd = [
        SUDO, "-n", "-u", user, "env",
        f"XDG_RUNTIME_DIR=/run/user/{uid}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
        f"PULSE_RUNTIME_PATH=/run/user/{uid}/pulse",
    ] + list(args)
    return _run(cmd, timeout=timeout)


# -- PipeWire Backend --

def _codec_enabled() -> bool:
    """Return True when the ADAU1860 codec is physically present.

    The adau1860 SSDT injects the PMOD1860 ACPI device at boot. When the codec
    plugin is disabled the SSDT is absent, PMOD1860 never appears, and the
    I2S_BT back-end (hw:1,1) is just the opaque off-SoC "Versal I2S" bridge.
    """
    import glob
    return bool(glob.glob("/sys/bus/acpi/devices/PMOD1860:*"))


# The I2S_BT back-end (card 1, PCM dev 1 = hw:1,1) is published by two PipeWire
# sinks: the custom 'acp_versal_i2s' adapter (which runs the ADAU1860 BCLK
# relock sequence and actually drives the link) and a UCM-managed duplicate
# whose node name ends in this suffix. They are the same physical output, so
# the duplicate is hidden and the adapter is relabelled after what is really
# downstream (see _collapse_i2s_bt_sink).
_UCM_ADAU_SINK_SUFFIX = "hw_acp70nau8822_1__sink"
_I2S_BT_ADAPTER_NAME = "acp_versal_i2s"


def _default_sink_name() -> str:
    """Return the node.name of the current default sink, or "".

    Reads pw-metadata's `default.audio.sink`, whose value is a JSON object
    carrying the sink's node.name. node.name is what every sink is keyed by
    (the menu sink "id"), so it maps unambiguously back to a listed sink.
    """
    res = _run_as_user(["pw-metadata"], timeout=5)
    if res.returncode != 0:
        return ""
    for line in (res.stdout or "").splitlines():
        if "default.audio.sink" not in line or "configured" in line:
            continue
        m = re.search(r"value:'(\{.*?\})'", line)
        if not m:
            continue
        try:
            return json.loads(m.group(1)).get("name", "") or ""
        except json.JSONDecodeError:
            return ""
    return ""


def _collapse_i2s_bt_sink(sinks: list) -> list:
    """Expose the I2S_BT back-end as a single, clearly labelled sink.

    Keeps the 'acp_versal_i2s' adapter, drops the UCM duplicate, and names the
    survivor by codec state:
      codec enabled  (PMOD1860 present) -> "ADAU1860 DAC"
      codec disabled (dummy bridge)     -> "Versal I2S"
    """
    codec_enabled = _codec_enabled()
    result = []
    for s in sinks:
        node_name = s.get("id", "")
        if node_name.endswith(_UCM_ADAU_SINK_SUFFIX):
            # Duplicate of the adapter sink on hw:1,1 -> hide it.
            continue
        if node_name == _I2S_BT_ADAPTER_NAME:
            s["name"] = "ADAU1860 DAC" if codec_enabled else "Versal I2S"
        result.append(s)
    return result


def _pw_list_sinks() -> list:
    """List sinks via pw-dump (JSON native)."""
    res = _run_as_user(["pw-dump"], timeout=8)
    if res.returncode != 0:
        return []
    try:
        nodes = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return []

    sinks = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        info = node.get("info", {}) or {}
        props = info.get("props", {}) or {}
        media_class = props.get("media.class", "")
        if media_class != "Audio/Sink":
            continue

        # Get volume from params if available
        params = info.get("params", {}) or {}
        volume_percent = 100
        muted = False
        for prop_info in params.get("Props", []):
            if isinstance(prop_info, dict):
                # channelVolumes holds actual per-channel linear volumes
                ch_vols = prop_info.get("channelVolumes")
                if ch_vols and isinstance(ch_vols, list) and len(ch_vols) > 0:
                    linear = float(ch_vols[0])
                    # PipeWire uses cubic scale: percent = linear^(1/3)
                    volume_percent = int(round((linear ** (1.0/3.0)) * 100)) if linear > 0 else 0
                m = prop_info.get("mute")
                if m is not None:
                    muted = bool(m)

        node_name = props.get("node.name", "")
        sinks.append({
            "id": node_name,
            "name": props.get("node.description", "") or props.get("node.nick", "") or node_name,
            "available": True,
            "is_default": False,  # filled in later
            "volume_percent": volume_percent,
            "muted": muted,
            "type": "pipewire",
            "node_id": node.get("id"),
            "card_name": props.get("alsa.card_name", ""),
        })

    # Mark the default sink. Read it from pw-metadata's default.audio.sink,
    # which carries the sink's node.name (== our sink "id"). The older approach
    # parsed `wpctl status`, whose printed default index does not map back to
    # the node id reliably (it is the object.serial), so the menu could flag a
    # different sink as default than the one pw-metadata actually points at.
    default_name = _default_sink_name()
    if default_name:
        # The UCM duplicate of the I2S_BT back-end is collapsed onto the
        # adapter sink below, so fold a default that lands on the duplicate
        # onto the surviving adapter node so the flag is not lost.
        if default_name.endswith(_UCM_ADAU_SINK_SUFFIX):
            default_name = _I2S_BT_ADAPTER_NAME
        for s in sinks:
            if s.get("id") == default_name:
                s["is_default"] = True
                break

    return _collapse_i2s_bt_sink(sinks)


def _pw_set_default(sink_id: str) -> tuple:
    """Set default sink via wpctl."""
    # Find node_id for this sink name
    sinks = _pw_list_sinks()
    target = None
    for s in sinks:
        if s["id"] == sink_id:
            target = s
            break
    if not target:
        return False, f"sink not found: {sink_id}"

    node_id = target.get("node_id")
    if node_id is None:
        return False, "no node_id for sink"

    res = _run_as_user(["wpctl", "set-default", str(node_id)], timeout=5)
    if res.returncode != 0:
        return False, (res.stderr or "wpctl set-default failed").strip()
    return True, "ok"


def _pw_set_volume(sink_id: str, percent: int) -> tuple:
    """Set sink volume via wpctl."""
    sinks = _pw_list_sinks()
    target = None
    for s in sinks:
        if s["id"] == sink_id:
            target = s
            break
    if not target:
        return False, f"sink not found: {sink_id}"

    node_id = target.get("node_id")
    if node_id is None:
        return False, "no node_id for sink"

    vol = max(0, min(150, percent)) / 100.0
    res = _run_as_user(["wpctl", "set-volume", str(node_id), f"{vol:.2f}"], timeout=5)
    if res.returncode != 0:
        return False, (res.stderr or "wpctl set-volume failed").strip()
    return True, "ok"


def _pw_set_mute(sink_id: str, mute: bool) -> tuple:
    """Set sink mute via wpctl."""
    sinks = _pw_list_sinks()
    target = None
    for s in sinks:
        if s["id"] == sink_id:
            target = s
            break
    if not target:
        return False, f"sink not found: {sink_id}"

    node_id = target.get("node_id")
    if node_id is None:
        return False, "no node_id for sink"

    res = _run_as_user(["wpctl", "set-mute", str(node_id), "1" if mute else "0"], timeout=5)
    if res.returncode != 0:
        return False, (res.stderr or "wpctl set-mute failed").strip()
    return True, "ok"


CHIME_PATH = "/opt/rave-menu/static/chime.wav"


def play_chime(sink_id: str):
    """Play a short chime on the specified sink for audio confirmation."""
    sinks = list_sinks()
    target = next((s for s in sinks if s.get("id") == sink_id), None)
    if not target:
        return False, "sink not found"
    # Target by node.name (a stable string), NOT the numeric node id. In
    # libpipewire 1.0.9 a numeric --target is matched against object.serial,
    # which only coincides with the node id for some nodes; for the others
    # (notably the NAU8822 UCM sink) it silently falls back to the default
    # sink, so the chime crosses to whatever output is currently live. The
    # node.name resolves unambiguously for every sink.
    node_name = target.get("id") or sink_id
    # pw-play with --target sends audio to a specific sink node
    res = _run_as_user(
        ["pw-play", "--target", str(node_name), CHIME_PATH],
        timeout=5,
    )
    if res.returncode != 0:
        return False, (res.stderr or "pw-play failed").strip()
    return True, "ok"


# -- PulseAudio Backend --

def _pa_run(args, timeout=10) -> subprocess.CompletedProcess:
    """Run pactl as the audio session user."""
    pactl = os.environ.get("PACTL", "/usr/bin/pactl")
    return _run_as_user([pactl] + list(args), timeout=timeout)


def _pa_list_sinks() -> list:
    """List sinks via pactl JSON."""
    res = _pa_run(["-f", "json", "list", "sinks"], timeout=8)
    if res.returncode != 0:
        return []
    try:
        raw = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return []

    default_name = _pa_get_default()
    sinks = []
    for s in raw:
        vols = s.get("volume", {}) or {}
        percents = []
        for ch in vols.values():
            txt = (ch or {}).get("value_percent", "")
            m = re.match(r"(\d+)%", txt or "")
            if m:
                percents.append(int(m.group(1)))
        avg = int(sum(percents) / len(percents)) if percents else 0

        props = s.get("properties", {}) or {}
        sink_name = s.get("name", "")
        sinks.append({
            "id": sink_name,
            "name": s.get("description", "") or sink_name,
            "available": True,
            "is_default": sink_name == default_name,
            "volume_percent": avg,
            "muted": bool(s.get("mute", False)),
            "type": "pipewire",  # present uniformly to frontend
            "card_name": props.get("alsa.card_name", ""),
            "bus_path": props.get("device.bus_path", ""),
        })
    return sinks


def _pa_list_inactive_ports(active_sinks: list) -> list:
    """Return placeholder entries for disconnected ports (e.g. HDMI unplugged)."""
    res = _pa_run(["-f", "json", "list", "cards"], timeout=8)
    if res.returncode != 0:
        return []
    try:
        cards = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return []

    active_names = {s.get("id", "") for s in active_sinks}
    placeholders = []

    for card in cards:
        card_props = card.get("properties", {}) or {}
        card_friendly = (
            card_props.get("device.description")
            or card_props.get("alsa.card_name")
            or card.get("name", "")
        )
        ports = card.get("ports", {}) or {}
        if isinstance(ports, dict):
            port_iter = list(ports.items())
        else:
            port_iter = [((p or {}).get("name", ""), p) for p in ports]

        for pname, p in port_iter:
            avail = (p.get("availability") or "").lower()
            if avail != "not available":
                continue
            port_desc = p.get("description") or pname or "Output"
            placeholder_id = f"placeholder.{card.get('name','card')}.{pname or port_desc}"
            if placeholder_id in active_names:
                continue
            placeholders.append({
                "id": placeholder_id,
                "name": f"{card_friendly} \u2014 {port_desc} (not connected)",
                "available": False,
                "unavailable_reason": "Not connected",
                "is_default": False,
                "volume_percent": 0,
                "muted": True,
                "type": "pipewire",
            })
    return placeholders


def _pa_get_default() -> str:
    res = _pa_run(["get-default-sink"], timeout=5)
    if res.returncode != 0:
        return ""
    return (res.stdout or "").strip()


def _pa_set_default(sink_id: str) -> tuple:
    """Set default sink via pactl + persist."""
    if not _safe_name(sink_id):
        return False, "invalid sink name"

    res = _pa_run(["set-default-sink", sink_id], timeout=8)
    if res.returncode != 0:
        return False, (res.stderr or "set-default-sink failed").strip()

    # Move existing streams
    inputs = _pa_run(["list", "short", "sink-inputs"], timeout=5)
    if inputs.returncode == 0:
        for line in (inputs.stdout or "").splitlines():
            parts = line.split()
            if parts and parts[0].isdigit():
                _pa_run(["move-sink-input", parts[0], sink_id], timeout=5)

    # Persist
    try:
        PULSE_DROPIN_DIR.mkdir(parents=True, exist_ok=True)
        PULSE_DROPIN_FILE.write_text(
            "# Managed by rave-menu. Do not edit.\n"
            f"set-default-sink {sink_id}\n"
        )
        os.chmod(PULSE_DROPIN_FILE, 0o644)
    except OSError:
        pass

    return True, "ok"


def _pa_set_volume(sink_id: str, percent: int) -> tuple:
    if not _safe_name(sink_id):
        return False, "invalid sink name"
    pct = max(0, min(150, percent))
    res = _pa_run(["set-sink-volume", sink_id, f"{pct}%"], timeout=5)
    if res.returncode != 0:
        return False, (res.stderr or "set-sink-volume failed").strip()
    return True, "ok"


def _pa_set_mute(sink_id: str, mute: bool) -> tuple:
    if not _safe_name(sink_id):
        return False, "invalid sink name"
    res = _pa_run(["set-sink-mute", sink_id, "1" if mute else "0"], timeout=5)
    if res.returncode != 0:
        return False, (res.stderr or "set-sink-mute failed").strip()
    return True, "ok"


# -- Public API (backend-agnostic) --

def list_sinks() -> list:
    """List all audio output sinks."""
    backend = get_backend()
    if backend == "pipewire":
        return _pw_list_sinks()
    elif backend == "pulseaudio":
        return _pa_list_sinks()
    return []


def list_inactive_sinks(active_sinks: list) -> list:
    """List placeholder entries for disconnected outputs."""
    backend = get_backend()
    if backend == "pulseaudio":
        return _pa_list_inactive_ports(active_sinks)
    # PipeWire handles this differently (sinks just disappear)
    return []


def get_default_sink() -> str:
    """Get the current default sink ID."""
    backend = get_backend()
    if backend == "pipewire":
        sinks = _pw_list_sinks()
        for s in sinks:
            if s.get("is_default"):
                return s["id"]
        return ""
    elif backend == "pulseaudio":
        return _pa_get_default()
    return ""


def set_default_sink(sink_id: str) -> tuple:
    """Set the default audio output sink."""
    backend = get_backend()
    if backend == "pipewire":
        return _pw_set_default(sink_id)
    elif backend == "pulseaudio":
        return _pa_set_default(sink_id)
    return False, "no audio backend"


def set_sink_volume(sink_id: str, percent: int) -> tuple:
    """Set volume for a sink (0-150%)."""
    backend = get_backend()
    if backend == "pipewire":
        return _pw_set_volume(sink_id, percent)
    elif backend == "pulseaudio":
        return _pa_set_volume(sink_id, percent)
    return False, "no audio backend"


def set_sink_mute(sink_id: str, mute: bool) -> tuple:
    """Set mute state for a sink."""
    backend = get_backend()
    if backend == "pipewire":
        return _pw_set_mute(sink_id, mute)
    elif backend == "pulseaudio":
        return _pa_set_mute(sink_id, mute)
    return False, "no audio backend"


def audio_state() -> dict:
    """Return unified audio state for the frontend.

    This is the single endpoint the UI polls every 2 seconds.
    """
    sinks = list_sinks()
    inactive = list_inactive_sinks(sinks)

    # Build ADAU1860 placeholder if debugfs exists but no PipeWire sink
    adau_debugfs = Path("/sys/kernel/debug/adau1860")
    adau_sink_present = any(
        "adau" in (s.get("id") or "").lower() or
        "adau" in (s.get("name") or "").lower() or
        "versal" in (s.get("id") or "").lower()
        for s in sinks
    )
    adau_placeholder = None

    if adau_debugfs.exists() and not adau_sink_present:
        adau_placeholder = {
            "id": "adau1860_lineout",
            "name": "ADAU1860 Line Out",
            "available": False,
            "unavailable_reason": "Initializing\u2026",
            "is_default": False,
            "volume_percent": 0,
            "muted": True,
            "type": "hardware_dac",
        }

    # Combine active sinks + ADAU1860 placeholder + inactive ports
    all_sinks = list(sinks)
    if adau_placeholder:
        all_sinks.append(adau_placeholder)
    all_sinks.extend(inactive)

    return {
        "backend": get_backend(),
        "sinks": all_sinks,
        "inputs": _get_inputs_state(),
    }


def _get_inputs_state() -> dict:
    """Build the inputs map generically from the audio_targets registry.

    Each registered capture target contributes presence + metering state
    (detected from its ALSA capture device). Targets with extra mixer
    controls (e.g. NAU8822 PGA gain) are augmented below.
    """
    try:
        import audio_targets
    except ImportError:
        return {}

    inputs = {}
    for key in audio_targets.list_keys():
        try:
            inputs[key] = audio_targets.input_state(key)
        except Exception:
            inputs[key] = {"available": False, "channels": 0,
                           "metering_active": False}

    # NAU8822 analog mic exposes PGA gain / boost via ALSA mixer controls.
    nau = inputs.get("nau8822_mic")
    if nau is not None:
        nau.update(_get_nau8822_controls())

    return inputs


# -- NAU8822 Mic Controls (ALSA mixer) --

AMIXER = os.environ.get("AMIXER", "/usr/bin/amixer")

MIC_CONTROLS = [
    {"name": "PGA Volume", "label": "PGA Gain", "type": "volume", "min": 0, "max": 63},
    {"name": "PGA Boost", "label": "PGA Boost (+20dB)", "type": "switch", "min": 0, "max": 1},
]


def _amixer_run(args, timeout=5) -> subprocess.CompletedProcess:
    cmd = [AMIXER, "-c", "1"] + list(args)
    return _run(cmd, timeout=timeout)


def get_mic_control(name: str) -> Optional[dict]:
    """Get current value of an ALSA mixer control."""
    res = _amixer_run(["cget", f"name={name}"])
    if res.returncode != 0:
        return None
    output = res.stdout or ""
    value = None
    for line in output.splitlines():
        if ": values=" in line:
            # Parse "  : values=16,16" or "  : values=on"
            vals_str = line.split("=", 1)[1].strip()
            first_val = vals_str.split(",")[0]
            if first_val in ("on", "off"):
                value = 1 if first_val == "on" else 0
            else:
                try:
                    value = int(first_val)
                except ValueError:
                    pass
            break
    if value is None:
        return None
    return {"name": name, "value": value}


def set_mic_control(name: str, value) -> tuple:
    """Set ALSA mixer control value."""
    valid_names = {c["name"] for c in MIC_CONTROLS}
    if name not in valid_names:
        return False, f"unknown control: {name}"
    # Use cset with name= for reliable control access
    res = _amixer_run(["cset", f"name={name}", str(value)])
    if res.returncode != 0:
        return False, (res.stderr or "amixer failed").strip()
    return True, "ok"


def get_all_mic_controls() -> list:
    """Get current values for all mic controls."""
    controls = []
    for ctrl in MIC_CONTROLS:
        data = get_mic_control(ctrl["name"])
        if data:
            data.update({"label": ctrl["label"], "type": ctrl["type"],
                         "min": ctrl["min"], "max": ctrl["max"]})
            controls.append(data)
    return controls


def _get_nau8822_controls() -> dict:
    """NAU8822 PGA gain / boost values from ALSA mixer controls."""
    pga_gain = 0
    boost = False
    for c in get_all_mic_controls():
        if c["name"] == "PGA Volume":
            pga_gain = c["value"]
        elif c["name"] == "PGA Boost":
            boost = c["value"] > 0
    return {
        "pga_gain": pga_gain,
        "pga_gain_max": 63,
        "boost_20db": boost,
    }


# -- Volume Persistence --

def save_volume_state(volume_percent: int, volume_db: float):
    """Persist ADAU1860 DAC volume to disk."""
    try:
        VOLUME_STATE_DIR.mkdir(parents=True, exist_ok=True)
        state = {"volume_percent": volume_percent, "volume_db": volume_db}
        VOLUME_STATE_FILE.write_text(json.dumps(state))
    except OSError:
        pass


def load_volume_state() -> Optional[dict]:
    """Load persisted ADAU1860 DAC volume."""
    try:
        if VOLUME_STATE_FILE.exists():
            return json.loads(VOLUME_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    return None
