# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Single source of truth for the application slot layout.

Imported by both the installer orchestrator side (via the small CLI below,
callable from shell) and the Flask app, so the device-node / label mapping is
defined in exactly one place.

Only the first application slot is used. Install is erase-and-write on app-a
(p4). Partition p5 is persistent RAVE2 bulk storage and must never be exposed
as an application install target.

An application comes in one of two sizes. A *standard* app lives in APP_REGION,
the fixed 50 GiB extent around p4. A *jumbo* app is one that declares it cannot
fit there; it lives in JUMBO_REGION, the tail of the disk after rave-data, and
reuses the same partition indices. Sharing the indices is deliberate: the two
regions are mutually exclusive, and one app is installed at a time.
"""

import os
import sys

def _part(disk: str, index: int) -> str:
    """Device node for a partition, e.g. /dev/nvme0n1 + 4 -> /dev/nvme0n1p4."""
    sep = "p" if disk[-1:].isdigit() else ""
    return f"{disk}{sep}{index}"


# The single active application slot. Derived from APP_REGION below so the slot
# device and the region can never disagree about which disk they mean.
APP_SLOT = {"slot": "a", "device": None, "label": "app-a"}

DATA_PARTITION = {"device": "/dev/nvme0n1p5", "label": "rave-data", "index": 5}

# The app *region*: the disk extent the App Store is allowed to repartition for
# a disk-image app (one that ships a whole wic and its own bootloader). It is
# exactly the extent init-install-efi.sh gives app-a, so units already in the
# field need no re-imaging: p4 is deleted and p20.. created inside its bounds.
#
def _env(name, default):
    """Environment override, treating an empty value as unset.

    A set-but-empty variable would otherwise reach int() and raise, surfacing
    as an unrelated error much further up the installer.
    """
    value = os.environ.get(name, "")
    return value.strip() if value.strip() else default


# Sectors are 512-byte and match `parted mkpart app-a ext4 33281MiB 84481MiB`.
# The three RAVE_REGION_* overrides exist so the manager can be exercised
# against a disk image instead of the boot disk; production never sets them.
APP_REGION = {
    "disk": _env("RAVE_REGION_DISK", "/dev/nvme0n1"),
    "first_sector": int(_env("RAVE_REGION_FIRST", 33281 * 2048)),
    "last_sector": int(_env("RAVE_REGION_LAST", 84481 * 2048 - 1)),
    "index_min": 20,                        # app partitions live in the 20s-40s
    "index_max": 49,
    "partlabel_prefix": "app-",
    # p4 is the rootfs-style single-partition slot. It sits inside the region
    # and must be removable, even though it is outside the 20s.
    "legacy_index": 4,
}

# The jumbo region has no fixed extent: it is whatever lies between rave-data
# and the next partition (an Ubuntu install) or the end of the disk, so
# app_region.jumbo_region() measures it from the live partition table instead of
# declaring it here. A jumbo app's partitions use the same indices as a standard
# one, which is what keeps the two mutually exclusive.
JUMBO_REGION = {
    "after_index": DATA_PARTITION["index"],
    # A jumbo app defaults to the desired_storage it declares, leaving the rest
    # of the tail free for an Ubuntu install later. The user may override that
    # with any size from the bundle's minimum_storage up to the whole tail;
    # whatever is not taken is what Ubuntu gets.
    "size_gib_override": int(_env("RAVE_JUMBO_SIZE_GIB", 0)),
    # The single partition a rootfs-style jumbo app gets, in place of p4.
    "slot_partlabel": APP_SLOT["label"],
}

# What an Ubuntu install needs from the same tail a jumbo app takes from, so the
# UI can say when a jumbo size would leave no room for Ubuntu at all. Mirrors
# MIN_UBUNTU_ROOT_GIB and SWAP_MIB in scripts/install_ubuntu.sh, which is what
# actually enforces them.
UBUNTU_MIN_ROOT_GIB = 40
UBUNTU_SWAP_GIB = 16
UBUNTU_MIN_GIB = UBUNTU_MIN_ROOT_GIB + UBUNTU_SWAP_GIB


def removable_indices() -> set:
    """Partition indices the region manager may ever delete."""
    return set(
        range(APP_REGION["index_min"], APP_REGION["index_max"] + 1)
    ) | {APP_REGION["legacy_index"]}


# Matches init-install-efi.sh (mkpart app-a -> nvme0n1p4) and
# x86_replace_application.py PARTITION_MAP.
APP_SLOT["device"] = _part(APP_REGION["disk"], APP_REGION["legacy_index"])


# Slots the installer will actually accept on the `--slot` flag.
SUPPORTED_SLOTS = ("a",)


def resolve_slot(slot: str = "a") -> dict:
    """Return the slot descriptor for ``slot``, rejecting anything unsupported.

    Only ``a`` is supported today (D1). Asking for any other slot is a hard
    error rather than silently writing the reserved partition.
    """
    if slot not in SUPPORTED_SLOTS:
        raise ValueError(
            f"unsupported app slot {slot!r}; only {SUPPORTED_SLOTS} is available"
        )
    return APP_SLOT


def _main(argv) -> int:
    """Tiny CLI so shell scripts can read the slot without duplicating it.

    Usage:
        app_slots.py device [<slot>]   # print the device node (default a)
        app_slots.py label  [<slot>]   # print the FS label
        app_slots.py slot   [<slot>]   # print the slot id
    """
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(_main.__doc__.strip())
        return 0 if (len(argv) >= 2 and argv[1] in ("-h", "--help")) else 2

    field = argv[1]
    slot = argv[2] if len(argv) > 2 else "a"
    try:
        info = resolve_slot(slot)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if field not in info:
        print(f"error: unknown field {field!r}", file=sys.stderr)
        return 2
    print(info[field])
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
