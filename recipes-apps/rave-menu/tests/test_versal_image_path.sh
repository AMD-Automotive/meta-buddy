#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# Regression test for versal_image_path() in install_app_bundle.sh.
#
# This is the helper that lets persist_one_versal(), program_versal(),
# write_installed_json() and run_recovery() work against whichever
# compression (.gz or .zst) a Versal image happens to be persisted in,
# instead of each hardcoding .gz. It must: prefer whichever single suffix
# is actually on the slot, refuse to guess if both are somehow present
# (a state the stale-sibling rm -f in persist_versal_images/the streaming
# dispatcher is supposed to prevent), and fall back to the suffix implied
# by a declared component path when persisting for the first time.
#
# Host-only; no device needed.
set -u

FILES="${FILES:-$(cd "$(dirname "$0")/../files" 2>/dev/null && pwd)}"
SCRIPT="${FILES}/scripts/install_app_bundle.sh"
[ -f "$SCRIPT" ] || { echo "cannot find install_app_bundle.sh (set FILES=)"; exit 2; }

die() { echo "ERROR: $*" >&2; exit 1; }

# Pull just the function under test out of the installer.
eval "$(sed -n '/^versal_image_path()/,/^}/p' "$SCRIPT")"
command -v versal_image_path >/dev/null 2>&1 || {
    echo "FAIL: versal_image_path() not found in $SCRIPT"; exit 1; }

PASS=0; FAIL=0

check() {  # <name> <expected> <got>
    if [ "$2" = "$3" ]; then
        PASS=$((PASS + 1)); echo "  PASS  $1"
    else
        FAIL=$((FAIL + 1)); echo "  FAIL  $1"; echo "        expected: $2"; echo "        got:      $3"
    fi
}

check_dies() {  # <name> <needle> <output> <rc>
    if [ "$4" != "0" ] && case "$3" in *"$2"*) true ;; *) false ;; esac; then
        PASS=$((PASS + 1)); echo "  PASS  $1"
    else
        FAIL=$((FAIL + 1)); echo "  FAIL  $1"; echo "        expected failure mentioning: $2"
        echo "        rc=$4 output: $3"
    fi
}

VDIR="$(mktemp -d)"
trap 'rm -rf "$VDIR"' EXIT

# --- only one suffix present: use it -----------------------------------------
: > "${VDIR}/boot.bin.gz"
check "an existing .gz sibling is used" "${VDIR}/boot.bin.gz" "$(versal_image_path boot.bin)"
rm -f "${VDIR}/boot.bin.gz"

: > "${VDIR}/boot.bin.zst"
check "an existing .zst sibling is used" "${VDIR}/boot.bin.zst" "$(versal_image_path boot.bin)"
rm -f "${VDIR}/boot.bin.zst"

# --- neither present: fall back to the declared component path's suffix -----
check "no slot file yet, declared .zst path picks the .zst destination" \
      "${VDIR}/wic.ufs.zst" "$(versal_image_path wic.ufs versal/wic.ufs.zst)"
check "no slot file yet, declared .gz path picks the .gz destination" \
      "${VDIR}/wic.ufs.gz" "$(versal_image_path wic.ufs versal/wic.ufs.gz)"

# --- neither present, no declared path: nothing to report -------------------
check "no slot file and no declared path echoes nothing" \
      "" "$(versal_image_path wic.ufs)"

# --- an unsupported declared suffix is rejected, not silently guessed -------
out=$(versal_image_path wic.ufs versal/wic.ufs.bin 2>&1); rc=$?
check_dies "an unsupported declared suffix dies rather than guessing" \
      "unsupported suffix" "$out" "$rc"

# --- both present: refuse to guess, exactly the state stale-sibling rm -f
# in the caller is meant to prevent -----------------------------------------
: > "${VDIR}/boot.bin.gz"; : > "${VDIR}/boot.bin.zst"
out=$(versal_image_path boot.bin 2>&1); rc=$?
check_dies "both suffixes present refuses to pick one" \
      "refusing to guess" "$out" "$rc"
rm -f "${VDIR}/boot.bin.gz" "${VDIR}/boot.bin.zst"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
