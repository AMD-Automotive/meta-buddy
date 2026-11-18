#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Localisation config, catalogue parsing, validation and rendering.

Host-only: every path the module touches is redirected into a temp directory,
so no device, no root and no systemd are needed.

The validation cases matter more than usual here. Weston exits 1 on a keymap it
cannot compile and is Restart=on-failure with StartLimitBurst=5/10s, so a bad
value does not degrade gracefully -- it takes the display down until the failed
state is cleared over SSH.
"""
import configparser
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


FIXTURE_RULES = """<?xml version="1.0" encoding="UTF-8"?>
<xkbConfigRegistry version="1.1">
  <modelList>
    <model><configItem><name>pc105</name>
      <description>Generic 105-key PC</description></configItem></model>
    <model><configItem><name>pc104</name>
      <description>Generic 104-key PC</description></configItem></model>
  </modelList>
  <layoutList>
    <layout><configItem><name>gb</name>
        <description>English (UK)</description></configItem>
      <variantList>
        <variant><configItem><name>dvorak</name>
          <description>English (UK, Dvorak)</description></configItem></variant>
      </variantList>
    </layout>
    <layout><configItem><name>de</name>
        <description>German</description></configItem>
      <variantList>
        <variant><configItem><name>nodeadkeys</name>
          <description>German (no dead keys)</description></configItem></variant>
      </variantList>
    </layout>
    <layout><configItem><name>us</name>
        <description>English (US)</description></configItem>
      <variantList/>
    </layout>
  </layoutList>
  <optionList>
    <group><configItem><name>grp</name><description>Switching</description></configItem>
      <option><configItem><name>grp:alt_shift_toggle</name>
        <description>Alt+Shift</description></configItem></option>
    </group>
  </optionList>
</xkbConfigRegistry>
"""

TEMPLATE = """# header comment

[core]
idle-time=0
shell=kiosk-shell.so

[autolaunch]
path=/usr/bin/rave-cog-launch.sh
watch=true
"""


def main():
    tmp = tempfile.mkdtemp(prefix="localization-test-")
    rules = os.path.join(tmp, "evdev.xml")
    with open(rules, "w", encoding="utf-8") as handle:
        handle.write(FIXTURE_RULES)

    # Redirect every path before the module reads them at import time.
    os.environ["RAVE_XKB_RULES"] = rules
    os.environ["RAVE_LOCALIZATION_CONFIG"] = os.path.join(tmp, "localization.json")
    os.environ["RAVE_LOCALIZATION_PREV"] = os.path.join(tmp, "localization.prev.json")
    os.environ["RAVE_LOCALIZATION_DEFAULTS"] = os.path.join(tmp, "defaults.json")
    os.environ["XKBCLI"] = os.path.join(tmp, "no-such-xkbcli")
    os.environ["RAVE_WESTON_TEMPLATE"] = os.path.join(tmp, "weston.ini.in")
    os.environ["RAVE_WESTON_RUNTIME"] = os.path.join(tmp, "run", "weston.ini")
    os.environ["RAVE_WESTON_FALLBACK"] = os.path.join(tmp, "rave-menu.ini")
    os.environ["RAVE_LOCALIZATION_ENV"] = os.path.join(tmp, "rave-localization")
    os.environ["RAVE_LOCALE_CONF"] = os.path.join(tmp, "locale.conf")
    os.environ["RAVE_VCONSOLE_CONF"] = os.path.join(tmp, "vconsole.conf")
    os.environ["RAVE_LOCALIZATION_VERIFY_FLAG"] = os.path.join(tmp, "run", "verify")

    sys.path.insert(0, FILES)
    import localization as loc

    cfg_path = loc.CONFIG_FILE
    defaults_path = loc.DEFAULTS_FILE

    print("\n-- catalogue --")
    cat = loc.load_catalog()
    codes = [l["code"] for l in cat["layouts"]]
    check("layouts parsed", set(codes) == {"gb", "de", "us"}, codes)
    check("layouts sorted by name", codes == sorted(
        codes, key=lambda c: {"gb": "english (uk)", "de": "german",
                              "us": "english (us)"}[c]), codes)
    gb = loc.find_layout("gb")
    check("variant parsed", gb["variants"][0]["code"] == "dvorak")
    check("layout description", gb["name"] == "English (UK)", gb["name"])
    check("models parsed", "pc105" in cat["models"])
    check("options parsed", "grp:alt_shift_toggle" in cat["options"])
    check("shortlist filtered to present layouts",
          [l["code"] for l in loc.shortlist_layouts()] == ["gb", "us", "de"])

    loc.load_catalog()
    check("catalogue cached on mtime", loc._catalog_cache is cat)

    print("\n-- validation --")

    def good(**kw):
        base = {"version": 1,
                "keyboard": {"model": "pc105", "layout": "gb",
                             "variant": "", "options": ""},
                "language": "en_GB.UTF-8"}
        base["keyboard"].update(kw.pop("keyboard", {}))
        base.update(kw)
        return base

    def rejects(name, cfg):
        try:
            loc.validate(cfg)
        except loc.LocalizationError:
            check(name, True)
        except Exception as exc:  # noqa: BLE001 - any other error is a bug
            check(name, False, f"wrong exception: {exc!r}")
        else:
            check(name, False, "accepted")

    check("valid config accepted",
          loc.validate(good())["keyboard"]["layout"] == "gb")
    check("valid variant accepted",
          loc.validate(good(keyboard={"layout": "de", "variant": "nodeadkeys"})
                       )["keyboard"]["variant"] == "nodeadkeys")
    check("known option accepted",
          loc.validate(good(keyboard={"options": "grp:alt_shift_toggle"})
                       )["keyboard"]["options"] == "grp:alt_shift_toggle")

    rejects("unknown layout", good(keyboard={"layout": "zzzz"}))
    rejects("unknown variant", good(keyboard={"layout": "de",
                                              "variant": "nosuchvariant"}))
    rejects("variant from the wrong layout",
            good(keyboard={"layout": "de", "variant": "dvorak"}))
    rejects("unknown model", good(keyboard={"model": "pc999"}))
    rejects("unknown option", good(keyboard={"options": "grp:bogus"}))
    rejects("unsupported language", good(language="xx_XX.UTF-8"))
    rejects("empty layout", good(keyboard={"layout": ""}))
    rejects("missing keyboard block", {"version": 1, "language": "en_GB.UTF-8"})
    rejects("non-dict config", ["not", "a", "dict"])
    rejects("shell metacharacters", good(keyboard={"layout": "gb; rm -rf /"}))
    rejects("command substitution", good(keyboard={"layout": "$(id)"}))
    rejects("path traversal", good(keyboard={"layout": "../../etc/passwd"}))

    # Surrounding whitespace is normalised away rather than rejected, so a
    # stray newline from a form field is forgiving. What must never happen is
    # a newline surviving into a generated ini file or EnvironmentFile, where
    # it would smuggle an extra directive -- so assert the output, not just
    # the return value.
    padded = loc.validate(good(keyboard={"layout": "gb\n"},
                               language=" en_GB.UTF-8\n"))
    check("trailing newline normalised out of layout",
          padded["keyboard"]["layout"] == "gb", repr(padded["keyboard"]["layout"]))
    check("surrounding whitespace normalised out of language",
          padded["language"] == "en_GB.UTF-8", repr(padded["language"]))
    check("normalised value renders one clean ini line",
          "keymap_layout=gb\n" in loc.render_weston_ini(TEMPLATE, padded))
    check("normalised value renders one clean env line",
          'LANG="en_GB.UTF-8"\n' in loc.render_env_file(padded))

    # Internal newlines are a different matter: there is no safe normalisation,
    # so they must be refused outright.
    rejects("embedded newline smuggling an ini key",
            good(keyboard={"layout": "gb\nshell=evil.so"}))
    rejects("embedded newline in language",
            good(language="en_GB.UTF-8\nLANG=evil"))
    rejects("carriage return in layout", good(keyboard={"layout": "gb\rx"}))

    print("\n-- rendering --")
    cfg = loc.validate(good(keyboard={"layout": "de", "variant": "nodeadkeys"}))
    rendered = loc.render_weston_ini(TEMPLATE, cfg)
    check("keymap_layout written", "keymap_layout=de" in rendered)
    check("keymap_variant written", "keymap_variant=nodeadkeys" in rendered)
    check("empty options omitted", "keymap_options" not in rendered)
    check("base sections preserved",
          "[core]" in rendered and "[autolaunch]" in rendered)

    parser = configparser.ConfigParser()
    parser.read_string(rendered)
    check("renders as valid ini", parser["keyboard"]["keymap_layout"] == "de")
    check("autolaunch intact",
          parser["autolaunch"]["path"] == "/usr/bin/rave-cog-launch.sh")
    check("shell not clobbered", parser["core"]["shell"] == "kiosk-shell.so")

    again = loc.render_weston_ini(rendered, cfg)
    check("render is idempotent", again == rendered)

    plain = loc.validate(good())
    check("re-render replaces old keyboard block",
          loc.render_weston_ini(rendered, plain).count("[keyboard]") == 1)
    check("re-render switches layout",
          "keymap_layout=gb" in loc.render_weston_ini(rendered, plain))

    print("\n-- env file --")
    env = loc.render_env_file(loc.validate(good(language="de_DE.UTF-8")))
    check("LANG written", 'LANG="de_DE.UTF-8"' in env, env)
    check("chromium lang mapped", "--lang=de" in env, env)
    check("env file has no stray newline in values",
          all(line.count('"') in (0, 2) for line in env.splitlines()), env)

    print("\n-- helpers --")
    check("console keymap gb->uk", loc.console_keymap("gb") == "uk")
    check("console keymap jp->jp106", loc.console_keymap("jp") == "jp106")
    check("console keymap passthrough", loc.console_keymap("cz") == "cz")
    check("babel strips codeset", loc.babel_locale("en_GB.UTF-8") == "en_GB")
    check("chromium lang en_GB", loc.chromium_lang("en_GB.UTF-8") == "en-GB")

    print("\n-- UI locale resolution --")
    # Regression: ?lang= used to require the exact "de_DE" form, so the short
    # form anyone would actually type silently fell back to English. Missed
    # originally because the test supplied its own locale selector instead of
    # exercising this logic.
    check("short form resolves",
          loc.resolve_ui_locale(requested="de") == "de_DE")
    check("full form resolves",
          loc.resolve_ui_locale(requested="de_DE") == "de_DE")
    check("every offered locale resolves from its short form",
          all(loc.resolve_ui_locale(requested=code.split("_")[0]) == code
              for code in loc.ui_locales()), str(loc.ui_locales()))
    check("unknown request falls back to the configured language",
          loc.resolve_ui_locale(requested="xx",
                                configured="fr_FR.UTF-8") == "fr_FR")
    check("configured language used when nothing requested",
          loc.resolve_ui_locale(configured="it_IT.UTF-8") == "it_IT")
    check("codeset stripped from the configured language",
          loc.resolve_ui_locale(configured="es_ES.UTF-8") == "es_ES")
    check("unsupported configured language falls back to factory",
          loc.resolve_ui_locale(configured="ja_JP.UTF-8") == "en_GB")
    check("nothing at all falls back to factory",
          loc.resolve_ui_locale() == "en_GB")
    check("request beats configured",
          loc.resolve_ui_locale(requested="it",
                                configured="de_DE.UTF-8") == "it_IT")

    print("\n-- config load/save --")
    check("missing config falls back to factory",
          loc.load_config()["keyboard"]["layout"] == "gb")
    check("factory language is British English",
          loc.load_config()["language"] == "en_GB.UTF-8")

    saved = loc.save_config(good(keyboard={"layout": "de"}))
    check("saved config round-trips", saved["keyboard"]["layout"] == "de")
    check("reload sees saved value", loc.load_config()["keyboard"]["layout"] == "de")
    check("file mode is 0644", (os.stat(cfg_path).st_mode & 0o777) == 0o644)

    # Regression: this is the fresh-unit case, and it failed on hardware. The
    # snapshot used to be read from the config file, which does not exist until
    # the first save -- so the very first keyboard change, on a unit nobody has
    # configured yet, had no revert target and the auto-revert timer fired with
    # "nothing to revert to".
    first = loc.load_previous()
    check("first save snapshots the factory default for revert",
          first is not None and first["keyboard"]["layout"] == "gb",
          str(first))

    check("no temp files left behind",
          not [f for f in os.listdir(os.path.dirname(cfg_path))
               if f.startswith(".localization")],
          os.listdir(os.path.dirname(cfg_path)))

    loc.save_config(good(keyboard={"layout": "us"}))
    prev = loc.load_previous()
    check("previous config retained for revert",
          prev is not None and prev["keyboard"]["layout"] == "de",
          str(prev))

    # A language change must leave the keyboard revert target alone.
    loc.save_config(good(keyboard={"layout": "us"}, language="de_DE.UTF-8"))
    prev = loc.load_previous()
    check("language change does not consume the keyboard revert point",
          prev is not None and prev["keyboard"]["layout"] == "de", str(prev))

    with open(cfg_path, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")
    check("corrupt config falls back to factory",
          loc.load_config()["keyboard"]["layout"] == "gb")

    with open(cfg_path, "w", encoding="utf-8") as handle:
        json.dump({"version": 99, "keyboard": {"layout": "de"},
                   "language": "de_DE.UTF-8"}, handle)
    check("unknown schema version falls back to factory",
          loc.load_config()["keyboard"]["layout"] == "gb")

    with open(cfg_path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "keyboard": {"layout": "zzzz"},
                   "language": "en_GB.UTF-8"}, handle)
    check("config that no longer validates falls back to factory",
          loc.load_config()["keyboard"]["layout"] == "gb")

    os.unlink(cfg_path)
    with open(defaults_path, "w", encoding="utf-8") as handle:
        json.dump(good(keyboard={"layout": "us"}), handle)
    check("shipped defaults used when no user config",
          loc.load_config()["keyboard"]["layout"] == "us")

    print("\n-- preflight --")
    # xkbcli points at a nonexistent binary, so preflight must stay silent
    # rather than refuse every layout on an image without it.
    try:
        loc.preflight_keymap(loc.validate(good()))
        check("missing xkbcli does not block apply", True)
    except Exception as exc:  # noqa: BLE001
        check("missing xkbcli does not block apply", False, repr(exc))

    print("\n-- applier --")
    import apply_localization as app

    with open(loc.WESTON_TEMPLATE, "w", encoding="utf-8") as handle:
        handle.write(TEMPLATE)
    with open(loc.WESTON_FALLBACK, "w", encoding="utf-8") as handle:
        handle.write(TEMPLATE + "\n[keyboard]\nkeymap_layout=gb\n")

    applied = loc.validate(good(keyboard={"layout": "de"},
                                language="de_DE.UTF-8"))
    check("apply_all reports success", app.apply_all(applied) is True)

    runtime = loc.WESTON_RUNTIME.read_text(encoding="utf-8")
    check("runtime weston config written", "keymap_layout=de" in runtime)
    runtime_parsed = configparser.ConfigParser()
    runtime_parsed.read_string(runtime)
    check("runtime config keeps autolaunch",
          runtime_parsed.has_section("autolaunch"))

    check("locale.conf written",
          "LANG=de_DE.UTF-8" in loc.LOCALE_CONF.read_text(encoding="utf-8"))
    check("vconsole.conf written",
          "KEYMAP=de" in loc.VCONSOLE_CONF.read_text(encoding="utf-8"))
    env_text = loc.ENV_FILE.read_text(encoding="utf-8")
    check("env file carries chromium lang", "--lang=de" in env_text, env_text)

    check("boot apply does not set the verify flag",
          not loc.VERIFY_FLAG.exists())
    app.apply_all(applied, from_api=True)
    check("api apply sets the verify flag", loc.VERIFY_FLAG.exists())

    # The failure path matters more than the happy one: a weston config that
    # lost [autolaunch] boots to a blank screen with the unit reporting
    # success, so the applier must refuse it and install the fallback.
    broken = os.path.join(tmp, "broken.ini")
    with open(broken, "w", encoding="utf-8") as handle:
        handle.write("[core]\nshell=kiosk-shell.so\n")
    original_template = loc.WESTON_TEMPLATE
    loc.WESTON_TEMPLATE = __import__("pathlib").Path(broken)
    ok = app.write_weston_config(applied)
    loc.WESTON_TEMPLATE = original_template
    check("template without [autolaunch] is rejected", ok is False)
    recovered = loc.WESTON_RUNTIME.read_text(encoding="utf-8")
    check("fallback config installed after rejection",
          "[autolaunch]" in recovered, recovered)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
