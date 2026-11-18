#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Boundary test for MAX_LAYOUT_PARTS, the x86-disk-image layout cap.

The app region was widened from 10 to 30 partitions (APP_REGION["index_max"]
in app_slots.py), so the descriptor-side cap has to move with it. This checks
the two numbers stay in lockstep: exactly MAX_LAYOUT_PARTS partitions pass,
one more is rejected and the rejection names the limit.

Host-only, no device needed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "files"))
from descriptor_tool import DescriptorError, MAX_LAYOUT_PARTS, _validate_layout  # noqa: E402

PASS = FAIL = 0


def layout(count):
    parts = [{"part": "app-root", "grow": True, "path": "x86/disk/p1.ext4", "sha256": "x"}]
    for i in range(2, count + 1):
        parts.append({
            "part": f"app-p{i}", "size_mib": 16,
            "path": f"x86/disk/p{i}.ext4", "sha256": "x",
        })
    return {"layout": parts}


def ok(name, count):
    global PASS, FAIL
    try:
        _validate_layout(0, layout(count))
    except DescriptorError as exc:
        FAIL += 1
        print(f"  FAIL  {name}\n        unexpectedly rejected: {exc}")
        return
    PASS += 1
    print(f"  PASS  {name}")


def rejected(name, count, because=""):
    global PASS, FAIL
    try:
        _validate_layout(0, layout(count))
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


print(f"layout partition cap (MAX_LAYOUT_PARTS={MAX_LAYOUT_PARTS}):")
ok(f"exactly {MAX_LAYOUT_PARTS} partitions passes", MAX_LAYOUT_PARTS)
rejected(f"{MAX_LAYOUT_PARTS + 1} partitions is rejected", MAX_LAYOUT_PARTS + 1,
         because=f"at most {MAX_LAYOUT_PARTS} are available")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
