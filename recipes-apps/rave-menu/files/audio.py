# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Audio helpers -- PulseAudio (pactl) wrappers.

PulseAudio runs in the per-user `weston` session that hosts the kiosk
Chromium browser, so the rave-menu service (running as root) must impersonate
that user when invoking pactl.

Default sink selection is persisted by writing a drop-in to
/etc/pulse/default.pa.d/ so it survives reboot.
"""

import json
import os
import pwd
import re
import subprocess
from pathlib import Path
from typing import Optional

# -- Constants --

PULSE_USER_DEFAULT = "weston"
PULSE_DROPIN_DIR = Path("/etc/pulse/default.pa.d")
PULSE_DROPIN_FILE = PULSE_DROPIN_DIR / "99-rave-default-sink.pa"

PACTL = os.environ.get("PACTL", "/usr/bin/pactl")
SUDO = os.environ.get("SUDO", "/usr/bin/sudo")


# -- Utilities --

def _safe_name(name: str) -> bool:
    """Return True if name matches the conservative pactl/sink character set."""
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-+=/]+", name or ""))


def _run(cmd, timeout=10, input_text=None) -> subprocess.CompletedProcess:
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
        return subprocess.CompletedProcess(cmd, 124, exc.stdout or "", exc.stderr or "timeout")
    except Exception as exc:  # pragma: no cover
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def _pulse_user() -> Optional[tuple]:
    """Return (username, uid) of the user running PulseAudio, or None."""
    name = os.environ.get("RAVE_PULSE_USER", PULSE_USER_DEFAULT)
    try:
        pw = pwd.getpwnam(name)
        return name, pw.pw_uid
    except KeyError:
        return None


def pa_run(args, timeout=10) -> subprocess.CompletedProcess:
    """Run a pactl invocation as the PulseAudio session user."""
    info = _pulse_user()
    if not info:
        return subprocess.CompletedProcess(args, 1, "", "pulseaudio user not found")
    user, uid = info
    cmd = [
        SUDO,
        "-n",
        "-u", user,
        "env",
        f"XDG_RUNTIME_DIR=/run/user/{uid}",
        f"PULSE_RUNTIME_PATH=/run/user/{uid}/pulse",
        PACTL,
    ] + list(args)
    return _run(cmd, timeout=timeout)


# -- PulseAudio --

def list_sinks() -> list:
    """Return a list of dicts describing each PulseAudio sink."""
    res = pa_run(["-f", "json", "list", "sinks"], timeout=8)
    if res.returncode != 0:
        return []
    try:
        raw = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return []

    sinks = []
    for s in raw:
        vols = s.get("volume", {}) or {}
        # Average percent across channels
        percents = []
        for ch in vols.values():
            txt = (ch or {}).get("value_percent", "")
            m = re.match(r"(\d+)%", txt or "")
            if m:
                percents.append(int(m.group(1)))
        avg = int(sum(percents) / len(percents)) if percents else 0

        props = s.get("properties", {}) or {}
        sinks.append({
            "name": s.get("name", ""),
            "description": s.get("description", "") or s.get("name", ""),
            "mute": bool(s.get("mute", False)),
            "volume_percent": avg,
            "state": s.get("state", ""),
            "available": True,
            "bus_path": props.get("device.bus_path", ""),
            "card_name": props.get("alsa.card_name", ""),
        })
    return sinks


def _card_bus_path(card: dict) -> str:
    """Return the bus path for a pactl card record, falling back to its name."""
    props = card.get("properties", {}) or {}
    bp = props.get("device.bus_path")
    if bp:
        return bp
    return card.get("name", "")


def list_inactive_port_sinks(active_sinks: list) -> list:
    """Return placeholder entries for card ports that are not currently
    bound to a PulseAudio sink (no display attached, unbound jack, etc.).

    Each entry mirrors the shape returned by list_sinks but is flagged
    available=False so the UI can render it greyed out.
    """
    res = pa_run(["-f", "json", "list", "cards"], timeout=8)
    if res.returncode != 0:
        return []
    try:
        cards = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return []

    active_names = {s.get("name", "") for s in active_sinks}
    placeholders = []

    for card in cards:
        card_props = card.get("properties", {}) or {}
        card_bus = _card_bus_path(card)
        card_friendly = (
            card_props.get("device.description")
            or card_props.get("alsa.card_name")
            or card.get("name", "")
        )
        ports = card.get("ports", {}) or {}
        # pactl JSON keys ports by name; preserve that as the port name.
        if isinstance(ports, dict):
            port_iter = list(ports.items())
        else:
            port_iter = [((pdata or {}).get("name", ""), pdata) for pdata in ports]
        for pname, p in port_iter:
            avail = (p.get("availability") or "").lower()
            # Only flag ports the driver explicitly reports as disconnected
            # (e.g. HDMI with no display attached). "available" and
            # "availability unknown" are treated as potentially usable.
            if avail != "not available":
                continue

            port_desc = p.get("description") or pname or "Output"
            placeholder_name = f"placeholder.{card.get('name','card')}.{pname or port_desc}"
            if placeholder_name in active_names:
                continue
            placeholders.append({
                "name": placeholder_name,
                "description": f"{card_friendly} \u2014 {port_desc} (not connected)",
                "mute": True,
                "volume_percent": 0,
                "state": "unavailable",
                "available": False,
                "bus_path": card_bus,
                "card_name": card_props.get("alsa.card_name", ""),
            })
    return placeholders


def get_default_sink() -> str:
    res = pa_run(["get-default-sink"], timeout=5)
    if res.returncode != 0:
        return ""
    return (res.stdout or "").strip()


def set_default_sink(name: str, persist: bool = True) -> tuple:
    """Set the default sink and optionally persist via /etc/pulse/default.pa.d."""
    if not _safe_name(name):
        return False, "invalid sink name"

    res = pa_run(["set-default-sink", name], timeout=8)
    if res.returncode != 0:
        return False, (res.stderr or res.stdout or "set-default-sink failed").strip()

    # Move any existing sink-inputs (playing streams) onto the new default
    inputs = pa_run(["list", "short", "sink-inputs"], timeout=5)
    if inputs.returncode == 0:
        for line in (inputs.stdout or "").splitlines():
            parts = line.split()
            if not parts:
                continue
            sid = parts[0]
            if sid.isdigit():
                pa_run(["move-sink-input", sid, name], timeout=5)

    if persist:
        try:
            PULSE_DROPIN_DIR.mkdir(parents=True, exist_ok=True)
            PULSE_DROPIN_FILE.write_text(
                "# Managed by rave-menu Setup Audio. Do not edit by hand.\n"
                f"set-default-sink {name}\n"
            )
            os.chmod(PULSE_DROPIN_FILE, 0o644)
        except OSError as exc:
            return True, f"applied (warning: persist failed: {exc})"

    return True, "ok"


def set_sink_volume(name: str, percent: int) -> tuple:
    if not _safe_name(name):
        return False, "invalid sink name"
    pct = max(0, min(150, int(percent)))
    res = pa_run(["set-sink-volume", name, f"{pct}%"], timeout=5)
    if res.returncode != 0:
        return False, (res.stderr or res.stdout or "set-sink-volume failed").strip()
    return True, "ok"


def set_sink_mute(name: str, mute: bool) -> tuple:
    if not _safe_name(name):
        return False, "invalid sink name"
    res = pa_run(["set-sink-mute", name, "1" if mute else "0"], timeout=5)
    if res.returncode != 0:
        return False, (res.stderr or res.stdout or "set-sink-mute failed").strip()
    return True, "ok"


def audio_snapshot() -> dict:
    """Return a complete snapshot of audio state for the UI.

    Sinks are ordered so that disconnected port placeholders appear next
    to the active sink for the same physical card (matching bus_path).
    Cards with no active sink keep their placeholders grouped at the end.
    """
    sinks = list_sinks()
    placeholders = list_inactive_port_sinks(sinks)

    order = []
    seen = set()
    for s in sinks:
        bp = s.get("bus_path") or ""
        if bp not in seen:
            seen.add(bp)
            order.append(bp)
    for p in placeholders:
        bp = p.get("bus_path") or ""
        if bp not in seen:
            seen.add(bp)
            order.append(bp)

    grouped: list = []
    for bp in order:
        for s in sinks:
            if (s.get("bus_path") or "") == bp:
                grouped.append(s)
        for p in placeholders:
            if (p.get("bus_path") or "") == bp:
                grouped.append(p)

    return {
        "sinks": grouped,
        "default_sink": get_default_sink(),
    }


# -- ALSA Mixer Controls for Mic Input --

AMIXER = os.environ.get("AMIXER", "/usr/bin/amixer")

# Controls we expose for mic calibration (card 1 = ACP70)
# Note: ADAU1860 DMIC controls are accessed via debugfs registers in adau1860.py,
# not through ALSA mixer. Only NAU8822 analog controls are available here.
MIC_CONTROLS = [
    # NAU8822 analog mic path (card 1)
    {"name": "PGA", "label": "PGA Gain", "type": "volume", "min": 0, "max": 63},
    {"name": "PGA Boost", "label": "PGA Boost (+20dB)", "type": "switch", "min": 0, "max": 1},
]


def _amixer_run(args, timeout=5) -> subprocess.CompletedProcess:
    """Run amixer command."""
    cmd = [AMIXER, "-c", "1"] + list(args)
    return _run(cmd, timeout=timeout)


def get_mic_control(name: str) -> Optional[dict]:
    """Get current value of an ALSA mixer control."""
    res = _amixer_run(["sget", name])
    if res.returncode != 0:
        return None

    output = res.stdout or ""
    # Parse value from output like "Front Left: 48 [76%] [24.00dB]"
    # or "Mono: Capture 255 [100%] [24.00dB] [on]"
    value = None
    percent = None
    db = None
    enabled = None

    for line in output.splitlines():
        # Look for value patterns
        m = re.search(r':\s*(?:Capture\s+)?(\d+)\s*\[(\d+)%\](?:\s*\[([^\]]+dB)\])?(?:\s*\[(on|off)\])?', line)
        if m:
            value = int(m.group(1))
            percent = int(m.group(2))
            db = m.group(3)
            enabled = m.group(4) == "on" if m.group(4) else None
            break

    if value is None:
        return None

    return {
        "name": name,
        "value": value,
        "percent": percent,
        "db": db,
        "enabled": enabled,
    }


def set_mic_control(name: str, value: int) -> tuple:
    """Set ALSA mixer control value."""
    # Validate control name
    valid_names = {c["name"] for c in MIC_CONTROLS}
    if name not in valid_names:
        return False, f"unknown control: {name}"

    res = _amixer_run(["sset", name, str(value)])
    if res.returncode != 0:
        return False, (res.stderr or res.stdout or "amixer failed").strip()
    return True, "ok"


def get_all_mic_controls() -> list:
    """Get current values for all mic controls."""
    controls = []
    for ctrl in MIC_CONTROLS:
        data = get_mic_control(ctrl["name"])
        if data:
            data.update({
                "label": ctrl["label"],
                "type": ctrl["type"],
                "min": ctrl["min"],
                "max": ctrl["max"],
            })
            controls.append(data)
    return controls
