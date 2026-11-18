# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

CHROMIUM_PREBUILT ?= "0"
CHROMIUM_PREBUILT_DATE ?= "2026_04_28"
CHROMIUM_PREBUILT_ARCHIVE ?= "chromium-ozone-wayland-${PV}_${CHROMIUM_PREBUILT_DATE}-amd64-rootfs.tar.xz"
CHROMIUM_PREBUILT_URL ?= "https://mkmartifactory.amd.com:8443/artifactory/SW-AutomotiveDevOps-DEV-LOCAL/installs/buddy/${CHROMIUM_PREBUILT_ARCHIVE}"

python __anonymous () {
    if d.getVar("CHROMIUM_PREBUILT") != "1":
        return

    archive = d.getVar("CHROMIUM_PREBUILT_ARCHIVE")

    d.setVar("SRC_URI", d.getVar("CHROMIUM_PREBUILT_URL") + f";downloadfilename={archive};unpack=0")
    d.setVarFlag("SRC_URI", "sha256sum", "1d2c08f7dadcb864a8704a0e68cf2fcec49cbe1ce0afcdf2a245515ae8c07199")
    d.setVar("DEPENDS", "")
    d.setVar("LICENSE", "CLOSED")
    d.setVar("LIC_FILES_CHKSUM", "")
    d.setVar("PACKAGES", d.getVar("PN"))
    d.setVar(
        "RDEPENDS:" + d.getVar("PN"),
        " ".join([
            "bash",
            "desktop-file-utils",
            "gdk-pixbuf",
            "gtk+3",
            "hicolor-icon-theme",
            "upower",
            "alsa-lib",
            "at-spi2-core",
            "dbus-lib",
            "expat",
            "glib-2.0",
            "glibc",
            "libffi",
            "libflac",
            "libgbm",
            "libgcc",
            "libjpeg-turbo",
            "libudev",
            "libxkbcommon",
            "libxslt",
            "nspr",
            "nss",
        ]),
    )
    d.setVar("INHIBIT_PACKAGE_DEBUG_SPLIT", "1")
    d.setVar("INHIBIT_PACKAGE_STRIP", "1")
    d.setVarFlag("do_patch", "noexec", "1")
    d.setVarFlag("do_check_llvm_version", "noexec", "1")
    d.setVarFlag("do_write_toolchain_file", "noexec", "1")
    d.setVarFlag("do_configure", "noexec", "1")
    d.setVarFlag("do_add_nodejs_symlink", "noexec", "1")
    d.setVarFlag("do_add_clang_latest", "noexec", "1")
    d.setVarFlag("do_copy_clang_library", "noexec", "1")
    d.setVarFlag("do_copy_target_rustlibs", "noexec", "1")
    d.setVarFlag("do_compile", "noexec", "1")
    d.setVar("do_install", d.getVar("do_install_prebuilt"))
}

# ----- Rust 1.92 V2 ABI compatibility patch -----
# Yocto Rust 1.92 stdlib calls V2-mangled allocator shim symbols as functions,
# but Chromium 138 only defines V1 symbols as static data.  This patch adds
# the V2 function definitions to build/rust/allocator/lib.rs.
# The mangled names embed the __rustc crate disambiguator and are NOT stable
# across rustc releases; they are currently correct for rustc 1.98.1.  See the
# patch header for how to re-derive them after a Rust upgrade.
SRC_URI += "file://0001-rust-allocator-provide-V2-ABI-symbols-as-functions.patch"

# ----- bytemuck portable-SIMD workaround -----
# The vendored bytemuck 1.23 is built with nightly_portable_simd, which needs
# core::simd::LaneCount/SupportedLaneCount.  The Yocto Rust toolchain does not
# provide them, so the crate fails to compile.  Dropping the feature removes
# only the Zeroable impls for core::simd types.
SRC_URI += "file://0002-bytemuck-drop-nightly_portable_simd-feature.patch"

# ----- crabbyavif disable_cfi cleanup (Rust 1.91+) -----
# Replaces the do_compile:prepend sed with a declared patch carrying a
# modification notice (MBUD-MODNOTICE-01).
SRC_URI += "file://0003-crabbyavif-remove-disable_cfi-no_sanitize-for-rust-1.91.patch"

# ----- qr_code-v2 explicit lifetimes -----
# Replaces the do_compile:prepend sed with a declared patch carrying a
# modification notice (MBUD-MODNOTICE-01).
SRC_URI += "file://0004-qr_code-add-lifetime-annotations-for-rust-2024.patch"

# ----- GCC runtime flags for the Yocto cross toolchain -----
# Replaces the do_compile:prepend Python rewrite of wrapper_utils.py with a
# declared patch carrying a modification notice (MBUD-MODNOTICE-01).
SRC_URI += "file://0005-toolchain-inject-yocto-gcc-runtime-flags.patch"

# ----- Hardware video acceleration (VA-API on AMD radeonsi / VCN) -----
# Upstream default is "upower use-egl use-v4l2".  V4L2 is ARM-centric;
# on x86 AMD the decode path is VA-API.  proprietary-codecs gives H.264/H.265.
PACKAGECONFIG:append = " use-vaapi proprietary-codecs"
PACKAGECONFIG:remove = "use-v4l2"

# ----- VA-API runtime feature flags -----
# Chromium needs explicit feature flags to activate VA-API hardware decode.
# VaapiIgnoreDriverChecks: radeonsi isn't always on Chromium's built-in allowlist.
CHROMIUM_EXTRA_ARGS:append = " \
    --enable-features=VaapiVideoDecoder,VaapiVideoDecodeLinuxGL,VaapiVideoEncoder,VaapiIgnoreDriverChecks \
"

INSANE_SKIP:${PN}:append = "${@bb.utils.contains('CHROMIUM_PREBUILT', '1', ' already-stripped', '', d)}"

do_install_prebuilt() {
    install -d "${D}"

    tar --no-same-owner -xJf "${WORKDIR}/${CHROMIUM_PREBUILT_ARCHIVE}" -C "${D}"
    chown -R 0:0 "${D}"
}
