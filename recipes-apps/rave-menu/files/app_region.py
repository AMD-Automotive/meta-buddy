#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""app_region.py — owns the app region of the boot disk.

A "disk-image" application ships a whole disk image (its own partition table,
its own bootloader). Rather than nesting that table inside a partition — which
Linux will not enumerate and GRUB cannot read — we *explode* it: each of the
image's partitions becomes a real GPT entry inside a reserved extent of the
RAVE2 disk, the app region (see app_slots.APP_REGION).

Everything destructive in here is gated three ways. A partition is only ever
removed when **all** of these hold:

  1. its index is one we are allowed to touch (the 20s, plus the legacy p4),
  2. its PARTLABEL is in the ``app-`` namespace,
  3. its extent lies wholly inside one of the app regions.

Anything else — the ESP, the menu slots, rave-data, an Ubuntu install — fails
the gates and the run is refused rather than guessed at.

There are two app regions and an app lives in exactly one of them. The
*standard* region is the fixed 50 GiB extent around p4. The *jumbo* region is
the tail of the disk after rave-data, up to an Ubuntu install or the end of the
disk, and is measured from the live table because its size depends on the unit.
Both use the same partition indices, so installing into one clears the other —
which is how the two stay mutually exclusive.

State lives in two places: a manifest on the menu slot for fast reads, and a
copy plus a full ``sfdisk`` table backup on rave-data, which survives a menu
slot update. The manifest is written *before* the table is touched and again
after, so an interrupted install is detectable on the next run.

Sub-commands::

    app_region.py show                      current region state
    app_region.py capacity                  JSON sizes of both regions
    app_region.py plan   <layout.json>      what apply would do (no changes)
    app_region.py apply  <layout.json> [--app-id ID --app-version V] [--dry-run]
    app_region.py verify                    on-disk table vs manifest
    app_region.py restore                   put back the pre-install table
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

import app_slots

MANIFEST = os.environ.get("RAVE_REGION_MANIFEST", "/var/lib/rave-menu/app-region.json")
STORAGE = os.environ.get("RAVE_REGION_STORAGE", "/var/lib/rave-menu/storage/app-region")
MANIFEST_BACKUP = os.path.join(STORAGE, "app-region.json")
# Rolling backup, replaced on every apply, and a one-shot backup of the table as
# it was before the *first* app install, so the original layout is recoverable
# however many times an app is reinstalled.
TABLE_BACKUP = os.path.join(STORAGE, "table-before-install.sfdisk")
TABLE_ORIGINAL = os.path.join(STORAGE, "table-original.sfdisk")

MANIFEST_VERSION = 1
SECTOR = 512
ALIGN = 2048  # 1 MiB
MIB = 1024 * 1024 // SECTOR  # sectors per MiB

STANDARD = "standard"
JUMBO = "jumbo"


class RegionError(Exception):
    """Any refusal or failure; the message is operator-facing."""


# ---- process helpers --------------------------------------------------------

def _run(cmd, dry_run=False, check=True):
    if dry_run:
        print("  would run: " + " ".join(cmd))
        return ""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RegionError(f"{cmd[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _run_input(cmd, data, dry_run=False, check=True):
    if dry_run:
        print("  would run: " + " ".join(cmd) + " <<'EOF'\n" + data + "EOF")
        return ""
    proc = subprocess.run(cmd, input=data, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RegionError(f"{cmd[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _require_tools():
    for tool in ("sfdisk", "partx"):
        if shutil.which(tool) is None:
            raise RegionError(f"{tool} not found; the menu image needs util-linux")


# The app region lives on the boot disk, which always has the ESP, rave-data and
# swap in use, so sfdisk aborts with "this disk is currently in use" and cannot
# re-read the table afterwards. Neither check earns its keep here: the gates in
# this file are stricter than sfdisk's, and _refresh_partitions() reconciles the
# kernel's view through BLKPG instead.
SFDISK_WRITE = ["sfdisk", "--no-reread", "--force"]

# GPT type GUIDs, keyed by the short codes the layout format borrows from gptfdisk.
_TYPE_GUIDS = {
    "0700": "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7",  # Microsoft basic data
    "8300": "0FC63DAF-8483-4772-8E79-3D69D8477DE4",  # Linux filesystem
    "8200": "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F",  # Linux swap
    "ef00": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",  # EFI system
}


def _type_guid(code):
    key = str(code).strip()
    if re.fullmatch(r"[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}", key):
        return key.upper()
    guid = _TYPE_GUIDS.get(key.lower())
    if guid is None:
        raise RegionError(f"unknown partition type {code!r}")
    return guid


# ---- reading the current table ----------------------------------------------

def read_table(disk=None):
    """Return {index: {start, end, name, partuuid}} for every partition."""
    disk = disk or app_slots.APP_REGION["disk"]
    out = _run(["sfdisk", "--dump", disk])
    parts = {}
    for line in out.splitlines():
        m = re.match(r"^(?P<dev>\S+?)(?P<idx>\d+)\s*:\s*(?P<fields>.*\bstart=.*)$", line)
        if not m:
            continue
        fields = m.group("fields")
        start = re.search(r"\bstart=\s*(\d+)", fields)
        size = re.search(r"\bsize=\s*(\d+)", fields)
        if not start or not size:
            continue
        name = re.search(r'\bname="([^"]*)"', fields)
        uuid = re.search(r"\buuid=\s*([0-9A-Fa-f-]+)", fields)
        idx = int(m.group("idx"))
        parts[idx] = {
            "index": idx,
            "start": int(start.group(1)),
            "end": int(start.group(1)) + int(size.group(1)) - 1,
            "name": name.group(1).strip() if name else "",
            "partuuid": uuid.group(1).upper() if uuid else "",
        }
    return parts


def part_device(index, disk=None):
    disk = disk or app_slots.APP_REGION["disk"]
    sep = "p" if re.search(r"\d$", disk) else ""
    return f"{disk}{sep}{index}"


def disk_last_sector(disk=None):
    """Last usable sector of the disk, from the GPT header sfdisk reports."""
    disk = disk or app_slots.APP_REGION["disk"]
    out = _run(["sfdisk", "--dump", disk])
    m = re.search(r"^last-lba:\s*(\d+)", out, re.MULTILINE)
    if m:
        return int(m.group(1))
    # A disk image written without a backup header has no last-lba line.
    size = _run(["blockdev", "--getsz", disk], check=False).strip()
    if size.isdigit():
        return int(size) - 34
    raise RegionError(f"could not determine the last usable sector of {disk}")


def _is_ours(part):
    """True if `part` is an app partition this manager owns, wherever it sits."""
    return (
        part["index"] in app_slots.removable_indices()
        and part["name"].startswith(app_slots.APP_REGION["partlabel_prefix"])
    )


def standard_region():
    """The fixed extent around p4 that a standard app installs into."""
    region = dict(app_slots.APP_REGION)
    region["kind"] = STANDARD
    return region


def jumbo_region(table=None, disk=None):
    """Measure the jumbo extent: the disk tail kept for oversized apps.

    It starts after rave-data and runs to the start of the next partition that
    is not ours — an Ubuntu install — or to the end of the disk when there is
    none. App partitions already installed in the tail are *not* boundaries:
    they are ours to reclaim, so a reinstall sees the same extent as the first
    install did.
    """
    disk = disk or app_slots.APP_REGION["disk"]
    table = table if table is not None else read_table(disk)
    jumbo = app_slots.JUMBO_REGION

    anchor = table.get(jumbo["after_index"])
    if anchor is None:
        raise RegionError(
            f"p{jumbo['after_index']} (rave-data) is missing; "
            "the jumbo region is measured from it"
        )
    first = anchor["end"] + 1
    first += -first % ALIGN

    last = disk_last_sector(disk)
    for part in table.values():
        if part["start"] >= first and not _is_ours(part):
            last = min(last, part["start"] - 1)
    last -= (last + 1) % ALIGN

    region = dict(app_slots.APP_REGION)
    region.update({"kind": JUMBO, "first_sector": first, "last_sector": last})
    return region


def resolve_region(kind, table=None):
    if kind == JUMBO:
        return jumbo_region(table)
    if kind in (STANDARD, None, ""):
        return standard_region()
    raise RegionError(f"unknown region {kind!r}")


def all_regions(table=None):
    """Both extents, for the deletion gate.

    A jumbo install has to clear a standard app and the other way round, so
    removal is allowed anywhere either region reaches. Creation is not: that is
    confined to the region being installed into.
    """
    regions = [standard_region()]
    try:
        regions.append(jumbo_region(table))
    except RegionError:
        # No rave-data, or no readable tail: there is simply no jumbo extent to
        # clear, which must not stop a standard install.
        pass
    return regions


def region_size_mib(region):
    return (region["last_sector"] - region["first_sector"] + 1) * SECTOR // (1024 ** 2)


def _mounted_devices():
    mounted = set()
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                mounted.add(line.split()[0])
    except OSError:
        pass
    return mounted


# ---- the safety gates -------------------------------------------------------

def check_removable(part, regions=None, allow_mounted=False):
    """Return None if `part` may be deleted, else the reason it may not be."""
    region = app_slots.APP_REGION
    regions = regions if regions is not None else all_regions()
    idx, name = part["index"], part["name"]

    if idx not in app_slots.removable_indices():
        return f"index {idx} is outside the app indices"
    if not name.startswith(region["partlabel_prefix"]):
        return f"PARTLABEL {name!r} is outside the {region['partlabel_prefix']}* namespace"
    if not any(
        part["start"] >= r["first_sector"] and part["end"] <= r["last_sector"]
        for r in regions
    ):
        extents = ", ".join(
            f"{r['kind']} {r['first_sector']}-{r['last_sector']}" for r in regions
        )
        return (
            f"extent {part['start']}-{part['end']} is not inside an app region "
            f"({extents})"
        )
    dev = part_device(idx)
    if not allow_mounted and dev in _mounted_devices():
        return f"{dev} is mounted"
    return None


def release_region(table=None, regions=None, dry_run=False):
    """Unmount anything mounted from the region's own partitions.

    Only partitions that already pass every other gate are touched, so this can
    never unmount a menu slot, the ESP or rave-data. Without it, a reinstall
    trips the mount check against the app it is replacing.
    """
    table = table if table is not None else read_table()
    regions = regions if regions is not None else all_regions(table)
    mounted = _mounted_devices()
    released = []
    for part in region_partitions(table, regions):
        if check_removable(part, regions, allow_mounted=True) is not None:
            continue
        dev = part_device(part["index"])
        if dev not in mounted:
            continue
        if dry_run:
            print(f"  would unmount {dev}")
            released.append(dev)
            continue
        print(f"  unmounting {dev}")
        if subprocess.run(["umount", dev], capture_output=True).returncode != 0:
            subprocess.run(["umount", "-l", dev], capture_output=True)
        released.append(dev)
    return released


def region_partitions(table, regions=None):
    """Partitions that overlap an app region at all, removable or not."""
    regions = regions if regions is not None else all_regions(table)
    out = []
    for part in table.values():
        if any(
            part["end"] >= r["first_sector"] and part["start"] <= r["last_sector"]
            for r in regions
        ):
            out.append(part)
    return sorted(out, key=lambda p: p["start"])


# ---- manifest ---------------------------------------------------------------

def _read_manifest(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_manifest(path=MANIFEST):
    manifest = _read_manifest(path)
    if manifest is None and path == MANIFEST:
        # The primary sits on the menu's RAUC slot, so a slot update leaves it
        # behind; the rave-data copy is written for exactly this case.
        manifest = _read_manifest(MANIFEST_BACKUP)
        if manifest is not None:
            try:
                os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
                shutil.copyfile(MANIFEST_BACKUP, MANIFEST)
            except OSError:
                pass
    return manifest


def save_manifest(manifest, dry_run=False):
    if dry_run:
        print(f"  would write manifest state={manifest['state']}")
        return
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(manifest, fh, indent=2)
    os.replace(tmp, MANIFEST)
    # rave-data copy is best effort: it survives a menu slot update, but a
    # missing/unmounted bulk store must not fail an install.
    try:
        os.makedirs(STORAGE, exist_ok=True)
        shutil.copyfile(MANIFEST, MANIFEST_BACKUP)
    except OSError:
        pass


def backup_table(dry_run=False):
    disk = app_slots.APP_REGION["disk"]
    dump = _run(["sfdisk", "--dump", disk])
    if dry_run:
        print(f"  would back up the partition table to {TABLE_BACKUP}")
        return
    try:
        os.makedirs(STORAGE, exist_ok=True)
        with open(TABLE_BACKUP, "w") as fh:
            fh.write(dump)
        if not os.path.exists(TABLE_ORIGINAL):
            with open(TABLE_ORIGINAL, "w") as fh:
                fh.write(dump)
    except OSError as exc:
        raise RegionError(f"could not write the table backup: {exc}")


# ---- layout -----------------------------------------------------------------

def load_layout(path):
    """Read and validate a layout: the ordered partitions to create."""
    try:
        with open(path) as fh:
            layout = json.load(fh)
    except (OSError, ValueError) as exc:
        raise RegionError(f"could not read layout {path}: {exc}")
    if isinstance(layout, dict):
        layout = layout.get("layout", [])
    if not isinstance(layout, list) or not layout:
        raise RegionError("layout must be a non-empty list")

    region = app_slots.APP_REGION
    span = region["index_max"] - region["index_min"] + 1
    if len(layout) > span:
        raise RegionError(f"layout has {len(layout)} partitions; only {span} indices available")

    grow = 0
    for i, part in enumerate(layout):
        if not part.get("part", "").startswith(region["partlabel_prefix"]):
            raise RegionError(
                f"layout #{i}: part name must start with {region['partlabel_prefix']!r}"
            )
        if part.get("grow"):
            grow += 1
        elif not part.get("size_mib"):
            raise RegionError(f"layout #{i}: needs either size_mib or grow")
    if grow > 1:
        raise RegionError("at most one partition may set grow")
    return layout


def negotiate_size(region, min_gib=0, desired_gib=0, size_gib=0):
    """Trim `region` to the slot size this install should get.

    ``minimum_storage`` is a hard floor: under it the app is refused outright
    rather than installed into a space it cannot run in. Above it the app takes
    the ``desired_storage`` it asked for, or the size the user chose, or
    everything there is — whichever is smallest. What is left over stays
    unallocated, which is what an Ubuntu install uses later.
    """
    available = region_size_mib(region)
    floor = int(min_gib) * 1024
    if floor and available < floor:
        raise RegionError(
            f"this application needs at least {min_gib} GiB but the {region['kind']} "
            f"region has only {available // 1024} GiB ({available} MiB) free"
        )

    if size_gib:
        want = int(size_gib) * 1024
        if floor and want < floor:
            raise RegionError(
                f"requested size {size_gib} GiB is below the {min_gib} GiB this "
                "application declares it needs"
            )
    elif desired_gib:
        want = int(desired_gib) * 1024
    else:
        want = available
    want = max(min(want, available), floor)

    trimmed = dict(region)
    last = region["first_sector"] + want * MIB - 1
    trimmed["last_sector"] = min(region["last_sector"], last - (last + 1) % ALIGN)
    return trimmed


def plan_layout(layout, region=None):
    """Assign indices and sector extents. Raises if it will not fit."""
    region = region or standard_region()
    cursor = region["first_sector"]
    last = region["last_sector"]
    planned = []

    fixed_sectors = sum(
        int(p["size_mib"]) * MIB for p in layout if not p.get("grow")
    )
    # Each partition may be padded up to ALIGN-1 sectors by rounding.
    if fixed_sectors + ALIGN * len(layout) > last - cursor + 1:
        raise RegionError(
            f"layout does not fit inside the {region['kind']} app region "
            f"({region_size_mib(region)} MiB)"
        )

    for i, part in enumerate(layout):
        index = region["index_min"] + i
        start = cursor + (-cursor % ALIGN)
        if part.get("grow"):
            end = last - (last + 1) % ALIGN
        else:
            end = start + int(part["size_mib"]) * MIB - 1
        if end > last:
            raise RegionError(
                f"{part['part']} does not fit inside the {region['kind']} app region"
            )
        planned.append(
            {
                "index": index,
                "name": part["part"],
                "start": start,
                "end": end,
                "type": part.get("type", "8300"),
                "grow": bool(part.get("grow")),
                "source": part.get("path"),
                "sha256": part.get("sha256"),
                "filesystem": part.get("filesystem"),
            }
        )
        cursor = end + 1
    return planned


# ---- the operations ---------------------------------------------------------

def _refresh_partitions(disk, expect=(), dry_run=False):
    """Make the kernel and udev catch up with a rewritten partition table.

    The app region lives on the boot disk, which always has mounted partitions
    (the ESP, rave-data, swap). The kernel therefore refuses to re-read its
    partition table, so `partprobe` and `sfdisk`'s own re-read are both no-ops
    here and report success while changing nothing. `partx --nr` goes through
    BLKPG instead, which updates one entry at a time and has no such
    restriction. Only app indices are touched, so a mistake cannot disturb a
    neighbour.
    """
    if dry_run:
        print(f"  would refresh the partition table of {disk}")
        return
    # A plain file (the test rigs use one) has no kernel partition table and no
    # device nodes, so there is nothing to refresh or wait for.
    try:
        if not stat.S_ISBLK(os.stat(disk).st_mode):
            return
    except OSError:
        return
    _run(["partprobe", disk], check=False)
    for idx in sorted(app_slots.removable_indices()):
        _run(["partx", "-d", "--nr", str(idx), disk], check=False)
    for dev in expect:
        idx = dev[len(dev.rstrip("0123456789")):]
        if idx:
            _run(["partx", "-a", "--nr", idx, disk], check=False)
    if shutil.which("udevadm"):
        _run(["udevadm", "settle", "--timeout=15"], check=False)
    for dev in expect:
        for _ in range(15):
            if os.path.exists(dev):
                break
            time.sleep(1)
        else:
            raise RegionError(f"{dev} did not appear after partitioning")


def describe_state():
    table = read_table()
    manifest = load_manifest()
    lines = []
    regions = all_regions(table)
    for region in regions:
        lines.append(
            f"{region['kind']} region: {region['disk']} sectors "
            f"{region['first_sector']}-{region['last_sector']} "
            f"({region_size_mib(region)} MiB)"
        )
    for part in region_partitions(table, regions):
        reason = check_removable(part, regions)
        state = "removable" if reason is None else f"KEEP ({reason})"
        lines.append(
            f"  p{part['index']:<3} {part['name']:<12} "
            f"{part['start']}-{part['end']}  {state}"
        )
    if manifest:
        app = manifest.get("app") or {}
        lines.append(
            f"manifest: state={manifest.get('state')} "
            f"region={manifest.get('region_kind', STANDARD)} "
            f"app={app.get('id', '-')} {app.get('version', '')}".rstrip()
        )
        if manifest.get("state") == "installing":
            lines.append("  WARNING: a previous install did not complete")
    else:
        lines.append("manifest: none (no disk-image app installed by us)")
    return "\n".join(lines)


def capacity():
    """Sizes of both regions as JSON, for the UI and the install pre-checks.

    ``jumbo_free_mib`` is what a jumbo app could take *now* — the whole tail,
    including any app partitions already installed in it, because installing
    over them reclaims their space.
    """
    table = read_table()
    standard = standard_region()
    out = {
        "disk": standard["disk"],
        "standard_mib": region_size_mib(standard),
        "jumbo_mib": 0,
        "jumbo_first_mib": 0,
        "jumbo_installed": False,
        "ubuntu_installed": False,
        "ubuntu_min_gib": app_slots.UBUNTU_MIN_GIB,
        "app_tail_end_mib": 0,
    }
    try:
        jumbo = jumbo_region(table)
    except RegionError as exc:
        out["error"] = str(exc)
        return out

    out["jumbo_mib"] = region_size_mib(jumbo)
    out["jumbo_first_mib"] = jumbo["first_sector"] * SECTOR // (1024 ** 2)
    out["disk_end_mib"] = (disk_last_sector(standard["disk"]) + 1) * SECTOR // (1024 ** 2)
    tail_end = jumbo["first_sector"] - 1
    for part in table.values():
        if part["start"] < jumbo["first_sector"]:
            continue
        if _is_ours(part):
            out["jumbo_installed"] = True
            tail_end = max(tail_end, part["end"])
        else:
            out["ubuntu_installed"] = True
    # Where a later Ubuntu install has to start so it cannot overwrite a jumbo
    # app sitting at the front of the tail.
    out["app_tail_end_mib"] = (tail_end + 1) * SECTOR // (1024 ** 2)
    return out


def apply_layout(layout_path, app_id="", app_version="", dry_run=False, force=False,
                 region_kind=STANDARD, min_gib=0, desired_gib=0, size_gib=0):
    _require_tools()
    disk = app_slots.APP_REGION["disk"]
    layout = load_layout(layout_path)
    table = read_table()
    regions = all_regions(table)
    region = resolve_region(region_kind, table)
    if region["kind"] == JUMBO:
        region = negotiate_size(
            region, min_gib, desired_gib,
            size_gib or app_slots.JUMBO_REGION["size_gib_override"],
        )
        print(
            f"jumbo region: {region_size_mib(region)} MiB of "
            f"{region_size_mib(jumbo_region(table))} MiB available"
        )
    planned = plan_layout(layout, region)

    # A reinstall replaces the app that is currently mounted; let go of it first,
    # or the mount gate below will (correctly) refuse. Both regions are swept:
    # installing a jumbo app has to clear a standard one, and the other way
    # round, since they share partition indices.
    release_region(table, regions, dry_run=dry_run)

    existing = region_partitions(table, regions)
    refusals = [
        (p, check_removable(p, regions)) for p in existing
        if check_removable(p, regions) is not None
    ]
    if refusals:
        detail = "; ".join(f"p{p['index']} {reason}" for p, reason in refusals)
        raise RegionError(f"refusing to touch the region: {detail}")

    manifest = load_manifest()
    if manifest and manifest.get("state") == "installing" and not force:
        print("note: previous install did not complete; re-applying over it")

    if manifest and not force:
        recorded = {p["index"] for p in manifest.get("partitions", [])}
        actual = {p["index"] for p in existing}
        # An interrupted install records partitions it never managed to create,
        # so its manifest describes an intent, not the disk. Refusing on that
        # would make a half-applied region impossible to retry.
        if manifest.get("state") == "installed" and recorded and recorded != actual:
            raise RegionError(
                f"on-disk partitions {sorted(actual)} do not match the manifest "
                f"{sorted(recorded)}; refusing (use --force to override)"
            )

    print(f"region: {disk} sectors {region['first_sector']}-{region['last_sector']}")
    for part in existing:
        print(f"  remove p{part['index']} ({part['name']})")
    for part in planned:
        size = (part["end"] - part["start"] + 1) * SECTOR // (1024 * 1024)
        print(f"  create p{part['index']} {part['name']} {size} MiB"
              + ("  [grow]" if part["grow"] else ""))

    backup_table(dry_run=dry_run)

    in_progress = {
        "version": MANIFEST_VERSION,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disk": disk,
        "region_kind": region["kind"],
        "region": {
            "first_sector": region["first_sector"],
            "last_sector": region["last_sector"],
        },
        "state": "installing",
        "app": {"id": app_id, "version": app_version},
        "partitions": [
            {k: p[k] for k in ("index", "name", "start", "end")} for p in planned
        ],
    }
    save_manifest(in_progress, dry_run=dry_run)

    if existing:
        _run(
            SFDISK_WRITE + ["--delete", disk] + [str(p["index"]) for p in existing],
            dry_run=dry_run,
        )
    _run_input(
        SFDISK_WRITE + ["--append", disk],
        "".join(
            f"{part_device(p['index'], disk)} : start={p['start']}, "
            f"size={p['end'] - p['start'] + 1}, type={_type_guid(p['type'])}, "
            f'name="{p["name"]}"\n'
            for p in planned
        ),
        dry_run=dry_run,
    )
    _refresh_partitions(
        disk, expect=[part_device(p["index"]) for p in planned], dry_run=dry_run
    )

    if not dry_run:
        after = read_table()
        for part in planned:
            got = after.get(part["index"])
            if not got or got["start"] != part["start"]:
                raise RegionError(
                    f"p{part['index']} was not created as planned; "
                    f"restore with: app_region.py restore"
                )
            part["partuuid"] = got["partuuid"]
            part["device"] = part_device(part["index"])

    final = dict(in_progress)
    final["state"] = "installed"
    final["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    final["partitions"] = [
        {
            k: p.get(k)
            for k in ("index", "name", "start", "end", "partuuid", "device",
                      "filesystem", "sha256", "grow")
        }
        for p in planned
    ]
    save_manifest(final, dry_run=dry_run)
    return final


def devices():
    """TSV of the installed partitions, for `while read` loops in shell.

    Columns: name, device, partuuid, filesystem, grow.
    """
    manifest = load_manifest()
    if not manifest or manifest.get("state") != "installed":
        raise RegionError("no completed region install to report")
    rows = []
    for part in manifest.get("partitions", []):
        rows.append(
            "\t".join(
                [
                    part.get("name", ""),
                    part.get("device") or part_device(part["index"]),
                    part.get("partuuid") or "",
                    part.get("filesystem") or "",
                    "grow" if part.get("grow") else "",
                ]
            )
        )
    return "\n".join(rows)


def verify():
    manifest = load_manifest()
    if not manifest:
        raise RegionError("no manifest; nothing to verify")
    table = read_table()
    problems = []
    for want in manifest.get("partitions", []):
        got = table.get(want["index"])
        if not got:
            problems.append(f"p{want['index']} is missing")
            continue
        for field in ("name", "start", "end"):
            if want.get(field) is not None and got[field] != want[field]:
                problems.append(
                    f"p{want['index']} {field}: manifest {want[field]!r} != disk {got[field]!r}"
                )
    if manifest.get("state") != "installed":
        problems.append(f"manifest state is {manifest.get('state')!r}, not 'installed'")
    if problems:
        raise RegionError("; ".join(problems))
    return f"region matches the manifest ({len(manifest.get('partitions', []))} partitions)"


def restore_legacy(dry_run=False):
    """Put the region back to the single `app-a` partition at the legacy index.

    A disk-image app replaces p4 with its own partitions in the 20s, and a jumbo
    app puts them in the disk tail instead. The rootfs-style kinds expect p4 to
    exist, so installing one of those after either has to reverse the layout
    first — which means clearing app partitions wherever they landed.
    """
    _require_tools()
    region = standard_region()
    disk = region["disk"]
    legacy = region["legacy_index"]

    table = read_table()
    regions = all_regions(table)
    release_region(table, regions, dry_run=dry_run)
    existing = region_partitions(table, regions)

    if [p["index"] for p in existing] == [legacy]:
        return f"region already holds only p{legacy}"

    refusals = [
        (p, check_removable(p, regions)) for p in existing
        if check_removable(p, regions) is not None
    ]
    if refusals:
        detail = "; ".join(f"p{p['index']} {reason}" for p, reason in refusals)
        raise RegionError(f"refusing to touch the region: {detail}")

    backup_table(dry_run=dry_run)
    if existing:
        for part in existing:
            print(f"  remove p{part['index']} ({part['name']})")
        _run(
            SFDISK_WRITE + ["--delete", disk] + [str(p["index"]) for p in existing],
            dry_run=dry_run,
        )
    print(f"  create p{legacy} app-a across the whole region")
    _run_input(
        SFDISK_WRITE + ["--append", disk],
        f"{part_device(legacy, disk)} : start={region['first_sector']}, "
        f"size={region['last_sector'] - region['first_sector'] + 1}, "
        f'type={_type_guid("8300")}, name="app-a"\n',
        dry_run=dry_run,
    )
    _refresh_partitions(disk, expect=[part_device(legacy)], dry_run=dry_run)

    save_manifest(
        {
            "version": MANIFEST_VERSION,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "disk": disk,
            "region_kind": region["kind"],
            "region": {
                "first_sector": region["first_sector"],
                "last_sector": region["last_sector"],
            },
            "state": "legacy",
            "app": {},
            "partitions": [],
        },
        dry_run=dry_run,
    )
    return f"region restored to the legacy p{legacy} layout"


def restore(dry_run=False, original=False):
    _require_tools()
    source = TABLE_ORIGINAL if original else TABLE_BACKUP
    if not os.path.exists(source):
        raise RegionError(f"no table backup at {source}")
    disk = app_slots.APP_REGION["disk"]
    print(f"restoring the partition table of {disk} from {source}")
    if dry_run:
        print(f"  would run: sfdisk {disk} < {source}")
        return "dry run"
    release_region()
    with open(source) as fh:
        proc = subprocess.run(["sfdisk", disk], stdin=fh, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RegionError(f"sfdisk restore failed: {proc.stderr.strip()}")
    _refresh_partitions(disk)
    save_manifest(
        {
            "version": MANIFEST_VERSION,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "disk": disk,
            "state": "restored",
            "app": {},
            "partitions": [],
        }
    )
    return "restored"


# ---- CLI --------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="print the current region state")
    sub.add_parser("capacity", help="JSON sizes of the standard and jumbo regions")

    def _region_args(p):
        p.add_argument("--region", choices=(STANDARD, JUMBO), default=STANDARD)
        p.add_argument("--min-gib", type=int, default=0,
                       help="the bundle's minimum_storage; refuse below it")
        p.add_argument("--desired-gib", type=int, default=0,
                       help="the bundle's desired_storage; the default size")
        p.add_argument("--size-gib", type=int, default=0,
                       help="user override, from --min-gib up to the whole tail")

    p_plan = sub.add_parser("plan", help="show what apply would do")
    p_plan.add_argument("layout")
    _region_args(p_plan)

    p_apply = sub.add_parser("apply", help="repartition the region from a layout")
    p_apply.add_argument("layout")
    p_apply.add_argument("--app-id", default="")
    p_apply.add_argument("--app-version", default="")
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.add_argument("--force", action="store_true",
                         help="proceed even if the disk disagrees with the manifest")
    _region_args(p_apply)

    sub.add_parser("verify", help="check the on-disk table against the manifest")
    sub.add_parser("devices", help="TSV of installed partitions for shell use")

    p_legacy = sub.add_parser(
        "legacy", help="put the region back to the single app-a partition"
    )
    p_legacy.add_argument("--dry-run", action="store_true")
    p_restore = sub.add_parser("restore", help="restore a backed-up table")
    p_restore.add_argument("--dry-run", action="store_true")
    p_restore.add_argument("--original", action="store_true",
                           help="restore the table as it was before the first app install")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "show":
            print(describe_state())
        elif args.cmd == "capacity":
            json.dump(capacity(), sys.stdout, indent=2)
            print()
        elif args.cmd == "plan":
            apply_layout(args.layout, dry_run=True, region_kind=args.region,
                         min_gib=args.min_gib, desired_gib=args.desired_gib,
                         size_gib=args.size_gib)
        elif args.cmd == "apply":
            result = apply_layout(
                args.layout,
                app_id=args.app_id,
                app_version=args.app_version,
                dry_run=args.dry_run,
                force=args.force,
                region_kind=args.region,
                min_gib=args.min_gib,
                desired_gib=args.desired_gib,
                size_gib=args.size_gib,
            )
            if not args.dry_run:
                print("region ready:")
                for part in result["partitions"]:
                    print(f"  {part['device']}  {part['name']}  PARTUUID={part['partuuid']}")
        elif args.cmd == "verify":
            print(verify())
        elif args.cmd == "devices":
            print(devices())
        elif args.cmd == "legacy":
            print(restore_legacy(dry_run=args.dry_run))
        elif args.cmd == "restore":
            print(restore(dry_run=args.dry_run, original=args.original))
    except RegionError as exc:
        print(f"app_region: ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
