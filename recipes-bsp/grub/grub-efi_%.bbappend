# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

FILESEXTRAPATHS:prepend := "${THISDIR}/${PN}:"

SRC_URI:append = " \
    file://grub-xen.cfg \
    file://grub-noxen.cfg \
    file://grub-nvme-rauc.cfg \
"

# Install the NVMe RAUC GRUB template into the rootfs so the RAUC post-install
# hook can upgrade old ESP configs during field updates.
PACKAGES =+ "${PN}-nvme-cfg"
FILES:${PN}-nvme-cfg = "${datadir}/grub/grub-nvme-rauc.cfg"
RDEPENDS:${PN}-nvme-cfg = ""
ALLOW_EMPTY:${PN}-nvme-cfg = "1"

do_install:append() {
    install -d ${D}${datadir}/grub
    install -m 0644 ${WORKDIR}/grub-nvme-rauc.cfg ${D}${datadir}/grub/grub-nvme-rauc.cfg
}

do_deploy:append() {
    install -m 0644 ${S}/../grub-xen.cfg ${DEPLOYDIR}
    install -m 0644 ${S}/../grub-noxen.cfg ${DEPLOYDIR}
    install -m 0644 ${S}/../grub-nvme-rauc.cfg ${DEPLOYDIR}
}
