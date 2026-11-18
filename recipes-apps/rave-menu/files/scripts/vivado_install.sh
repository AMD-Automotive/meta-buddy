#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# vivado_install.sh -- extract the AMD Vivado HW Server installer tarball,
# run xsetup --batch Install, and patch the embedded paths in settings64*.
#
# Invoked by routes_vivado_install.py via Popen; stdout is streamed to the
# browser through the existing /api/stream/<job_id> SSE channel.
#
# Args:
#   $1  absolute path to the verified installer .tar
#   $2  install location (e.g. /opt/tools)
#   $3  version subdir to install into (e.g. 2025.2, 2026.1). Several
#       versions can coexist; only this one is touched.
#   $4  state.json path to update on success
#   $5  expected SHA-256 (for the success record)
#
# Exit codes:
#   0  install complete
#   2  bad arguments
#   3  tar extraction failed
#   4  xsetup failed
set -eu

TAR="${1:-}"
INSTALL_PREFIX="${2:-}"
VERSION="${3:-}"
STATE_PATH="${4:-}"
EXPECTED_SHA="${5:-}"

if [ -z "$TAR" ] || [ -z "$INSTALL_PREFIX" ] || [ -z "$VERSION" ] || [ -z "$STATE_PATH" ]; then
    echo "ERROR: bad arguments" >&2
    exit 2
fi

if [ ! -f "$TAR" ]; then
    echo "ERROR: installer tar not found: $TAR" >&2
    exit 2
fi

WORK="$(mktemp -d /var/lib/rave-menu/vivado/work-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# Wipe any prior install at the target version dir before extracting. xsetup
# refuses to overwrite an existing location, and we may be re-installing
# after a state.json was lost (e.g. files copied across a slot update but
# the manifest was not). This is the only place we ever delete from
# $INSTALL_PREFIX, and we scope it strictly to the version subdir so other
# installed versions survive.
TARGET_DIR="$INSTALL_PREFIX/$VERSION"
if [ -e "$TARGET_DIR" ]; then
    case "$TARGET_DIR" in
        /|""|/opt|/opt/tools)
            echo "ERROR: refusing to wipe suspicious target path: $TARGET_DIR"
            exit 2
            ;;
    esac
    echo "Step: cleanup"
    echo "Removing existing install at $TARGET_DIR ..."
    rm -rf "$TARGET_DIR"
fi
# Also drop any stale state file -- it will be re-written on success.
if [ -f "$STATE_PATH" ]; then
    rm -f "$STATE_PATH"
fi

echo "Step: extract"
echo "Extracting $(basename "$TAR") ..."
tar -xf "$TAR" -C "$WORK" || {
    echo "ERROR: extraction failed"
    exit 3
}

# Locate the extracted directory (single top-level dir from xsetup tarballs).
SRC_DIR="$(find "$WORK" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [ -z "$SRC_DIR" ] || [ ! -x "$SRC_DIR/xsetup" ]; then
    echo "ERROR: extracted tar missing xsetup (looked in $SRC_DIR)"
    exit 3
fi
echo "Extracted into $SRC_DIR"

# xsetup writes ~/.Xilinx; route it under our work dir so a stale state from
# a previous attempt doesn't confuse it.
export HOME="$WORK/home"
export TMPDIR="$WORK/tmp"
export LC_ALL=C
export LANG=C
mkdir -p "$HOME" "$TMPDIR"

echo "Step: install"
echo "Running xsetup --batch Install (this takes a few minutes)..."

# Same arguments the build-time recipe used. Install directly to $INSTALL_PREFIX
# (xsetup will create $INSTALL_PREFIX/$VERSION/...).
mkdir -p "$INSTALL_PREFIX"
cd "$SRC_DIR"

set +e
./xsetup \
    --batch Install \
    -a XilinxEULA,3rdPartyEULA \
    --edition "Hardware Server (Standalone)" \
    --location "$INSTALL_PREFIX" 2>&1
xrc=$?
set -e

if [ $xrc -ne 0 ]; then
    echo "ERROR: xsetup exited with status $xrc"
    exit 4
fi

# Sanity: did xsetup create the expected version directory? A tar for a
# different release would have landed elsewhere under $INSTALL_PREFIX.
if [ ! -d "$TARGET_DIR" ]; then
    echo "ERROR: expected install dir not present: $TARGET_DIR"
    echo "       the installer tar is probably not the $VERSION release"
    exit 4
fi

echo "Step: tidy"
# Drop xsetup's housekeeping dir so it doesn't linger on the rootfs. xsetup
# writes .xinstall at the *install prefix* ($INSTALL_PREFIX/.xinstall), not the
# version subdir, so remove both spellings defensively. Not needed at runtime.
rm -rf "$INSTALL_PREFIX/.xinstall" "$TARGET_DIR/.xinstall" || true

# Fix any settings64* scripts that hard-coded a transient path.
for subdir in HWSRVR Vivado_Lab Vivado_HW_Server Vivado; do
    d="$TARGET_DIR/$subdir"
    [ -d "$d" ] || continue
    find "$d" -maxdepth 1 \( -name 'settings64*' -o -name '.settings64*' \) | while read -r f; do
        sed -i \
            -e "s#$WORK/install-root/opt/tools#$INSTALL_PREFIX#g" \
            "$f" 2>/dev/null || true
    done
    break
done

# Surface the resolved hw_server binary so the SSE log makes the result obvious.
HW=""
for cand in HWSRVR/bin/hw_server Vivado_HW_Server/bin/hw_server Vivado_Lab/bin/hw_server Vivado/bin/hw_server; do
    if [ -x "$TARGET_DIR/$cand" ]; then
        HW="$TARGET_DIR/$cand"
        break
    fi
done
if [ -z "$HW" ]; then
    echo "WARNING: could not find hw_server binary under $TARGET_DIR"
fi

echo "Step: state"
mkdir -p "$(dirname "$STATE_PATH")"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "$STATE_PATH" <<EOF
{
  "installed": true,
  "version": "$VERSION",
  "sha256": "$EXPECTED_SHA",
  "installed_at": "$NOW",
  "hw_server": "$HW"
}
EOF

echo "Step: cleanup-installer"
# Reclaim the ~1 GB installer tarball now that the tools are installed. It was
# only staged so xsetup could run; the installed tree under $INSTALL_PREFIX is
# what gets propagated across RAUC slot updates, not this tar.
if [ -n "$TAR" ] && [ -f "$TAR" ]; then
    echo "Removing cached installer $(basename "$TAR") ($(du -h "$TAR" 2>/dev/null | cut -f1)) ..."
    rm -f "$TAR"
fi
# Also drop any tar left in the uploads staging dir. A browser-uploaded
# installer lands there (~1 GB) and is only the source for the cache copy
# above, so once the install has succeeded it is pure dead weight on the rootfs.
UPLOADS_DIR="$(dirname "$STATE_PATH")/uploads"
if [ -d "$UPLOADS_DIR" ]; then
    for up in "$UPLOADS_DIR"/*.tar "$UPLOADS_DIR"/*.tar.*; do
        [ -f "$up" ] || continue
        echo "Removing staged upload $(basename "$up") ($(du -h "$up" 2>/dev/null | cut -f1)) ..."
        rm -f "$up"
    done
fi

echo "Vivado HW Server $VERSION installed into $TARGET_DIR."
[ -n "$HW" ] && echo "hw_server: $HW"
echo "Step: done"
exit 0
