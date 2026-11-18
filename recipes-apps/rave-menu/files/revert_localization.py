#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Restore the previous localisation settings when nobody confirmed the change.

Launched as a transient systemd timer by the keyboard apply path and cancelled
by POST /api/localization/confirm. Running at all means the user never came
back to confirm -- most likely because the new layout is unusable, or because
the compositor did not survive it.

It therefore cannot assume anything still works. In particular it must clear
weston's failed state before restarting: if the new keymap killed the
compositor, Restart=on-failure will have burned through StartLimitBurst and a
plain restart is refused, so the revert would silently achieve nothing in
exactly the case it exists for.
"""

import subprocess
import sys

import apply_localization
import localization as loc


def run(cmd):
    try:
        subprocess.run(cmd, capture_output=True, text=True,
                       timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"revert_localization: {cmd[0]} failed: {exc!r}", file=sys.stderr)


def main() -> int:
    previous = loc.load_previous()
    if previous is None:
        print("revert_localization: nothing to revert to", file=sys.stderr)
        return 0

    print(f"revert_localization: restoring layout="
          f"{previous['keyboard']['layout']} language={previous['language']}",
          file=sys.stderr)

    loc.save_config(previous)
    apply_localization.apply_all(previous)

    run(["systemctl", "reset-failed", "weston"])
    run(["systemctl", "restart", "weston"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
