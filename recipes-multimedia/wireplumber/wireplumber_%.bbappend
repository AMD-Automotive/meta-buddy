# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# WirePlumber bbappend: install custom audio routing rules + kiosk session service

FILESEXTRAPATHS:prepend := "${THISDIR}/${PN}:"

SRC_URI += " \
    file://51-adau1860-volume-pin.conf \
    file://51-rave-audio-defaults.conf \
    file://wireplumber-session.service \
"

inherit systemd

SYSTEMD_SERVICE:${PN} = "wireplumber-session.service"
SYSTEMD_AUTO_ENABLE:${PN} = "enable"

do_install:append() {
    install -d ${D}${sysconfdir}/wireplumber/wireplumber.conf.d
    install -m 0644 ${WORKDIR}/51-adau1860-volume-pin.conf  ${D}${sysconfdir}/wireplumber/wireplumber.conf.d/
    install -m 0644 ${WORKDIR}/51-rave-audio-defaults.conf   ${D}${sysconfdir}/wireplumber/wireplumber.conf.d/

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/wireplumber-session.service ${D}${systemd_system_unitdir}/
}

FILES:${PN} += " \
    ${sysconfdir}/wireplumber/wireplumber.conf.d/*.conf \
    ${systemd_system_unitdir}/wireplumber-session.service \
"
