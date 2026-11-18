# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# The upstream meta-chromium chromium-x11 recipe (at the pinned commit) does not
# set a LICENSE, so BitBake halts while parsing this layer even though no image
# in meta-erica-x86 installs the X11 variant. Provide a default LICENSE so
# parsing always succeeds. Uses ?= so an upstream-provided LICENSE still wins.
LICENSE ?= "CLOSED"
