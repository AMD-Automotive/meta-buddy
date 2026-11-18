# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# shellcheck shell=sh
# Resolve which Vivado HW Server install the programming tools must come from.
#
# Sourced by the scripts that program the Versal. Intermixing releases is not
# supported -- the xsdb used here must come from the same tree as the
# hw_server it will connect to -- so the decision is made in exactly one
# place, vivado_installer.py, and this file is a thin wrapper over it.
#
# Precedence (see vivado_installer.resolve_version):
#   RAVE_VIVADO_VERSION -> running hw_server -> user selection -> base -> newest

VIVADO_RESOLVER="${VIVADO_RESOLVER:-/opt/rave-menu/vivado_installer.py}"
VIVADO_MANIFEST="${VIVADO_MANIFEST:-/usr/share/vivado-installer/manifest.json}"
# Keep the resolver reading the same manifest this file was told to use.
RAVE_VIVADO_MANIFEST="$VIVADO_MANIFEST"
export RAVE_VIVADO_MANIFEST

vivado_install_prefix() {
    python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        prefix = json.load(fh).get("install_prefix") or ""
except Exception:
    prefix = ""
print((prefix or "/opt/tools").rstrip("/"))
' "$VIVADO_MANIFEST" 2>/dev/null || printf '/opt/tools\n'
}

# Echo just the release, e.g. "2026.1".
vivado_resolved_version() {
    python3 "$VIVADO_RESOLVER" resolve-version 2>/dev/null | cut -f1
}

# Echo the install tree the tools must come from.
vivado_base_dir() {
    _vivado_v="$(vivado_resolved_version)"
    if [ -n "$_vivado_v" ]; then
        printf '%s/%s\n' "$(vivado_install_prefix)" "$_vivado_v"
    else
        vivado_install_prefix
    fi
}

# Echo the settings script to source, or return 1 if nothing usable is found.
vivado_find_settings() {
    if [ -r "$VIVADO_RESOLVER" ] && \
       _vivado_s="$(python3 "$VIVADO_RESOLVER" resolve-settings 2>/dev/null)" && \
       [ -n "$_vivado_s" ] && [ -f "$_vivado_s" ]; then
        printf '%s\n' "$_vivado_s"
        return 0
    fi
    vivado_find_settings_fallback
}

# Last-resort scan, used only if the resolver is missing or python3 is broken.
# Picks the newest installed release rather than a hardcoded one.
vivado_find_settings_fallback() {
    _vivado_prefix="$(vivado_install_prefix)"
    for _vivado_v in $(ls -1 "$_vivado_prefix" 2>/dev/null | sort -r); do
        for _vivado_d in "${_vivado_prefix}/${_vivado_v}/HWSRVR" \
                         "${_vivado_prefix}/${_vivado_v}/Vivado_Lab" \
                         "${_vivado_prefix}/${_vivado_v}/Vivado_HW_Server" \
                         "${_vivado_prefix}/${_vivado_v}/Vivado"; do
            for _vivado_s in "${_vivado_d}/settings64.sh" "${_vivado_d}/bin/setupEnv.sh"; do
                if [ -f "$_vivado_s" ]; then
                    printf '%s\n' "$_vivado_s"
                    return 0
                fi
            done
        done
    done
    return 1
}

# Print "[tag] using <version> (<reason>)" plus any warnings, so a support log
# says which toolchain ran without guesswork.
vivado_announce() {
    _vivado_tag="${1:-tools}"
    python3 "$VIVADO_RESOLVER" describe 2>/dev/null | while IFS= read -r _vivado_line; do
        printf '[%s] %s\n' "$_vivado_tag" "$_vivado_line"
    done
}

# Source the resolved settings and put the tools on PATH. Returns 1 if no
# usable install was found; callers decide how to fail.
vivado_setup_env() {
    _vivado_tag="${1:-tools}"
    _vivado_settings="$(vivado_find_settings || true)"
    [ -n "$_vivado_settings" ] || return 1
    vivado_announce "$_vivado_tag"
    printf '[%s] Sourcing %s\n' "$_vivado_tag" "$_vivado_settings"
    # setupEnv.sh references unset variables; `set -u` would kill the shell.
    set +u
    # shellcheck disable=SC1090
    . "$_vivado_settings"
    set -u
    PATH="$(dirname "$_vivado_settings"):${PATH}"
    export PATH
    return 0
}
