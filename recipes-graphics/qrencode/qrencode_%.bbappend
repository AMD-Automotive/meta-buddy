# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# Enable the qrencode CLI tool for the native build so our
# vivado-installer-assets recipe can call `qrencode` from do_compile.
PACKAGECONFIG:append:class-native = " tools"
