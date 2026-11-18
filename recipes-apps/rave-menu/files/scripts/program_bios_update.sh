#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# BIOS update — Phase 1 (see docs/bios-update-plan.md).
#
# Usage: program_bios_update.sh <zip-source>
#   <zip-source> is a URL, Dropbox share link, or local/uploaded file path
#   pointing at a vendor BIOS update package (currently: AMI AFU-style .zip).
#
# This script validates the package against the unit's detected BIOS vendor
# and product line, stages the AFU tool + ROM + a regenerated startup.nsh on
# the existing EFI System Partition, and arms a one-shot UEFI boot entry via
# efibootmgr. It deliberately does NOT reboot the board itself -- the operator
# must do that explicitly (e.g. the menu's "Reboot Now" button) after
# confirming they are physically present, per the Phase 1 safety requirement.
#
# efibootmgr safety: a prior investigation (see
# /memories/repo/bios-update-target-notes.md) accidentally deleted an
# existing NVRAM boot entry by using `-c` without explicit -d/-p and by
# passing an empty value to `-b`. This script always resolves -d/-p
# explicitly and aborts rather than guessing if anything is empty/ambiguous.
set -eu

die() { echo "[bios-update] ERROR: $*" >&2; exit 1; }

SRC="${1:?Usage: program_bios_update.sh <zip-source>}"

WORKDIR="/var/lib/rave-menu/bios-update-cache"
rm -rf "$WORKDIR"
mkdir -p "$WORKDIR"
trap 'echo "[bios-update] Cleaning up work dir"; rm -rf "$WORKDIR"' EXIT

PENDING_FILE="/var/lib/rave-menu/bios-update-pending.json"
ESP="/boot/efi"

# ── Source resolution (URL/Dropbox/local path) ──────────────────────────
# Mirrors program_remote_versal_payload.sh's resolve_source, minus the
# gzip-transparent handling (BIOS packages are plain .zip files).

normalize_url() {
    python3 - "$1" <<'PY'
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

parts = urlsplit(sys.argv[1])
if parts.hostname in {"dropbox.com", "www.dropbox.com"}:
    query = parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key not in {"dl", "raw"}]
    query.append(("dl", "1"))
    parts = parts._replace(query=urlencode(query))
print(urlunsplit(parts._replace(fragment="")))
PY
}

url_filename() {
    python3 - "$1" <<'PY'
import os
import sys
from urllib.parse import unquote, urlsplit

print(os.path.basename(unquote(urlsplit(sys.argv[1]).path)) or "bios-update.zip")
PY
}

download_url() {
    src="$1"
    dest="$2"
    case "$src" in
        https://www.dropbox.com/*|https://dropbox.com/*)
            python3 - "$src" "$dest" <<'PY'
import sys
import urllib.request

request = urllib.request.Request(sys.argv[1], headers={"User-Agent": "RAVE2-BIOS-Downloader/1"})
with urllib.request.urlopen(request) as response, open(sys.argv[2], "wb") as output:
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        output.write(chunk)
PY
            ;;
        *) wget -O "$dest" "$src" >&2 ;;
    esac
}

resolve_source() {
    src="$1"
    case "$src" in
        http://*|https://*)
            src=$(normalize_url "$src")
            fname=$(url_filename "$src")
            dest="${WORKDIR}/${fname}"
            echo "[bios-update] Downloading ${src} -> ${dest}" >&2
            download_url "$src" "$dest" || die "Failed to download ${src}"
            ;;
        *)
            [ -f "$src" ] || die "Local file not found: ${src}"
            dest="$src"
            echo "[bios-update] Using local file ${src}" >&2
            ;;
    esac
    echo "$dest"
}

ZIP_PATH=$(resolve_source "$SRC")

# ── Detect BIOS vendor + product line (§4a) ─────────────────────────────

BIOS_VENDOR=$(python3 - <<'PY'
import re
import subprocess

out = subprocess.run(["dmidecode", "-t", "bios"], capture_output=True, text=True).stdout
m = re.search(r"Vendor:\s*(.+)", out)
v = (m.group(1).strip().lower() if m else "")
if "coreboot" in v:
    print("coreboot")
elif "megatrends" in v:
    print("ami")
else:
    print("unknown")
PY
)

PRODUCT_LINE=$(python3 - <<'PY'
import re
import subprocess

out = subprocess.run(["dmidecode", "-t", "system"], capture_output=True, text=True).stdout
m = re.search(r"Product Name:\s*(.+)", out)
p = (m.group(1).strip().lower() if m else "")
print(p if p in ("rave2-core", "rave2-pro", "rave2-max") else "unknown")
PY
)

echo "[bios-update] Detected BIOS vendor: ${BIOS_VENDOR}, product line: ${PRODUCT_LINE}"
[ "$BIOS_VENDOR" != "unknown" ] || die "Could not determine BIOS vendor via dmidecode"
[ "$PRODUCT_LINE" != "unknown" ] || echo "[bios-update] WARNING: could not determine product line (dmidecode Product Name); continuing without that check" >&2

# ── Extract + validate package against detected vendor (§5) ────────────

EXTRACT_DIR="${WORKDIR}/extracted"
mkdir -p "$EXTRACT_DIR"
unzip -q -o "$ZIP_PATH" -d "$EXTRACT_DIR" || die "Failed to unzip ${ZIP_PATH} (zip-slip-safe: unzip refuses paths escaping the target dir)"

case "$BIOS_VENDOR" in
    ami)
        FLASH_DIR="${EXTRACT_DIR}/flash"
        [ -d "$FLASH_DIR" ] || die "Package does not contain a flash/ directory -- not an AMI AFU package, or wrong vendor for this unit"

        EFI_BIN=$(find "$FLASH_DIR" -maxdepth 1 -iname 'Afu*.efi' | head -n1)
        SHELL_BIN=$(find "$FLASH_DIR" -maxdepth 1 -iname 'Shell*.efi' | head -n1)
        ROM_FILE=$(find "$FLASH_DIR" -maxdepth 1 -iname '*.rom' | head -n1)
        [ -n "$EFI_BIN" ]   || die "No AFU *.efi binary found under flash/"
        [ -n "$SHELL_BIN" ] || die "No Shellx64.efi found under flash/ (required to auto-run AFU without a keyboard)"
        [ -n "$ROM_FILE" ]  || die "No *.rom file found under flash/"

        ROM_SIZE=$(stat -c %s "$ROM_FILE")
        echo "[bios-update] Found AFU=$(basename "$EFI_BIN") SHELL=$(basename "$SHELL_BIN") ROM=$(basename "$ROM_FILE") (${ROM_SIZE} bytes)"

        # Board-id sanity check: the ROM filename convention seen so far is
        # <board_id>_<version>_<pcb_rev>_<size>.rom (e.g. G897X209_P132_PCB01_32M.rom).
        # We don't yet have a hard mapping of board_id -> product line, so this
        # is logged for the operator to review, not enforced (see docs §11).
        echo "[bios-update] ROM filename: $(basename "$ROM_FILE") -- verify this matches your board before continuing"
        ;;
    coreboot)
        die "Coreboot BIOS update packages are not yet supported (format still TBD -- see docs/bios-update-plan.md §11)"
        ;;
esac

# ── Stage payload on the existing ESP (§4b) ─────────────────────────────

mountpoint -q "$ESP" || die "${ESP} is not mounted -- refusing to stage"

FREE_KB=$(df -Pk "$ESP" | awk 'NR==2 {print $4}')
NEED_KB=$(( (ROM_SIZE / 1024) + 4096 ))
[ "$FREE_KB" -ge "$NEED_KB" ] || die "Not enough space on ${ESP}: need ~${NEED_KB} KiB, have ${FREE_KB} KiB"

STAGE_DIR="${ESP}/bios-update"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"

# Copy-then-verify-then-sync: a prior bench test rebooted right after staging
# and the ESP (vfat, no explicit sync) still had dirty pages in the page
# cache -- AfuEfix64.efi was missing from the staged directory on next boot
# even though Shellx64.efi (loaded earlier in the boot process) was present.
# Never trust a bare `cp` before a reboot; verify size and force a sync.
stage_file() {
    src="$1"
    dest="$2"
    cp "$src" "$dest" || die "Failed to copy $(basename "$src") to ${dest}"
    src_size=$(stat -c %s "$src")
    dest_size=$(stat -c %s "$dest" 2>/dev/null || echo -1)
    [ "$src_size" = "$dest_size" ] || die "Copy verification failed for ${dest}: expected ${src_size} bytes, got ${dest_size}"
}

stage_file "$EFI_BIN" "${STAGE_DIR}/AfuEfix64.efi"
stage_file "$SHELL_BIN" "${STAGE_DIR}/Shellx64.efi"
stage_file "$ROM_FILE" "${STAGE_DIR}/bios.rom"

# Regenerated ourselves rather than trusting the package's own flash.nsh
# verbatim -- we never execute the package's file directly (that would let a
# malicious/corrupt zip inject arbitrary Shell commands) -- but we DO use the
# exact AFU switches the vendor's flash.nsh specified (/P /B /N /CAPSULE),
# not invented ones. A bench test with unverified switches (/p /b /n /x)
# reached "System Secure Flash: Enabled" and then failed with
# "43 - Error: Problem erasing flash" -- this board's secure-flash mode
# almost certainly requires /CAPSULE for AFU to be allowed to erase/write.
#
# Self-locating on purpose: a separate bench test showed the UEFI Shell's
# current directory when auto-running startup.nsh is NOT reliably the
# directory the script lives in (a bare "AfuEfix64.efi" failed with "not
# recognized" even though the file was verified present and correctly sized
# right next to startup.nsh). Scan the known filesystem aliases and
# explicitly cd before running AFU, rather than depending on an assumed CWD.
cat > "${STAGE_DIR}/startup.nsh" <<'EOF'
echo -off
for %a in fs0 fs1 fs2 fs3 fs4 fs5 fs6 fs7
  if exist %a:\bios-update\AfuEfix64.efi then
    %a:
    cd \bios-update
    AfuEfix64.efi bios.rom /P /B /N /CAPSULE
  endif
endfor
reset
EOF

sync
sync
echo "[bios-update] Staged AFU package under ${STAGE_DIR} (synced to disk):"
ls -l "$STAGE_DIR" | sed 's/^/[bios-update]   /'

# ── Arm a one-shot UEFI boot entry (§4b step 3) ─────────────────────────
#
# SAFETY: always resolve -d/-p explicitly and verify every value used with
# efibootmgr is non-empty before use. See the header comment and
# /memories/repo/bios-update-target-notes.md for why this matters.

command -v efibootmgr >/dev/null 2>&1 || die "efibootmgr not found"

INITIAL_ORDER=$(efibootmgr | sed -n 's/^BootOrder: //p')
[ -n "$INITIAL_ORDER" ] || die "Could not read current BootOrder -- refusing to touch NVRAM"
efibootmgr -v > "${WORKDIR}/efibootmgr-before.txt" || die "efibootmgr -v failed"
echo "[bios-update] Current BootOrder: ${INITIAL_ORDER} (snapshot saved to ${WORKDIR}/efibootmgr-before.txt)"

ESP_DEV=$(findmnt -no SOURCE "$ESP") || die "Could not resolve ESP device via findmnt"
[ -n "$ESP_DEV" ] || die "findmnt returned an empty ESP device"
ESP_DEV_NAME=$(basename "$ESP_DEV")
ESP_DISK=$(lsblk -no PKNAME "$ESP_DEV")
[ -n "$ESP_DISK" ] || die "Could not resolve ESP parent disk via lsblk"
ESP_PARTNUM=$(cat "/sys/class/block/${ESP_DEV_NAME}/partition" 2>/dev/null || true)
[ -n "$ESP_PARTNUM" ] || die "Could not resolve ESP partition number"

# Remove any stale entry from a previous, aborted attempt (by label, not by
# number, and only if the label matches exactly -- never delete blindly).
STALE_NUM=$(efibootmgr | sed -n -E 's/^Boot([0-9A-Fa-f]{4})\* RAVE2 BIOS Update.*/\1/p' | head -n1)
if [ -n "$STALE_NUM" ]; then
    echo "[bios-update] Removing stale boot entry Boot${STALE_NUM} from a previous attempt"
    efibootmgr -q -b "$STALE_NUM" -B || die "Failed to remove stale boot entry Boot${STALE_NUM}"
fi

# Re-read BootOrder *after* any stale-entry removal above, so the value we
# restore to later (post -c) never references an entry number that no longer
# exists.
BEFORE_ORDER=$(efibootmgr | sed -n 's/^BootOrder: //p')
[ -n "$BEFORE_ORDER" ] || die "Could not read current BootOrder -- refusing to touch NVRAM"

echo "[bios-update] Creating one-shot boot entry (disk=/dev/${ESP_DISK} part=${ESP_PARTNUM})"
efibootmgr -c -d "/dev/${ESP_DISK}" -p "${ESP_PARTNUM}" -L "RAVE2 BIOS Update" \
    -l '\bios-update\Shellx64.efi' > "${WORKDIR}/efibootmgr-create.txt" \
    || die "efibootmgr -c failed"

NEW_NUM=$(sed -n -E 's/^Boot([0-9A-Fa-f]{4})\* RAVE2 BIOS Update.*/\1/p' "${WORKDIR}/efibootmgr-create.txt" | tail -n1)
if [ -z "$NEW_NUM" ]; then
    NEW_NUM=$(efibootmgr | sed -n -E 's/^Boot([0-9A-Fa-f]{4})\* RAVE2 BIOS Update.*/\1/p' | tail -n1)
fi
[ -n "$NEW_NUM" ] || die "Could not determine the new boot entry number -- aborting without touching BootNext"

# IMPORTANT: efibootmgr -c prepends the new entry to BootOrder by default --
# that makes the change permanent, not one-shot, and was the cause of a
# bench-test boot loop (the Shell kept re-entering on every subsequent boot
# even after BootNext had already been consumed). Restore BootOrder to
# exactly what it was before -c touched it; BootNext alone drives the single
# one-shot boot into the Shell.
echo "[bios-update] Restoring BootOrder to its pre-existing value: ${BEFORE_ORDER}"
efibootmgr -q -o "$BEFORE_ORDER" || die "Failed to restore BootOrder to ${BEFORE_ORDER}"

echo "[bios-update] Setting BootNext=${NEW_NUM}"
efibootmgr -q -n "$NEW_NUM" || die "efibootmgr -n failed"

cat > "$PENDING_FILE" <<EOF
{
  "boot_entry": "${NEW_NUM}",
  "bios_vendor": "${BIOS_VENDOR}",
  "product_line": "${PRODUCT_LINE}",
  "rom_file": "$(basename "$ROM_FILE")",
  "staged_at": "$(date '+%Y-%m-%dT%H:%M:%S%z')"
}
EOF

echo "[bios-update] Staged successfully."
echo "[bios-update] Boot entry Boot${NEW_NUM} ('RAVE2 BIOS Update') is set as BootNext."
echo "[bios-update] Nothing has been flashed yet -- reboot the board (Reboot Now) to run it."
echo "[bios-update] Local physical presence at the board is required to complete this update."
