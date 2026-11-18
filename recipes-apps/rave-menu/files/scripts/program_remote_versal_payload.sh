#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "${SCRIPT_DIR}/vivado_env.sh"

die() { echo "[payload] ERROR: $*" >&2; exit 1; }

# Sources are determined per slot:
#   - If a positional arg is given (even empty string or "-"): use it verbatim
#     (empty / "-" => skip that phase).
#   - If a positional arg is omitted: fall back to the matching env var, then
#     to the packaged default (preserving the legacy "Run board test" path).
# Slot 1 is WIC (rootfs) and slot 2 is OSPI, matching the menu invocation
# order.
if [ "${1+set}" = "set" ]; then
    WIC_SRC="$1"
else
    WIC_SRC="${WIC_URL:-/usr/lib/firmware/boardtest/wic.ufs}"
fi

if [ "${2+set}" = "set" ]; then
    OSPI_SRC="$2"
else
    OSPI_SRC="${OSPI_URL:-/usr/lib/firmware/boardtest/boot.bin}"
fi

BMAP_SRC="${3:-}"
RANGES_SPEC="${4:-}"
ZERO_SPEC="${5:-}"

# Normalise the "skip" sentinel
if [ "$WIC_SRC" = "-" ]; then WIC_SRC=""; fi
if [ "$OSPI_SRC" = "-" ]; then OSPI_SRC=""; fi
if [ "$BMAP_SRC" = "-" ]; then BMAP_SRC=""; fi
if [ "$RANGES_SPEC" = "-" ]; then RANGES_SPEC=""; fi
if [ "$ZERO_SPEC" = "-" ]; then ZERO_SPEC=""; fi

# Sparse programming only writes the blocks the bmap marks as mapped, so any
# block the image leaves as a hole keeps the previous image's content. Zeroing
# first restores the empty-destination assumption bmap relies on.
ZERO_ARGS=""
if [ -n "$ZERO_SPEC" ]; then
    [ -n "$BMAP_SRC" ] || die "Zeroing requires a bmap file"
    ZERO_ARGS="--zero-before-write"
fi

if [ -z "$WIC_SRC" ] && [ -z "$OSPI_SRC" ]; then
    die "Nothing to program: both WIC and OSPI are empty"
fi
if [ -n "$BMAP_SRC" ] && [ -z "$WIC_SRC" ]; then
    die "A bmap file requires a WIC image"
fi

# A partial update writes only the selected partitions, so it needs the bmap to
# know which bytes within them are real data.
RANGE_ARGS=""
if [ -n "$RANGES_SPEC" ]; then
    [ -n "$WIC_SRC" ] || die "Partition ranges require a WIC image"
    [ -n "$BMAP_SRC" ] || die "Partition ranges require a bmap file"
    _old_ifs=$IFS
    IFS=','
    for _range in $RANGES_SPEC; do
        _offset=${_range%%:*}
        _length=${_range#*:}
        case "$_offset" in ''|*[!0-9]*) die "invalid range offset: ${_range}" ;; esac
        case "$_length" in ''|*[!0-9]*) die "invalid range length: ${_range}" ;; esac
        RANGE_ARGS="${RANGE_ARGS} --ufs-range ${_offset} ${_length}"
    done
    IFS=$_old_ifs
fi

DOWNLOAD_DIR="/var/lib/rave-menu/payload-cache"
mkdir -p "$DOWNLOAD_DIR"
trap 'echo "[payload] Cleaning up downloaded files"; rm -rf "$DOWNLOAD_DIR"/* "$DOWNLOAD_DIR"/.[!.]* "$DOWNLOAD_DIR"/..?*' EXIT

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
    local download_src="$1"
    local download_dest="$2"
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
                print(f"[payload] Downloaded {copied // 1048576} of {total // 1048576} MiB ({copied * 100 // total}%)", file=sys.stderr)
            else:
                print(f"[payload] Downloaded {copied // 1048576} MiB", file=sys.stderr)
PY
            ;;
        *) wget -O "$download_dest" "$download_src" >&2 ;;
    esac
}

check_remote_space() {
    local src="$1"
    local content_length
    local free_mb
    local needed_mb

    case "$src" in
        https://www.dropbox.com/*|https://dropbox.com/*)
            content_length=$(python3 - "$src" <<'PY' 2>/dev/null || true
import sys
import urllib.request

request = urllib.request.Request(sys.argv[1], method="HEAD", headers={"User-Agent": "RAVE2-Payload-Downloader/1"})
with urllib.request.urlopen(request) as response:
    print(response.headers.get("Content-Length", ""))
PY
            )
            ;;
        *) content_length=$(wget --spider -S -q "$src" 2>&1 | grep -i content-length | awk '{print $2}' | tr -d '\r' | tail -n1) ;;
    esac
    if [ -z "$content_length" ] || ! [ "$content_length" -gt 0 ] 2>/dev/null; then
        echo "[payload] Could not determine size for $src, proceeding without pre-check" >&2
        return 0
    fi

    free_mb=$(df -Pm "$DOWNLOAD_DIR" | awk 'NR==2 {print $4}')
    needed_mb=$(( (content_length * 11 / 10) / 1048576 ))

    echo "[payload] Space check: ${src} requires ${needed_mb} MiB, ${free_mb} MiB free on ${DOWNLOAD_DIR}" >&2
    if [ "$free_mb" -lt "$needed_mb" ]; then
        die "Not enough space on ${DOWNLOAD_DIR}: need ${needed_mb} MiB, have ${free_mb} MiB"
    fi
}

# Resolve a source (local path or URL, optionally .gz or .zst) to a local file path.
# Prints the resolved local path to stdout.
resolve_source() {
    local src="$1"
    local preserve_compressed="${2:-0}"

    local fname

    case "$src" in
        http://*|https://*)
            src=$(normalize_url "$src")
            fname=$(url_filename "$src")
            check_remote_space "$src"
            case "$fname" in
                *.zst)
                    local dest="${DOWNLOAD_DIR}/${fname%.zst}"
                    local tmp_zst="${DOWNLOAD_DIR}/${fname}"
                    echo "[payload] Downloading ${src}" >&2
                    download_url "$src" "$tmp_zst" || die "Failed to download ${src}"
                    if [ "$preserve_compressed" = "1" ]; then
                        dest="$tmp_zst"
                    else
                        echo "[payload] Extracting ${tmp_zst} -> ${dest}" >&2
                        zstd -dc "$tmp_zst" > "$dest" || die "Failed to extract ${tmp_zst}"
                        rm -f "$tmp_zst"
                    fi
                    ;;
                *.gz)
                    local dest="${DOWNLOAD_DIR}/${fname%.gz}"
                    local tmp_gz="${DOWNLOAD_DIR}/${fname}"
                    echo "[payload] Downloading ${src}" >&2
                    download_url "$src" "$tmp_gz" || die "Failed to download ${src}"
                    if [ "$preserve_compressed" = "1" ]; then
                        dest="$tmp_gz"
                    else
                        echo "[payload] Extracting ${tmp_gz} -> ${dest}" >&2
                        gunzip -c "$tmp_gz" > "$dest" || die "Failed to extract ${tmp_gz}"
                        rm -f "$tmp_gz"
                    fi
                    ;;
                *)
                    local dest="${DOWNLOAD_DIR}/${fname}"
                    echo "[payload] Downloading ${src} -> ${dest}" >&2
                    download_url "$src" "$dest" || die "Failed to download ${src}"
                    ;;
            esac
            ;;
        *)
            fname=$(echo "$src" | sed 's|.*/||')
            case "$src" in
                *.zst)
                    [ -f "$src" ] || die "Local file not found: ${src}"
                    if [ "$preserve_compressed" = "1" ]; then
                        local dest="$src"
                    else
                        local dest="${DOWNLOAD_DIR}/${fname%.zst}"
                        echo "[payload] Extracting ${src} -> ${dest}" >&2
                        zstd -dc "$src" > "$dest" || die "Failed to extract ${src}"
                    fi
                    ;;
                *.gz)
                    [ -f "$src" ] || die "Local file not found: ${src}"
                    if [ "$preserve_compressed" = "1" ]; then
                        local dest="$src"
                    else
                        local dest="${DOWNLOAD_DIR}/${fname%.gz}"
                        echo "[payload] Extracting ${src} -> ${dest}" >&2
                        gunzip -c "$src" > "$dest" || die "Failed to extract ${src}"
                    fi
                    ;;
                *)
                    local dest="$src"
                    [ -f "$src" ] || die "Local file not found: ${src}"
                    echo "[payload] Using local file ${src}" >&2
                    ;;
            esac
            ;;
    esac
    echo "$dest"
}

echo "Resolve payload files"

if [ -n "$OSPI_SRC" ]; then
    OSPI_PATH=$(resolve_source "$OSPI_SRC")
else
    OSPI_PATH=""
    echo "[payload] OSPI not provided -- skipping OSPI phase"
fi

if [ -n "$WIC_SRC" ]; then
    if [ -n "$BMAP_SRC" ]; then
        WIC_PATH=$(resolve_source "$WIC_SRC" 1)
    else
        WIC_PATH=$(resolve_source "$WIC_SRC")
    fi
else
    WIC_PATH=""
    echo "[payload] WIC not provided -- skipping rootfs/SDA phase"
fi

if [ -n "$BMAP_SRC" ]; then
    BMAP_PATH=$(resolve_source "$BMAP_SRC")
else
    BMAP_PATH=""
fi

echo "  OSPI_PATH=${OSPI_PATH:-(skipped)}"
echo "  WIC_PATH=${WIC_PATH:-(skipped)}"
echo "  BMAP_PATH=${BMAP_PATH:-(not provided)}"
echo "  RANGES=${RANGES_SPEC:-(whole image)}"
echo "  ZERO_FIRST=${ZERO_SPEC:+yes}${ZERO_SPEC:-no}"

echo "Programmer Init"

echo "[payload] Running Versal reprogram sequence"
# Source Vivado/HW Server settings (works with HW Server, Vivado Lab, or full
# Vivado). The release is resolved centrally so it always matches any running
# hw_server; /sbin/versal-reprogram inherits xsdb from the PATH set here.
vivado_setup_env payload || die "Cannot find Vivado/HW Server settings under $(vivado_base_dir)/"
command -v xsdb >/dev/null 2>&1 || die "xsdb not found on PATH after sourcing the Vivado settings"
/sbin/versal-reprogram

echo "[payload] Running payload programming"
# Empty slots are forwarded as "-" so program_versal_payload skips that phase.
# RANGE_ARGS is intentionally unquoted: it expands to repeated --ufs-range pairs.
# shellcheck disable=SC2086
/sbin/program_versal_payload "${OSPI_PATH:--}" "${WIC_PATH:--}" "${BMAP_PATH:--}" $RANGE_ARGS $ZERO_ARGS

echo "Reconfigure Versal to boot from OSPI"

# Resolve the root port the Versal sits behind from the board's DMI board_name
# (see /sbin/versal-root-port). Falls back to the RAVE2-CORE root port if the
# helper is unavailable.
ROOT_PORT="$(/sbin/versal-root-port 2>/dev/null || echo 0000:00:02.3)"
SYSFS="/sys/bus/pci/devices"
ROOT_PATH="${SYSFS}/${ROOT_PORT}"
ROOT_REAL="$(readlink -f "${ROOT_PATH}")"

# Remove PCI endpoints under the root port before reset
echo "[payload] Removing Versal PCI endpoints..."
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

# Trigger POR_B reset with MODE3 high -> Versal boots from OSPI
echo "[payload] Reconfiguring Versal (POR_B reset, MODE3=high)..."
/sbin/reconfigure_versal --mode3-high

# Rescan PCI bus and wait for Versal to re-enumerate
echo "[payload] Rescanning PCI bus..."
sleep 1
echo 1 > "${ROOT_PATH}/rescan"

echo "[payload] Waiting for Versal to re-enumerate..."
TRIES=0
MAX_TRIES=30
while [ $TRIES -lt $MAX_TRIES ]; do
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
        echo "[payload] Versal PCIe endpoint detected."
        break
    fi
    TRIES=$((TRIES + 1))
    sleep 1
done

if [ $FOUND -eq 0 ]; then
    echo "[payload] WARNING: Versal did not re-enumerate after ${MAX_TRIES}s"
fi

echo "[payload] Done."
