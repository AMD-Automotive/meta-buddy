# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

SUMMARY = "Dummy ADAU1860 TDSP firmware blob"
DESCRIPTION = "Ships a no-op adi/adau1860_tdsp_fw.bin so the ADAU1860 codec \
driver's request_firmware path registers the TDSP/FDSP DAPM widgets at probe. \
Without this blob the static codec routes reference widgets that never get \
created and the ACP sound card fails to instantiate (-19). Only the TDSP blob \
is shipped on purpose: shipping fdsp/eq as well would double-register the DSP \
widgets (fatal)."
LICENSE = "CLOSED"

INHIBIT_DEFAULT_DEPS = "1"

S = "${WORKDIR}"

do_install() {
    install -d ${D}${nonarch_base_libdir}/firmware/adi
    # 64 zero bytes is sufficient for the codec to accept the load and register
    # the TDSP/FDSP DAPM widgets; no real DSP program is run.
    dd if=/dev/zero of=${D}${nonarch_base_libdir}/firmware/adi/adau1860_tdsp_fw.bin \
        bs=64 count=1 2>/dev/null
    chmod 0644 ${D}${nonarch_base_libdir}/firmware/adi/adau1860_tdsp_fw.bin
}

FILES:${PN} = "${nonarch_base_libdir}/firmware/adi"
RPROVIDES:${PN} = "adau1860-firmware"
