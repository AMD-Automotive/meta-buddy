# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

FILESEXTRAPATHS:prepend := "${THISDIR}/${PN}:"

SRC_URI += " \
    file://set-headphone-port.pa \
    file://load-versal-i2s-sink.pa \
    file://client.conf \
"

do_install:append() {
    install -d ${D}${sysconfdir}/pulse/default.pa.d
    install -m 0644 ${WORKDIR}/set-headphone-port.pa     ${D}${sysconfdir}/pulse/default.pa.d/
    install -m 0644 ${WORKDIR}/load-versal-i2s-sink.pa   ${D}${sysconfdir}/pulse/default.pa.d/

    # Disable autospawn — pipewire-pulse handles the PA socket now
    install -m 0644 ${WORKDIR}/client.conf ${D}${sysconfdir}/pulse/client.conf
}

FILES:${PN}-server += " \
    ${sysconfdir}/pulse/default.pa.d/set-headphone-port.pa \
    ${sysconfdir}/pulse/default.pa.d/load-versal-i2s-sink.pa \
"
