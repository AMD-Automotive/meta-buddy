# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

SUMMARY = "RAUC bundle for rave2-buddy menu system"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

inherit bundle

RAUC_BUNDLE_COMPATIBLE = "rave2-x86-boardtest"
RAUC_BUNDLE_FORMAT = "verity"
RAUC_BUNDLE_VERSION ?= "${DATETIME}"
RAUC_BUNDLE_VERSION[vardepsexclude] += "DATETIME"
RAUC_BUNDLE_SLOTS = "rootfs"

RAUC_SLOT_rootfs = "rave2-buddy"
RAUC_SLOT_rootfs[fstype] = "ext4"
RAUC_SLOT_rootfs[hooks] = "post-install"

# Bundle hook: propagates Vivado HW Server install (/opt/tools) and the
# rave-menu Vivado state (/var/lib/rave-menu/vivado) from the running
# slot into the freshly-installed slot before RAUC switches the boot
# flag. Failures are non-fatal -- a missing /opt/tools is normal.
SRC_URI += "file://rave2-buddy-bundle-hook.sh"
RAUC_BUNDLE_HOOKS[file] = "rave2-buddy-bundle-hook.sh"
RAUC_BUNDLE_HOOKS[hooks] = "install-check"

# Signing credentials — override via local.conf or CI environment:
#   RAUC_KEY_FILE = "/path/to/rauc-signing.key.pem"
#   RAUC_CERT_FILE = "/path/to/rauc-signing.cert.pem"
RAUC_KEY_FILE ?= "${TOPDIR}/../certs/rauc-signing.key.pem"
RAUC_CERT_FILE ?= "${TOPDIR}/../certs/rauc-signing.cert.pem"
