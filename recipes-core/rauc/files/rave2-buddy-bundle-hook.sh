#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# RAUC install-check and per-slot post-install hook for rave2-buddy-bundle.
#
# Runs after RAUC has written the new rootfs into $RAUC_SLOT_MOUNT_POINT
# and while that slot is still mounted, but before RAUC switches the boot
# flag. We use this window to copy user state from the currently-running
# slot into the freshly-installed one so it survives the update:
#
#   /opt/tools/                     -- Vivado HW Server install tree
#   /var/lib/rave-menu/vivado/      -- state.json + cached installer tar
#   /etc/rave-menu/localization.json -- keyboard layout and language
#
# This hook ships inside the bundle, so the copy that runs is always the one
# from the bundle being installed, never the one already on the device.
#
# Storage failures must not abort an update. A legacy app-b partition is
# initialized during slot-post-install; damaged or missing storage is left
# untouched and the new rootfs boots using its local directories.

set -u

# RAUC consumes hook stdout as its event protocol. Emitting lines in the
# "message=..." format causes them to surface in `rauc install` output and
# in journald, so the operator can see what the hook did. Anything written
# to stderr would only land in RAUC's debug log.
log() { printf 'message=%s\n' "[$(date '+%Y-%m-%dT%H:%M:%S%z')] bundle-hook: $*"; }

DATA_DEV="${RAVE_STORAGE_DEVICE:-/dev/nvme0n1p5}"
DATA_LABEL="rave-data"
DATA_MARKER=".rave-storage-v1"

storage_mountpoint() {
    findmnt -rn -o TARGET --source "$DATA_DEV" 2>/dev/null || true
}

release_legacy_storage() {
    attempts=0
    while :; do
        mountpoint=$(storage_mountpoint | head -n1)
        [ -n "$mountpoint" ] || return 0
        attempts=$((attempts + 1))
        if [ "$attempts" -gt 16 ]; then
            log "legacy app-b keeps remounting; continuing degraded"
            return 1
        fi

        log "unmounting legacy app-b from $mountpoint"
        umount "$mountpoint" 2>/dev/null || {
            log "legacy app-b mount is busy; detaching $mountpoint"
            umount -l "$mountpoint" 2>/dev/null || {
                log "could not unmount legacy app-b from $mountpoint; continuing degraded"
                return 1
            }
        }
    done
}

verify_uploads() {
    src_dir="$1"
    dst_dir="$2"
    for src in "$src_dir"/*; do
        [ -f "$src" ] || continue
        dst="$dst_dir/$(basename "$src")"
        [ -f "$dst" ] || return 1
        src_hash=$(sha256sum "$src" 2>/dev/null | awk '{print $1}')
        dst_hash=$(sha256sum "$dst" 2>/dev/null | awk '{print $1}')
        [ -n "$src_hash" ] && [ "$src_hash" = "$dst_hash" ] || return 1
    done
    return 0
}

storage_preflight() {
    if [ ! -b "$DATA_DEV" ]; then
        log "bulk storage device missing; update will continue in degraded mode"
        return 0
    fi

    label=$(blkid -o value -s LABEL "$DATA_DEV" 2>/dev/null || true)
    mountpoint=$(storage_mountpoint)
    if [ "$label" = "app-b" ]; then
        log "legacy app-b partition detected; it will be initialized after slot install"
    elif [ "$label" = "$DATA_LABEL" ]; then
        if [ -n "$mountpoint" ] && [ ! -f "$mountpoint/$DATA_MARKER" ]; then
            log "rave-data marker missing; preserving storage and continuing degraded"
        else
            log "existing rave-data partition detected"
        fi
    else
        log "unexpected storage label '${label:-none}'; preserving partition and continuing degraded"
    fi
    return 0
}

initialize_legacy_storage() {
    [ -b "$DATA_DEV" ] || return 0
    label=$(blkid -o value -s LABEL "$DATA_DEV" 2>/dev/null || true)
    [ "$label" = "app-b" ] || return 0

    release_legacy_storage || return 0
    [ -z "$(storage_mountpoint)" ] || {
        log "legacy app-b is still mounted; refusing to format $DATA_DEV"
        return 0
    }

    log "formatting legacy app-b as rave-data"
    mkfs.ext4 -F -m 1 -L "$DATA_LABEL" "$DATA_DEV" >/dev/null 2>&1 || {
        log "could not format $DATA_DEV; continuing degraded"
        return 0
    }

    data_mnt="/tmp/rave-data-$$"
    mkdir -p "$data_mnt" || return 0
    mount "$DATA_DEV" "$data_mnt" 2>/dev/null || {
        log "could not mount initialized storage; continuing degraded"
        rmdir "$data_mnt" 2>/dev/null || true
        return 0
    }

    migration_ok=1
    mkdir -p "$data_mnt/uploads" "$data_mnt/payload-cache" \
        "$data_mnt/app-cache" "$data_mnt/updates" || migration_ok=0
    if [ "$migration_ok" = "1" ] && [ -d /var/lib/rave-menu/uploads ]; then
        cp -a /var/lib/rave-menu/uploads/. "$data_mnt/uploads/" 2>/dev/null \
            || migration_ok=0
        if [ "$migration_ok" = "1" ]; then
            verify_uploads /var/lib/rave-menu/uploads "$data_mnt/uploads" \
                || migration_ok=0
        fi
    fi
    if [ "$migration_ok" = "1" ]; then
        sync
        echo "version=1" > "$data_mnt/$DATA_MARKER.tmp" || migration_ok=0
    fi
    if [ "$migration_ok" = "1" ]; then
        sync
        mv "$data_mnt/$DATA_MARKER.tmp" "$data_mnt/$DATA_MARKER" \
            || migration_ok=0
        sync
    fi

    umount "$data_mnt" 2>/dev/null || true
    rmdir "$data_mnt" 2>/dev/null || true
    if [ "$migration_ok" = "1" ]; then
        log "legacy app-b initialized as rave-data"
    else
        log "storage initialization incomplete; reinstall is required, update will continue degraded"
    fi
    return 0
}

case "${1:-}" in
    install-check)
        storage_preflight
        exit 0
        ;;
    slot-post-install) ;;
    *)
        log "ignoring hook invocation \"${1:-}\""
        exit 0
        ;;
esac

DEST="${RAUC_SLOT_MOUNT_POINT:-}"
if [ -z "$DEST" ] || [ ! -d "$DEST" ]; then
    log "RAUC_SLOT_MOUNT_POINT not set or not a directory; skipping"
    exit 0
fi

initialize_legacy_storage

# Pick the best copy tool available. rsync preserves perms/symlinks and
# is much faster on a re-update; fall back to cp -a if it's missing.
copy_tree() {
    src="$1"
    dst="$2"
    if [ ! -e "$src" ]; then
        log "source $src does not exist; nothing to copy"
        return 0
    fi
    mkdir -p "$dst" || {
        log "could not create $dst; skipping"
        return 0
    }
    if command -v rsync >/dev/null 2>&1; then
        rsync -aHX --delete "$src/" "$dst/" 2>&1 | sed 's/^/  rsync: /' >&2 || \
            log "rsync $src -> $dst returned non-zero (continuing)"
    else
        cp -a "$src/." "$dst/" 2>&1 | sed 's/^/  cp: /' >&2 || \
            log "cp $src -> $dst returned non-zero (continuing)"
    fi
}

log "propagating Vivado state to ${RAUC_SLOT_NAME:-new slot} at $DEST"
copy_tree /opt/tools                    "$DEST/opt/tools"
copy_tree /var/lib/rave-menu/vivado     "$DEST/var/lib/rave-menu/vivado"

# Keyboard layout and language. Copied as a single file rather than with
# copy_tree because that uses rsync --delete, which would silently remove any
# file a future image ships under /etc/rave-menu/ that this slot happens not to
# have. Without this the setting reverts to the factory GB default on every
# firmware update.
if [ -f /etc/rave-menu/localization.json ]; then
    if mkdir -p "$DEST/etc/rave-menu" && \
       cp -a /etc/rave-menu/localization.json "$DEST/etc/rave-menu/"; then
        log "propagated localisation settings"
    else
        log "could not propagate localisation settings (continuing)"
    fi
fi

# Prune installer scratch that older images left behind so it doesn't get
# carried forward slot-to-slot forever. The ~1 GB installer.tar and xsetup's
# .xinstall/uploads are only needed transiently during an install; the runtime
# tools live under /opt/tools/<version>. We clean the DESTINATION (not the
# running slot) so an in-progress install on the active slot is untouched.
prune_installer_scratch() {
    for stale in \
        "$DEST/opt/tools/.xinstall" \
        "$DEST/var/lib/rave-menu/vivado/installer.tar" \
        "$DEST/var/lib/rave-menu/vivado/uploads"
    do
        if [ -e "$stale" ]; then
            sz="$(du -sh "$stale" 2>/dev/null | cut -f1)"
            log "pruning stale installer scratch $stale (${sz:-?})"
            rm -rf "$stale" || log "could not remove $stale (continuing)"
        fi
    done
}
prune_installer_scratch

# ── Upgrade ESP GRUB config after a RAUC update ──────────────────────
#
# Upgrade old GRUB configs on the ESP to the current template so that field
# devices gain SSDT cmdline support ($ssdt_param) and slot-local initrd
# loading after a RAUC update from a legacy image.
#
# NOTE: the SSDT is deliberately NOT prepended into the initrd here.  The
# ADAU1860 codec is gated by the menu toggle (grubenv `ssdt_enabled` ->
# `ssdt=` cmdline -> initramfs ssdt-loader).  Baking it into the early-ACPI
# override forced the codec on every boot, defeating the toggle and wedging
# the acp70-nau8822 card in EPROBE_DEFER when the codec was absent.
#
upgrade_esp_grub() {
    # Find boot partition - look for EFI partition with initrd
    BOOT_PART=""
    for part in /dev/nvme0n1p1 /dev/sda1; do
        if [ -b "$part" ]; then
            mount_point=$(findmnt -n -o TARGET "$part" 2>/dev/null || true)
            if [ -z "$mount_point" ]; then
                # Not mounted, try mounting temporarily
                TMP_BOOT="/tmp/boot-check-$$"
                mkdir -p "$TMP_BOOT"
                if mount -o ro "$part" "$TMP_BOOT" 2>/dev/null; then
                    if [ -f "$TMP_BOOT/initrd" ] || [ -d "$TMP_BOOT/EFI" ]; then
                        BOOT_PART="$part"
                        umount "$TMP_BOOT"
                    else
                        umount "$TMP_BOOT"
                    fi
                fi
                rmdir "$TMP_BOOT" 2>/dev/null || true
            elif [ -f "$mount_point/initrd" ] || [ -d "$mount_point/EFI" ]; then
                BOOT_PART="$part"
            fi
        fi
        [ -n "$BOOT_PART" ] && break
    done

    if [ -z "$BOOT_PART" ]; then
        log "Could not find boot partition; skipping GRUB config upgrade"
        return 0
    fi

    # Mount boot partition RW
    BOOT_MNT="/tmp/boot-fix-$$"
    mkdir -p "$BOOT_MNT"
    if ! mount "$BOOT_PART" "$BOOT_MNT" 2>/dev/null; then
        log "Could not mount $BOOT_PART; skipping GRUB config upgrade"
        rmdir "$BOOT_MNT" 2>/dev/null || true
        return 0
    fi

    # ── Upgrade ESP GRUB config if outdated ──────────────────────────
    # Old installs lack $ssdt_param on the kernel cmdline and slot-local
    # initrd loading.  Replace the entire grub.cfg from the current
    # template (shipped in the new rootfs), preserving device PARTUUIDs.
    upgrade_esp_grub_config() {
        local grub_cfg="$1"
        local template="$DEST/usr/share/grub/grub-nvme-rauc.cfg"

        [ -f "$grub_cfg" ] || return 0
        [ -f "$template" ] || {
            log "GRUB template not in new rootfs ($template); skipping config upgrade"
            return 0
        }

        # A template revision ensures changes to the config itself reach
        # already-deployed ESPs. Retain the feature checks for legacy images.
        #
        # The revision below is deliberately left at 2 rather than tracked
        # forward. No fielded ESP has carried 2 for a long time, so this test
        # always fails and the config is re-rendered on every update - which is
        # precisely what we want, and is how revision 7 reaches units in the
        # field. Bumping it would restore the intended skip, but the constant
        # has already been forgotten once, and a forgotten bump silently stops
        # template changes reaching devices: a much worse failure than an
        # unnecessary re-render. The cost of leaving it is that hand edits to a
        # device's grub.cfg do not survive an update, which is intended anyway.
        if grep -q 'ssdt_param' "$grub_cfg" && grep -q 'boot_part/boot/initrd' "$grub_cfg" \
            && grep -q 'menu_timeout' "$grub_cfg" \
            && grep -q 'ESP_GRUB_TEMPLATE_REVISION=2' "$grub_cfg"; then
            log "ESP GRUB config already current ($grub_cfg)"
            return 0
        fi

        log "Upgrading $grub_cfg from template..."

        # Extract device-specific PARTUUIDs from old config (order of first
        # appearance: MENU_A, MENU_B, APP_A when in active=A branch first)
        ALL_UUIDS=$(grep -o 'PARTUUID=[0-9a-f-]*' "$grub_cfg" | sed 's/PARTUUID=//' | awk '!seen[$0]++')
        MENU_A_UUID=$(echo "$ALL_UUIDS" | sed -n '1p')
        MENU_B_UUID=$(echo "$ALL_UUIDS" | sed -n '2p')
        APP_A_UUID=$(echo "$ALL_UUIDS" | sed -n '3p')

        if [ -z "$MENU_A_UUID" ] || [ -z "$MENU_B_UUID" ]; then
            log "Could not extract PARTUUIDs from $grub_cfg; skipping upgrade"
            return 0
        fi

        # Generate new config from template with device PARTUUIDs
        local tmp_cfg="${grub_cfg}.new.$$"
        sed -e "s/@@MENU_A_PARTUUID@@/$MENU_A_UUID/g" \
            -e "s/@@MENU_B_PARTUUID@@/$MENU_B_UUID/g" \
            -e "s/@@APP_A_PARTUUID@@/${APP_A_UUID:-00000000-0000-0000-0000-000000000000}/g" \
            "$template" > "$tmp_cfg" || {
            rm -f "$tmp_cfg"
            log "Failed to generate new GRUB config from template"
            return 0
        }

        if mv "$tmp_cfg" "$grub_cfg"; then
            log "Upgraded $grub_cfg (added SSDT + slot-local initrd support)"
        else
            rm -f "$tmp_cfg"
            log "Failed to replace $grub_cfg"
        fi
    }

    upgrade_esp_grub_config "$BOOT_MNT/EFI/BOOT/grub.cfg"
    upgrade_esp_grub_config "$BOOT_MNT/EFI/rave2/grub.cfg"

    umount "$BOOT_MNT"
    rmdir "$BOOT_MNT" 2>/dev/null || true
}

upgrade_esp_grub

sync || true
log "done"
exit 0
