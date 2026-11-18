#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# report_hw_info.sh - report BIOS revision and system component strings.
#
#   report_hw_info.sh            print the report to stdout
#   report_hw_info.sh --cache    write the report to $CACHE (for boot service)
#
# The report is slow to generate (dmidecode + SPI flash scan), so at boot a
# oneshot service caches it once; the menu then dumps the cache instantly.
set -eu

CACHE=/run/rave-menu/hw_info.txt

# dmidecode needs root to read the SMBIOS tables.
[ "$(id -u)" -eq 0 ] || exec sudo -n "$0" "$@"

# Print "  Label           : <dmidecode -s value>", trimming trailing padding.
field() {
    printf '  %-16s: %s\n' "$2" "$(dmidecode -s "$1" | sed 's/[[:space:]]*$//')"
}

# Print "  Label           : <first "Field:" value from an SMBIOS type>".
# $1 = SMBIOS type   $2 = field label as it appears in dmidecode   $3 = display label
line() {
    printf '  %-16s: %s\n' "$3" \
        "$(dmidecode -t "$1" | sed -n "s/^[[:space:]]*$2:[[:space:]]*//p" | sed 's/[[:space:]]*$//' | head -n1)"
}

report() {
echo "--- BIOS / Firmware ---"
field bios-vendor       "Vendor"
field bios-version      "Version"
field bios-release-date "Release Date"
printf '  %-16s: %s\n' "Build ID" "unknown"

echo
echo "--- System ---"
field system-manufacturer  "Manufacturer"
field system-product-name  "Product Name"
field system-version       "Version"
field system-family        "Family"
field system-sku-number    "SKU"
field system-serial-number "Serial Number"
field system-uuid          "UUID"

echo
echo "--- Baseboard ---"
field baseboard-manufacturer  "Manufacturer"
field baseboard-product-name  "Product Name"
field baseboard-version       "Version"
field baseboard-serial-number "Serial Number"
field baseboard-asset-tag     "Asset Tag"

echo
echo "--- Processor ---"
field processor-manufacturer "Manufacturer"
field processor-version      "Model"
field processor-family       "Family"
line 4 "Core Count"          "Core Count"
line 4 "Thread Count"        "Thread Count"

echo
echo "--- Memory ---"
dmidecode -t memory | awk -F': ' '
    /Memory Device/       { loc=sz=ty=sp=mf=pn="" }
    /^\tSize:/            { sz=$2 }
    /Locator:/ && !/Bank/ { loc=$2 }
    /^\tType:/            { ty=$2 }
    /Manufacturer:/       { mf=$2 }
    /Part Number:/        { pn=$2; sub(/[ \t]+$/, "", pn) }
    /Configured Memory Speed:/ {
        sp=$2
        if (sz !~ /No Module Installed/ && sz != "")
            printf "  %-8s: %s %s @ %s  %s (%s)\n", loc, sz, ty, sp, mf, pn
    }'

echo
echo "--- TPM ---"
line 43 "Vendor ID"             "Vendor"
line 43 "Specification Version" "Spec Version"
line 43 "Firmware Revision"     "Firmware Rev"

echo
echo "--- Firmware Inventory (SMBIOS type 45) ---"
dmidecode -t 45 | awk -F': ' '
    /Firmware Component Name/ { name=$2; sub(/[ \t]+$/, "", name) }
    /^\tFirmware Version/      { ver=$2; sub(/[ \t]+$/, "", ver);
                                printf "  %-16s: %s\n", name, ver }'

echo
echo "--- Network (MAC addresses) ---"
for n in /sys/class/net/*; do
    ifn=${n##*/}
    [ "$(cat "$n/type" 2>/dev/null)" = "1" ] || continue   # ARPHRD_ETHER only
    mac=$(cat "$n/address" 2>/dev/null)
    [ -n "$mac" ] && [ "$mac" != "00:00:00:00:00:00" ] || continue
    printf '  %-10s: %s\n' "$ifn" "$mac"
done

echo
echo "--- OEM / Component Strings (SMBIOS type 11) ---"
dmidecode -t 11 | grep -E '^[[:space:]]*String [0-9]+:' | sed 's/^[[:space:]]*/  /' || true
}

if [ "${1:-}" = "--cache" ]; then
    mkdir -p "$(dirname "$CACHE")"
    report > "$CACHE.tmp" && mv "$CACHE.tmp" "$CACHE"
else
    report
fi
