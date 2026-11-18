# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

SUMMARY = "ALSA UCM2 profile for the ADAU1860 (Lark) on the ACP70 card"
DESCRIPTION = "Ships an ALSA Use Case Manager (UCM2) profile for the AMD ACP70 \
+ NAU8822 + ADAU1860 sound card (driver 'acp70-nau8822'). Exposes the ADAU1860 \
DAC playback (Headphones) and 4x PDM DMIC capture devices on PCM dev 1, with \
the routing/mixer values captured during dev-kit bring-up."
LICENSE = "CLOSED"

SRC_URI = " \
    file://acp70-nau8822.conf \
    file://HiFi.conf \
"

INHIBIT_DEFAULT_DEPS = "1"

# UCM2 looks the card config up under conf.d/${CardDriver}/${CardDriver}.conf.
UCMDIR = "${datadir}/alsa/ucm2/conf.d/acp70-nau8822"

S = "${WORKDIR}"

do_install() {
    install -d ${D}${UCMDIR}
    install -m 0644 ${WORKDIR}/acp70-nau8822.conf ${D}${UCMDIR}/acp70-nau8822.conf
    install -m 0644 ${WORKDIR}/HiFi.conf ${D}${UCMDIR}/HiFi.conf
}

FILES:${PN} = "${datadir}/alsa/ucm2"

# The profile is useless without the UCM2 base config tree.
RDEPENDS:${PN} = "alsa-ucm-conf"

RPROVIDES:${PN} = "adau1860-config"
