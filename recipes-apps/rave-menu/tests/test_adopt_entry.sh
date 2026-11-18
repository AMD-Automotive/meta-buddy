#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# adopt_boot_entry() lifts an entry out of an app's own GRUB config and rewrites
# it to boot from where the installer actually put the app.
#
# The first-adoption output is deliberate parity with the vendor's install-efi.sh,
# duplicated `quiet` and all; the tests below pin that shape so it cannot drift.
# Re-adoption must be idempotent, because a cloned app's entry is by definition
# one that has already been adopted once.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
FILES="${FILES:-${HERE}/../files}"
SCRIPT="${FILES}/scripts/install_app_bundle.sh"
[ -f "$SCRIPT" ] || { echo "cannot find install_app_bundle.sh (set FILES=)"; exit 2; }

eval "$(sed -n '/^adopt_boot_entry()/,/^}/p' "$SCRIPT")"
command -v adopt_boot_entry >/dev/null 2>&1 || {
    echo "FAIL: adopt_boot_entry() not found in $SCRIPT"; exit 1; }

PASS=0; FAIL=0
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/boot"

check() {  # <name> <expected> <got>
    if [ "$2" = "$3" ]; then
        PASS=$((PASS + 1)); echo "  PASS  $1"
    else
        FAIL=$((FAIL + 1)); echo "  FAIL  $1"
        echo "        expected:"; echo "$2" | sed 's/^/          /'
        echo "        got:";      echo "$3" | sed 's/^/          /'
    fi
}

refuses() {  # <name> <args...>
    _n="$1"; shift
    if adopt_boot_entry "$@" >/dev/null 2>&1; then
        FAIL=$((FAIL + 1)); echo "  FAIL  $_n"
    else
        PASS=$((PASS + 1)); echo "  PASS  $_n"
    fi
}

NEW=aaaaaaaa-1111-2222-3333-444444444444

# --- first adoption, shaped like the real VAS3 entry -------------------------
cat > "$TMP/boot/isolinux.cfg" <<'EOF'
menuentry 'boot' {
    multiboot2 /xen placeholder dom0=pvh dom0_mem=38G
    module2 /bzImage ramdisk_size=32768 root=LABEL=r2c-root quiet splash cma=512M
    module2 /initrd
}
EOF
FIRST=$(adopt_boot_entry "$TMP/boot" /isolinux.cfg boot "$NEW" '$boot_disk,gpt20')
EXPECT='set root=($boot_disk,gpt20)
multiboot2 /xen placeholder dom0=pvh dom0_mem=38G
module2 /bzImage ramdisk_size=32768 root=PARTUUID='"$NEW"' rw rootwait quiet quiet splash cma=512M
module2 /initrd'
check "first adoption keeps vendor parity (duplicated quiet included)" "$EXPECT" "$FIRST"

# --- re-adoption of that very output -----------------------------------------
{ echo "menuentry 'boot' {"; echo "$FIRST"; echo "}"; } > "$TMP/boot/adopted.cfg"
AGAIN=$(adopt_boot_entry "$TMP/boot" /adopted.cfg boot "$NEW" '$boot_disk,gpt20')
check "re-adopting an adopted entry changes nothing" "$FIRST" "$AGAIN"

THIRD=$(adopt_boot_entry "$TMP/boot" /adopted.cfg boot "$NEW" '$boot_disk,gpt20')
check "adoption is stable over repeats" "$FIRST" "$THIRD"

# --- re-adoption onto a different partition ----------------------------------
OTHER=bbbbbbbb-1111-2222-3333-444444444444
MOVED=$(adopt_boot_entry "$TMP/boot" /adopted.cfg boot "$OTHER" '$boot_disk,gpt21')
EXPECT='set root=($boot_disk,gpt21)
multiboot2 /xen placeholder dom0=pvh dom0_mem=38G
module2 /bzImage ramdisk_size=32768 root=PARTUUID='"$OTHER"' rw rootwait quiet quiet splash cma=512M
module2 /initrd'
check "re-adoption substitutes PARTUUID and device, and only those" "$EXPECT" "$MOVED"

case "$MOVED" in
    *"gpt20"*) FAIL=$((FAIL + 1)); echo "  FAIL  the stale set root is gone, not merely outranked" ;;
    *) PASS=$((PASS + 1)); echo "  PASS  the stale set root is gone, not merely outranked" ;;
esac

if [ "$(echo "$MOVED" | grep -c 'rw rootwait quiet')" -eq 1 ]; then
    PASS=$((PASS + 1)); echo "  PASS  no second rw rootwait quiet accumulates"
else
    FAIL=$((FAIL + 1)); echo "  FAIL  no second rw rootwait quiet accumulates"
fi

# --- a vendor set root must be replaced, not kept and not mangled ------------
# Before this was handled, `\s+root=` matched inside `set root=(hd0,gpt1)` and
# rewrote it to `set root=PARTUUID=...`, which is not a device GRUB can use.
cat > "$TMP/boot/vendor-root.cfg" <<'EOF'
menuentry 'boot' {
    set root=(hd0,gpt1)
    linux /vmlinuz root=/dev/sda2 quiet
}
EOF
GOT=$(adopt_boot_entry "$TMP/boot" /vendor-root.cfg boot "$NEW" '$boot_disk,gpt4')
EXPECT='set root=($boot_disk,gpt4)
linux /vmlinuz root=PARTUUID='"$NEW"' rw rootwait quiet quiet'
check "a stale vendor set root is dropped, not mangled" "$EXPECT" "$GOT"

# --- the PARTUUID is lower-cased ---------------------------------------------
GOT=$(adopt_boot_entry "$TMP/boot" /isolinux.cfg boot "ABCD1234-EF00-0000-0000-000000000000" '$boot_disk,gpt20')
case "$GOT" in
    *"root=PARTUUID=abcd1234-ef00-0000-0000-000000000000 "*)
        PASS=$((PASS + 1)); echo "  PASS  PARTUUID is lower-cased" ;;
    *)  FAIL=$((FAIL + 1)); echo "  FAIL  PARTUUID is lower-cased" ;;
esac

# --- refusals ----------------------------------------------------------------
refuses "an entry name that is not there is refused" \
        "$TMP/boot" /isolinux.cfg nosuch "$NEW" '$boot_disk,gpt20'
refuses "a path escaping the boot partition is refused" \
        "$TMP/boot" ../../etc/passwd boot "$NEW" '$boot_disk,gpt20'
refuses "a missing config is refused" \
        "$TMP/boot" /nope.cfg boot "$NEW" '$boot_disk,gpt20'

cat > "$TMP/boot/unsafe.cfg" <<'EOF'
menuentry 'boot' {
    linux /vmlinuz root=/dev/sda2 init=`reboot`
}
EOF
refuses "shell metacharacters in an entry are refused" \
        "$TMP/boot" /unsafe.cfg boot "$NEW" '$boot_disk,gpt20'

cat > "$TMP/boot/empty.cfg" <<'EOF'
menuentry 'boot' {
}
EOF
refuses "an empty entry is refused" \
        "$TMP/boot" /empty.cfg boot "$NEW" '$boot_disk,gpt20'

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
