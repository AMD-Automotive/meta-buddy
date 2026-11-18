# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

SUMMARY = "rave2-buddy: x86 rootfs for Heterogeneous demo"
LICENSE = "MIT"


IMAGE_INSTALL = "packagegroup-rave2-weston-base \
                ${CORE_IMAGE_EXTRA_INSTALL} \
                wireless-regdb-static \
                linux-firmware-iwlwifi \
                wpa-supplicant \
                iw \
                rfkill \
                net-tools \
                iproute2 \
                iproute2-ss \
                tcpdump \
                iptables \
                openocd \
                chromium-ozone-wayland \
                pulseaudio \
                alsa-plugins-pulseaudio-conf \
                pipewire \
                pipewire-pulse \
                pipewire-alsa \
                pipewire-tools \
                wireplumber \
                alsa-utils \
                rave-menu \
                python3-pyusb \
                python3-pyserial \
                libusb1 \
                vivado-installer-assets \
                tar \
                coreutils \
                curl \
                jq \
                rsync \
                socat \
                glibc-utils \
                usbutils \
                minicom \
                tio \
                dmidecode \
                xilinx-usb-access \
                versal-reprogram \
                xen-tools-xl \
                xen-tools-xenstore \
                xen-tools-xenstored \
                xen-tools-xencommons \
                xen-tools-xenstat \
                xen-tools-xenhypfs \
                xen-tools-misc \
                xen-tools-console \
                rauc \
                rauc-conf \
                grub-editenv \
                grub-efi-nvme-cfg \
                xen \
                xen-tools \
                alsa-state \
                xkeyboard-config \
                xkbcli \
                kbd \
                kbd-keymaps \
"


# Locales offered by the localisation menu. The previous value was
# " en_US.UTF-8 UTF-8": libc-package.bbclass splits this on whitespace, so the
# trailing "UTF-8" was parsed as a locale name, warned "Unsupported locale",
# and generated a junk locale literally called UTF-8. It came from misreading
# the two-column format of /usr/share/i18n/SUPPORTED.
#
# IMAGE_LINGUAS is what actually installs the locale-base-* packages; without
# it they are built and never shipped, so `locale -a` still shows only en_US
# and the language picker has nothing to offer.
#
# ja_JP/zh_CN are deliberately absent: they need CJK fonts to render at all,
# and are phased in with those and their message catalogues.
GLIBC_GENERATE_LOCALES = "en_GB.UTF-8 en_US.UTF-8 de_DE.UTF-8 el_GR.UTF-8 \
                          fr_FR.UTF-8 es_ES.UTF-8 it_IT.UTF-8"
IMAGE_LINGUAS = "en-gb en-us de-de el-gr fr-fr es-es it-it"


inherit core-image


# Add compatibility loader path expected by Vivado Lab / XSDB
ROOTFS_POSTPROCESS_COMMAND:append = " xsdb_compat_fixes; ssh_sudo_policy_fixes; stamp_rauc_version; rauc_fstab_fixup; install_slot_initrd;"
EXTRA_USERS_PARAMS += "usermod -a -G xilinxusb,dialout amd-edf; usermod -a -G audio weston;"

# Allow amd-edf to use sudo without a password prompt.
EXTRA_USERS_SUDOERS = "amd-edf ALL=(ALL) NOPASSWD: ALL;"

xsdb_compat_fixes() {
    # Ensure /lib64 exists and provide the loader path expected by AMD binaries
    install -d ${IMAGE_ROOTFS}/lib64

    if [ -e ${IMAGE_ROOTFS}/usr/lib/ld-linux-x86-64.so.2 ] && \
       [ ! -e ${IMAGE_ROOTFS}/lib64/ld-linux-x86-64.so.2 ]; then
        ln -sf /usr/lib/ld-linux-x86-64.so.2 \
            ${IMAGE_ROOTFS}/lib64/ld-linux-x86-64.so.2
    fi
}

RAUC_VERSION_LAYER_DIR = "${THISDIR}/.."

stamp_rauc_version() {
    install -d ${IMAGE_ROOTFS}${sysconfdir}/rauc
    GIT_HASH=$(cd "${RAUC_VERSION_LAYER_DIR}" 2>/dev/null && git rev-parse --short=6 HEAD 2>/dev/null || echo "unknown")
    BUILD_DATE=$(date -u +%Y-%m-%d)
    echo "${BUILD_DATE}_${GIT_HASH}" > ${IMAGE_ROOTFS}${sysconfdir}/rauc/version
}

# Ensure the EFI partition is mounted at /boot/efi in every rootfs.
# The NVMe installer patches fstab for slot A, but slot B gets its rootfs
# from a RAUC bundle (raw image copy) so it needs this baked in.
rauc_fstab_fixup() {
    install -d ${IMAGE_ROOTFS}/boot/efi
    if ! grep -qs '/boot/efi' ${IMAGE_ROOTFS}${sysconfdir}/fstab; then
        echo "/dev/nvme0n1p1  /boot/efi  vfat  defaults  0 2" \
            >> ${IMAGE_ROOTFS}${sysconfdir}/fstab
    fi
}

# Ship the built initramfs in the rootfs /boot/ so each RAUC slot owns its
# initrd alongside bzImage and xen.gz.  GRUB loads it from the active slot.
install_slot_initrd() {
    INITRD_FILE="${DEPLOY_DIR_IMAGE}/${INITRAMFS_IMAGE}-${MACHINE}.cpio.gz"
    if [ -f "$INITRD_FILE" ]; then
        install -d ${IMAGE_ROOTFS}/boot
        install -m 0644 "$INITRD_FILE" ${IMAGE_ROOTFS}/boot/initrd
    else
        bbwarn "Initramfs not found at $INITRD_FILE — slot will lack /boot/initrd"
    fi
}


### Bootloader overrides
GRUB_TIMEOUT = "60"
GRUB_TERMINAL = "console serial"
GRUB_SERIAL = "--unit=0 --speed=115200 --word=8 --parity=no --stop=1"
GRUB_OPTS = "serial --unit=0 --speed=115200 --word=8 --parity=no --stop=1;terminal_input console serial;terminal_output console serial"
APPEND += " quiet systemd.show_status=false loglevel=0 vt.global_cursor_default=0 console=ttyS0,115200n8 console=tty0"
SYSTEMD_DEFAULT_TARGET = "graphical.target"

INITRAMFS_IMAGE ?= "core-image-minimal-initramfs"

# Ensure the initramfs is built before our rootfs so we can copy it into /boot/
do_rootfs[depends] += "${INITRAMFS_IMAGE}:do_image_complete"

IMAGE_BOOT_FILES += "splash.png;EFI/BOOT/splash.png grub-noxen.cfg;EFI/BOOT/grub.cfg grub-nvme-rauc.cfg xen.gz ${INITRAMFS_IMAGE}-${MACHINE}.cpio.gz;initrd EFI/BOOT/x86_64-efi/*.mod;EFI/BOOT/x86_64-efi unicode.pf2;EFI/BOOT/unicode.pf2 theme/theme.txt;EFI/BOOT/theme/theme.txt theme/background.png;EFI/BOOT/theme/background.png theme/select_c.png;EFI/BOOT/theme/select_c.png theme/select_w.png;EFI/BOOT/theme/select_w.png theme/select_e.png;EFI/BOOT/theme/select_e.png theme/DejaVuSans12.pf2;EFI/BOOT/theme/DejaVuSans12.pf2 theme/DejaVuSans14.pf2;EFI/BOOT/theme/DejaVuSans14.pf2 theme/DejaVuSans16.pf2;EFI/BOOT/theme/DejaVuSans16.pf2 theme/DejaVuSans18.pf2;EFI/BOOT/theme/DejaVuSans18.pf2 theme/DejaVuSansBold18.pf2;EFI/BOOT/theme/DejaVuSansBold18.pf2 theme/DejaVuSansBold20.pf2;EFI/BOOT/theme/DejaVuSansBold20.pf2"
do_bootimg[depends] += "${INITRAMFS_IMAGE}:do_image_complete"
do_image_wic[depends] += "xen:do_deploy"
IMAGE_ROOTFS_SIZE ?= "8192"
IMAGE_ROOTFS_EXTRA_SPACE:append = "${@bb.utils.contains("DISTRO_FEATURES", "systemd", " + 4096", "", d)}"

ssh_sudo_policy_fixes() {
    # Enable remote SSH password auth for accounts that intentionally have
    # empty passwords in this board-test image.
    if [ -e ${IMAGE_ROOTFS}${sysconfdir}/ssh/sshd_config ]; then
        sed -i -e 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' \
               -e 's/^#\\?PermitEmptyPasswords.*/PermitEmptyPasswords yes/' \
               ${IMAGE_ROOTFS}${sysconfdir}/ssh/sshd_config

        grep -q '^PasswordAuthentication' ${IMAGE_ROOTFS}${sysconfdir}/ssh/sshd_config || \
            echo 'PasswordAuthentication yes' >> ${IMAGE_ROOTFS}${sysconfdir}/ssh/sshd_config
        grep -q '^PermitEmptyPasswords' ${IMAGE_ROOTFS}${sysconfdir}/ssh/sshd_config || \
            echo 'PermitEmptyPasswords yes' >> ${IMAGE_ROOTFS}${sysconfdir}/ssh/sshd_config
    fi
}
