# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Localisation state: keyboard layout and system language.

Pure logic plus file IO -- no Flask import, so the whole module is exercisable
on a build host with no device and no root.

The layout catalogue is parsed from the installed xkeyboard-config rules file
rather than hardcoded, so it cannot drift from what libxkbcommon will actually
accept.

Safety note: Weston exits 1 on a keymap it cannot compile, and weston.service
is Restart=on-failure with StartLimitBurst=5/10s, so an unusable keymap does
not degrade -- it takes the display down until someone clears the failed state
over SSH. Validation here is therefore safety-critical, not cosmetic, and is
backed by an actual test-compile (see preflight_keymap).
"""

import json
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

# -- Paths (overridable so tests can run on plain files) --

CONFIG_FILE = Path(os.environ.get(
    "RAVE_LOCALIZATION_CONFIG", "/etc/rave-menu/localization.json"))
PREV_FILE = Path(os.environ.get(
    "RAVE_LOCALIZATION_PREV", "/etc/rave-menu/localization.prev.json"))
DEFAULTS_FILE = Path(os.environ.get(
    "RAVE_LOCALIZATION_DEFAULTS", "/opt/rave-menu/localization.defaults.json"))

XKB_RULES = Path(os.environ.get(
    "RAVE_XKB_RULES", "/usr/share/X11/xkb/rules/evdev.xml"))

WESTON_TEMPLATE = Path(os.environ.get(
    "RAVE_WESTON_TEMPLATE", "/opt/rave-menu/weston.ini.in"))
WESTON_RUNTIME = Path(os.environ.get(
    "RAVE_WESTON_RUNTIME", "/run/rave-menu/weston.ini"))
WESTON_FALLBACK = Path(os.environ.get(
    "RAVE_WESTON_FALLBACK", "/etc/xdg/weston/rave-menu.ini"))

ENV_FILE = Path(os.environ.get(
    "RAVE_LOCALIZATION_ENV", "/etc/default/rave-localization"))
LOCALE_CONF = Path(os.environ.get("RAVE_LOCALE_CONF", "/etc/locale.conf"))
VCONSOLE_CONF = Path(os.environ.get("RAVE_VCONSOLE_CONF", "/etc/vconsole.conf"))

VERIFY_FLAG = Path(os.environ.get(
    "RAVE_LOCALIZATION_VERIFY_FLAG", "/run/rave-menu/localization-verify"))

XKBCLI = os.environ.get("XKBCLI", "xkbcli")

SCHEMA_VERSION = 1

# Factory fallback, used when neither the user config nor the shipped defaults
# file can be read. Keeping it in code means there is no filesystem state that
# can leave the unit with no usable keyboard.
FACTORY = {
    "version": SCHEMA_VERSION,
    "keyboard": {"model": "pc105", "layout": "gb", "variant": "", "options": ""},
    "language": "en_GB.UTF-8",
}

# Locales built into the image. Phase 2 adds ja_JP/zh_CN with CJK fonts.
# en_US is generated but deliberately not offered: source strings are British
# English and there is no en_US catalog yet, so picking it would change nothing.
OFFERED_LOCALES = [
    ("en_GB.UTF-8", "English (United Kingdom)"),
    ("de_DE.UTF-8", "Deutsch (Deutschland)"),
    ("el_GR.UTF-8", "Ελληνικά (Ελλάδα)"),
    ("fr_FR.UTF-8", "Français (France)"),
    ("es_ES.UTF-8", "Español (España)"),
    ("it_IT.UTF-8", "Italiano (Italia)"),
]

# Chromium resolves its UI language from a BCP-47 tag, which is not the locale
# name. Only tags with a matching .pak in the image belong here.
CHROMIUM_LANG = {
    "en_GB.UTF-8": "en-GB",
    "en_US.UTF-8": "en-US",
    "de_DE.UTF-8": "de",
    "el_GR.UTF-8": "el",
    "fr_FR.UTF-8": "fr",
    "es_ES.UTF-8": "es",
    "it_IT.UTF-8": "it",
}

# Layouts surfaced before the "show all" expander.
SHORTLIST = ["gb", "us", "de", "fr", "es", "it", "gr", "nl",
             "se", "no", "dk", "fi", "pt", "pl", "be"]

# /etc/vconsole.conf uses kbd keymap names, which are not XKB layout codes.
# Only the mappings that actually differ, or that are confirmed present in
# /usr/share/keymaps, are listed; anything else falls through to the XKB code.
CONSOLE_KEYMAP = {
    "gb": "uk",
    "us": "us",
    "de": "de",
    "fr": "fr",
    "es": "es",
    "it": "it",
    "gr": "gr",
    "jp": "jp106",
}

# Defence in depth behind the catalogue allow-list. Layout/variant/model codes
# are a restricted alphabet; XKB *options* legitimately contain ':' and ','
# (e.g. "grp:alt_shift_toggle"), so they get their own pattern.
_CODE_RE = re.compile(r"[A-Za-z0-9_.+-]+")
_OPTIONS_RE = re.compile(r"[A-Za-z0-9_.+:,-]+")
_LOCALE_RE = re.compile(r"[A-Za-z0-9_.@-]+")


class LocalizationError(ValueError):
    """Rejected input. Carries a message safe to show the user."""


# -- Catalogue ----------------------------------------------------------------

_catalog_cache: dict | None = None
_catalog_mtime: float | None = None


def _text(node, path):
    found = node.find(path)
    return found.text.strip() if found is not None and found.text else ""


def load_catalog(force: bool = False) -> dict:
    """Parse the XKB rules file into layouts, variants, models and options.

    Cached on mtime: the file is ~250 KB and would otherwise be re-parsed on
    every page load, but the mtime check keeps it authoritative.
    """
    global _catalog_cache, _catalog_mtime
    try:
        mtime = XKB_RULES.stat().st_mtime
    except OSError as exc:
        raise LocalizationError(f"XKB rules unreadable: {XKB_RULES}") from exc

    if not force and _catalog_cache is not None and _catalog_mtime == mtime:
        return _catalog_cache

    root = ET.parse(XKB_RULES).getroot()

    layouts = []
    for layout in root.findall("./layoutList/layout"):
        code = _text(layout, "./configItem/name")
        if not code:
            continue
        variants = []
        for variant in layout.findall("./variantList/variant"):
            vcode = _text(variant, "./configItem/name")
            if vcode:
                variants.append(
                    {"code": vcode,
                     "name": _text(variant, "./configItem/description") or vcode})
        layouts.append({
            "code": code,
            "name": _text(layout, "./configItem/description") or code,
            "variants": sorted(variants, key=lambda v: v["name"].lower()),
        })

    models = [_text(m, "./configItem/name")
              for m in root.findall("./modelList/model")]
    options = [_text(o, "./configItem/name")
               for g in root.findall("./optionList/group")
               for o in g.findall("./option")]

    _catalog_cache = {
        "layouts": sorted(layouts, key=lambda item: item["name"].lower()),
        "models": [m for m in models if m],
        "options": [o for o in options if o],
    }
    _catalog_mtime = mtime
    return _catalog_cache


def find_layout(code: str) -> dict | None:
    return next((l for l in load_catalog()["layouts"] if l["code"] == code), None)


def shortlist_layouts() -> list:
    """Shortlist entries, in the declared order, skipping any absent locally."""
    by_code = {l["code"]: l for l in load_catalog()["layouts"]}
    return [by_code[c] for c in SHORTLIST if c in by_code]


def available_locales() -> list:
    """Offered locales filtered to those glibc actually has on this system."""
    try:
        out = subprocess.run(["locale", "-a"], capture_output=True,
                             text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    # locale -a reports "en_GB.utf8" where the config carries "en_GB.UTF-8".
    present = {line.strip().replace("-", "").lower() for line in out.splitlines()}

    result = []
    for code, name in OFFERED_LOCALES:
        key = code.replace("-", "").lower()
        result.append({"code": code, "name": name, "installed": key in present})
    return result


# -- Validation ---------------------------------------------------------------

def _check(pattern, value, field):
    if not pattern.fullmatch(value):
        raise LocalizationError(f"invalid characters in {field}")


def validate(cfg: dict) -> dict:
    """Allow-list a candidate config, returning the normalised form.

    Raises LocalizationError on anything not present in the catalogue. Uses
    fullmatch rather than an anchored search because Python's '$' also matches
    before a trailing newline, which would let a newline through into an ini
    file or an EnvironmentFile.
    """
    if not isinstance(cfg, dict):
        raise LocalizationError("config must be an object")

    kb = cfg.get("keyboard")
    if not isinstance(kb, dict):
        raise LocalizationError("missing keyboard settings")

    layout = str(kb.get("layout", "")).strip()
    variant = str(kb.get("variant", "") or "").strip()
    model = str(kb.get("model", "") or FACTORY["keyboard"]["model"]).strip()
    options = str(kb.get("options", "") or "").strip()
    language = str(cfg.get("language", "") or FACTORY["language"]).strip()

    if not layout:
        raise LocalizationError("layout is required")
    _check(_CODE_RE, layout, "layout")
    _check(_CODE_RE, model, "model")
    if variant:
        _check(_CODE_RE, variant, "variant")
    if options:
        _check(_OPTIONS_RE, options, "options")
    _check(_LOCALE_RE, language, "language")

    catalog = load_catalog()

    entry = find_layout(layout)
    if entry is None:
        raise LocalizationError(f"unknown keyboard layout: {layout}")
    if variant and variant not in {v["code"] for v in entry["variants"]}:
        raise LocalizationError(f"unknown variant for {layout}: {variant}")
    if catalog["models"] and model not in catalog["models"]:
        raise LocalizationError(f"unknown keyboard model: {model}")
    if options:
        known = set(catalog["options"])
        for token in options.split(","):
            if token and token not in known:
                raise LocalizationError(f"unknown keyboard option: {token}")
    if language not in {code for code, _ in OFFERED_LOCALES}:
        raise LocalizationError(f"unsupported language: {language}")

    return {
        "version": SCHEMA_VERSION,
        "keyboard": {"model": model, "layout": layout,
                     "variant": variant, "options": options},
        "language": language,
    }


def preflight_keymap(cfg: dict) -> None:
    """Test-compile the keymap, so a bad one is a 400 and not a dead display.

    The catalogue says what xkeyboard-config advertises; this proves
    libxkbcommon can actually build it. Weston uses the same library, so a
    success here means Weston will start.
    """
    kb = cfg["keyboard"]
    cmd = [XKBCLI, "compile-keymap", "--layout", kb["layout"]]
    if kb["variant"]:
        cmd += ["--variant", kb["variant"]]
    if kb["model"]:
        cmd += ["--model", kb["model"]]
    if kb["options"]:
        cmd += ["--options", kb["options"]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=20, check=False)
    except FileNotFoundError:
        # No xkbcli in the image: the catalogue check above still applied.
        return
    except subprocess.SubprocessError as exc:
        raise LocalizationError(f"keymap check failed: {exc}") from exc
    if proc.returncode != 0:
        raise LocalizationError(
            f"keyboard layout '{kb['layout']}' could not be compiled")


# -- Config load/save ---------------------------------------------------------

def _read_json(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _normalise(data: dict | None) -> dict | None:
    """Accept a stored config only if it still validates against this image."""
    if data is None or data.get("version") != SCHEMA_VERSION:
        return None
    try:
        return validate(data)
    except LocalizationError:
        return None


def load_config() -> dict:
    """User config, else shipped defaults, else the in-code factory values."""
    for candidate in (CONFIG_FILE, DEFAULTS_FILE):
        resolved = _normalise(_read_json(candidate))
        if resolved is not None:
            return resolved
    return dict(FACTORY)


def write_atomic(path: Path, content: str, mode: int = 0o644) -> None:
    """Write via a temp file in the same directory, then rename.

    Every generated file goes through this: a half-written weston config or
    EnvironmentFile stops the compositor starting.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_config(cfg: dict) -> dict:
    """Persist a validated config, keeping the last keyboard state for revert.

    The snapshot is taken from the *effective* current config, not from the
    config file: on a fresh unit there is no file yet, so reading the file
    would leave the first keyboard change -- the riskiest one, on a unit nobody
    has configured -- with nothing to revert to.

    It is only taken when the keyboard block actually changes. A language
    change must not consume it, or changing language during the confirm window
    after a risky layout change would move the revert target onto the bad
    layout, so the auto-revert would restore the very thing it exists to undo.
    """
    checked = validate(cfg)
    current = load_config()
    if current.get("keyboard") != checked["keyboard"]:
        write_atomic(PREV_FILE, json.dumps(current, indent=2) + "\n")
    write_atomic(CONFIG_FILE, json.dumps(checked, indent=2) + "\n")
    return checked


def load_previous() -> dict | None:
    return _normalise(_read_json(PREV_FILE))


# -- Rendering ----------------------------------------------------------------

def console_keymap(layout: str) -> str:
    return CONSOLE_KEYMAP.get(layout, layout)


def chromium_lang(language: str) -> str:
    return CHROMIUM_LANG.get(language, language.split(".")[0].replace("_", "-"))


def babel_locale(language: str) -> str:
    """Babel cannot parse the '.UTF-8' suffix, so strip the codeset."""
    return language.split(".")[0]


def ui_locales() -> list:
    return [babel_locale(code) for code, _ in OFFERED_LOCALES]


def resolve_ui_locale(requested=None, configured=None) -> str:
    """Choose the catalogue locale for a request.

    An explicit ?lang= wins so a field engineer is not stuck in a language
    they cannot read; otherwise the configured system language is used, so a
    remote view matches what the kiosk is showing.

    Both "de" and "de_DE" are accepted. Babel resolves either to the same
    catalogue, and the short form is what anyone types by hand.
    """
    supported = ui_locales()
    if requested:
        for code in supported:
            if requested in (code, code.split("_")[0]):
                return code
    if configured:
        candidate = babel_locale(configured)
        if candidate in supported:
            return candidate
    return babel_locale(FACTORY["language"])


def render_weston_ini(template: str, cfg: dict) -> str:
    """Append a [keyboard] block to the base template.

    Any existing [keyboard] section is dropped first so repeated applies stay
    idempotent. Empty values are omitted rather than written blank.
    """
    kept = []
    skipping = False
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped.lower() == "[keyboard]"
        if not skipping:
            kept.append(line)

    while kept and not kept[-1].strip():
        kept.pop()

    kb = cfg["keyboard"]
    block = ["", "[keyboard]", "keymap_rules=evdev"]
    for key, value in (("keymap_model", kb["model"]),
                       ("keymap_layout", kb["layout"]),
                       ("keymap_variant", kb["variant"]),
                       ("keymap_options", kb["options"])):
        if value:
            block.append(f"{key}={value}")
    return "\n".join(kept + block) + "\n"


def render_env_file(cfg: dict) -> str:
    """EnvironmentFile consumed by our own weston drop-in.

    Values come from the validated allow-list, so they cannot contain spaces,
    quotes or newlines; they are still quoted so the file stays well-formed if
    that ever changes.
    """
    return (
        "# Generated by rave-menu. Edits are overwritten.\n"
        f'LANG="{cfg["language"]}"\n'
        f'RAVE_CHROMIUM_EXTRA_ARGS="--lang={chromium_lang(cfg["language"])}"\n'
    )
