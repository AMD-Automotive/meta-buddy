#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC
#
# Read the Versal's UFS LUN layout over PCIe and restore OSPI boot mode.
#
# Usage: scan_versal_luns.sh <output-json>
#
# Read-only: boots the recovery payload, queries the UFS device, geometry and
# configuration descriptors, then resets the Versal back to OSPI so the board
# is left in its normal state. Nothing is written to the UFS.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "${SCRIPT_DIR}/vivado_env.sh"

die() { echo "[luns] ERROR: $*" >&2; exit 1; }

OUT="${1:?usage: scan_versal_luns.sh <output-json>}"

vivado_setup_env payload || die "Cannot find Vivado/HW Server settings under $(vivado_base_dir)/"
command -v xsdb >/dev/null 2>&1 || die "xsdb not found on PATH after sourcing the Vivado settings"

echo "[luns] Booting recovery payload"
/sbin/versal-reprogram

echo "[luns] Reading the UFS LUN layout"
/sbin/program_versal_payload --ufs-luns --json-out "$OUT"

echo "[luns] Restoring OSPI boot mode"
"${SCRIPT_DIR}/restore_versal_ospi.sh"

echo "[luns] Done"
