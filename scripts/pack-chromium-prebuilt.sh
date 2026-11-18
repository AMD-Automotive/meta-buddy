#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

set -eu

usage() {
    cat <<'EOF'
Usage: pack-chromium-prebuilt.sh [options]

Create a Chromium prebuilt fileset for meta-erica-x86.

Options:
  --build-dir DIR     BitBake build directory to inspect.
  --image-dir DIR     Existing Chromium image/ directory to package.
  --deb FILE          Chromium runtime .deb to extract.
    --output-dir DIR    Destination directory for the tarball.
  --arch NAME         Output artifact architecture label. Default: amd64.
    --date YYYY_MM_DD   Date suffix for the archive name. Default: current UTC date.
  --keep-work         Keep the temporary extraction directory.
  -h, --help          Show this help.

If neither --image-dir nor --deb is supplied, the script tries to find a
Chromium image/ directory under the build tree first and falls back to the
latest chromium-ozone-wayland runtime .deb in tmp/deploy/deb/core2-64.
EOF
}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LAYER_DIR=$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)
DEFAULT_BUILD_DIR="$(CDPATH= cd -- "${LAYER_DIR}/.." && pwd)/build"

BUILD_DIR="${BUILD_DIR:-${DEFAULT_BUILD_DIR}}"
IMAGE_DIR=""
DEB_PATH=""
OUTPUT_DIR="${LAYER_DIR}/recipes-browser/chromium/files"
OUTPUT_ARCH="amd64"
OUTPUT_DATE=$(date -u '+%Y_%m_%d')
KEEP_WORK=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --build-dir)
            BUILD_DIR="$2"
            shift 2
            ;;
        --image-dir)
            IMAGE_DIR="$2"
            shift 2
            ;;
        --deb)
            DEB_PATH="$2"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --arch)
            OUTPUT_ARCH="$2"
            shift 2
            ;;
        --date)
            OUTPUT_DATE="$2"
            shift 2
            ;;
        --keep-work)
            KEEP_WORK=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

find_latest_image_dir() {
    if [ ! -d "${BUILD_DIR}/tmp/work" ]; then
        return 1
    fi

    find "${BUILD_DIR}/tmp/work" \
        -type d \
        -path '*/chromium-ozone-wayland/*/image' \
        | sort -V \
        | tail -n 1
}

find_latest_deb() {
    deploy_dir="${BUILD_DIR}/tmp/deploy/deb/core2-64"
    if [ ! -d "${deploy_dir}" ]; then
        return 1
    fi

    find "${deploy_dir}" \
        -maxdepth 1 \
        -type f \
        -name 'chromium-ozone-wayland_[0-9]*_amd64.deb' \
        | sort -V \
        | tail -n 1
}

if [ -z "${IMAGE_DIR}" ] && [ -z "${DEB_PATH}" ]; then
    IMAGE_DIR=$(find_latest_image_dir || true)
    if [ -z "${IMAGE_DIR}" ]; then
        DEB_PATH=$(find_latest_deb || true)
    fi
fi

if [ -n "${IMAGE_DIR}" ] && [ ! -d "${IMAGE_DIR}" ]; then
    echo "Image directory not found: ${IMAGE_DIR}" >&2
    exit 1
fi

if [ -n "${DEB_PATH}" ] && [ ! -f "${DEB_PATH}" ]; then
    echo "Debian package not found: ${DEB_PATH}" >&2
    exit 1
fi

if [ -z "${IMAGE_DIR}" ] && [ -z "${DEB_PATH}" ]; then
    echo "Could not find either a Chromium image directory or a Chromium .deb package." >&2
    exit 1
fi

WORK_DIR=$(mktemp -d)
ROOTFS_DIR="${WORK_DIR}/rootfs"
mkdir -p "${ROOTFS_DIR}"

cleanup() {
    if [ "${KEEP_WORK}" = "1" ]; then
        echo "Temporary workspace retained at ${WORK_DIR}" >&2
        return
    fi
    rm -rf "${WORK_DIR}"
}
trap cleanup EXIT INT TERM

PV=""

if [ -n "${IMAGE_DIR}" ]; then
    PV=$(basename "$(dirname "${IMAGE_DIR}")")
    cp -a "${IMAGE_DIR}/." "${ROOTFS_DIR}/"
else
    deb_name=$(basename "${DEB_PATH}")
    version_release=${deb_name#chromium-ozone-wayland_}
    version_release=${version_release%_amd64.deb}
    PV=${version_release%-r*}

    if command -v dpkg-deb >/dev/null 2>&1; then
        dpkg-deb -x "${DEB_PATH}" "${ROOTFS_DIR}"
    else
        DEB_EXTRACT_DIR="${WORK_DIR}/deb"
        mkdir -p "${DEB_EXTRACT_DIR}"
        (
            cd "${DEB_EXTRACT_DIR}"
            ar x "${DEB_PATH}"
        )

        data_tar=$(find "${DEB_EXTRACT_DIR}" -maxdepth 1 -type f -name 'data.tar.*' | head -n 1)
        if [ -z "${data_tar}" ]; then
            echo "Could not find data.tar payload inside ${DEB_PATH}" >&2
            exit 1
        fi

        case "${data_tar}" in
            *.tar)
                tar -xf "${data_tar}" -C "${ROOTFS_DIR}"
                ;;
            *.tar.gz)
                tar -xzf "${data_tar}" -C "${ROOTFS_DIR}"
                ;;
            *.tar.xz)
                tar -xJf "${data_tar}" -C "${ROOTFS_DIR}"
                ;;
            *.tar.zst)
                tar --zstd -xf "${data_tar}" -C "${ROOTFS_DIR}"
                ;;
            *)
                echo "Unsupported data payload format: ${data_tar}" >&2
                exit 1
                ;;
        esac
    fi
fi

mkdir -p "${OUTPUT_DIR}"

artifact_base="chromium-ozone-wayland-${PV}_${OUTPUT_DATE}-${OUTPUT_ARCH}-rootfs"
archive_path="${OUTPUT_DIR}/${artifact_base}.tar.xz"

tar -C "${ROOTFS_DIR}" -cJf "${archive_path}" .

echo "Created Chromium prebuilt fileset:"
echo "  ${archive_path}"