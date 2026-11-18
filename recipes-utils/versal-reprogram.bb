# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

SUMMARY = "Versal PCIe reprogram utility"
DESCRIPTION = "Scripts to remove the Versal PCIe endpoint, program the FPGA via XSDB, and rescan the bus"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# Prebuilt recovery artefacts, published per CI build. The upstream basename is
# unique per build and is deliberately kept in DL_DIR, so builds from different
# branches never collide on one cache entry. They are fetched packed because
# bitbake decompresses a plain .gz into the unpack root and ignores subdir, so
# do_install gunzips them itself rather than having to know their names.
#
# The pcieImage and this recipe's Python must agree on the BAR protocol, since
# there is no longer a runtime download that could replace one without the
# other. After meta-erica-edf CI publishes a rave2-recovery-image carrying new
# protocol phases, bump the pcieimage URL and its sha256sum together with the
# host-side change that uses them.
SRC_URI = " \
    file://versal-reprogram.sh \
    file://versal-root-port.sh \
    file://program_versal.tcl \
    file://program_versal_payload.py \
    file://versal_payload_constants.py \
    file://versal_payload_pcie.py \
    file://versal_payload_protocol.py \
    file://versal_ufs_luns.py \
    file://reconfigure_versal.py \
    file://x86_replace_application.py \
    https://mkmartifactory.amd.com:8443/artifactory/SW-AutomotiveDevOps-DEV-LOCAL/ERICA-H/meta-erica-edf/7902cb___6dce4c___amd-cortexa78-mali-common_2025.2-rave2-recovery-image.pcieImage.gz;name=pcieimage;subdir=pcieimage;unpack=0 \
    https://mkmartifactory.amd.com:8443/artifactory/SW-AutomotiveDevOps-DEV-LOCAL/ERICA-H/meta-erica-edf/f3e03a_78fa5a___rave2-recovery_2025.2-xilinx-bootbin.bin.gz;name=bootbin;subdir=bootbin;unpack=0 \
"
SRC_URI[pcieimage.sha256sum] = "b17b406a51bfaf9501c81920ce8658528ecff69b5b887daf2d8ed6fd5f622a40"
SRC_URI[bootbin.sha256sum] = "0696bd54890180b3107c751ebb842e3e3733c260958c4219116aa72ad41f1ee1"

S = "${WORKDIR}"

RDEPENDS:${PN} += " \
    bash \
    pciutils \
    sudo \
    python3 \
    python3-json \
    python3-datetime \
    python3-pyftdi \
    python3-pyusb \
    e2fsprogs-tune2fs \
"

do_install() {

    install -d ${D}${sbindir}
    install -m 0755 ${WORKDIR}/versal-reprogram.sh \
        ${D}${sbindir}/versal-reprogram

    install -m 0755 ${WORKDIR}/versal-root-port.sh \
        ${D}${sbindir}/versal-root-port

    install -m 0755 ${WORKDIR}/program_versal_payload.py \
        ${D}${sbindir}/program_versal_payload

    install -m 0755 ${WORKDIR}/reconfigure_versal.py \
        ${D}${sbindir}/reconfigure_versal

    install -m 0755 ${WORKDIR}/x86_replace_application.py \
        ${D}${sbindir}/x86-replace-application

    install -d ${D}${nonarch_base_libdir}/firmware/versal
    gzip -dc ${WORKDIR}/pcieimage/*.gz > ${D}${nonarch_base_libdir}/firmware/versal/pcieImage
    gzip -dc ${WORKDIR}/bootbin/*.gz > ${D}${nonarch_base_libdir}/firmware/versal/boot.bin
    chmod 0644 ${D}${nonarch_base_libdir}/firmware/versal/pcieImage
    chmod 0644 ${D}${nonarch_base_libdir}/firmware/versal/boot.bin

    install -d ${D}${libdir}/versal
    install -m 0644 ${WORKDIR}/program_versal.tcl \
        ${D}${libdir}/versal/program_versal.tcl
    install -m 0644 ${WORKDIR}/versal_payload_constants.py \
        ${D}${libdir}/versal/versal_payload_constants.py
    install -m 0644 ${WORKDIR}/versal_payload_pcie.py \
        ${D}${libdir}/versal/versal_payload_pcie.py
    install -m 0644 ${WORKDIR}/versal_payload_protocol.py \
        ${D}${libdir}/versal/versal_payload_protocol.py
    install -m 0644 ${WORKDIR}/versal_ufs_luns.py \
        ${D}${libdir}/versal/versal_ufs_luns.py

    # Holds the captured UFS layout baseline for each device, keyed by serial.
    # It is the only route back to a board's original LUN layout, because the
    # part ships multi-LUN and there is no factory state to fall back on.
    install -d ${D}${localstatedir}/lib/versal/ufs-baseline

}

FILES:${PN} += " \
    ${nonarch_base_libdir}/firmware/versal/pcieImage \
    ${nonarch_base_libdir}/firmware/versal/boot.bin \
    ${sbindir}/versal-reprogram \
    ${sbindir}/versal-root-port \
    ${sbindir}/program_versal_payload \
    ${sbindir}/reconfigure_versal \
    ${sbindir}/x86-replace-application \
    ${libdir}/versal \
    ${libdir}/versal/program_versal.tcl \
    ${libdir}/versal/versal_payload_constants.py \
    ${libdir}/versal/versal_payload_pcie.py \
    ${libdir}/versal/versal_payload_protocol.py \
    ${libdir}/versal/versal_ufs_luns.py \
    ${localstatedir}/lib/versal \
"
