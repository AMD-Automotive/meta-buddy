# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# Avoid configure-time executable link probes that require crtbegin/libgcc.
EXTRA_OECMAKE:append:class-target = " -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY"

do_configure:prepend:class-target() {
    gccverdir="$(ls -1d ${RECIPE_SYSROOT}${libdir}/${TARGET_SYS}/* 2>/dev/null | sort | tail -n1 || true)"
    if [ -n "${gccverdir}" ] && [ -d "${gccverdir}" ]; then
        export CFLAGS="${CFLAGS} --gcc-install-dir=${gccverdir}"
        export CXXFLAGS="${CXXFLAGS} --gcc-install-dir=${gccverdir}"
        export LDFLAGS="${LDFLAGS} --gcc-install-dir=${gccverdir} -B${gccverdir} -L${gccverdir}"
    fi
}
