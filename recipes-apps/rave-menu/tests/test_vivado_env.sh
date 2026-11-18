#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# vivado_env.sh must resolve to exactly what the Python resolver says, so the
# shell and web UI can never disagree about which toolchain is in use.
#
# Runs on the build host: a fake install prefix and manifest stand in for a
# board, and the resolver is invoked directly out of files/.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
FILES="$(dirname "$HERE")/files"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PREFIX="$TMP/opt/tools"
FAILURES=0

ok()   { printf 'ok   - %s\n' "$1"; }
fail() { printf 'FAIL - %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

make_install() {
    mkdir -p "$PREFIX/$1/HWSRVR/bin"
    printf '#!/bin/sh\n' > "$PREFIX/$1/HWSRVR/bin/hw_server"
    chmod +x "$PREFIX/$1/HWSRVR/bin/hw_server"
    printf '# fake\n' > "$PREFIX/$1/HWSRVR/bin/setupEnv.sh"
}

cat > "$TMP/manifest.json" <<JSON
{
  "install_prefix": "$PREFIX",
  "base_version": "2025.2",
  "default_version": "2025.2",
  "versions": [
    {"version": "2025.2", "filename": "a.tar", "url": "", "sha256": "", "qr": ""},
    {"version": "2026.1", "filename": "b.tar", "url": "", "sha256": "", "qr": ""}
  ]
}
JSON

mkdir -p "$PREFIX"
make_install 2025.2
make_install 2026.1

VIVADO_MANIFEST="$TMP/manifest.json"
VIVADO_RESOLVER="$FILES/vivado_installer.py"
export VIVADO_MANIFEST VIVADO_RESOLVER

# The resolver reads its own module-level paths from the manifest, so point
# the state/runtime dirs at the sandbox too.
RAVE_TEST_HOME="$TMP"
export RAVE_TEST_HOME

. "$FILES/scripts/vivado_env.sh"

# -- install prefix comes from the manifest, not a hardcoded path ------------
got="$(vivado_install_prefix)"
[ "$got" = "$PREFIX" ] && ok "install prefix from manifest" \
    || fail "install prefix: got '$got' want '$PREFIX'"

# -- shell and Python agree on the settings script ---------------------------
shell_settings="$(vivado_find_settings || echo NONE)"
py_settings="$(python3 "$VIVADO_RESOLVER" resolve-settings 2>/dev/null || echo NONE)"
[ "$shell_settings" = "$py_settings" ] && ok "shell settings match resolver ($shell_settings)" \
    || fail "settings mismatch: shell='$shell_settings' python='$py_settings'"

# -- explicit override is honoured -------------------------------------------
RAVE_VIVADO_VERSION=2026.1 export RAVE_VIVADO_VERSION
got="$(vivado_resolved_version)"
[ "$got" = "2026.1" ] && ok "RAVE_VIVADO_VERSION override honoured" \
    || fail "override: got '$got' want '2026.1'"
got="$(vivado_base_dir)"
[ "$got" = "$PREFIX/2026.1" ] && ok "base dir follows override" \
    || fail "base dir: got '$got' want '$PREFIX/2026.1'"
unset RAVE_VIVADO_VERSION

# -- fallback scan picks the newest installed, never a hardcoded release -----
got="$(vivado_find_settings_fallback || echo NONE)"
[ "$got" = "$PREFIX/2026.1/HWSRVR/bin/setupEnv.sh" ] && ok "fallback picks newest installed" \
    || fail "fallback: got '$got'"

# -- fallback must not invent an install when the prefix is empty ------------
EMPTY="$TMP/empty"; mkdir -p "$EMPTY"
( vivado_install_prefix() { printf '%s\n' "$EMPTY"; }
  vivado_find_settings_fallback >/dev/null 2>&1 ) \
    && fail "fallback reported success on an empty prefix" \
    || ok "fallback fails cleanly on an empty prefix"

# -- announce emits a tagged, human-readable line ----------------------------
line="$(vivado_announce jtag | head -n 1)"
case "$line" in
    "[jtag] using "*) ok "announce line: $line" ;;
    *) fail "announce produced '$line'" ;;
esac

[ "$FAILURES" -eq 0 ] && { echo "all vivado_env tests passed"; exit 0; }
echo "$FAILURES vivado_env test(s) failed"
exit 1
