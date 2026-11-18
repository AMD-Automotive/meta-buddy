# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Localisation API -- keyboard layout and system language.

    GET  /api/localization            -- current settings, catalogue, locales
    GET  /api/localization/layouts    -- full layout list (lazy, for "show all")
    POST /api/localization/keyboard   -- {"layout","variant","model","options"}
    POST /api/localization/language   -- {"language": "de_DE.UTF-8"}
    POST /api/localization/confirm    -- keep the pending change
    POST /api/localization/revert     -- restore the previous settings now

Restart behaviour is the delicate part. Measured on hardware: weston.service is
Restart=on-failure with StartLimitBurst=5 over 10s, so six applies in ten
seconds leaves the compositor in start-limit-hit with the kiosk dead until
someone clears it over SSH. Hence the no-op short circuit, the cooldown, and
reset-failed before every restart.

Only a keyboard change needs the compositor restarted. A language change is
picked up by killing Chromium, which the launcher's watch loop relaunches in
about a second.
"""

import subprocess
import time

from flask import Blueprint, jsonify, request

import apply_localization
import localization as loc
import support_log

localization_bp = Blueprint("localization", __name__)

# Long enough to cover a restart (~6s measured) plus the user noticing, short
# enough not to feel broken. Well inside StartLimitBurst either way.
COOLDOWN_SECONDS = 30

# The revert window after a keyboard change. 60s rather than the 15s a display
# dialog uses: the restart alone costs ~6s and the user has to find a text
# field and type something before they can judge it.
REVERT_SECONDS = 60

_last_apply = 0.0


def _error(message, status=400):
    return jsonify({"ok": False, "error": message}), status


def _run(cmd, timeout=15):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False)
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, repr(exc)


def _restart_display(delay=2):
    """Restart Weston after the HTTP response has been delivered.

    reset-failed first: if a previous bad config or a burst of applies left the
    unit in start-limit-hit, a plain restart is refused and the kiosk stays
    dark. No fixed --unit name, so two applies cannot collide on it.
    """
    _run(["systemctl", "reset-failed", "weston"])
    return _run(["systemd-run", "--collect", f"--on-active={delay}",
                 "systemctl", "restart", "weston"])


def _schedule_revert():
    """Auto-restore the previous config unless the user confirms in time."""
    _run(["systemctl", "stop", "rave-localization-revert.timer"])
    return _run(["systemd-run", "--collect",
                 "--unit=rave-localization-revert",
                 f"--on-active={REVERT_SECONDS}",
                 "/usr/bin/python3", "/opt/rave-menu/revert_localization.py"])


def _cancel_revert():
    _run(["systemctl", "stop", "rave-localization-revert.timer"])
    _run(["systemctl", "reset-failed", "rave-localization-revert.service"])


def _restart_browser():
    """Language-only change: cheaper than taking the compositor down."""
    return _run(["pkill", "-f", "chromium-bin"])


def _state(extra=None):
    cfg = loc.load_config()
    payload = {
        "ok": True,
        "current": cfg,
        "shortlist": loc.shortlist_layouts(),
        "locales": loc.available_locales(),
        "pending_revert": loc.PREV_FILE.exists(),
    }
    if extra:
        payload.update(extra)
    return payload


@localization_bp.get("/api/localization")
def get_localization():
    try:
        return jsonify(_state())
    except loc.LocalizationError as exc:
        return _error(str(exc), 500)


@localization_bp.get("/api/localization/layouts")
def get_layouts():
    """Full catalogue, fetched only when the user opens "show all".

    Kept off the main payload because it is ~100 layouts with several hundred
    variants, which is a pointless download on every page load.
    """
    try:
        return jsonify({"ok": True, "layouts": loc.load_catalog()["layouts"]})
    except loc.LocalizationError as exc:
        return _error(str(exc), 500)


def _apply(candidate, restart_display):
    """Validate, persist and apply. Returns a Flask response tuple."""
    global _last_apply

    try:
        checked = loc.validate(candidate)
    except loc.LocalizationError as exc:
        return _error(str(exc))

    current = loc.load_config()
    if checked == current:
        # Also the DoS short circuit: a replayed POST costs nothing.
        return jsonify(_state({"changed": False,
                               "message": "already applied"}))

    remaining = COOLDOWN_SECONDS - (time.time() - _last_apply)
    if remaining > 0:
        response = _error("a localisation change was just applied; "
                          f"try again in {int(remaining) + 1}s", 429)
        response[0].headers["Retry-After"] = str(int(remaining) + 1)
        return response

    # Proves libxkbcommon can build it. Without this a bogus layout takes the
    # display down instead of returning an error.
    try:
        loc.preflight_keymap(checked)
    except loc.LocalizationError as exc:
        return _error(str(exc))

    loc.save_config(checked)
    support_log.record("localisation.apply", checked, "", 0)
    apply_localization.apply_all(checked, from_api=True)
    _last_apply = time.time()

    if restart_display:
        ok, detail = _restart_display()
        _schedule_revert()
        return jsonify(_state({
            "changed": True,
            "restarting": ok,
            "revert_seconds": REVERT_SECONDS,
            "message": ("Display restarting. Confirm the new layout works, "
                        f"or it reverts in {REVERT_SECONDS}s.")
                       if ok else f"could not restart display: {detail}",
        }))

    _restart_browser()
    return jsonify(_state({"changed": True, "restarting": False,
                           "message": "Language applied."}))


@localization_bp.post("/api/localization/keyboard")
def set_keyboard():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _error("expected a JSON object")

    current = loc.load_config()
    candidate = {
        "version": loc.SCHEMA_VERSION,
        "keyboard": {
            "model": data.get("model") or current["keyboard"]["model"],
            "layout": data.get("layout", ""),
            "variant": data.get("variant", "") or "",
            "options": data.get("options", "") or "",
        },
        "language": current["language"],
    }
    return _apply(candidate, restart_display=True)


@localization_bp.post("/api/localization/language")
def set_language():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _error("expected a JSON object")

    current = loc.load_config()
    candidate = {
        "version": loc.SCHEMA_VERSION,
        "keyboard": dict(current["keyboard"]),
        "language": data.get("language", ""),
    }
    return _apply(candidate, restart_display=False)


@localization_bp.post("/api/localization/confirm")
def confirm():
    _cancel_revert()
    return jsonify(_state({"confirmed": True,
                           "message": "Settings kept."}))


@localization_bp.post("/api/localization/revert")
def revert():
    previous = loc.load_previous()
    if previous is None:
        return _error("no previous settings to restore", 404)

    _cancel_revert()
    loc.save_config(previous)
    support_log.record("localisation.revert", previous, "", 0)
    apply_localization.apply_all(previous, from_api=True)
    _restart_display()
    return jsonify(_state({"reverted": True,
                           "message": "Previous settings restored."}))
