#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# Resolve the PCIe root port that the Versal add-in card sits behind.
#
# The root-port BDF depends on the motherboard and firmware PCIe layout. Prefer
# stable DMI identity so this works while an unprogrammed Versal exposes no
# endpoint. If DMI sysfs is unavailable, use dmidecode when passwordless sudo is
# available, then fall back to live Xilinx endpoint ancestry.
#
# firmware / board             -> Versal root port
#   coreboot / RAVE2-CORE      -> 0000:00:03.3
#   AMI / VPR-7P132 (CORE)     -> 0000:00:02.3
#   AMI / VPR-7X188 (MAX)      -> 0000:00:03.2
#
# Unknown boards fall back to the RAVE2-CORE root port.
#
# An explicit VERSAL_ROOT_PORT environment variable overrides the lookup.
set -u

# RAVE2-CORE (VPR-7P132) root port, used as the default for unknown boards.
DEFAULT_ROOT_PORT="0000:00:02.3"

if [ -n "${VERSAL_ROOT_PORT:-}" ]; then
    echo "$VERSAL_ROOT_PORT"
    exit 0
fi

board="$(cat /sys/class/dmi/id/board_name 2>/dev/null || true)"
bios_vendor="$(cat /sys/class/dmi/id/bios_vendor 2>/dev/null || true)"

if [ -z "$bios_vendor" ] && command -v dmidecode >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then
        bios_vendor="$(dmidecode -s bios-vendor 2>/dev/null || true)"
    elif command -v sudo >/dev/null 2>&1; then
        bios_vendor="$(sudo -n dmidecode -s bios-vendor 2>/dev/null || true)"
    fi
fi

case "$bios_vendor" in
    *coreboot*) echo "0000:00:03.3"; exit 0 ;;
esac

case "$board" in
    VPR-7P132) echo "0000:00:02.3"; exit 0 ;;   # RAVE2-CORE with AMI
    VPR-7X188) echo "0000:00:03.2"; exit 0 ;;   # RAVE2-MAX with AMI
esac

for endpoint in /sys/bus/pci/devices/*; do
    [ -e "$endpoint" ] || continue
    [ "$(cat "$endpoint/vendor" 2>/dev/null || true)" = "0x10ee" ] || continue
    parent="$(dirname "$(readlink -f "$endpoint")")"
    case "${parent##*/}" in
        0000:*) echo "${parent##*/}"; exit 0 ;;
    esac
done

detected_port=""
for candidate in 0000:00:02.3 0000:00:03.2 0000:00:03.3; do
    [ -d "/sys/bus/pci/devices/$candidate" ] || continue
    [ -z "$detected_port" ] || detected_port="ambiguous"
    [ "$detected_port" = "ambiguous" ] || detected_port="$candidate"
done

case "$detected_port" in
    0000:*) echo "$detected_port"; exit 0 ;;
esac

echo "$DEFAULT_ROOT_PORT"
