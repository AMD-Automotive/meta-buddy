# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

SUMMARY = "RAVE2 Buddy on-target documentation (HTTP API reference + agent orientation)"
DESCRIPTION = "Installs the Buddy HTTP API reference and agent-orientation notes as \
Markdown under ${docdir}/buddy. The same files are served by rave-menu at /llms.txt, \
/AGENTS.md and /api.md so that a tool connecting over HTTP can discover them."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "\
    file://llms.txt \
    file://AGENTS.md \
    file://api.md \
    file://buddy-docs.sh \
"

S = "${WORKDIR}"

inherit allarch

BUDDY_DOCDIR = "${docdir}/buddy"

do_install() {
    install -d ${D}${BUDDY_DOCDIR}
    install -m 0644 ${WORKDIR}/llms.txt  ${D}${BUDDY_DOCDIR}/llms.txt
    install -m 0644 ${WORKDIR}/AGENTS.md ${D}${BUDDY_DOCDIR}/AGENTS.md
    install -m 0644 ${WORKDIR}/api.md    ${D}${BUDDY_DOCDIR}/api.md

    install -d ${D}${sysconfdir}/profile.d
    install -m 0644 ${WORKDIR}/buddy-docs.sh ${D}${sysconfdir}/profile.d/buddy-docs.sh
}

# Keep the docs in the main package: the default FILES:${PN}-doc claims ${docdir},
# and a -doc package is not installed unless the image asks for doc-pkgs.
FILES:${PN} += "${BUDDY_DOCDIR} ${sysconfdir}/profile.d/buddy-docs.sh"
FILES:${PN}-doc = ""

# AGENTS.md in a login home is the one place a coding agent opening the board as a
# remote workspace picks up automatically. Done on target so the symlink lands with
# the right ownership once the user actually exists.
pkg_postinst_ontarget:${PN}() {
    for home in /home/amd-edf /root; do
        [ -d "$home" ] || continue
        ln -sfn ${BUDDY_DOCDIR}/AGENTS.md "$home/AGENTS.md"
    done
    exit 0
}
