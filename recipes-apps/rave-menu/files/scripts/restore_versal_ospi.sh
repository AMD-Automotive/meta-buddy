#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC
#
# Put the Versal back into OSPI boot mode after a recovery-payload operation.
#
# Every operation that boots the recovery payload has to leave the board in its
# normal state afterwards, so that sequence lives here rather than being copied
# into each one.

set -eu

ROOT_PORT="$(/sbin/versal-root-port 2>/dev/null || echo 0000:00:02.3)"
SYSFS="/sys/bus/pci/devices"
ROOT_PATH="${SYSFS}/${ROOT_PORT}"
ROOT_REAL="$(readlink -f "${ROOT_PATH}")"

for d in "${SYSFS}"/*; do
    [ -e "$d" ] || continue
    rp="$(readlink -f "$d")"
    [ "$rp" = "$ROOT_REAL" ] && continue
    case "$rp" in
        "${ROOT_REAL}"/*)
            dev=$(basename "$d")
            if [ -e "${SYSFS}/${dev}/remove" ]; then
                echo "  removing ${dev}"
                echo 1 > "${SYSFS}/${dev}/remove" 2>/dev/null || true
            fi
            ;;
    esac
done

/sbin/reconfigure_versal --mode3-high

sleep 1
echo 1 > "${ROOT_PATH}/rescan"

# Deliberately no wait for re-enumeration: the OSPI image is not guaranteed to
# be valid, so the endpoint may never come back and the caller must not hang.
