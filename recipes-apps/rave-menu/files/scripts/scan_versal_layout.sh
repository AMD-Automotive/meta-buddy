#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC
#
# Read the Versal UFS partition table over PCIe and restore OSPI boot mode.
#
# Usage: scan_versal_layout.sh <output-file> [bytes]
#
# Boots the recovery payload, reads the head of /dev/sda (enough for the
# protective MBR, GPT header and all entries), then resets the Versal back to
# OSPI so the board is left in its normal state.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "${SCRIPT_DIR}/vivado_env.sh"

die() { echo "[layout] ERROR: $*" >&2; exit 1; }

OUT="${1:?usage: scan_versal_layout.sh <output-file> [bytes]}"
BYTES="${2:-131072}"

vivado_setup_env payload || die "Cannot find Vivado/HW Server settings under $(vivado_base_dir)/"
command -v xsdb >/dev/null 2>&1 || die "xsdb not found on PATH after sourcing the Vivado settings"

echo "[layout] Booting recovery payload"
/sbin/versal-reprogram

echo "[layout] Reading ${BYTES} bytes from the start of UFS"
/sbin/program_versal_payload --read-ufs 0 "$BYTES" "$OUT"

echo "[layout] Restoring OSPI boot mode"
"${SCRIPT_DIR}/restore_versal_ospi.sh"

echo "[layout] Done"
