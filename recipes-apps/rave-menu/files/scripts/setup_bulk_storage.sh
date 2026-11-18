#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

set -u

DEVICE="${RAVE_STORAGE_DEVICE:-/dev/nvme0n1p5}"
MOUNT_POINT="${RAVE_STORAGE_MOUNT:-/var/lib/rave-menu/storage}"
STATE_ROOT="${RAVE_STORAGE_STATE_ROOT:-/var/lib/rave-menu}"
STATUS_FILE="${RAVE_STORAGE_STATUS:-/run/rave-menu/storage-status}"
EXPECTED_LABEL="rave-data"
MARKER=".rave-storage-v1"
MANAGED_DIRS="uploads payload-cache app-cache updates"

log() { echo "[rave-storage] $*"; }

write_status() {
    state="$1"
    reason="$2"
    mkdir -p "$(dirname "$STATUS_FILE")"
    {
        echo "state=$state"
        echo "reason=$reason"
        echo "device=$DEVICE"
        echo "mount_point=$MOUNT_POINT"
    } > "${STATUS_FILE}.tmp"
    mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
}

degraded() {
    log "degraded: $1"
    write_status degraded "$1"
    exit 0
}

mounted_source() {
    findmnt -rn -o SOURCE --mountpoint "$1" 2>/dev/null || true
}

mounted_root() {
    findmnt -rn -o FSROOT --mountpoint "$1" 2>/dev/null || true
}

source_device() {
    echo "$1" | sed 's/\[.*$//'
}

bind_targets=""
storage_mounted_here=0
cleanup_partial() {
    for target in $bind_targets; do
        umount "$target" 2>/dev/null || true
    done
    if [ "$storage_mounted_here" = "1" ]; then
        umount "$MOUNT_POINT" 2>/dev/null || true
    fi
}

[ -b "$DEVICE" ] || degraded "device-missing"

label=$(blkid -o value -s LABEL "$DEVICE" 2>/dev/null || true)
[ "$label" = "$EXPECTED_LABEL" ] || degraded "unexpected-label"

mkdir -p "$MOUNT_POINT"
source=$(mounted_source "$MOUNT_POINT")
if [ -n "$source" ]; then
    [ "$(readlink -f "$(source_device "$source")")" = "$(readlink -f "$DEVICE")" ] \
        || degraded "unexpected-mount-source"
else
    mount "$DEVICE" "$MOUNT_POINT" 2>/dev/null \
        || degraded "mount-failed"
    storage_mounted_here=1
fi

[ -f "$MOUNT_POINT/$MARKER" ] || {
    cleanup_partial
    degraded "marker-missing"
}

for name in $MANAGED_DIRS; do
    storage_dir="$MOUNT_POINT/$name"
    target="$STATE_ROOT/$name"
    mkdir -p "$storage_dir" "$target" || {
        cleanup_partial
        degraded "directory-setup-failed"
    }

    target_source=$(mounted_source "$target")
    if [ -n "$target_source" ]; then
        [ "$(readlink -f "$(source_device "$target_source")")" = "$(readlink -f "$DEVICE")" ] \
            || {
                cleanup_partial
                degraded "unexpected-bind-source"
            }
        [ "$(mounted_root "$target")" = "/$name" ] || {
            cleanup_partial
            degraded "unexpected-bind-root"
        }
        continue
    fi

    mount --bind "$storage_dir" "$target" 2>/dev/null || {
        cleanup_partial
        degraded "bind-mount-failed"
    }
    bind_targets="$target $bind_targets"
done

write_status mounted ok
log "mounted $DEVICE at $MOUNT_POINT"
exit 0