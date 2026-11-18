# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# PipeWire bbappend: install custom ALSA node config for Versal I2S,
# system-level session services for kiosk, and disable PA autospawn.

FILESEXTRAPATHS:prepend := "${THISDIR}/${PN}:"

SRC_URI += " \
    file://50-versal-i2s-sink.conf \
    file://pipewire-session.service \
    file://pipewire-pulse-session.service \
"

# Ensure PipeWire builds with ALSA, PulseAudio compat, and WirePlumber
PACKAGECONFIG:append = " alsa pipewire-alsa pulseaudio wireplumber pw-cat sndfile"

inherit systemd

SYSTEMD_SERVICE:${PN} = "pipewire-session.service pipewire-pulse-session.service"
SYSTEMD_AUTO_ENABLE:${PN} = "enable"

do_install:append() {
    # PipeWire conf.d snippet: create ALSA sink for hw:1,1 (ADAU1860 via Versal I2S)
    install -d ${D}${sysconfdir}/pipewire/pipewire.conf.d
    install -m 0644 ${WORKDIR}/50-versal-i2s-sink.conf ${D}${sysconfdir}/pipewire/pipewire.conf.d/

    # System-level services for kiosk (weston user has no systemd --user session)
    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/pipewire-session.service ${D}${systemd_system_unitdir}/
    install -m 0644 ${WORKDIR}/pipewire-pulse-session.service ${D}${systemd_system_unitdir}/
}

FILES:${PN} += " \
    ${sysconfdir}/pipewire/pipewire.conf.d/50-versal-i2s-sink.conf \
    ${systemd_system_unitdir}/pipewire-session.service \
    ${systemd_system_unitdir}/pipewire-pulse-session.service \
"
