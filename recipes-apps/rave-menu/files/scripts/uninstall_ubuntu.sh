#!/bin/bash
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# uninstall_ubuntu.sh — remove an Ubuntu install created by install_ubuntu.sh.
#
# Invoked by the rave-menu App Store "Uninstall Ubuntu" action via
# /api/run/uninstall-ubuntu. Runs as root. All output is line-buffered and
# streamed to the browser through the existing SSE terminal.
#
# What it does (free space only — never touches p1..p5):
#   1. Release any mounts/swap on the Ubuntu root/swap partitions.
#   2. Delete the Ubuntu root (p6) and swap (p7) partitions, returning the
#      NVMe disk to the same free-space layout it had before Ubuntu was ever
#      installed (rave2 install itself does not create p6/p7; those only
#      exist once Ubuntu has been installed at least once).
#   3. Clear the Ubuntu grubenv flags so the boot menu and default-boot
#      selector fall back to the rave2 menu.
#
# This is the reverse of install_ubuntu.sh's partition() + configure_grub()
# steps and reuses the same release/teardown logic.

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────
NVME_DISK="${NVME_DISK:-/dev/nvme0n1}"
ROOT_PART="${NVME_DISK}p6"
SWAP_PART="${NVME_DISK}p7"

UBUNTU_BROWSE_MOUNT="${UBUNTU_BROWSE_MOUNT:-/run/rave-menu/ubuntu-root}"

GRUB_TEMPLATE_CANDIDATES=(
    /boot/efi/EFI/BOOT/grub.cfg
    /boot/efi/EFI/rave2/grub.cfg
    /boot/EFI/BOOT/grub.cfg
)

# ── Output helpers ───────────────────────────────────────────────────
step() { printf '\n==> %s\n' "$*"; }
log()  { printf '    %s\n' "$*"; }
die()  { printf '\n!!! ERROR: %s\n' "$*" >&2; exit 1; }

# ── Releasing the Ubuntu partitions ──────────────────────────────────
# Mirrors install_ubuntu.sh::release_device() — a partition can be held by
# more than one mount at once (udev automount under /run/media, plus the
# rave-menu file browser's read-only mount), so `umount <device>` only drops
# one of them and a busy mount has to have its users killed off first.
release_device() {
    local dev="$1" target tries=0
    [ -b "$dev" ] || return 0
    while :; do
        target=$(findmnt -rn -o TARGET -S "$dev" 2>/dev/null | head -n1)
        [ -n "$target" ] || return 0
        tries=$(( tries + 1 ))
        [ "$tries" -le 30 ] || return 1
        log "Unmounting $dev from $target ..."
        umount "$target" 2>/dev/null && continue
        log "$target is busy; stopping the processes using it ..."
        fuser -km "$target" 2>/dev/null || true
        sleep 2
        umount "$target" 2>/dev/null || umount -l "$target" 2>/dev/null || true
    done
}

# ── Preconditions ────────────────────────────────────────────────────
preflight() {
    step "Checking preconditions"
    [ "$(id -u)" = "0" ] || die "Must run as root."

    local root_src root_real
    root_src=$(findmnt -no SOURCE / 2>/dev/null || true)
    root_real=$(readlink -f "$root_src" 2>/dev/null || echo "$root_src")
    case "$root_real" in
        /dev/*) : ;;
        *) root_real=$(readlink -f /dev/root 2>/dev/null || echo "$root_real") ;;
    esac
    case "$root_real" in
        *nvme*) : ;;
        *) die "Ubuntu uninstall is only supported when running from the NVMe system (root=$root_src)." ;;
    esac

    [ -b "$NVME_DISK" ] || die "NVMe disk not found: $NVME_DISK"

    for t in parted partprobe partx blkid findmnt fuser sfdisk; do
        command -v "$t" >/dev/null 2>&1 || die "Required tool missing: $t"
    done

    if ! parted -sm "$NVME_DISK" print 2>/dev/null | grep -q '^6:'; then
        die "No Ubuntu partition (${ROOT_PART}) found; nothing to uninstall."
    fi
    log "Running from: $root_src"
    log "Target disk:  $NVME_DISK"
}

# ── Release + delete partitions ──────────────────────────────────────
remove_partitions() {
    step "Releasing Ubuntu partitions"
    swapoff "$SWAP_PART" 2>/dev/null || true
    # Drop the read-only file-browser mount too, if it's currently attached.
    mountpoint -q "$UBUNTU_BROWSE_MOUNT" 2>/dev/null \
        && { umount "$UBUNTU_BROWSE_MOUNT" 2>/dev/null || umount -l "$UBUNTU_BROWSE_MOUNT" 2>/dev/null || true; }
    release_device "$ROOT_PART" \
        || die "$ROOT_PART is still mounted and could not be released."
    release_device "$SWAP_PART" \
        || die "$SWAP_PART is still mounted and could not be released."

    step "Deleting Ubuntu partitions (${ROOT_PART}, ${SWAP_PART})"
    for p in 7 6; do
        if parted -sm "$NVME_DISK" print 2>/dev/null | grep -q "^${p}:"; then
            log "Removing partition ${p} ..."
            sfdisk --no-reread --force --delete "$NVME_DISK" "$p" >/dev/null
        fi
    done

    partprobe "$NVME_DISK" 2>/dev/null || true
    for p in 6 7; do
        partx -d --nr "$p" "$NVME_DISK" 2>/dev/null || true
    done
    udevadm settle --timeout=15 2>/dev/null || true

    if [ -b "$ROOT_PART" ] || [ -b "$SWAP_PART" ]; then
        log "WARNING: device node(s) for partition 6/7 still present after removal; a reboot will clear them."
    else
        log "Free NVMe space reclaimed."
    fi
}

# ── GRUB / grubenv cleanup ────────────────────────────────────────────
clear_grub_state() {
    step "Clearing Ubuntu boot state"
    local esp_cfg=""
    for c in "${GRUB_TEMPLATE_CANDIDATES[@]}"; do
        [ -f "$c" ] && esp_cfg="$c" && break
    done
    if [ -z "$esp_cfg" ]; then
        log "WARNING: could not find the ESP grub.cfg; skipping grubenv cleanup."
        return 0
    fi
    local grubenv="${esp_cfg%/grub.cfg}/grubenv"
    [ -f "$grubenv" ] || { log "No grubenv found; nothing to clear."; return 0; }

    # If the newer selector currently points at Ubuntu, fall back to the menu.
    if grub-editenv "$grubenv" list 2>/dev/null | grep -q '^boot_default=ubuntu$'; then
        grub-editenv "$grubenv" set boot_default=menu
    fi

    for key in ubuntu_installed ubuntu_fsuuid ubuntu_partuuid ubuntu_default; do
        grub-editenv "$grubenv" unset "$key" 2>/dev/null || true
    done
    log "grubenv cleared: Ubuntu is no longer selectable from the boot menu."
}

# ── Main ─────────────────────────────────────────────────────────────
main() {
    preflight
    remove_partitions
    clear_grub_state
    sync

    step "Uninstall complete"
    log "Ubuntu has been removed. The NVMe disk is back to its original,"
    log "free-space state (partitions 1-5 are untouched)."
}

main "$@"
