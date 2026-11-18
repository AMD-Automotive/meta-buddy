#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Localisation HTTP API: validation, apply guards, confirm/revert.

Host-only. Every path is redirected into a temp directory and the system tools
the routes shell out to (systemctl, systemd-run, pkill, loadkeys, locale) are
replaced with stubs on PATH, so nothing is restarted and the calls can be
asserted.

The guards are the point of this file. Measured on hardware: weston.service is
Restart=on-failure with StartLimitBurst=5 over 10s, so six applies in ten
seconds leaves the compositor dead until someone clears it over SSH. The
no-op, cooldown and reset-failed behaviours below are what stop an
unauthenticated POST loop doing that.

Skipped when Flask is not importable, so it stays runnable on a bare host.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = os.path.join(os.path.dirname(HERE), "files")

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))


STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "{log}"\nexit 0\n'
LOCALE_STUB = '#!/bin/sh\nprintf "C\\nen_GB.utf8\\nde_DE.utf8\\nfr_FR.utf8\\n"\n'


def build_stubs(tmp):
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir, exist_ok=True)
    log = os.path.join(tmp, "calls.log")
    for name in ("systemctl", "systemd-run", "pkill", "loadkeys"):
        path = os.path.join(bindir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(STUB.format(log=log))
        os.chmod(path, 0o755)
    path = os.path.join(bindir, "locale")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(LOCALE_STUB)
    os.chmod(path, 0o755)
    os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
    return log


def main():
    try:
        from flask import Flask
    except ImportError:
        print("  SKIP  flask not installed on this host")
        return 0

    rules = "/usr/share/X11/xkb/rules/evdev.xml"
    if not os.path.exists(rules):
        print("  SKIP  no xkeyboard-config on this host")
        return 0

    tmp = tempfile.mkdtemp(prefix="localization-api-test-")
    calls = build_stubs(tmp)

    os.environ.update({
        "RAVE_XKB_RULES": rules,
        "RAVE_LOCALIZATION_CONFIG": os.path.join(tmp, "localization.json"),
        "RAVE_LOCALIZATION_PREV": os.path.join(tmp, "prev.json"),
        "RAVE_LOCALIZATION_DEFAULTS": os.path.join(FILES,
                                                   "localization.defaults.json"),
        "RAVE_WESTON_TEMPLATE": os.path.join(FILES, "weston.ini.in"),
        "RAVE_WESTON_FALLBACK": os.path.join(FILES, "weston.ini"),
        "RAVE_WESTON_RUNTIME": os.path.join(tmp, "run", "weston.ini"),
        "RAVE_LOCALIZATION_ENV": os.path.join(tmp, "rave-localization"),
        "RAVE_LOCALE_CONF": os.path.join(tmp, "locale.conf"),
        "RAVE_VCONSOLE_CONF": os.path.join(tmp, "vconsole.conf"),
        "RAVE_LOCALIZATION_VERIFY_FLAG": os.path.join(tmp, "run", "verify"),
        # Absent on a build host; preflight must degrade, not refuse everything.
        "XKBCLI": os.path.join(tmp, "no-such-xkbcli"),
    })

    sys.path.insert(0, FILES)
    import routes_localization as rl

    app = Flask(__name__)
    app.register_blueprint(rl.localization_bp)
    client = app.test_client()

    def post(path, payload):
        return client.post(path, json=payload)

    print("\n-- state --")
    resp = client.get("/api/localization")
    body = resp.get_json()
    check("GET returns 200", resp.status_code == 200)
    check("factory layout is gb", body["current"]["keyboard"]["layout"] == "gb")
    check("factory language is en_GB", body["current"]["language"] == "en_GB.UTF-8")
    check("shortlist populated", len(body["shortlist"]) >= 10,
          str(len(body["shortlist"])))
    installed = {l["code"]: l["installed"] for l in body["locales"]}
    check("locale install state reported",
          installed.get("en_GB.UTF-8") is True and
          installed.get("it_IT.UTF-8") is False, str(installed))

    full = client.get("/api/localization/layouts").get_json()
    check("full catalogue served separately", len(full["layouts"]) > 50,
          str(len(full["layouts"])))

    print("\n-- rejected input --")
    for name, payload in (
        ("unknown layout", {"layout": "zzzz"}),
        ("shell injection", {"layout": "gb;reboot"}),
        ("command substitution", {"layout": "$(reboot)"}),
        ("newline injection", {"layout": "gb\nshell=evil.so"}),
        ("unknown variant", {"layout": "de", "variant": "nope"}),
        ("missing layout", {"variant": "nodeadkeys"}),
    ):
        check(name, post("/api/localization/keyboard", payload).status_code == 400)

    check("non-object body rejected",
          post("/api/localization/keyboard", ["x"]).status_code == 400)
    check("empty body rejected",
          client.post("/api/localization/keyboard", data="",
                      content_type="application/json").status_code == 400)
    check("unsupported language rejected",
          post("/api/localization/language",
               {"language": "xx_XX.UTF-8"}).status_code == 400)
    check("nothing written after rejections",
          not os.path.exists(os.environ["RAVE_LOCALIZATION_CONFIG"]))

    print("\n-- apply --")
    resp = post("/api/localization/keyboard", {"layout": "de"})
    body = resp.get_json()
    check("apply accepted", resp.status_code == 200)
    check("reports changed", body.get("changed") is True)
    check("reports restarting", body.get("restarting") is True)
    check("offers a revert window", body.get("revert_seconds") == rl.REVERT_SECONDS)
    check("config persisted", body["current"]["keyboard"]["layout"] == "de")

    # Regression, caught on hardware: the FIRST change on a fresh unit had no
    # revert target, so the timer fired with "nothing to revert to" and the
    # safety net silently did nothing in the case it most needs to work.
    check("first apply on a fresh unit still leaves a revert target",
          os.path.exists(os.environ["RAVE_LOCALIZATION_PREV"]),
          "no prev file written")
    with open(os.environ["RAVE_LOCALIZATION_PREV"], encoding="utf-8") as handle:
        check("revert target is the factory default",
              json.load(handle)["keyboard"]["layout"] == "gb")

    runtime = open(os.environ["RAVE_WESTON_RUNTIME"], encoding="utf-8").read()
    check("weston config regenerated", "keymap_layout=de" in runtime)
    check("weston config kept autolaunch", "[autolaunch]" in runtime)
    check("verify flag set for the kiosk",
          os.path.exists(os.environ["RAVE_LOCALIZATION_VERIFY_FLAG"]))

    log = open(calls, encoding="utf-8").read()
    check("failed state cleared before restart",
          "systemctl reset-failed weston" in log, log)
    check("restart deferred past the response",
          "systemd-run" in log and "--on-active=2" in log, log)
    check("no fixed --unit on the restart (would collide)",
          "--unit=rave-weston-restart" not in log, log)
    check("revert timer armed", "rave-localization-revert" in log, log)

    print("\n-- guards --")
    # Identical config must cost nothing: this is what stops a replayed or
    # looping POST walking weston into start-limit-hit.
    body = post("/api/localization/keyboard", {"layout": "de"}).get_json()
    check("identical apply is a no-op", body.get("changed") is False)

    resp = post("/api/localization/keyboard", {"layout": "fr"})
    check("second apply inside cooldown gets 429", resp.status_code == 429)
    check("429 carries Retry-After", resp.headers.get("Retry-After") is not None)
    check("layout unchanged after 429",
          client.get("/api/localization").get_json()
          ["current"]["keyboard"]["layout"] == "de")

    rl._last_apply = 0
    check("apply works once the cooldown lapses",
          post("/api/localization/keyboard",
               {"layout": "fr"}).get_json().get("changed") is True)

    print("\n-- language --")
    rl._last_apply = 0
    open(calls, "w", encoding="utf-8").close()
    body = post("/api/localization/language",
                {"language": "de_DE.UTF-8"}).get_json()
    check("language applied", body["current"]["language"] == "de_DE.UTF-8")
    check("language change does NOT restart the compositor",
          body.get("restarting") is False)
    log = open(calls, encoding="utf-8").read()
    check("language change restarts only the browser",
          "pkill" in log and "restart weston" not in log, log)
    env_text = open(os.environ["RAVE_LOCALIZATION_ENV"], encoding="utf-8").read()
    check("chromium language updated", "--lang=de" in env_text, env_text)
    check("keyboard preserved across a language change",
          body["current"]["keyboard"]["layout"] == "fr")

    print("\n-- confirm / revert --")
    open(calls, "w", encoding="utf-8").close()
    check("confirm accepted",
          post("/api/localization/confirm", {}).status_code == 200)
    check("confirm cancels the revert timer",
          "stop rave-localization-revert.timer" in
          open(calls, encoding="utf-8").read())

    # The revert slot must survive the language change applied above. If a
    # language change consumed it, changing language during the confirm window
    # after a risky layout change would leave the auto-revert pointing at the
    # bad layout -- restoring the exact thing it exists to undo.
    rl._last_apply = 0
    resp = post("/api/localization/revert", {})
    check("revert accepted", resp.status_code == 200)
    check("revert restores the keyboard from before the layout change",
          resp.get_json()["current"]["keyboard"]["layout"] == "de",
          resp.get_json()["current"]["keyboard"]["layout"])
    check("revert clears the failed state first",
          "reset-failed weston" in open(calls, encoding="utf-8").read())

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
