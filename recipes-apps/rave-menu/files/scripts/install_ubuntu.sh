#!/bin/bash
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC, UH
# Maintainer: KC

# install_ubuntu.sh — install Ubuntu Desktop into the free NVMe space.
#
# Invoked by the rave-menu App Store "Install Ubuntu" action via
# /api/run/install-ubuntu. Runs as root. All output is line-buffered and
# streamed to the browser through the existing SSE terminal, so every echo
# here shows up live in the UI.
#
# What it does (free space only — never touches p1..p5):
#   1. Optionally reserve space ahead of Ubuntu for future "Jumbo
#      application" images (JUMBO_RESERVE_GIB, default 150 GiB) — left
#      unpartitioned, not formatted.
#   2. Create a root (ext4) + swap partition in the remaining free space.
#   3. Download the Ubuntu base rootfs from Canonical over the network.
#   4. apt-install the desktop + a stock kernel inside a chroot.
#   5. Create a passwordless-sudo autologin user (amd / amd).
#   6. Re-render the ESP GRUB config from the template and make Ubuntu the
#      default boot entry (rave2 menu stays selectable via the GRUB timeout).
#
# Design notes:
#   * The board normally boots via Xen with a custom kernel; Ubuntu boots
#     bare-metal with its own generic kernel (best-effort hardware support).
#   * rave2 GRUB remains the boot manager — Ubuntu's grub is never installed.
#   * mkfs.ext4 disables orphan_file so the rave2 GRUB ext2 module can read
#     Ubuntu's /boot.

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────
UBUNTU_RELEASE="${UBUNTU_RELEASE:-26.04}"
UBUNTU_CODENAME="${UBUNTU_CODENAME:-resolute}"
UBUNTU_ARCH="${UBUNTU_ARCH:-amd64}"
BASE_MIRROR="${BASE_MIRROR:-https://cdimage.ubuntu.com/ubuntu-base/releases}"
APT_MIRROR="${APT_MIRROR:-http://archive.ubuntu.com/ubuntu}"
SECURITY_MIRROR="${SECURITY_MIRROR:-http://security.ubuntu.com/ubuntu}"
DESKTOP_PACKAGE="${DESKTOP_PACKAGE:-ubuntu-desktop-minimal}"

NVME_DISK="${NVME_DISK:-/dev/nvme0n1}"
ROOT_PART="${NVME_DISK}p6"
SWAP_PART="${NVME_DISK}p7"
SWAP_MIB="${SWAP_MIB:-16384}"          # 16 GiB swap

# Space set aside ahead of the Ubuntu root partition for future "Jumbo
# application" images (see app_region.py). Left unpartitioned — this is
# just a reservation, not a partition, so a 0 value uses all free space
# for Ubuntu exactly as before this option existed. Ignored once a jumbo app is
# actually installed: Ubuntu then starts after the partitions it created.
JUMBO_RESERVE_GIB="${JUMBO_RESERVE_GIB:-150}"
APP_REGION="${APP_REGION:-/opt/rave-menu/app_region.py}"

# Floor for the Ubuntu root partition (excludes swap). ubuntu-desktop-minimal
# plus Docker and the amdgpu_top build toolchain need real headroom for apt
# upgrades and container images; below this the install is likely to fail
# or leave the user with an unusable amount of free space post-install.
# Mirrored in app_slots.UBUNTU_MIN_ROOT_GIB, which the App Store reads to warn
# when a jumbo application would leave no room for Ubuntu.
MIN_UBUNTU_ROOT_GIB="${MIN_UBUNTU_ROOT_GIB:-40}"

NEW_USER="${NEW_USER:-amd}"
NEW_PASS="${NEW_PASS:-amd}"
NEW_HOSTNAME="${NEW_HOSTNAME:-ubuntu-rave2}"

GRUB_TEMPLATE="${GRUB_TEMPLATE:-/usr/share/grub/grub-nvme-rauc.cfg}"
WORKDIR="/var/lib/rave-menu/ubuntu-install"
MNT="/mnt/ubuntu-install"

# Which OS boots by default after install: "ubuntu" (default) or "buddy".
UBUNTU_DEFAULT_BOOT=1

# ── Output helpers ───────────────────────────────────────────────────
step() { printf '\n==> %s\n' "$*"; }
log()  { printf '    %s\n' "$*"; }
die()  { printf '\n!!! ERROR: %s\n' "$*" >&2; exit 1; }

# ── Releasing the old Ubuntu partitions ──────────────────────────────
# A previous Ubuntu root can be held by more than one mount at once: the udev
# automounter puts it under /run/media, and the rave-menu file browser mounts
# it read-only under /run/rave-menu. `umount <device>` only drops one of them,
# and a busy mount has to have its users killed off before it will go.
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

# mkfs needs an exclusive open, which an unmounted-but-still-open device (a
# lazy umount, or udev still probing a freshly added partition) will refuse.
# `mkfs.ext4 -n` is a dry run that performs exactly that check.
wait_for_exclusive() {
    local dev="$1" tries=0
    while [ "$tries" -lt 15 ]; do
        mkfs.ext4 -F -n "$dev" >/dev/null 2>&1 && return 0
        tries=$(( tries + 1 )); sleep 1
    done
    return 1
}

# ── Cleanup (always run) ─────────────────────────────────────────────
SWAP_ACTIVE=0
cleanup() {
    set +e
    for d in dev/pts dev sys proc run; do
        mountpoint -q "$MNT/$d" 2>/dev/null && umount -l "$MNT/$d" 2>/dev/null
    done
    mountpoint -q "$MNT" 2>/dev/null && umount -l "$MNT" 2>/dev/null
    [ "$SWAP_ACTIVE" = "1" ] && swapoff "$SWAP_PART" 2>/dev/null
}
trap cleanup EXIT

# ── Preconditions ────────────────────────────────────────────────────
preflight() {
    step "Checking preconditions"
    [ "$(id -u)" = "0" ] || die "Must run as root."

    # Must be running from the NVMe install (not the recovery USB), since we
    # rely on the installed partition layout and the live ESP GRUB config.
    # The root source may be a symlink (e.g. /dev/disk/by-partuuid/...), so
    # resolve it to the real device before checking.
    local root_src root_real
    root_src=$(findmnt -no SOURCE / 2>/dev/null || true)
    root_real=$(readlink -f "$root_src" 2>/dev/null || echo "$root_src")
    # Fall back to the kernel's view if findmnt gives a non-device source.
    case "$root_real" in
        /dev/*) : ;;
        *) root_real=$(readlink -f /dev/root 2>/dev/null || echo "$root_real") ;;
    esac
    case "$root_real" in
        *nvme*) : ;;
        *) die "Ubuntu install is only supported when running from the NVMe system (root=$root_src)." ;;
    esac

    [ -b "$NVME_DISK" ]   || die "NVMe disk not found: $NVME_DISK"
    [ -b "${NVME_DISK}p1" ] || die "Expected partition ${NVME_DISK}p1 (EFI) is missing."
    [ -b "${NVME_DISK}p5" ] || die "Expected partition ${NVME_DISK}p5 (rave-data) is missing."
    [ -f "$GRUB_TEMPLATE" ] || die "GRUB template not found: $GRUB_TEMPLATE"

    for t in parted partprobe mkfs.ext4 mkswap swapon blkid wget tar chroot \
             grub-editenv findmnt fuser; do
        command -v "$t" >/dev/null 2>&1 || die "Required tool missing: $t"
    done
    log "Running from: $root_src"
    log "Target disk:  $NVME_DISK"
    log "Ubuntu:       $UBUNTU_RELEASE ($UBUNTU_CODENAME) / $DESKTOP_PACKAGE"
}

# ── Partitioning (free space only) ───────────────────────────────────
partition() {
    step "Preparing partitions in the free NVMe space"

    # Refuse to proceed if p3/p4/p5 are somehow absent — that would mean the
    # layout isn't what we expect and we must not guess where free space is.
    local free_start free_end
    # Determine the end of the last existing rave2 partition (p5) and the disk
    # end, both in MiB, from parted's machine-readable output.
    free_start=$(parted -sm "$NVME_DISK" unit MiB print 2>/dev/null \
        | awk -F: '/^5:/ {gsub(/MiB/,"",$3); printf "%d", $3+1}')
    free_end=$(parted -sm "$NVME_DISK" unit MiB print 2>/dev/null \
        | awk -F: '/^\/dev/ {gsub(/MiB/,"",$2); printf "%d", $2}')
    [ -n "$free_start" ] || die "Could not determine end of partition 5."
    [ -n "$free_end" ]   || die "Could not determine disk size."

    # The Jumbo-application reservation sits logically ahead of the Ubuntu
    # root partition, in the free space right after p5. It is left
    # unpartitioned (just a gap in the GPT) for a later stage to claim.
    local jumbo_mib=$(( JUMBO_RESERVE_GIB * 1024 ))

    # Once a jumbo app has claimed that space the reservation is moot: its
    # partitions are real, and Ubuntu must start after them or it would write
    # straight over the installed application.
    local app_end=0
    if [ -f "$APP_REGION" ]; then
        app_end=$(python3 "$APP_REGION" capacity 2>/dev/null | python3 -c \
            'import json,sys
try:
    d = json.load(sys.stdin)
except ValueError:
    d = {}
print(d.get("app_tail_end_mib", 0) if d.get("jumbo_installed") else 0)' \
            2>/dev/null || echo 0)
    fi
    if [ "${app_end:-0}" -gt "$free_start" ]; then
        log "Jumbo app installed: Ubuntu starts after it at ${app_end} MiB"
        free_start=$app_end
        jumbo_mib=0
    fi

    local root_start=$(( free_start + jumbo_mib ))

    local min_root_mib=$(( MIN_UBUNTU_ROOT_GIB * 1024 ))
    local swap_start=$(( free_end - SWAP_MIB ))
    [ "$swap_start" -gt "$root_start" ] \
        || die "Not enough free space for a ${JUMBO_RESERVE_GIB} GiB Jumbo reservation plus Ubuntu + swap."
    [ "$(( swap_start - root_start ))" -ge "$min_root_mib" ] \
        || die "Only $(( (swap_start - root_start) / 1024 )) GiB would be left for Ubuntu" \
               " (minimum is ${MIN_UBUNTU_ROOT_GIB} GiB) — reduce the Jumbo reservation and try again."

    log "Free region:  ${free_start} MiB .. ${free_end} MiB"
    if [ "$jumbo_mib" -gt 0 ]; then
        log "Jumbo apps:   ${free_start} MiB .. ${root_start} MiB (${JUMBO_RESERVE_GIB} GiB reserved, unpartitioned)"
    fi
    log "Root (p6):    ${root_start} MiB .. ${swap_start} MiB"
    log "Swap (p7):    ${swap_start} MiB .. ${free_end} MiB (${SWAP_MIB} MiB)"

    # Tear down any previous Ubuntu attempt so re-runs are clean. Only ever
    # remove partitions 6 and 7 — never the rave2 partitions 1..5.
    swapoff "$SWAP_PART" 2>/dev/null || true
    for d in dev/pts dev sys proc run; do
        mountpoint -q "$MNT/$d" 2>/dev/null && umount -l "$MNT/$d" 2>/dev/null || true
    done
    mountpoint -q "$MNT" 2>/dev/null && umount -l "$MNT" 2>/dev/null || true
    release_device "$ROOT_PART" \
        || die "$ROOT_PART is still mounted and could not be released."

    for p in 7 6; do
        if parted -sm "$NVME_DISK" print 2>/dev/null | grep -q "^${p}:"; then
            log "Removing existing partition ${p} ..."
            sfdisk --no-reread --force --delete "$NVME_DISK" "$p" >/dev/null
        fi
    done

    log "Creating root and swap partitions ..."
    # Pin the indices rather than letting parted pick the lowest free one: a
    # disk-image app frees p4 (its partitions live in the 20s), and parted would
    # then put Ubuntu there, breaking both the teardown above and the app region.
    printf '%sp6 : start=%sMiB, size=%sMiB, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="ubuntu"\n%sp7 : start=%sMiB, size=%sMiB, type=0657FD6D-A4AB-43C4-84E5-0933C84B4F4F, name="ubuntu-swap"\n' \
        "$NVME_DISK" "$root_start" "$(( swap_start - root_start ))" \
        "$NVME_DISK" "$swap_start" "$(( free_end - swap_start ))" \
        | sfdisk --no-reread --force --append "$NVME_DISK" >/dev/null

    # The boot disk always has mounted partitions, so the kernel will not
    # re-read its table; BLKPG via partx is the only thing that takes effect.
    partprobe "$NVME_DISK" 2>/dev/null || true
    for p in 6 7; do
        partx -d --nr "$p" "$NVME_DISK" 2>/dev/null || true
        partx -a --nr "$p" "$NVME_DISK" 2>/dev/null || true
    done
    udevadm settle --timeout=15 2>/dev/null || true

    # Wait for the new device nodes to appear.
    local c=0
    while [ "$c" -lt 15 ]; do
        [ -b "$ROOT_PART" ] && [ -b "$SWAP_PART" ] && break
        c=$(( c + 1 )); sleep 1
    done
    [ -b "$ROOT_PART" ] && [ -b "$SWAP_PART" ] || die "New partition nodes did not appear."

    # Re-adding the partition retriggers udev, which automounts anything with a
    # recognisable filesystem, so drop those mounts again before formatting.
    swapoff "$SWAP_PART" 2>/dev/null || true
    release_device "$ROOT_PART" \
        || die "$ROOT_PART was remounted and could not be released before formatting."
    wait_for_exclusive "$ROOT_PART" \
        || die "$ROOT_PART is still in use by another process; cannot format it."

    step "Formatting partitions"
    # Disable orphan_file: e2fsprogs 1.47 enables it by default but the rave2
    # GRUB ext2 module cannot read it, which would break /boot loading.
    log "mkfs.ext4 $ROOT_PART ..."
    mkfs.ext4 -F -O ^orphan_file -L ubuntu "$ROOT_PART"
    log "mkswap $SWAP_PART ..."
    mkswap -L ubuntu-swap "$SWAP_PART"

    # Use the new swap during install — the host has no swap and apt for the
    # desktop is memory hungry.
    if swapon "$SWAP_PART" 2>/dev/null; then
        SWAP_ACTIVE=1
        log "Activated swap for the install."
    fi
}

# ── Download + extract the Ubuntu base rootfs ────────────────────────
bootstrap_rootfs() {
    step "Downloading the Ubuntu base system"
    mkdir -p "$WORKDIR" "$MNT"
    mount "$ROOT_PART" "$MNT"

    local rel_url="${BASE_MIRROR}/${UBUNTU_RELEASE}/release"

    # The published file may carry a point-release version (e.g.
    # ubuntu-base-24.04.4-base-amd64.tar.gz) or a bare release at launch
    # (e.g. ubuntu-base-26.04-base-amd64.tar.gz). Resolve the newest one from
    # the release's SHA256SUMS so we always fetch the current image.
    log "Resolving latest ${UBUNTU_RELEASE} base image ..."
    local sums fname expected
    sums=$(wget -q -O - "${rel_url}/SHA256SUMS") \
        || die "Could not fetch SHA256SUMS from ${rel_url}"
    fname=$(printf '%s\n' "$sums" \
        | grep -oE "ubuntu-base-${UBUNTU_RELEASE}[0-9.]*-base-${UBUNTU_ARCH}\.tar\.gz" \
        | sort -V | tail -n1)
    [ -n "$fname" ] || die "No ${UBUNTU_ARCH} base image listed for ${UBUNTU_RELEASE}."
    expected=$(printf '%s\n' "$sums" | grep " [*]\?${fname}\$" | awk '{print $1}' | head -n1)

    local tarball="$WORKDIR/$fname"
    local url="${rel_url}/${fname}"
    rm -f "$tarball"

    log "Source: $url"
    # busybox wget's CR progress bar doesn't stream cleanly over SSE, so run it
    # quietly and report growing size from a background poller instead.
    wget -q -O "$tarball" "$url" &
    local wpid=$!
    ( while kill -0 "$wpid" 2>/dev/null; do
          sz=$(stat -c %s "$tarball" 2>/dev/null || echo 0)
          printf '    downloaded %d MiB\n' "$(( sz / 1024 / 1024 ))"
          sleep 3
      done ) &
    local ppid=$!
    if ! wait "$wpid"; then
        kill "$ppid" 2>/dev/null || true
        die "Download failed: $url"
    fi
    kill "$ppid" 2>/dev/null || true
    log "Download complete ($(stat -c %s "$tarball" 2>/dev/null) bytes)."

    if [ -n "$expected" ]; then
        step "Verifying checksum"
        local actual
        actual=$(sha256sum "$tarball" | awk '{print $1}')
        [ "$actual" = "$expected" ] || die "Checksum mismatch for $fname."
        log "Checksum OK ($fname)."
    fi

    step "Extracting the base system"
    tar --numeric-owner -xpf "$tarball" -C "$MNT"
    rm -f "$tarball"
    log "Base system extracted to $ROOT_PART."
}

# ── chroot helpers ───────────────────────────────────────────────────
mount_chroot() {
    step "Preparing the chroot environment"
    mount -t proc  proc   "$MNT/proc"
    mount -t sysfs sysfs  "$MNT/sys"
    mount --bind   /dev   "$MNT/dev"
    mount --bind   /dev/pts "$MNT/dev/pts"
    mount -t tmpfs tmpfs  "$MNT/run"
    cp -f /etc/resolv.conf "$MNT/etc/resolv.conf"
}

in_chroot() {
    # Run a command inside the chroot with a clean, non-interactive env.
    LANG=C LC_ALL=C DEBIAN_FRONTEND=noninteractive \
        chroot "$MNT" /usr/bin/env \
            DEBIAN_FRONTEND=noninteractive \
            PATH=/usr/sbin:/usr/bin:/sbin:/bin \
            "$@"
}

# ── Configure apt + install desktop and kernel ───────────────────────
install_packages() {
    step "Configuring apt sources"
    cat > "$MNT/etc/apt/sources.list" <<EOF
deb ${APT_MIRROR} ${UBUNTU_CODENAME} main restricted universe multiverse
deb ${APT_MIRROR} ${UBUNTU_CODENAME}-updates main restricted universe multiverse
deb ${SECURITY_MIRROR} ${UBUNTU_CODENAME}-security main restricted universe multiverse
EOF

    # Block service starts during package installation inside the chroot.
    cat > "$MNT/usr/sbin/policy-rc.d" <<'EOF'
#!/bin/sh
exit 101
EOF
    chmod +x "$MNT/usr/sbin/policy-rc.d"

    step "Updating package lists"
    in_chroot apt-get update

    step "Installing kernel and base utilities"
    in_chroot apt-get install -y --no-install-recommends \
        linux-generic initramfs-tools \
        sudo locales network-manager netplan.io \
        nano less ca-certificates \
        openssh-server busybox rsyslog apparmor

    step "Installing Docker"
    in_chroot apt-get install -y --no-install-recommends docker.io
    in_chroot test -x /usr/bin/docker || die "Docker installation did not provide /usr/bin/docker."
    [ -f "$MNT/lib/systemd/system/docker.service" ] || \
        die "Docker installation did not provide docker.service."

    step "Installing the Ubuntu desktop (this can take a while)"
    in_chroot apt-get install -y "$DESKTOP_PACKAGE"
}

install_tools() {
    step "Installing amdgpu_top"
    # amdgpu_top isn't packaged for Ubuntu; build it from crates.io with
    # the Ubuntu-provided rust toolchain. --locked is required: without it
    # cargo re-resolves dependencies and can pick a kstring version that
    # needs a newer rustc than the one Ubuntu ships. None of this is
    # allowed to fail the whole install — amdgpu_top is a nice-to-have.
    if in_chroot apt-get install -y --no-install-recommends cargo rustc libdrm-dev \
            && in_chroot env HOME=/root CARGO_HOME=/root/.cargo \
                cargo install amdgpu_top --root /usr/local --locked; then
        log "amdgpu_top installed to /usr/local/bin/amdgpu_top."
    else
        log "WARNING: amdgpu_top install failed; continuing without it."
    fi
}

# ── System configuration (user, autologin, fstab, locale) ────────────
configure_system() {
    step "Configuring the installed system"
    local root_uuid swap_uuid
    root_uuid=$(blkid -o value -s UUID "$ROOT_PART")
    swap_uuid=$(blkid -o value -s UUID "$SWAP_PART")
    [ -n "$root_uuid" ] || die "Could not read root filesystem UUID."

    log "Writing /etc/fstab"
    cat > "$MNT/etc/fstab" <<EOF
# Generated by rave-menu Ubuntu installer
UUID=${root_uuid}  /      ext4  errors=remount-ro  0 1
EOF
    [ -n "$swap_uuid" ] && echo "UUID=${swap_uuid}  none   swap  sw  0 0" >> "$MNT/etc/fstab"

    log "Setting hostname"
    echo "$NEW_HOSTNAME" > "$MNT/etc/hostname"
    cat > "$MNT/etc/hosts" <<EOF
127.0.0.1   localhost
127.0.1.1   ${NEW_HOSTNAME}
::1         localhost ip6-localhost ip6-loopback
EOF

    log "Generating locale (en_US.UTF-8)"
    echo "en_US.UTF-8 UTF-8" > "$MNT/etc/locale.gen"
    in_chroot locale-gen || true
    echo "LANG=en_US.UTF-8" > "$MNT/etc/default/locale"

    log "Creating user '${NEW_USER}' with sudo access"
    in_chroot useradd -m -s /bin/bash -G sudo,docker "$NEW_USER" || true
    printf '%s:%s\n' "$NEW_USER" "$NEW_PASS" | in_chroot chpasswd
    # Passwordless sudo for the new user.
    echo "${NEW_USER} ALL=(ALL) NOPASSWD: ALL" > "$MNT/etc/sudoers.d/90-${NEW_USER}"
    chmod 0440 "$MNT/etc/sudoers.d/90-${NEW_USER}"

    log "Enabling Docker at first boot"
    systemctl --root="$MNT" enable docker.service
    in_chroot getent group docker >/dev/null || die "Docker group is missing after Docker installation."

    log "Keeping AppArmor enabled at first boot"
    if ! systemctl --root="$MNT" enable apparmor.service 2>/dev/null; then
        log "WARNING: AppArmor service enablement helper unavailable; package remains installed."
    fi

    log "Enabling graphical autologin for '${NEW_USER}'"
    mkdir -p "$MNT/etc/gdm3"
    cat > "$MNT/etc/gdm3/custom.conf" <<EOF
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=${NEW_USER}
EOF

    # Setup vi/editor symlinks
    in_chroot ln -sf /bin/busybox /usr/local/bin/vi || true
    in_chroot ln -sf /bin/busybox /usr/local/bin/editor || true

    # Also configure NetworkManager to manage all interfaces by default.
    mkdir -p "$MNT/etc/netplan"
    cat > "$MNT/etc/netplan/01-network-manager-all.yaml" <<EOF
network:
  version: 2
  renderer: NetworkManager
EOF
    chmod 0600 "$MNT/etc/netplan/01-network-manager-all.yaml"

    step "Building the initramfs"
    in_chroot update-initramfs -u -k all || in_chroot update-initramfs -c -k all

    # Remove the service-start block before leaving the chroot.
    rm -f "$MNT/usr/sbin/policy-rc.d"
}

# ── GRUB integration ─────────────────────────────────────────────────
configure_grub() {
    step "Adding Ubuntu to the boot menu"
    local root_uuid root_puuid
    root_uuid=$(blkid -o value -s UUID "$ROOT_PART")
    root_puuid=$(blkid -o value -s PARTUUID "$ROOT_PART")

    # Locate the live ESP GRUB config.
    local esp_cfg=""
    for c in /boot/efi/EFI/BOOT/grub.cfg /boot/efi/EFI/rave2/grub.cfg \
             /boot/EFI/BOOT/grub.cfg; do
        [ -f "$c" ] && esp_cfg="$c" && break
    done
    [ -n "$esp_cfg" ] || die "Could not find the ESP grub.cfg."
    log "ESP config: $esp_cfg"

    # Re-render grub.cfg from the (now Ubuntu-aware) template, preserving the
    # existing rave2 PARTUUIDs — same technique as the RAUC post-install hook.
    local uuids menu_a menu_b app_a
    uuids=$(grep -o 'PARTUUID=[0-9a-fA-F-]*' "$esp_cfg" | sed 's/PARTUUID=//' | awk '!seen[$0]++')
    menu_a=$(echo "$uuids" | sed -n '1p')
    menu_b=$(echo "$uuids" | sed -n '2p')
    app_a=$(echo "$uuids" | sed -n '3p')

    if [ -n "$menu_a" ] && [ -n "$menu_b" ]; then
        log "Regenerating grub.cfg from template (preserving rave2 slots)"
        local tmp="${esp_cfg}.new.$$"
        sed -e "s/@@MENU_A_PARTUUID@@/$menu_a/g" \
            -e "s/@@MENU_B_PARTUUID@@/$menu_b/g" \
            -e "s/@@APP_A_PARTUUID@@/${app_a:-00000000-0000-0000-0000-000000000000}/g" \
            "$GRUB_TEMPLATE" > "$tmp"
        mv "$tmp" "$esp_cfg"
    else
        log "WARNING: could not extract rave2 PARTUUIDs; leaving grub.cfg as-is."
        log "         The Ubuntu entry still works via the grubenv flags below."
    fi

    # Point the grubenv at the new Ubuntu root and make it the default. The
    # template's menuentry reads these variables.
    local grubenv="${esp_cfg%/grub.cfg}/grubenv"
    [ -f "$grubenv" ] || grub-editenv "$grubenv" create
    grub-editenv "$grubenv" set ubuntu_installed=1
    grub-editenv "$grubenv" set ubuntu_fsuuid="$root_uuid"
    grub-editenv "$grubenv" set ubuntu_partuuid="$root_puuid"
    grub-editenv "$grubenv" set ubuntu_default="$UBUNTU_DEFAULT_BOOT"
    # Record the Jumbo-application reservation so later stages (and the menu
    # UI) can find it without re-deriving it from the partition table.
    grub-editenv "$grubenv" set jumbo_reserve_gib="$JUMBO_RESERVE_GIB"
    log "grubenv updated: ubuntu_fsuuid=$root_uuid"
    if [ "$UBUNTU_DEFAULT_BOOT" = "1" ]; then
        log "Ubuntu is now the default boot entry (rave2 menu remains selectable)."
    else
        log "RAVE2 Buddy menu remains the default boot entry (Ubuntu is selectable)."
    fi
}

# ── Main ─────────────────────────────────────────────────────────────
parse_args() {
    for arg in "$@"; do
        case "$arg" in
            default=ubuntu)   UBUNTU_DEFAULT_BOOT=1 ;;
            default=buddy)    UBUNTU_DEFAULT_BOOT=0 ;;
            jumbo_reserve_gib=*)
                local val="${arg#jumbo_reserve_gib=}"
                case "$val" in
                    ''|*[!0-9]*) die "jumbo_reserve_gib must be a non-negative integer, got '$val'." ;;
                esac
                JUMBO_RESERVE_GIB="$val"
                ;;
            *) log "Ignoring unknown argument: $arg" ;;
        esac
    done
}

main() {
    parse_args "$@"
    preflight
    partition
    bootstrap_rootfs
    mount_chroot
    install_packages
    install_tools
    configure_system
    configure_grub
    sync

    step "Install complete"
    log "Ubuntu ${UBUNTU_RELEASE} is installed on ${ROOT_PART}."
    log "Reboot the board to start Ubuntu. The rave2 menu is still available"
    log "from the GRUB boot menu at startup."
}

main "$@"
