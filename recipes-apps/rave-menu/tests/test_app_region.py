#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""The two app regions, exercised against a real GPT on a sparse file.

A jumbo app lives in the disk tail after rave-data and reuses the same
partition indices as a standard one, so the pair are mutually exclusive by
construction. What matters, and what is checked here, is that the tail is
measured from the live table (an Ubuntu install bounds it, an installed app
does not), that the safety gates still refuse everything outside the app
namespace, and that installing into one region clears the other.

Host-only: sfdisk operates on the image file, so no device and no root are
needed. Needs sfdisk from util-linux.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = os.path.join(os.path.dirname(HERE), "files")

MIB = 1024 * 1024
DISK_MIB = 400 * 1024  # 400 GiB, sparse

# Mirrors init-install-efi.sh, in MiB: ESP, menu A/B, app-a, rave-data, then
# the unpartitioned tail a jumbo app is carved out of.
LAYOUT = [
    (1, "EFI", 1, 513),
    (2, "menu-a", 513, 16897),
    (3, "menu-b", 16897, 33281),
    (4, "app-a", 33281, 84481),
    (5, "rave-data", 84481, 135681),
]

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))


def make_disk(path, extra=()):
    """Write the RAVE2 GPT onto a sparse file, plus any `extra` partitions."""
    with open(path, "wb") as fh:
        fh.truncate(DISK_MIB * MIB)
    script = ["label: gpt", "unit: sectors", ""]
    for idx, name, start, end in list(LAYOUT) + list(extra):
        script.append(
            f"{path}{idx} : start={start * 2048}, "
            f"size={(end - start) * 2048}, "
            f'type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="{name}"'
        )
    subprocess.run(["sfdisk", "--no-reread", "--force", path],
                   input="\n".join(script) + "\n", text=True,
                   capture_output=True, check=True)


def load_region_module(disk):
    """Import app_region pointed at `disk` (the RAVE_REGION_* test hooks)."""
    os.environ["RAVE_REGION_DISK"] = disk
    for mod in ("app_slots", "app_region"):
        sys.modules.pop(mod, None)
    sys.path.insert(0, FILES)
    import app_region  # noqa: PLC0415 - re-imported per disk on purpose
    return app_region


if shutil.which("sfdisk") is None:
    print("  SKIP  sfdisk not available")
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="app-region-test.")
try:
    # --- no Ubuntu: the tail runs to the end of the disk ----------------------
    disk = os.path.join(tmp, "disk.img")
    make_disk(disk)
    ar = load_region_module(disk)

    standard = ar.standard_region()
    jumbo = ar.jumbo_region()
    check("the standard region is the 50 GiB extent around p4",
          ar.region_size_mib(standard) == 51200, str(ar.region_size_mib(standard)))
    check("the jumbo region starts after rave-data",
          jumbo["first_sector"] * 512 // MIB == 135681,
          str(jumbo["first_sector"] * 512 // MIB))
    check("with no Ubuntu the tail runs to the end of the disk",
          abs(ar.region_size_mib(jumbo) - (DISK_MIB - 135681)) <= 1,
          str(ar.region_size_mib(jumbo)))

    # --- Ubuntu installed: the tail stops at it -------------------------------
    disk2 = os.path.join(tmp, "disk2.img")
    make_disk(disk2, extra=[(6, "ubuntu", 285681, DISK_MIB - 16384),
                            (7, "ubuntu-swap", DISK_MIB - 16384, DISK_MIB - 1)])
    ar = load_region_module(disk2)
    jumbo = ar.jumbo_region()
    check("an Ubuntu install bounds the tail",
          abs(ar.region_size_mib(jumbo) - (285681 - 135681)) <= 1,
          str(ar.region_size_mib(jumbo)))

    # --- a jumbo app in the tail does not bound it ---------------------------
    disk3 = os.path.join(tmp, "disk3.img")
    make_disk(disk3, extra=[(20, "app-a", 135681, 235681),
                            (6, "ubuntu", 285681, DISK_MIB - 1)])
    ar = load_region_module(disk3)
    jumbo = ar.jumbo_region()
    check("an installed jumbo app is reclaimable, not a boundary",
          abs(ar.region_size_mib(jumbo) - (285681 - 135681)) <= 1,
          str(ar.region_size_mib(jumbo)))

    table = ar.read_table()
    regions = ar.all_regions(table)
    by_name = {p["name"]: p for p in table.values()}
    check("a jumbo app partition may be removed",
          ar.check_removable(by_name["app-a"], regions, allow_mounted=True) is None,
          str(ar.check_removable(by_name["app-a"], regions, allow_mounted=True)))
    check("rave-data may never be removed",
          ar.check_removable(by_name["rave-data"], regions, allow_mounted=True)
          is not None)
    check("an Ubuntu partition may never be removed",
          ar.check_removable(by_name["ubuntu"], regions, allow_mounted=True)
          is not None)
    check("a menu slot may never be removed",
          ar.check_removable(by_name["menu-a"], regions, allow_mounted=True)
          is not None)

    cap = ar.capacity()
    check("capacity reports the app tail so Ubuntu can start after it",
          cap["jumbo_installed"] and cap["app_tail_end_mib"] == 235681,
          json.dumps(cap))

    # --- mutual exclusivity: a jumbo install sweeps the standard region ------
    disk4 = os.path.join(tmp, "disk4.img")
    make_disk(disk4, extra=[(20, "app-a", 135681, 235681)])
    ar = load_region_module(disk4)
    table = ar.read_table()
    existing = {p["name"] for p in ar.region_partitions(table, ar.all_regions(table))}
    check("both regions' app partitions are swept before an install",
          {"app-a"} <= existing and "rave-data" not in existing, str(existing))
    check("p4 is included in the sweep", 4 in {
        p["index"] for p in ar.region_partitions(table, ar.all_regions(table))})

    # --- planning inside a negotiated jumbo extent ---------------------------
    region = ar.negotiate_size(ar.jumbo_region(), min_gib=90, desired_gib=140)
    planned = ar.plan_layout(
        [{"part": "app-boot", "size_mib": 4096}, {"part": "app-root", "grow": True}],
        region)
    root = planned[1]
    check("the grow partition fills the negotiated extent",
          abs((root["end"] - root["start"] + 1) * 512 // MIB
              - (140 * 1024 - 4096)) <= 2,
          str((root["end"] - root["start"] + 1) * 512 // MIB))
    check("jumbo partitions use the app indices",
          [p["index"] for p in planned] == [20, 21],
          str([p["index"] for p in planned]))

    # --- a real 30-partition layout applies through sfdisk --------------------
    disk5 = os.path.join(tmp, "disk5.img")
    make_disk(disk5)
    ar = load_region_module(disk5)
    spec = [{"part": f"app-p{i}", "size_mib": 64} for i in range(1, 31)]
    region = ar.standard_region()
    planned = ar.plan_layout(spec, region)
    check("a 30-partition layout plans indices 20..49",
          [p["index"] for p in planned] == list(range(20, 50)),
          str([p["index"] for p in planned]))
    # A real install sweeps the region's existing partition (app-a, p4) before
    # creating the new ones, since they share the same sector extent.
    subprocess.run(["sfdisk", "--no-reread", "--force", "--delete", disk5, "4"],
                   capture_output=True, check=True)
    script = ["label: gpt", "unit: sectors", ""]
    for p in planned:
        script.append(
            f'{disk5}{p["index"]} : start={p["start"]}, '
            f'size={p["end"] - p["start"] + 1}, '
            f'type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="{p["name"]}"'
        )
    result = subprocess.run(["sfdisk", "--no-reread", "--force", "--append", disk5],
                            input="\n".join(script) + "\n", text=True,
                            capture_output=True)
    check("sfdisk --append applies all 30 partitions",
          result.returncode == 0, result.stderr)
    table = ar.read_table()
    applied = sorted(idx for idx, p in table.items() if p["name"].startswith("app-"))
    check("all 30 land on-disk with indices 20..49",
          applied == list(range(20, 50)), str(applied))

    # A 31st partition must be rejected before ever touching sfdisk, via the
    # same load_layout() span check the installer calls before plan_layout().
    spec31 = spec + [{"part": "app-p31", "size_mib": 64}]
    layout_path = os.path.join(tmp, "layout31.json")
    with open(layout_path, "w") as fh:
        json.dump(spec31, fh)
    try:
        ar.load_layout(layout_path)
        check("a 31st partition is rejected", False)
    except ar.RegionError as exc:
        check("a 31st partition is rejected", "only" in str(exc) and "available" in str(exc),
              str(exc))

    # jumbo_region() has no fixed extent, so only the in-memory plan is checked.
    jumbo_planned = ar.plan_layout(spec, ar.jumbo_region())
    check("jumbo_region also plans indices 20..49 in-memory",
          [p["index"] for p in jumbo_planned] == list(range(20, 50)),
          str([p["index"] for p in jumbo_planned]))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
