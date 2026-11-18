#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

set -eu
echo "=-=-=-="
echo "Reconfigure Versal (OSPI boot)"
echo "=-=-=-="
/sbin/reconfigure_versal --mode3-high

# Resolve the root port the Versal sits behind from the board's DMI board_name
# (see /sbin/versal-root-port). Falls back to the RAVE2-CORE root port if the
# helper is unavailable.
ROOT_PORT="$(/sbin/versal-root-port 2>/dev/null || echo 0000:00:02.3)"
SYSFS="/sys/bus/pci/devices"
ROOT_PATH="${SYSFS}/${ROOT_PORT}"

echo "Rescanning PCI bus..."
sleep 1
echo 1 > "${ROOT_PATH}/rescan"

echo "Waiting for Versal to re-enumerate..."
TRIES=0
MAX_TRIES=30
while [ $TRIES -lt $MAX_TRIES ]; do
    ROOT_REAL="$(readlink -f "${ROOT_PATH}")"
    FOUND=0
    for d in "${SYSFS}"/*; do
        [ -e "$d" ] || continue
        rp="$(readlink -f "$d")"
        [ "$rp" = "$ROOT_REAL" ] && continue
        case "$rp" in
            "${ROOT_REAL}"/*)
                FOUND=1
                break
                ;;
        esac
    done
    if [ $FOUND -eq 1 ]; then
        echo "Versal PCIe endpoint detected."
        break
    fi
    TRIES=$((TRIES + 1))
    sleep 1
done

if [ $FOUND -eq 0 ]; then
    echo "WARNING: Versal did not re-enumerate after ${MAX_TRIES}s"
fi

echo "Done."
