# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

FILESEXTRAPATHS:prepend := "${THISDIR}/${PN}:"

# The stock alsa-restore.service races with the ACP70 PCI probe at boot and
# exits before /dev/snd/controlC1 appears, leaving the NAU8822 mixer at its
# low power-on defaults. Ship a small oneshot unit that waits for card 1 and
# re-runs alsactl restore against it.
SRC_URI += "file://alsa-restore-acp.service"

# Targeted headphone fixup: WirePlumber disables the NAU8822 headphone route
# during ACP probe. This unit re-applies only the HP controls after WirePlumber.
SRC_URI += "file://nau8822-hp-fixup.service"

# HDA position-tracking fixes for HDMI audio under Xen PCI passthrough.
# Without these the kernel falls back to an IRQ-timing workaround that
# causes a click + brief fast-playback transient on every new PCM stream.
SRC_URI += "file://snd-hda-intel-amdx86.conf"

inherit systemd

SYSTEMD_SERVICE:${PN} += "alsa-restore-acp.service nau8822-hp-fixup.service"
SYSTEMD_AUTO_ENABLE:${PN} = "enable"

do_install:append() {
    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/alsa-restore-acp.service \
        ${D}${systemd_system_unitdir}/alsa-restore-acp.service
    install -m 0644 ${WORKDIR}/nau8822-hp-fixup.service \
        ${D}${systemd_system_unitdir}/nau8822-hp-fixup.service

    install -d ${D}${sysconfdir}/modprobe.d
    install -m 0644 ${WORKDIR}/snd-hda-intel-amdx86.conf \
        ${D}${sysconfdir}/modprobe.d/snd-hda-intel-amdx86.conf
}

FILES:${PN} += " \
    ${systemd_system_unitdir}/alsa-restore-acp.service \
    ${systemd_system_unitdir}/nau8822-hp-fixup.service \
    ${sysconfdir}/modprobe.d/snd-hda-intel-amdx86.conf \
"
