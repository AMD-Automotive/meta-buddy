#!/usr/bin/env bash
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

set -u

# Contract: the caller must already have sourced the Vivado/HW Server settings
# and exported PATH, because `xsdb` below is resolved from PATH. Which release
# that is gets decided centrally by rave-menu's vivado_env.sh; intermixing an
# xsdb with a different release's hw_server is not supported.

# Resolve the root port the Versal sits behind from the board's DMI board_name
# (see /sbin/versal-root-port). Falls back to the RAVE2-CORE root port if the
# helper is unavailable.
ROOT_PORT="$(/sbin/versal-root-port 2>/dev/null || echo 0000:00:02.3)"
FIRMWARE_DIR="/lib/firmware/versal"
BOOT_BIN="${FIRMWARE_DIR}/boot.bin"
PCI_IMAGE="${FIRMWARE_DIR}/pcieImage"

SYSFS="/sys/bus/pci/devices"
ROOT_PATH="${SYSFS}/${ROOT_PORT}"

TCL_SCRIPT="/usr/lib/versal/program_versal.tcl"

usage() {
    cat <<USAGE
Usage: $(basename "$0")

Boots the Versal recovery payload over JTAG. Both ${BOOT_BIN} and
${PCI_IMAGE} ship in this rootfs via the versal-reprogram package.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
    shift
done

run_as_root() {
    if [[ ${EUID} -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

ensure_root_or_sudo() {
    if [[ ${EUID} -eq 0 ]]; then
        return 0
    fi

    if ! command -v sudo >/dev/null 2>&1; then
        echo "ERROR: root or sudo permissions are required." >&2
        exit 1
    fi

    if ! sudo -n true >/dev/null 2>&1; then
        if ! sudo -v; then
            echo "ERROR: root or sudo permissions are required." >&2
            exit 1
        fi
    fi
}

# The payload and the host tooling have to agree on the BAR protocol, and they
# are versioned together in this rootfs by the versal-reprogram package. A
# runtime download could silently substitute an unrelated build, so a missing
# file is treated as a packaging fault rather than something to paper over.
ensure_firmware_files() {
    local missing=0
    for file in "$BOOT_BIN" "$PCI_IMAGE"; do
        if [[ ! -f "$file" ]]; then
            echo "ERROR: ${file} is missing" >&2
            missing=1
        fi
    done
    if [[ $missing -ne 0 ]]; then
        echo "ERROR: the versal-reprogram package is not correctly installed." >&2
        exit 1
    fi
}

ensure_root_or_sudo
ensure_firmware_files

if [[ ! -d "${ROOT_PATH}" ]]; then
    echo "ERROR: root port ${ROOT_PORT} not found" >&2
    exit 1
fi

if [[ ! -f "${TCL_SCRIPT}" ]]; then
    echo "ERROR: Tcl script not found: ${TCL_SCRIPT}" >&2
    exit 1
fi

ROOT_REAL="$(readlink -f "${ROOT_PATH}")"

list_children() {
    for d in "${SYSFS}"/*; do
        [[ -e "$d" ]] || continue
        rp="$(readlink -f "$d")"
        [[ "$rp" == "$ROOT_REAL" ]] && continue
        if [[ "$rp" == "${ROOT_REAL}"/* ]]; then
            basename "$d"
        fi
    done | sort
}

echo "Current devices below ${ROOT_PORT}:"
list_children | while read -r dev; do
    [[ -n "$dev" ]] && lspci -D -s "$dev" || true
done
echo

echo "Removing devices below ${ROOT_PORT}..."
while read -r dev; do
    [[ -z "$dev" ]] && continue
    if [[ -e "${SYSFS}/${dev}/remove" ]]; then
        echo "  removing ${dev}"
        run_as_root sh -c "echo 1 > '${SYSFS}/${dev}/remove'" || true
    fi
done < <(list_children)
echo

echo "Running XSDB Tcl script"
if ! command -v xsdb >/dev/null 2>&1; then
    echo "ERROR: xsdb not found on PATH -- the caller must source the Vivado/HW" >&2
    echo "       Server settings before running versal-reprogram." >&2
    exit 1
fi
xsdb "${TCL_SCRIPT}"
echo

sleep 1

echo "Rescanning ..."
run_as_root sh -c "echo 1 > '${ROOT_PATH}/rescan'"
echo

sleep 1

echo "Devices now below ${ROOT_PORT}:"
list_children | while read -r dev; do
    [[ -n "$dev" ]] && lspci -D -s "$dev" || true
done
