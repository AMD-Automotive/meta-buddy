#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""descriptor_tool.py — generic loader/validator for a bundle's appimage.yaml.

This is the "generic initial load and interpretation" of a .rave2app payload:
it validates only the *envelope* (format/identity/component shape) and then lets
the shell orchestrator iterate components and dispatch each by `kind`. Adding a
new component kind therefore needs handler code in the orchestrator only — not
changes here.

See docs/app-bundle-installer/01-bundle-format.md §3 for the format.

Sub-commands (all read a parsed appimage.yaml path):

    descriptor_tool.py validate <appimage.yaml> [--menu-version <v>]
        Validate the envelope. Exit 0 if OK, non-zero with a reason on stderr.

    descriptor_tool.py get <appimage.yaml> <dotted.key>
        Print a single value, e.g. `slot.label` or `boot.make_default`.

    descriptor_tool.py components <appimage.yaml> [--field <name>]
        List components, one per line. With --field, print that field per
        component (TSV: index<TAB>value) — handy for shell `while read` loops.

    descriptor_tool.py members <appimage.yaml>
        List the referenced member paths (one per line) for tar cross-checking.
"""

import argparse
import json
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced as a clear runtime error
    sys.stderr.write(
        "descriptor_tool: PyYAML not available (add python3-pyyaml to RDEPENDS)\n"
    )
    sys.exit(3)


# ---- Envelope constants -----------------------------------------------------

FORMAT_TAG = "rave2-appimage"
# 1 is still accepted: bundles built before declaring a boot was mandatory remain
# installable, and take the boot menu's deprecated direct path as they always did.
# 3 adds the jumbo application type.
SUPPORTED_FORMAT_VERSIONS = (1, 2, 3, 4)

# A jumbo app declares it cannot fit the standard application slot and asks for
# a slice of the disk tail instead.
APP_TYPES = ("standard", "jumbo")
DEFAULT_APP_TYPE = "standard"
JUMBO_FORMAT_VERSION = 3
STANDARD_SLOT_GIB = 50
JUMBO_FIELDS = ("minimum_storage", "desired_storage")

# Application-image kinds: exactly one of these must be present.
APP_IMAGE_KINDS = ("x86-rootfs-tar", "x86-rootfs-ext4", "custom-build",
                  "x86-disk-image")
# Kinds carrying several members instead of a single `path`/`sha256` pair.
MULTI_MEMBER_KINDS = ("x86-disk-image",)
# Optional, repeatable add-on kinds.
VERSAL_KINDS = ("versal-ospi", "versal-wic")
INIT_KINDS = ("overlay", "hook-script")
KNOWN_KINDS = APP_IMAGE_KINDS + VERSAL_KINDS + INIT_KINDS

VALID_STAGES = ("post-deploy", "finalize")

# Partition names in a disk-image layout live in this namespace; the device's
# region manager will only ever delete partitions labelled this way.
PART_PREFIX = "app-"
MAX_LAYOUT_PARTS = 30
# The device mounts this partition to apply overlays and hook-scripts.
ROOT_PART = "app-root"

# slot defaults (01-bundle-format.md §3)
DEFAULT_SLOT = {
    "filesystem": "ext4",
    "label": "app-a",
    "mkfs_options": "-O ^orphan_file",
}


class DescriptorError(Exception):
    """Raised for any envelope validation failure (message is user-facing)."""


# ---- Loading ----------------------------------------------------------------

def load(path: str) -> dict:
    try:
        with open(path, "r") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        raise DescriptorError(f"descriptor not found: {path}")
    except yaml.YAMLError as exc:
        raise DescriptorError(f"descriptor is not valid YAML: {exc}")
    if not isinstance(data, dict):
        raise DescriptorError("descriptor top level must be a mapping")
    return data


# ---- Path safety ------------------------------------------------------------

def _bad_member_path(path: str) -> bool:
    """True if a tar member path is unsafe (absolute or escaping)."""
    if not path or not isinstance(path, str):
        return True
    if path.startswith("/"):
        return True
    parts = path.split("/")
    return ".." in parts


# ---- Version comparison (semver-ish, lenient) -------------------------------

def _ver_tuple(v: str):
    """The ordered numeric groups of a version string.

    Menu releases look like ``2026-09-07_075784``: a date, then the build's git
    sha. Splitting on '.' and taking leading digits collapsed every one of those
    to ``(2026,)``, so any menu satisfied any requirement and the compatibility
    gate never fired. Read every numeric group instead, and stop at the '_': the
    sha identifies a build, it does not order them. Semver falls out of the same
    rule — ``1.2.3`` -> ``(1, 2, 3)``.
    """
    text = str(v).split("_", 1)[0]
    return tuple(int(g) for g in re.findall(r"\d+", text))


def _version_ge(have: str, want: str) -> bool:
    """Return True if have >= want (missing/empty have fails a non-empty want)."""
    if not want:
        return True
    if not have:
        return False
    a, b = _ver_tuple(have), _ver_tuple(want)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a >= b


# ---- Validation -------------------------------------------------------------

def _validate_layout(idx: int, comp: dict, min_gib: int = 0) -> None:
    """Validate an ``x86-disk-image`` component's ``layout``.

    The layout is the exploded source image: one raw member per partition, in
    on-disk order, to be recreated inside the device's app region.
    """
    layout = comp.get("layout")
    if not isinstance(layout, list) or not layout:
        raise DescriptorError(
            f"component #{idx} (x86-disk-image) needs a non-empty 'layout' list"
        )
    if len(layout) > MAX_LAYOUT_PARTS:
        raise DescriptorError(
            f"component #{idx} layout has {len(layout)} partitions; "
            f"at most {MAX_LAYOUT_PARTS} are available"
        )
    grow = 0
    names = []
    fixed_mib = 0
    for i, part in enumerate(layout):
        if not isinstance(part, dict):
            raise DescriptorError(f"component #{idx} layout #{i} must be a mapping")
        name = part.get("part", "")
        if not isinstance(name, str) or not name.startswith(PART_PREFIX):
            raise DescriptorError(
                f"component #{idx} layout #{i}: 'part' must start with {PART_PREFIX!r}"
            )
        if name in names:
            raise DescriptorError(f"component #{idx} layout: duplicate part {name!r}")
        names.append(name)
        path = part.get("path")
        if not path:
            raise DescriptorError(f"component #{idx} layout #{i} ({name}) missing path")
        if _bad_member_path(path):
            raise DescriptorError(
                f"component #{idx} layout #{i} ({name}) has unsafe path {path!r}"
            )
        if not part.get("sha256"):
            raise DescriptorError(f"component #{idx} layout #{i} ({name}) missing sha256")
        if part.get("grow"):
            grow += 1
        elif not part.get("size_mib"):
            raise DescriptorError(
                f"component #{idx} layout #{i} ({name}) needs size_mib or grow"
            )
        else:
            fixed_mib += int(part["size_mib"])
    if grow > 1:
        raise DescriptorError(
            f"component #{idx} layout: at most one partition may set 'grow'"
        )
    # A jumbo app is only guaranteed its minimum_storage, so everything it pins
    # to a fixed size has to fit inside that floor; `grow` absorbs the rest.
    if min_gib and fixed_mib > min_gib * 1024:
        raise DescriptorError(
            f"component #{idx} layout: fixed partitions need {fixed_mib} MiB, "
            f"more than the {min_gib} GiB minimum_storage the bundle declares"
        )
    # The device mounts app-root to apply overlays and hooks, so it must exist.
    if ROOT_PART not in names:
        raise DescriptorError(
            f"component #{idx} layout: one partition must be named {ROOT_PART!r} "
            f"(got {names})"
        )


# ---- boot block -------------------------------------------------------------

# The entry is rendered into a GRUB script, so anything that could break out of
# it is refused rather than escaped. Kept deliberately identical to the set
# adopt_boot_entry() rejects, so authoring and install cannot drift.
_UNSAFE_BOOT_CHARS = re.compile(r"""[`"'${}\\\r\n]""")
_MAX_BOOT_ARG_LEN = 512

# Installer-owned: root= names the partition it just created, and rauc.slot=
# belongs to the menu's A/B logic.
_RESERVED_KERNEL_ARGS = ("root=", "rauc.slot=")

_CMDLINE_PATHS = ("hypervisor", "kernel", "ramdisk")
_CMDLINE_ARGS = ("hypervisor_args", "kernel_args")
_CMDLINE_KEYS = _CMDLINE_PATHS + _CMDLINE_ARGS


def _check_boot_arg(where: str, text: str) -> None:
    if not isinstance(text, str):
        raise DescriptorError(f"boot.cmdline.{where} must be a string")
    if len(text) > _MAX_BOOT_ARG_LEN:
        raise DescriptorError(
            f"boot.cmdline.{where} is {len(text)} chars (max {_MAX_BOOT_ARG_LEN})")
    bad = _UNSAFE_BOOT_CHARS.search(text)
    if bad:
        raise DescriptorError(
            f"boot.cmdline.{where} contains {bad.group(0)!r}, which the device "
            "refuses in a boot entry")


def _check_boot_path(where: str, path: str) -> None:
    if not isinstance(path, str) or not path:
        raise DescriptorError(f"boot.cmdline.{where} must be a non-empty string")
    if not path.startswith("/"):
        raise DescriptorError(
            f"boot.cmdline.{where} must be absolute within the partition it is "
            f"loaded from, got {path!r}")
    if ".." in path.split("/"):
        raise DescriptorError(f"boot.cmdline.{where} has unsafe path {path!r}")
    _check_boot_arg(where, path)


def _validate_boot(boot) -> list:
    """Validate the `boot` block. Returns non-fatal warnings."""
    warnings: list = []
    if not isinstance(boot, dict):
        raise DescriptorError("boot must be a mapping if present")

    entries_cfg = boot.get("entries_cfg")
    cmdline = boot.get("cmdline")

    if entries_cfg is not None and cmdline is not None:
        raise DescriptorError(
            "boot declares both entries_cfg and cmdline; use one or the other "
            "(entries_cfg to adopt your own GRUB entry, cmdline to declare the "
            "arguments and let the device render it)")

    if entries_cfg is not None:
        _check_boot_path("entries_cfg", entries_cfg)
        entry = boot.get("entry")
        if entry is not None and (not isinstance(entry, str) or not entry):
            raise DescriptorError("boot.entry must be a non-empty string")

    if cmdline is not None:
        if not isinstance(cmdline, dict):
            raise DescriptorError("boot.cmdline must be a mapping")
        unknown = sorted(set(cmdline) - set(_CMDLINE_KEYS))
        if unknown:
            warnings.append(
                f"boot.cmdline has unknown key(s) {', '.join(unknown)} — ignored")
        if not cmdline.get("kernel"):
            raise DescriptorError("boot.cmdline needs a 'kernel'")
        for key in _CMDLINE_PATHS:
            if cmdline.get(key) is not None:
                _check_boot_path(key, cmdline[key])
        for key in _CMDLINE_ARGS:
            if cmdline.get(key) is not None:
                _check_boot_arg(key, cmdline[key])
        if cmdline.get("hypervisor_args") and not cmdline.get("hypervisor"):
            raise DescriptorError(
                "boot.cmdline.hypervisor_args is set but no hypervisor is — "
                "the arguments would have nothing to apply to")
        for reserved in _RESERVED_KERNEL_ARGS:
            if reserved in str(cmdline.get("kernel_args", "")):
                raise DescriptorError(
                    f"boot.cmdline.kernel_args must not set {reserved!r}; the "
                    "installer owns it")
    return warnings


def _validate_app_type(data: dict) -> int:
    """Validate `type` and the jumbo storage fields. Returns minimum_storage GiB.

    A standard bundle carrying the storage fields is refused rather than
    ignored: it almost always means the `type: jumbo` line was left out, and
    silently installing such an app into the 50 GiB slot is the failure the
    declaration exists to prevent.
    """
    app_type = data.get("type", DEFAULT_APP_TYPE)
    if app_type not in APP_TYPES:
        raise DescriptorError(
            f"unknown type {app_type!r} (supported: {'/'.join(APP_TYPES)})"
        )

    declared = [f for f in JUMBO_FIELDS if data.get(f) is not None]
    if app_type != "jumbo":
        if declared:
            raise DescriptorError(
                f"{', '.join(declared)} may only be set on a jumbo bundle "
                f"(this one is type {app_type!r})"
            )
        return 0

    fmt_ver = data.get("format_version")
    if isinstance(fmt_ver, int) and fmt_ver < JUMBO_FORMAT_VERSION:
        raise DescriptorError(
            f"type: jumbo needs format_version {JUMBO_FORMAT_VERSION}, "
            f"the bundle declares {fmt_ver}"
        )

    sizes = {}
    for field in JUMBO_FIELDS:
        value = data.get(field)
        if value is None:
            raise DescriptorError(f"a jumbo bundle must declare {field}")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DescriptorError(
                f"{field} must be a whole number of GiB, got {value!r}"
            )
        sizes[field] = value

    if sizes["minimum_storage"] <= STANDARD_SLOT_GIB:
        raise DescriptorError(
            f"minimum_storage is {sizes['minimum_storage']} GiB, which fits the "
            f"{STANDARD_SLOT_GIB} GiB standard slot; ship this as a standard bundle"
        )
    if sizes["desired_storage"] < sizes["minimum_storage"]:
        raise DescriptorError(
            f"desired_storage ({sizes['desired_storage']} GiB) is below "
            f"minimum_storage ({sizes['minimum_storage']} GiB)"
        )
    return sizes["minimum_storage"]


def validate(data: dict, menu_version: str = "") -> list:
    """Validate the envelope. Returns a list of non-fatal warnings.

    Raises DescriptorError on the first fatal problem.
    """
    warnings: list = []

    if data.get("format") != FORMAT_TAG:
        raise DescriptorError(
            f"format must be {FORMAT_TAG!r}, got {data.get('format')!r}"
        )

    fmt_ver = data.get("format_version")
    if fmt_ver not in SUPPORTED_FORMAT_VERSIONS:
        raise DescriptorError(
            f"unsupported format_version {fmt_ver!r} "
            f"(supported: {SUPPORTED_FORMAT_VERSIONS})"
        )

    for key in ("id", "name", "version"):
        if not data.get(key):
            raise DescriptorError(f"missing required field {key!r}")

    min_gib = _validate_app_type(data)

    components = data.get("components")
    if not isinstance(components, list) or not components:
        raise DescriptorError("components must be a non-empty list")

    # Optional compatibility gate.
    requires = data.get("requires") or {}
    if isinstance(requires, dict):
        want = requires.get("menu_min_version") or ""
        if want and not _version_ge(menu_version, want):
            raise DescriptorError(
                f"bundle requires menu version >= {want!r}, have {menu_version or 'unknown'!r}"
            )

    # slot block sanity (optional; defaults applied elsewhere).
    slot = data.get("slot")
    if slot is not None:
        if not isinstance(slot, dict):
            raise DescriptorError("slot must be a mapping if present")
        fs = slot.get("filesystem", DEFAULT_SLOT["filesystem"])
        if fs not in ("ext4",):
            raise DescriptorError(f"slot.filesystem unsupported: {fs!r} (only ext4)")
        if "label" in slot and not str(slot["label"]).strip():
            raise DescriptorError("slot.label must be non-empty if present")

    app_image_count = 0
    for idx, comp in enumerate(components):
        if not isinstance(comp, dict):
            raise DescriptorError(f"component #{idx} must be a mapping")

        kind = comp.get("kind")
        path = comp.get("path")
        sha = comp.get("sha256")
        required = bool(comp.get("required", False))

        if not kind:
            raise DescriptorError(f"component #{idx} missing kind")
        if kind in MULTI_MEMBER_KINDS:
            # Members are named inside `layout`, one per partition, so the
            # component itself carries no single path/sha256 pair.
            _validate_layout(idx, comp, min_gib)
        else:
            if not path:
                raise DescriptorError(f"component #{idx} ({kind}) missing path")
            if not sha:
                raise DescriptorError(f"component #{idx} ({kind}) missing sha256")
            if _bad_member_path(path):
                raise DescriptorError(
                    f"component #{idx} ({kind}) has unsafe path {path!r}"
                )

        if kind in APP_IMAGE_KINDS:
            app_image_count += 1
        elif kind in INIT_KINDS:
            stage = comp.get("stage", "post-deploy")
            if stage not in VALID_STAGES:
                raise DescriptorError(
                    f"component #{idx} ({kind}) has invalid stage {stage!r} "
                    f"(valid: {VALID_STAGES})"
                )
        elif kind in VERSAL_KINDS:
            pass
        else:
            # Unknown kind: forward-compat hinge.
            if required:
                raise DescriptorError(
                    f"component #{idx} has unknown required kind {kind!r}; "
                    "this installer cannot satisfy it"
                )
            warnings.append(
                f"component #{idx} has unknown kind {kind!r} (required: false) — "
                "will be skipped"
            )

    if app_image_count != 1:
        raise DescriptorError(
            f"exactly one application-image component "
            f"({'/'.join(APP_IMAGE_KINDS)}) required, found {app_image_count}"
        )

    if data.get("boot") is not None:
        warnings.extend(_validate_boot(data["boot"]))

    return warnings


# ---- dotted get -------------------------------------------------------------

def dotted_get(data, key: str):
    cur = data
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


# ---- CLI --------------------------------------------------------------------

def _emit(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def cmd_validate(args) -> int:
    try:
        data = load(args.descriptor)
        warnings = validate(data, menu_version=args.menu_version or "")
    except DescriptorError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    print("OK")
    return 0


def cmd_get(args) -> int:
    try:
        data = load(args.descriptor)
    except DescriptorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    value = dotted_get(data, args.key)
    # Apply slot defaults so `get slot.label` works even without a slot block.
    if value is None and args.key.startswith("slot."):
        value = DEFAULT_SLOT.get(args.key.split(".", 1)[1])
    if value is None and args.key == "type":
        value = DEFAULT_APP_TYPE
    if value is None:
        return 1
    print(_emit(value))
    return 0


def cmd_components(args) -> int:
    try:
        data = load(args.descriptor)
    except DescriptorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    components = data.get("components") or []
    for idx, comp in enumerate(components):
        if args.field:
            print(f"{idx}\t{_emit(comp.get(args.field))}")
        else:
            print(_emit(comp.get("kind")))
    return 0


def cmd_members(args) -> int:
    try:
        data = load(args.descriptor)
    except DescriptorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for comp in data.get("components") or []:
        path = comp.get("path")
        if path:
            print(path)
        for part in comp.get("layout") or []:
            if part.get("path"):
                print(part["path"])
    return 0


def cmd_info(args) -> int:
    """Emit the envelope's identity and size class as JSON.

    Lets the menu learn what a bundle is before installing it — in particular
    whether it is jumbo and what it needs — from the leading appimage.yaml
    alone, without fetching the payload.
    """
    try:
        data = load(args.descriptor)
    except DescriptorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out = {
        "id": data.get("id", ""),
        "name": data.get("name", ""),
        "version": str(data.get("version", "")),
        "vendor": data.get("vendor", ""),
        "format_version": data.get("format_version"),
        "type": data.get("type", DEFAULT_APP_TYPE),
        "minimum_storage": data.get("minimum_storage"),
        "desired_storage": data.get("desired_storage"),
        "has_versal": any((c or {}).get("kind") in VERSAL_KINDS
                          for c in data.get("components") or []),
    }
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


def cmd_layout(args) -> int:
    """Emit a component's layout as JSON, for the device's region manager."""
    try:
        data = load(args.descriptor)
    except DescriptorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    components = data.get("components") or []
    if args.index is not None:
        candidates = [components[args.index]] if args.index < len(components) else []
    else:
        candidates = [c for c in components if c.get("kind") in MULTI_MEMBER_KINDS]
    if not candidates or not candidates[0].get("layout"):
        print("error: no layout found", file=sys.stderr)
        return 1
    json.dump(candidates[0]["layout"], sys.stdout, indent=2)
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("validate", help="validate the envelope")
    pv.add_argument("descriptor")
    pv.add_argument("--menu-version", default="", help="installed menu RAUC version")
    pv.set_defaults(func=cmd_validate)

    pg = sub.add_parser("get", help="print a dotted value")
    pg.add_argument("descriptor")
    pg.add_argument("key")
    pg.set_defaults(func=cmd_get)

    pc = sub.add_parser("components", help="list components / a field")
    pc.add_argument("descriptor")
    pc.add_argument("--field", help="print this field per component (TSV)")
    pc.set_defaults(func=cmd_components)

    pm = sub.add_parser("members", help="list referenced member paths")
    pm.add_argument("descriptor")
    pm.set_defaults(func=cmd_members)

    pi = sub.add_parser("info", help="identity + size class as JSON")
    pi.add_argument("descriptor")
    pi.set_defaults(func=cmd_info)

    pl = sub.add_parser("layout", help="print a disk-image component's layout as JSON")
    pl.add_argument("descriptor")
    pl.add_argument("--index", type=int, default=None)
    pl.set_defaults(func=cmd_layout)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
