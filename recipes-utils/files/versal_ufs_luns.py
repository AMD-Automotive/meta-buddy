#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Host-side UFS LUN layout: queries, size planning and the baseline store.

The target validates every plan again before touching anything. This module
exists so the operator sees an accurate preview — including the sizes after
rounding — without a round trip, and so a bad plan is rejected before the
board is taken down.

Nothing here hard-codes a device limit. Allocation unit size, LUN count,
capacity and capacity adjustment factors all come from the layout report.
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

MODULE_DIRS = [Path(__file__).resolve().parent, Path("/usr/lib/versal")]
for module_dir in MODULE_DIRS:
    module_dir_str = str(module_dir)
    if module_dir.exists() and module_dir_str not in sys.path:
        sys.path.insert(0, module_dir_str)

from versal_payload_constants import UFS_BASELINE_DIR  # noqa: E402

SIZE_SUFFIXES = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}

# Per-LUN configuration descriptor fields a plan may carry. Anything not
# stated is taken from the LUN's current configuration, so a plan that only
# changes sizes cannot flatten a boot LUN's enhanced-memory shape.
DESCRIPTOR_FIELDS = (
    "boot_lun_id",
    "memory_type",
    "data_reliability",
    "write_protect",
    "logical_block_size_exp",
    "provisioning_type",
)


class PlanError(ValueError):
    """The requested layout cannot be built on this device."""


# ---------------------------------------------------------------------------
#  Known factory layouts
#
#  Read from two RAVE2 boards with different UFS serials; both returned the
#  same 230 bytes, so for this part the layout is a property of the part and
#  not of the board. That is what makes "restore factory default" a real
#  guarantee rather than "restore whatever this board was first seen with".
#
#  A device only matches on product, manufacturer and full geometry, because a
#  descriptor from a different capacity or allocation unit size would be
#  actively harmful.
# ---------------------------------------------------------------------------

FACTORY_LAYOUTS = (
    {
        "product": "KLUEG8UHGC-B0E1",
        "manufacturer_id": 0x01CE,
        "au_bytes": 4 * 1024 ** 2,
        "budget_alloc_units": 61022,
        "max_number_lu": 8,
        "sha256": "02e9edf242e07cee268ed5b9b698901a80391b4980b12544de71488ee8250eb3",
        # LUN 0 of 61016 AU, plus 4 MiB enhanced-memory boot LUNs A and B.
        "hex": (
            "e601000100017f000000000000000000"
            "000000000000010000000000ee58000c"
            "02000000000000000000000000000000"
            "0101000300000003010c020000000000"
            "00000000000000000000010200030000"
            "0003010c020000000000000000000000"
            "000000000000000000000000000c0000"
            "00000000000000000000000000000000"
            "000000000000000c0000000000000000"
            "00000000000000000000000000000000"
            "000c0000000000000000000000000000"
            "00000000000000000000000c00000000"
            "00000000000000000000000000000000"
            "00000000000c00000000000000000000"
            "000000000000"
        ),
    },
)


def factory_descriptor(report):
    """The known factory descriptor for this device, or None.

    Matching is deliberately strict: a descriptor that does not belong to this
    exact part and geometry would re-provision the device to a layout it
    cannot hold.
    """
    product = (report.get("product") or "").strip()
    geometry = report.get("geometry") or {}
    for entry in FACTORY_LAYOUTS:
        if product != entry["product"]:
            continue
        if report.get("manufacturer_id") != entry["manufacturer_id"]:
            continue
        if any(geometry.get(key) != entry[key] for key in
               ("au_bytes", "budget_alloc_units", "max_number_lu")):
            continue
        raw = bytes.fromhex(entry["hex"])
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise PlanError(
                f"the built-in factory layout for {product} is corrupt")
        current_len = len(report.get("config_descriptor_hex", "")) // 2
        if current_len and current_len != len(raw):
            continue
        return raw
    return None


def parse_size(text):
    """Parse 32G, 512M, 0x100000 or a plain byte count. 'rest' passes through."""
    text = str(text).strip()
    if text.lower() == "rest":
        return "rest"
    if not text:
        raise PlanError("empty size")
    suffix = text[-1].lower()
    if suffix in SIZE_SUFFIXES:
        number, scale = text[:-1], SIZE_SUFFIXES[suffix]
    else:
        number, scale = text, 1
    try:
        value = int(number, 0)
    except ValueError:
        raise PlanError(f"cannot parse size {text!r}")
    if value <= 0:
        raise PlanError(f"size must be positive: {text!r}")
    return value * scale


def parse_boot_slots(text):
    """Parse a --ufs-boot-luns value into slot indices for build_plan.

    None means "leave the boot LUNs alone". 'none' means the opposite: manage
    them, and end up with none. Positions are 1-based to match how the sizes
    are written on the command line.
    """
    if text is None:
        return None
    text = str(text).strip()
    if not text or text.lower() == "none":
        return []
    slots = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            position = int(item, 0)
        except ValueError:
            raise PlanError(f"cannot parse boot LUN position {item!r}")
        if position < 1:
            raise PlanError(f"boot LUN positions are 1-based, got {position}")
        slots.append(position - 1)
    return slots


def human_bytes(value):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{value} B"
        value /= 1024
    return f"{value:.2f} TiB"


def boot_luns(report):
    """LUNs currently carrying a non-zero bBootLunID."""
    return {entry["lun"]: entry for entry in report["luns"]
            if entry["enabled"] and entry["boot_lun_id"]}


def max_boot_luns(report):
    """How many boot LUNs this device can have.

    The target reports it; the fallback is the spec limit, since bBootLunID
    only encodes Boot LUN A and Boot LUN B.
    """
    return report.get("max_boot_luns", 2)


def available_alloc_units(report, manage_boot=False):
    """Allocation units a plan may spend.

    Boot LUNs only come out of the budget when the plan is leaving them alone;
    a plan that manages them is choosing their size itself.
    """
    if manage_boot:
        return report["geometry"]["budget_alloc_units"]
    reserved = sum(entry["num_alloc_units"] for entry in boot_luns(report).values())
    return report["geometry"]["budget_alloc_units"] - reserved


def assignable_luns(report, manage_boot=False):
    """LUN indices a plan may use, in order."""
    reserved = {} if manage_boot else boot_luns(report)
    return [lun for lun in range(report["geometry"]["max_number_lu"])
            if lun not in reserved]


def build_plan(report, entries, manage_boot=True):
    """Turn per-LUN entries into a plan the target will accept.

    Each entry names its own LUN, so indices survive exactly as given and a
    gap in the numbering stays a gap. Descriptor fields the caller does not
    state are copied from that LUN's current configuration, so a plan that
    only moves capacity around changes nothing else.

    Sizes are validated against the live geometry here and again on the
    target; this copy exists to fail fast and to show the rounded result.
    """
    geometry = report["geometry"]
    au = geometry["au_bytes"]
    max_luns = geometry["max_number_lu"]
    current = {entry["lun"]: entry for entry in report["luns"]}

    wanted = []
    seen = set()
    for item in entries:
        lun = item.get("lun")
        if not isinstance(lun, int) or not 0 <= lun < max_luns:
            raise PlanError(f"invalid LUN index {lun!r}; this device has {max_luns}")
        if lun in seen:
            raise PlanError(f"LUN {lun} named twice")
        seen.add(lun)
        wanted.append(dict(item))

    if not wanted:
        raise PlanError("a plan must name at least one LUN")

    boot_ids = [item.get("boot_lun_id", 0) for item in wanted]
    boot_ids = [b for b in boot_ids if b]
    limit = max_boot_luns(report)
    if len(boot_ids) > limit:
        raise PlanError(f"{len(boot_ids)} boot LUNs requested, the device allows {limit}")
    if len(set(boot_ids)) != len(boot_ids):
        raise PlanError("Boot LUN A and Boot LUN B may each be assigned once")
    for boot_id in boot_ids:
        if boot_id not in (1, 2):
            raise PlanError(f"invalid bBootLunID {boot_id}; only 1 and 2 exist")

    if sum(1 for item in wanted if item.get("size") == "rest") > 1:
        raise PlanError("at most one LUN may be sized 'rest'")

    budget = available_alloc_units(report, manage_boot)
    used = 0
    plan_luns = []
    for item in sorted(wanted, key=lambda i: i["lun"]):
        lun = item["lun"]
        base = current.get(lun, {})
        entry = {"lun": lun, "enabled": True}
        # Only a plan that manages boot LUNs may state descriptor fields; the
        # target refuses them otherwise, and preserves them itself regardless.
        if manage_boot:
            for field in DESCRIPTOR_FIELDS:
                value = item.get(field, base.get(field))
                if value is not None:
                    entry[field] = value
            entry.setdefault("boot_lun_id", 0)

        size = item.get("size")
        if size == "rest":
            entry["size_bytes"] = "rest"
        else:
            if not isinstance(size, int) or size <= 0:
                raise PlanError(f"LUN {lun} has an invalid size {size!r}")
            units = size // au
            if units == 0:
                raise PlanError(
                    f"LUN {lun}: {human_bytes(size)} rounds down to zero "
                    f"allocation units of {human_bytes(au)}")
            used += units
            entry["size_bytes"] = units * au
        plan_luns.append(entry)

    if used > budget:
        raise PlanError(
            f"plan needs {used} allocation units ({human_bytes(used * au)}) but only "
            f"{budget} are available ({human_bytes(budget * au)})")

    plan = {"schema": 1, "token": report.get("token", 0), "luns": plan_luns}
    if manage_boot:
        plan["manage_boot_luns"] = True
    return plan


def plan_from_file(report, path):
    """Build a plan from a JSON file naming each LUN explicitly.

    This is the route the GUI uses: the per-LUN fields do not survive being
    flattened into command line arguments.
    """
    try:
        with open(path) as handle:
            document = json.load(handle)
    except (OSError, ValueError) as error:
        raise PlanError(f"cannot read plan {path}: {error}")

    entries = document.get("luns")
    if not isinstance(entries, list):
        raise PlanError("plan file has no 'luns' list")

    parsed = []
    for item in entries:
        if not isinstance(item, dict):
            raise PlanError(f"plan entry {item!r} is not an object")
        entry = dict(item)
        if "size" in entry and not isinstance(entry["size"], int):
            entry["size"] = parse_size(entry["size"])
        parsed.append(entry)

    manage_boot = bool(document.get("manage_boot_luns", True))
    return build_plan(report, parsed, manage_boot)


def plan_from_sizes(report, sizes, boot_slots=None):
    """Positional convenience for --ufs-sizes: assign LUN indices in order.

    Keeps the command line simple for the common case of "lay the device out
    like this", where the caller does not care which index each size lands on.
    """
    manage_boot = boot_slots is not None
    usable = assignable_luns(report, manage_boot)
    if len(sizes) > len(usable):
        raise PlanError(
            f"{len(sizes)} LUNs requested but only {len(usable)} are assignable "
            f"({len(boot_luns(report))} reserved for boot)")

    boot_slots = list(boot_slots or [])
    for slot in boot_slots:
        if not isinstance(slot, int) or slot < 0 or slot >= len(sizes):
            raise PlanError(f"boot LUN slot {slot!r} is outside the plan")
    if len(set(boot_slots)) != len(boot_slots):
        raise PlanError("a LUN may only be named once as a boot LUN")

    # Boot LUN A before Boot LUN B, in the order the caller listed them.
    boot_id_for = {slot: index + 1 for index, slot in enumerate(boot_slots)}

    entries = []
    for index, size in enumerate(sizes):
        entry = {"lun": usable[index], "size": size}
        if index in boot_id_for:
            entry["boot_lun_id"] = boot_id_for[index]
        elif manage_boot:
            entry["boot_lun_id"] = 0
        entries.append(entry)
    return build_plan(report, entries, manage_boot)


def format_report(report):
    """One-screen summary of a device's current layout and limits."""
    geometry = report["geometry"]
    lines = [
        f"UFS {report['spec_version']}  {report.get('product') or ''} "
        f"serial {report.get('serial') or '(unknown)'}",
        f"  BSG node            {report['bsg']}",
        f"  Provisioning lock   {report['config_descr_lock']} "
        f"({'LOCKED - layout can never be changed' if report['config_descr_lock'] else 'unlocked'})",
        f"  Max LUNs            {geometry['max_number_lu']}",
        f"  Allocation unit     {human_bytes(geometry['au_bytes'])}",
        f"  Raw capacity        {human_bytes(geometry['total_raw_bytes'])}",
        f"  Allocatable         {geometry['budget_alloc_units']} AU "
        f"({human_bytes(geometry['budget_bytes'])})",
        f"  Active boot LUN     {report['boot_lun_en']}",
        "",
        f"  {'LUN':<5}{'State':<10}{'AU':>10}  {'Capacity':>12}  {'Type':<10}{'Boot':<6}{'Device':<8}",
    ]
    for entry in report["luns"]:
        if not entry["enabled"]:
            continue
        kind = "enhanced" if entry["memory_type"] else "normal"
        lines.append(
            f"  {entry['lun']:<5}{'enabled':<10}{entry['num_alloc_units']:>10}  "
            f"{human_bytes(entry['capacity_bytes']):>12}  {kind:<10}"
            f"{entry['boot_lun_id']:<6}{entry.get('block_device') or '-':<8}"
        )
    disabled = [str(e["lun"]) for e in report["luns"] if not e["enabled"]]
    if disabled:
        lines.append(f"  disabled: {', '.join(disabled)}")
    return "\n".join(lines)


def format_preview(preview):
    """Summary of the layout a validated plan would produce."""
    lines = [
        f"  Allocation unit     {human_bytes(preview['allocation_unit_bytes'])}",
        f"  Allocatable         {preview['budget_alloc_units']} AU",
        f"  Plan uses           {preview['used_alloc_units']} AU",
        "",
        f"  {'LUN':<5}{'State':<10}{'AU':>10}  {'Capacity':>12}  Note",
    ]
    for entry in preview["luns"]:
        boot_id = entry.get("boot_lun_id") or 0
        note = f"boot LUN {'AB'[boot_id - 1]}" if boot_id else ""
        state = "enabled" if entry["enabled"] else "disabled"
        lines.append(
            f"  {entry['lun']:<5}{state:<10}{entry['num_alloc_units']:>10}  "
            f"{human_bytes(entry['capacity_bytes']):>12}  {note}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Baseline store
#
#  The fallback for a part with no entry in FACTORY_LAYOUTS: the first layout
#  a device is ever seen with is recorded and written back verbatim. It is
#  weaker than a known factory layout, because a board that was re-provisioned
#  before its first scan records that layout instead and nothing afterwards
#  can tell.
# ---------------------------------------------------------------------------

def restore_descriptor(report, directory=UFS_BASELINE_DIR):
    """The bytes "restore factory default" should write, and where they came from.

    The built-in factory layout wins when the part is known, because a
    captured baseline is only ever "what this board looked like the first time
    anyone asked" -- which is the factory layout only if nobody re-provisioned
    it first, and there is no way to tell after the fact.

    Returns (descriptor, source) where source is 'factory' or 'baseline', or
    (None, None) when neither is available.
    """
    known = factory_descriptor(report)
    if known is not None:
        return known, "factory"
    captured = load_baseline(report.get("serial"), directory)
    if captured is not None:
        return captured, "baseline"
    return None, None


def baseline_paths(serial, directory=UFS_BASELINE_DIR):
    safe = "".join(ch for ch in (serial or "") if ch.isalnum() or ch in "-_")
    if not safe:
        return None, None
    base = Path(directory) / safe
    return base.with_suffix(".bin"), base.with_suffix(".json")


def capture_baseline(report, directory=UFS_BASELINE_DIR):
    """Record this device's layout if it has never been seen before.

    Never overwrites: a baseline captured after someone has already
    re-provisioned a board would enshrine the wrong layout as its default.
    Returns (path, created).
    """
    binary, meta = baseline_paths(report.get("serial"), directory)
    if binary is None:
        return None, False
    if binary.exists():
        return binary, False
    try:
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(bytes.fromhex(report["config_descriptor_hex"]))
        meta.write_text(json.dumps({
            "serial": report.get("serial"),
            "product": report.get("product"),
            "spec_version": report.get("spec_version"),
            "captured_utc": datetime.now(timezone.utc).isoformat(),
            "luns": report["luns"],
            "config_descriptor_hex": report["config_descriptor_hex"],
        }, indent=2))
    except OSError as error:
        print(f"WARNING: could not store UFS baseline: {error}")
        return None, False
    return binary, True


def load_baseline(serial, directory=UFS_BASELINE_DIR):
    binary, _ = baseline_paths(serial, directory)
    if binary is None or not binary.exists():
        return None
    try:
        return binary.read_bytes()
    except OSError:
        return None
