# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

SUMMARY = "FTDI device driver written in pure Python"
DESCRIPTION = "PyFtdi aims at providing a user-space driver for popular FTDI devices, \
implemented in pure Python language."
HOMEPAGE = "https://github.com/eblot/pyftdi"
LICENSE = "BSD-3-Clause"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/BSD-3-Clause;md5=550794465ba0ec5312d6919e203a55f9"

SRC_URI[sha256sum] = "cb2770b606507024a65e0a520bf892f239a36cec25cab45338ce3e3027de53c8"

inherit pypi setuptools3

RDEPENDS:${PN} += " \
    python3-pyusb \
    python3-pyserial \
"
