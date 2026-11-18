#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC
#
# Re-provision the Versal's UFS LUN layout over PCIe.
#
# Usage: provision_versal_luns.sh <output-json> --sizes <spec> [--commit]
#        provision_versal_luns.sh <output-json> --restore-baseline [--commit]
#
# Without --commit this is a dry run: the target validates the plan against the
# live device and reports the layout it would produce, writing nothing.
#
# With --commit it makes ALL DATA ON EVERY LUN UNRECOVERABLE, including the
# rootfs. The board will not boot afterwards until an image is programmed,
# which is a separate operation.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "${SCRIPT_DIR}/vivado_env.sh"

die() { echo "[luns] ERROR: $*" >&2; exit 1; }

OUT="${1:?usage: provision_versal_luns.sh <output-json> --sizes <spec>|--plan <file>|--restore-baseline [--boot-luns <list>] [--commit]}"
shift

SIZES=""
PLAN=""
BOOT_LUNS=""
HAVE_BOOT=0
RESTORE=0
COMMIT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --sizes)            SIZES="${2:?--sizes needs a value}"; shift 2 ;;
        --plan)             PLAN="${2:?--plan needs a value}"; shift 2 ;;
        --boot-luns)        BOOT_LUNS="${2?--boot-luns needs a value}"; HAVE_BOOT=1; shift 2 ;;
        --restore-baseline) RESTORE=1; shift ;;
        --commit)           COMMIT=1; shift ;;
        *)                  die "unknown option: $1" ;;
    esac
done

[ -n "$SIZES" ] || [ -n "$PLAN" ] || [ "$RESTORE" -eq 1 ] ||
    die "one of --sizes, --plan or --restore-baseline is required"
[ -n "$PLAN" ] && [ -n "$SIZES" ] && die "--plan and --sizes are mutually exclusive"
[ "$RESTORE" -eq 1 ] && { [ -n "$SIZES" ] || [ -n "$PLAN" ]; } &&
    die "--restore-baseline cannot be combined with a layout"
[ -n "$PLAN" ] && [ ! -r "$PLAN" ] && die "cannot read plan file: $PLAN"

vivado_setup_env payload || die "Cannot find Vivado/HW Server settings under $(vivado_base_dir)/"
command -v xsdb >/dev/null 2>&1 || die "xsdb not found on PATH after sourcing the Vivado settings"

set -- --ufs-luns --json-out "$OUT"
if [ "$RESTORE" -eq 1 ]; then
    set -- "$@" --ufs-restore-baseline
elif [ -n "$PLAN" ]; then
    set -- "$@" --ufs-plan "$PLAN"
else
    set -- "$@" --ufs-sizes "$SIZES"
    # An empty list still means "manage them", so it has to reach the tool as
    # 'none' rather than being dropped as an absent option.
    if [ "$HAVE_BOOT" -eq 1 ]; then
        set -- "$@" --ufs-boot-luns "${BOOT_LUNS:-none}"
    fi
fi
[ "$COMMIT" -eq 1 ] && set -- "$@" --yes

echo "[luns] Booting recovery payload"
/sbin/versal-reprogram

if [ "$COMMIT" -eq 1 ]; then
    echo "[luns] COMMITTING a new LUN layout - all UFS contents will be lost"
else
    echo "[luns] Validating the requested layout (dry run)"
fi

# Keep going on failure so OSPI boot mode is always restored; the exit status
# is reported after the board is back in its normal state.
rc=0
/sbin/program_versal_payload "$@" || rc=$?

echo "[luns] Restoring OSPI boot mode"
"${SCRIPT_DIR}/restore_versal_ospi.sh"

echo "[luns] Done"
exit "$rc"
