# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

SUMMARY = "udev rules for AMD/Xilinx USB programming cable access"
DESCRIPTION = "Installs udev rules so non-root users can access AMD/Xilinx USB JTAG/programming cables"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://99-xilinx-usb-access.rules"

S = "${WORKDIR}"

inherit allarch useradd

USERADD_PACKAGES = "${PN}"
GROUPADD_PARAM:${PN} = "-r xilinxusb"

do_install() {
    install -d ${D}${sysconfdir}/udev/rules.d
    install -m 0644 ${WORKDIR}/99-xilinx-usb-access.rules \
        ${D}${sysconfdir}/udev/rules.d/99-xilinx-usb-access.rules
}

FILES:${PN} += "${sysconfdir}/udev/rules.d/99-xilinx-usb-access.rules"

RDEPENDS:${PN} += "udev"