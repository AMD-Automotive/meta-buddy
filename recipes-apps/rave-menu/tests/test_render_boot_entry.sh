#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# render_boot_entry() turns a declared boot.cmdline into the same artefact
# adopt_boot_entry() produces from an app's own GRUB entry, so the boot menu has
# exactly one kind of file to source.
#
# This drives the real function against the real descriptor_tool, so the dotted
# reads and the rendering are both exercised.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
FILES="${FILES:-${HERE}/../files}"
SCRIPT="${FILES}/scripts/install_app_bundle.sh"
[ -f "$SCRIPT" ] || { echo "cannot find install_app_bundle.sh (set FILES=)"; exit 2; }

DESCRIPTOR_TOOL="${FILES}/descriptor_tool.py"
export DESCRIPTOR_TOOL

eval "$(sed -n '/^render_boot_entry()/,/^}/p' "$SCRIPT")"
command -v render_boot_entry >/dev/null 2>&1 || {
    echo "FAIL: render_boot_entry() not found in $SCRIPT"; exit 1; }

PASS=0; FAIL=0
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

descriptor() {  # <boot-cmdline yaml body>
    cat > "$TMP/d.yaml" <<EOF
format: rave2-appimage
format_version: 1
id: t
name: T
version: "1"
components:
  - kind: x86-rootfs-tar
    path: x86/rootfs.tar
    sha256: "aa00000000000000000000000000000000000000000000000000000000000000"
boot:
  label: T
  cmdline:
$1
EOF
    DESCRIPTOR="$TMP/d.yaml"; export DESCRIPTOR
}

check() {  # <name> <expected> <got>
    if [ "$2" = "$3" ]; then
        PASS=$((PASS + 1)); echo "  PASS  $1"
    else
        FAIL=$((FAIL + 1)); echo "  FAIL  $1"
        echo "        expected:"; echo "$2" | sed 's/^/          /'
        echo "        got:";      echo "$3" | sed 's/^/          /'
    fi
}

_cmdline_part='$boot_disk,gpt4'
_cmdline_uuid='ABCD1234-0000-0000-0000-00000000BEEF'   # upper case on purpose

# --- Xen ---------------------------------------------------------------------
descriptor '    hypervisor: /boot/xen.gz
    hypervisor_args: dom0_mem=32G sched=null
    kernel: /boot/bzImage
    kernel_args: console=hvc0 quiet
    ramdisk: /boot/initrd'
EXPECT='set root=($boot_disk,gpt4)
multiboot2 /boot/xen.gz placeholder dom0_mem=32G sched=null
module2 /boot/bzImage console=hvc0 quiet root=PARTUUID=abcd1234-0000-0000-0000-00000000beef rw rootwait quiet
module2 /boot/initrd'
check "Xen app renders multiboot2 + two modules" "$EXPECT" "$(render_boot_entry)"

# --- bare metal --------------------------------------------------------------
descriptor '    kernel: /boot/vmlinuz
    kernel_args: quiet splash
    ramdisk: /boot/initrd.img'
EXPECT='set root=($boot_disk,gpt4)
linux /boot/vmlinuz quiet splash root=PARTUUID=abcd1234-0000-0000-0000-00000000beef rw rootwait quiet
initrd /boot/initrd.img'
check "bare-metal app renders linux + initrd" "$EXPECT" "$(render_boot_entry)"

# --- no ramdisk --------------------------------------------------------------
descriptor '    kernel: /boot/vmlinuz
    kernel_args: quiet'
EXPECT='set root=($boot_disk,gpt4)
linux /boot/vmlinuz quiet root=PARTUUID=abcd1234-0000-0000-0000-00000000beef rw rootwait quiet'
check "ramdisk is optional" "$EXPECT" "$(render_boot_entry)"

# --- the app-region case -----------------------------------------------------
_cmdline_part='$boot_disk,gpt20'
descriptor '    hypervisor: /xen
    kernel: /bzImage'
EXPECT='set root=($boot_disk,gpt20)
multiboot2 /xen placeholder
module2 /bzImage root=PARTUUID=abcd1234-0000-0000-0000-00000000beef rw rootwait quiet'
check "a disk-image app roots at its own boot partition" "$EXPECT" "$(render_boot_entry)"
_cmdline_part='$boot_disk,gpt4'

# --- refusal -----------------------------------------------------------------
descriptor '    kernel_args: quiet'
if render_boot_entry >/dev/null 2>&1; then
    FAIL=$((FAIL + 1)); echo "  FAIL  a cmdline with no kernel must not render"
else
    PASS=$((PASS + 1)); echo "  PASS  a cmdline with no kernel must not render"
fi

# --- the PARTUUID is ours, and lower-cased -----------------------------------
descriptor '    kernel: /boot/vmlinuz'
case "$(render_boot_entry)" in
    *"root=PARTUUID=abcd1234-0000-0000-0000-00000000beef rw rootwait quiet"*)
        PASS=$((PASS + 1)); echo "  PASS  root= is installer-supplied and lower-cased" ;;
    *)  FAIL=$((FAIL + 1)); echo "  FAIL  root= is installer-supplied and lower-cased" ;;
esac

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
