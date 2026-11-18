#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# Regression test for resolve_app_partuuid() in install_app_bundle.sh.
#
# The bug this guards against: configure_boot() re-renders the ESP grub.cfg and
# recovers the PARTUUIDs by scraping the config it is replacing, taking the third
# as app-a. That is wrong whenever the install that just ran recreated the
# partition — most sharply when a rootfs app is installed over a disk-image app,
# where the scraped value is the *previous* app's app-root. The result is a
# root=PARTUUID= that does not exist and a busybox initramfs shell.
#
# Observed on 192.168.0.209, 2026-09-08: GRUB passed 4c119bfd-… (the outgoing
# disk-image app's app-root) while p4 was really 1620bcb2-….
#
# Host-only; blkid is stubbed.
set -u

FILES="${FILES:-$(cd "$(dirname "$0")/../files" 2>/dev/null && pwd)}"
SCRIPT="${FILES}/scripts/install_app_bundle.sh"
[ -f "$SCRIPT" ] || { echo "cannot find install_app_bundle.sh (set FILES=)"; exit 2; }

# Pull just the function under test out of the installer.
eval "$(sed -n '/^resolve_app_partuuid()/,/^}/p' "$SCRIPT")"
command -v resolve_app_partuuid >/dev/null 2>&1 || {
    echo "FAIL: resolve_app_partuuid() not found in $SCRIPT"; exit 1; }

PASS=0; FAIL=0
STUB_PARTUUID=""

# Stand in for the real blkid: reports STUB_PARTUUID for the slot device.
blkid() {
    [ -n "$STUB_PARTUUID" ] || return 1
    echo "$STUB_PARTUUID"
}

check() {  # <name> <expected> <got>
    if [ "$2" = "$3" ]; then
        PASS=$((PASS + 1)); echo "  PASS  $1"
    else
        FAIL=$((FAIL + 1)); echo "  FAIL  $1"; echo "        expected: $2"; echo "        got:      $3"
    fi
}

OLD_APP=4c119bfd-5b33-49c4-a650-a403dca226ca   # outgoing disk-image app's app-root
NEW_SLOT=1620bcb2-ecfe-4c68-8b95-639f5dfc4b68  # p4 as it actually is now
NEW_ROOT=c25ff6f8-586e-4ea2-a92d-7db049270ac0  # app-root a disk-image install made

# --- the regression itself ---------------------------------------------------
unset APP_ROOT_PARTUUID 2>/dev/null || true
SLOT_DEV=/dev/nvme0n1p4; STUB_PARTUUID="$NEW_SLOT"
check "rootfs over a disk-image app uses the real slot, not the stale scrape" \
      "$NEW_SLOT" "$(resolve_app_partuuid "$OLD_APP")"

# --- the disk-image path still wins ------------------------------------------
APP_ROOT_PARTUUID="$NEW_ROOT"; SLOT_DEV=""; STUB_PARTUUID=""
check "disk-image install uses the app-root it created" \
      "$NEW_ROOT" "$(resolve_app_partuuid "$OLD_APP")"

# APP_ROOT_PARTUUID arrives upper-case from sfdisk; udev symlinks are lower-case.
APP_ROOT_PARTUUID=$(echo "$NEW_ROOT" | tr 'a-z' 'A-Z'); SLOT_DEV=""
check "disk-image PARTUUID is lower-cased" \
      "$NEW_ROOT" "$(resolve_app_partuuid "$OLD_APP")"

unset APP_ROOT_PARTUUID 2>/dev/null || true
SLOT_DEV=/dev/nvme0n1p4; STUB_PARTUUID=$(echo "$NEW_SLOT" | tr 'a-z' 'A-Z')
check "slot PARTUUID is lower-cased" \
      "$NEW_SLOT" "$(resolve_app_partuuid "$OLD_APP")"

# --- fallbacks ---------------------------------------------------------------
unset APP_ROOT_PARTUUID 2>/dev/null || true
SLOT_DEV=""; STUB_PARTUUID=""
check "with nothing better to go on, keep the scraped value" \
      "$OLD_APP" "$(resolve_app_partuuid "$OLD_APP")"

unset APP_ROOT_PARTUUID 2>/dev/null || true
SLOT_DEV=/dev/nvme0n1p4; STUB_PARTUUID=""      # blkid fails
check "blkid failure falls back to the scraped value" \
      "$OLD_APP" "$(resolve_app_partuuid "$OLD_APP")"

# A first-ever install has no old config to scrape and no disk-image root.
unset APP_ROOT_PARTUUID 2>/dev/null || true
SLOT_DEV=/dev/nvme0n1p4; STUB_PARTUUID="$NEW_SLOT"
check "empty scrape still resolves from the slot" \
      "$NEW_SLOT" "$(resolve_app_partuuid "")"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
