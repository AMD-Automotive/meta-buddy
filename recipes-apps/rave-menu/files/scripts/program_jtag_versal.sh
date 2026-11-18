#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# JTAG programming flow for the Versal device.
#
# Steps:
#   1. Resolve the boot.bin/.pdi argument (URL or local path, optional .gz/.zst).
#   2. Reconfigure the Versal so it boots from JTAG (POR_B reset with MODE3
#      held LOW via the FT4232HQ).
#   3. Source the Vivado / HW Server settings so xsdb is on PATH.
#   4. Run the existing program_versal.tcl XSDB script with BOOTBIN pointing
#      at the user-supplied image.
#
# Usage:  program_jtag_versal.sh <bootbin-source>
#
# A bootbin source is required.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "${SCRIPT_DIR}/vivado_env.sh"

die() { echo "[jtag] ERROR: $*" >&2; exit 1; }

if [ $# -lt 1 ] || [ -z "${1:-}" ]; then
    die "missing required <bootbin-source> argument"
fi
BOOT_SRC="$1"

DOWNLOAD_DIR="/var/lib/rave-menu/payload-cache"
mkdir -p "$DOWNLOAD_DIR"
trap 'echo "[jtag] Cleaning up downloaded files"; rm -rf "$DOWNLOAD_DIR"/* "$DOWNLOAD_DIR"/.[!.]* "$DOWNLOAD_DIR"/..?*' EXIT

normalize_url() {
    python3 - "$1" <<'PY'
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

parts = urlsplit(sys.argv[1])
if parts.hostname in {"dropbox.com", "www.dropbox.com"}:
    query = parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key not in {"dl", "raw"}]
    query.append(("dl", "1"))
    parts = parts._replace(query=urlencode(query))
print(urlunsplit(parts._replace(fragment="")))
PY
}

url_filename() {
    python3 - "$1" <<'PY'
import os
import sys
from urllib.parse import unquote, urlsplit

print(os.path.basename(unquote(urlsplit(sys.argv[1]).path)) or "download.bin")
PY
}

download_url() {
    download_src="$1"
    download_dest="$2"
    case "$download_src" in
        https://www.dropbox.com/*|https://dropbox.com/*)
            python3 - "$download_src" "$download_dest" <<'PY'
import sys
import urllib.request

request = urllib.request.Request(sys.argv[1], headers={"User-Agent": "RAVE2-Payload-Downloader/1"})
with urllib.request.urlopen(request) as response, open(sys.argv[2], "wb") as output:
    total = int(response.headers.get("Content-Length") or 0)
    copied = 0
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        output.write(chunk)
        copied += len(chunk)
        if copied % (64 * 1024 * 1024) < len(chunk):
            if total:
                print(f"[jtag] Downloaded {copied // 1048576} of {total // 1048576} MiB ({copied * 100 // total}%)", file=sys.stderr)
            else:
                print(f"[jtag] Downloaded {copied // 1048576} MiB", file=sys.stderr)
PY
            ;;
        *) wget -O "$download_dest" "$download_src" >&2 ;;
    esac
}

# Resolve a source (local path or URL, optionally .gz or .zst) to a local file path.
# Prints the resolved local path to stdout, all chatter to stderr.
resolve_source() {
    src="$1"

    case "$src" in
        http://*|https://*)
            src=$(normalize_url "$src")
            fname=$(url_filename "$src")
            case "$fname" in
                *.zst)
                    dest="${DOWNLOAD_DIR}/${fname%.zst}"
                    tmp_zst="${DOWNLOAD_DIR}/${fname}"
                    echo "[jtag] Downloading ${src}" >&2
                    download_url "$src" "$tmp_zst" || die "Failed to download ${src}"
                    echo "[jtag] Extracting ${tmp_zst} -> ${dest}" >&2
                    zstd -dc "$tmp_zst" > "$dest" || die "Failed to extract ${tmp_zst}"
                    rm -f "$tmp_zst"
                    ;;
                *.gz)
                    dest="${DOWNLOAD_DIR}/${fname%.gz}"
                    tmp_gz="${DOWNLOAD_DIR}/${fname}"
                    echo "[jtag] Downloading ${src}" >&2
                    download_url "$src" "$tmp_gz" || die "Failed to download ${src}"
                    echo "[jtag] Extracting ${tmp_gz} -> ${dest}" >&2
                    gunzip -c "$tmp_gz" > "$dest" || die "Failed to extract ${tmp_gz}"
                    rm -f "$tmp_gz"
                    ;;
                *)
                    dest="${DOWNLOAD_DIR}/${fname}"
                    echo "[jtag] Downloading ${src} -> ${dest}" >&2
                    download_url "$src" "$dest" || die "Failed to download ${src}"
                    ;;
            esac
            ;;
        *)
            fname=$(echo "$src" | sed 's|.*/||')
            case "$src" in
                *.zst)
                    dest="${DOWNLOAD_DIR}/${fname%.zst}"
                    [ -f "$src" ] || die "Local file not found: ${src}"
                    echo "[jtag] Extracting ${src} -> ${dest}" >&2
                    zstd -dc "$src" > "$dest" || die "Failed to extract ${src}"
                    ;;
                *.gz)
                    dest="${DOWNLOAD_DIR}/${fname%.gz}"
                    [ -f "$src" ] || die "Local file not found: ${src}"
                    echo "[jtag] Extracting ${src} -> ${dest}" >&2
                    gunzip -c "$src" > "$dest" || die "Failed to extract ${src}"
                    ;;
                *)
                    dest="$src"
                    [ -f "$src" ] || die "Local file not found: ${src}"
                    echo "[jtag] Using local file ${src}" >&2
                    ;;
            esac
            ;;
    esac
    echo "$dest"
}

echo "=-=-=-="
echo "Resolve boot image"
echo "=-=-=-="
BOOT_PATH=$(resolve_source "$BOOT_SRC")
echo "[jtag] BOOT_PATH=${BOOT_PATH}"

echo "=-=-=-="
echo "Reconfigure Versal (JTAG boot mode)"
echo "=-=-=-="
# MODE3 LOW selects JTAG boot. The Versal will halt on the JTAG fabric and
# wait for an XSDB programming sequence. Removing PCI endpoints first keeps
# the host kernel from seeing transient link-down events.
# Resolve the root port the Versal sits behind from the board's DMI board_name
# (see /sbin/versal-root-port). Falls back to the RAVE2-CORE root port if the
# helper is unavailable.
ROOT_PORT="$(/sbin/versal-root-port 2>/dev/null || echo 0000:00:02.3)"
SYSFS="/sys/bus/pci/devices"
ROOT_PATH="${SYSFS}/${ROOT_PORT}"
if [ -d "${ROOT_PATH}" ]; then
    ROOT_REAL="$(readlink -f "${ROOT_PATH}")"
    echo "[jtag] Removing Versal PCI endpoints under ${ROOT_PORT}..."
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
fi

echo "[jtag] Reconfiguring Versal (POR_B reset, MODE3=low) ..."
/sbin/reconfigure_versal --mode3-low

echo "=-=-=-="
echo "Program via JTAG"
echo "=-=-=-="
# Source Vivado / HW Server settings so xsdb is available. The release is
# resolved centrally so it always matches any running hw_server.
vivado_setup_env jtag || die "Cannot find Vivado/HW Server settings under $(vivado_base_dir)/"

command -v xsdb >/dev/null 2>&1 || die "xsdb not found on PATH after sourcing the Vivado settings"

export BOOTBIN="$BOOT_PATH"
export MAX_ATTEMPTS=1
echo "[jtag] Running xsdb /usr/lib/versal/program_versal.tcl with BOOTBIN=${BOOTBIN}"
xsdb /usr/lib/versal/program_versal.tcl

echo "=-=-=-="
echo "Rescan PCI"
echo "=-=-=-="
# After JTAG programming, rescan the root port. If the loaded image exposes
# a PCIe endpoint it will be detected; otherwise the rescan is a no-op.
if [ -d "${ROOT_PATH}" ]; then
    echo "[jtag] Rescanning ${ROOT_PORT} ..."
    sleep 1
    echo 1 > "${ROOT_PATH}/rescan" 2>/dev/null || true

    TRIES=0
    MAX_TRIES=15
    FOUND=0
    while [ $TRIES -lt $MAX_TRIES ]; do
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
        [ $FOUND -eq 1 ] && break
        TRIES=$((TRIES + 1))
        sleep 1
    done
    if [ $FOUND -eq 1 ]; then
        echo "[jtag] Versal PCIe endpoint detected after JTAG load."
    else
        echo "[jtag] No PCIe endpoint after ${MAX_TRIES}s (image may not expose PCIe)."
    fi
fi

echo "[jtag] Done."
