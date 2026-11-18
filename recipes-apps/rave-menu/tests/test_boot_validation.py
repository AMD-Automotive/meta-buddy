#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Validation of the descriptor `boot` block, and the menu-version gate.

Two things are covered:

  * boot.cmdline / boot.entries_cfg shape. The rendered entry ends up inside a
    GRUB script, so the character rules must match what adopt_boot_entry()
    refuses on the device — if authoring is more permissive than install, a
    bundle passes packing and then fails on the board.

  * _ver_tuple(). Menu releases look like 2026-09-07_075784. Reading only the
    leading digits of each dot-separated chunk collapsed every one of those to
    (2026,), so requires.menu_min_version never rejected anything.

Host-only, no device needed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "files"))
from descriptor_tool import (  # noqa: E402
    DescriptorError, _validate_boot, _version_ge,
)

PASS = FAIL = 0


def ok(name, boot, expect_warnings=0):
    global PASS, FAIL
    try:
        warnings = _validate_boot(boot)
    except DescriptorError as exc:
        FAIL += 1
        print(f"  FAIL  {name}\n        unexpectedly rejected: {exc}")
        return
    if len(warnings) != expect_warnings:
        FAIL += 1
        print(f"  FAIL  {name}\n        expected {expect_warnings} warning(s), got {warnings}")
        return
    PASS += 1
    print(f"  PASS  {name}")


def rejected(name, boot, because=""):
    global PASS, FAIL
    try:
        _validate_boot(boot)
    except DescriptorError as exc:
        if because and because not in str(exc):
            FAIL += 1
            print(f"  FAIL  {name}\n        rejected, but for the wrong reason: {exc}")
            return
        PASS += 1
        print(f"  PASS  {name}")
        return
    FAIL += 1
    print(f"  FAIL  {name}\n        was accepted but should not be")


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


print("boot block:")
ok("a plain label-only boot block", {"label": "X", "make_default": False})
ok("entries_cfg with an entry name",
   {"entries_cfg": "/isolinux/isolinux.cfg", "entry": "boot"})
ok("a full cmdline", {"cmdline": {
    "hypervisor": "/boot/xen.gz", "hypervisor_args": "dom0_mem=32G",
    "kernel": "/boot/bzImage", "kernel_args": "console=hvc0", "ramdisk": "/boot/initrd"}})
ok("a bare-metal cmdline (no hypervisor)",
   {"cmdline": {"kernel": "/boot/bzImage", "kernel_args": "quiet"}})
ok("unknown cmdline keys warn rather than fail",
   {"cmdline": {"kernel": "/boot/bzImage", "modules": ["/a"]}}, expect_warnings=1)

rejected("entries_cfg and cmdline together",
         {"entries_cfg": "/x.cfg", "cmdline": {"kernel": "/boot/bzImage"}},
         because="one or the other")
rejected("cmdline with no kernel", {"cmdline": {"kernel_args": "quiet"}},
         because="needs a 'kernel'")
rejected("hypervisor_args with no hypervisor",
         {"cmdline": {"kernel": "/boot/bzImage", "hypervisor_args": "dom0_mem=32G"}},
         because="nothing to apply to")
rejected("relative kernel path",
         {"cmdline": {"kernel": "boot/bzImage"}}, because="must be absolute")
rejected("path escaping with ..",
         {"cmdline": {"kernel": "/boot/../../etc/shadow"}}, because="unsafe path")
rejected("boot that is not a mapping", ["nope"], because="must be a mapping")
rejected("empty entry name", {"entries_cfg": "/x.cfg", "entry": ""},
         because="non-empty string")

print("\ninstaller-owned arguments:")
for reserved in ("root=PARTUUID=abc", "rauc.slot=a"):
    rejected(f"kernel_args may not set {reserved.split('=')[0]}=",
             {"cmdline": {"kernel": "/boot/bzImage", "kernel_args": f"quiet {reserved}"}},
             because="installer owns it")

print("\ncharacters the device refuses (must match adopt_boot_entry):")
for ch in ('"', "'", "`", "$", "{", "}", "\\", "\r", "\n"):
    rejected(f"kernel_args containing {ch!r}",
             {"cmdline": {"kernel": "/boot/bzImage", "kernel_args": f"a{ch}b"}},
             because="refuses")
rejected("an over-long argument string",
         {"cmdline": {"kernel": "/boot/bzImage", "kernel_args": "x" * 513}},
         because="max")

print("\nmenu version gate:")
check("a January menu does NOT satisfy a September requirement",
      not _version_ge("2026-01-01_000000", "2026-09-07_075784"))
check("the same release satisfies itself",
      _version_ge("2026-09-07_075784", "2026-09-07_075784"))
check("a later menu satisfies an earlier requirement",
      _version_ge("2026-10-01_aaaaaa", "2026-09-07_075784"))
check("the build sha does not order same-day releases",
      _version_ge("2026-09-07_000111", "2026-09-07_999999"))
check("an empty requirement always passes", _version_ge("2026-09-07_075784", ""))
check("an unknown menu version fails a real requirement",
      not _version_ge("", "2026-09-07_075784"))
check("semver still compares", _version_ge("1.3.0", "1.2.9"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
