# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# Avoid configure-time link tests that require crtbegin/libgcc in this cross setup.
EXTRA_OECMAKE:append:class-target = " -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY"

do_compile:prepend:class-target() {
    cxxver="$(ls -1d ${RECIPE_SYSROOT}${includedir}/c++/* 2>/dev/null | sort | tail -n1 || true)"
    if [ -n "${cxxver}" ] && [ -d "${cxxver}" ]; then
        export CPLUS_INCLUDE_PATH="${cxxver}:${cxxver}/${TARGET_SYS}${CPLUS_INCLUDE_PATH:+:${CPLUS_INCLUDE_PATH}}"
    fi
}
