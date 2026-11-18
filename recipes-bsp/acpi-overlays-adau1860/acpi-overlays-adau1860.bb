# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

SUMMARY = "SSDT overlay templates for initramfs loading"
DESCRIPTION = "Compiles SSDT overlays and installs them as templates \
in /lib/firmware/acpi/ssdt-templates/. The initramfs ssdt-loader module \
loads templates specified via kernel cmdline (ssdt=name1,name2) before \
any drivers probe, ensuring ACPI devices exist at driver init time. \
Enable via: grub-editenv /boot/EFI/BOOT/grubenv set ssdt_enabled=adau1860"

LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://adau1860-ssdt.asl"

DEPENDS = "acpica-native"

S = "${WORKDIR}"

do_compile() {
    # Compile ADAU1860 SSDT - template name must match kernel cmdline arg
    iasl -tc -p ${S}/adau1860 ${S}/adau1860-ssdt.asl
}

do_install() {
    # Install SSDT templates to /lib/firmware/acpi/ssdt-templates/
    # Names match kernel cmdline: ssdt=adau1860 -> adau1860.aml
    install -d ${D}${nonarch_base_libdir}/firmware/acpi/ssdt-templates
    install -m 0644 ${S}/adau1860.aml \
        ${D}${nonarch_base_libdir}/firmware/acpi/ssdt-templates/adau1860.aml
}

FILES:${PN} = "${nonarch_base_libdir}/firmware/acpi/ssdt-templates"
