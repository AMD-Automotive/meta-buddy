# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

SUMMARY = "AMD Vivado Lab Tools installer URLs, SHA-256 manifest, and QR codes"
DESCRIPTION = "Ships /usr/share/vivado-installer/{manifest.json,vivado-installer-url-<ver>.png}. \
The rave-menu web UI reads the manifest at runtime to drive the user-driven \
Vivado HW Server installer flow. The manifest carries a catalog of selectable \
releases so a customer can install a newer HW Server alongside the base one; \
version/URL/SHA bumps live entirely in this recipe so they don't force a \
rave-menu rebuild."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# ---- Vivado HW Server release catalog -------------------------------------
# Each release installs into its own ${VIVADO_INSTALL_PREFIX}/<version>/ tree,
# so several may be present at once. Adding a release = append the version to
# VIVADO_VERSIONS and add its two varflags below.
#
#   VIVADO_BASE_VERSION     the release the Versal programming scripts use.
#                           Validated against the shipped board images -- do
#                           not move it without re-qualifying those flows.
#   VIVADO_DEFAULT_VERSION  the release pre-selected in the installer UI.
#   VIVADO_PROGRAMMING_VERSIONS  releases validated for Versal programming on
#                           this hardware. A release outside this list can
#                           still be installed, selected and served, but using
#                           it to program warns in the job log. Defaults to
#                           VIVADO_VERSIONS when unset.
VIVADO_VERSIONS = "2025.2 2026.1"
VIVADO_BASE_VERSION = "2025.2"
VIVADO_DEFAULT_VERSION = "2025.2"
VIVADO_PROGRAMMING_VERSIONS = "2025.2 2026.1"

VIVADO_INSTALLER_FILENAME[2025.2] = "Vivado_HW_Server_Lin_2025.2_1114_2157.tar"
VIVADO_INSTALLER_SHA256[2025.2] = "e9c6d5e86e2f67871c6129753364e701a5e79af4731f87f946a46e97fc20a3cc"

VIVADO_INSTALLER_FILENAME[2026.1] = "Vivado_HW_Server_Lin_2026.1_0616_1700.tar"
VIVADO_INSTALLER_SHA256[2026.1] = "e9c7a235c684f48bf096e4d6dd58b9fbc749cb59f12d8a5c3b57403a44d29576"

VIVADO_INSTALLER_URL_TEMPLATE = "https://account.amd.com/en/forms/downloads/xef.html?filename=@FILENAME@"
VIVADO_INSTALL_PREFIX = "/opt/tools"

DEPENDS = "qrencode-native"

# This recipe ships only generated content -- no upstream source.
SRC_URI = ""
S = "${WORKDIR}"

do_configure[noexec] = "1"

python do_compile() {
    import json
    import os
    import re

    workdir = d.getVar("WORKDIR")
    qrdir = os.path.join(workdir, "qr")
    bb.utils.mkdirhier(qrdir)

    # Versions become path components under VIVADO_INSTALL_PREFIX and are
    # echoed back to the web UI, so keep them to a boring charset.
    version_re = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
    url_template = d.getVar("VIVADO_INSTALLER_URL_TEMPLATE")

    versions = (d.getVar("VIVADO_VERSIONS") or "").split()
    if not versions:
        bb.fatal("VIVADO_VERSIONS is empty")

    entries = []
    for version in versions:
        if ".." in version or not version_re.match(version):
            bb.fatal("invalid VIVADO_VERSIONS entry: %s" % version)
        filename = d.getVarFlag("VIVADO_INSTALLER_FILENAME", version)
        sha256 = d.getVarFlag("VIVADO_INSTALLER_SHA256", version)
        if not filename or not sha256:
            bb.fatal("VIVADO_INSTALLER_FILENAME[%s] and VIVADO_INSTALLER_SHA256[%s] "
                     "must both be set" % (version, version))

        url = url_template.replace("@FILENAME@", filename)
        qr = "vivado-installer-url-%s.png" % version
        # Pre-rendered so the page renders even without JS / network.
        bb.process.run(["qrencode", "-t", "PNG", "-s", "8", "-m", "2",
                        "-o", os.path.join(qrdir, qr), url])

        entries.append({
            "version": version,
            "filename": filename,
            "url": url,
            "sha256": sha256.lower(),
            "qr": qr,
        })

    base = d.getVar("VIVADO_BASE_VERSION")
    default = d.getVar("VIVADO_DEFAULT_VERSION") or base
    for name, value in (("VIVADO_BASE_VERSION", base),
                        ("VIVADO_DEFAULT_VERSION", default)):
        if value not in versions:
            bb.fatal("%s='%s' is not listed in VIVADO_VERSIONS" % (name, value))

    programming = (d.getVar("VIVADO_PROGRAMMING_VERSIONS") or "").split()
    if not programming:
        programming = list(versions)
    for version in programming:
        if version not in versions:
            bb.fatal("VIVADO_PROGRAMMING_VERSIONS entry '%s' is not listed in "
                     "VIVADO_VERSIONS" % version)

    # Single source of truth read by rave-menu's vivado_installer.py.
    manifest = {
        "install_prefix": d.getVar("VIVADO_INSTALL_PREFIX"),
        "base_version": base,
        "default_version": default,
        "programming_versions": programming,
        "versions": entries,
    }
    with open(os.path.join(workdir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
}

do_install() {
    install -d ${D}${datadir}/vivado-installer
    install -m 0644 ${WORKDIR}/manifest.json \
        ${D}${datadir}/vivado-installer/manifest.json
    install -m 0644 ${WORKDIR}/qr/*.png ${D}${datadir}/vivado-installer/
}

FILES:${PN} = "${datadir}/vivado-installer"
