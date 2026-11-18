#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC, BM
# Maintainer: KC

# install_app_bundle.sh — orchestrate installing a .rave2app application bundle.
#
# Implemented scope (Phase 1 + Phase 2 + Phase 3):
#   - resolve a bundle (path or URL) and validate its appimage.yaml envelope.
#     For a URL whose application image is the primary x86-rootfs-tar, only the
#     leading appimage.yaml is fetched first (header pass); the slot is then
#     formatted and the whole bundle is streamed straight into it in a single
#     pass — the payload never lands on the menu filesystem, so the image may be
#     larger than the menu free space. Local files and the ext4/custom-build
#     kinds fall back to reading/downloading the full bundle (Phase 1),
#   - deploy the x86 application image into the app slot: format the slot per the
#     `slot` block and stream-extract an x86-rootfs-tar (primary) or write an
#     x86-rootfs-ext4 image via x86-replace-application, then apply overlays and
#     hook-scripts and register the GRUB boot entry (Phase 3),
#   - persist the bundle's Versal images (kept gzipped) into <app-slot>/lib/versal,
#     write installed.json, and program the Versal side via
#     program_remote_versal_payload.sh (Phase 2).
#   - recovery (--recovery): reprogram the Versal side from the images already
#     persisted on the app slot (no bundle, no x86 changes) (Phase 6).
#
# Usage:
#   install_app_bundle.sh <bundle_path_or_url> [--sha256 <sum>] [--slot a]
#                         [--dry-run] [--no-cache] [--keep-bundle]
#   install_app_bundle.sh --recovery [--slot a]
#
# --sha256 is accepted for compatibility but no longer gates the install. A
# bundle downloaded from a URL via the cached fallback is removed after a
# successful install; pass --keep-bundle to retain it (e.g. for debugging).
#
# Output is line-buffered and emits the multi-tab markers consumed by the
# rave-menu terminal modal (see docs/app-bundle-installer/02-console-reporting.md).

set -eu

# --- Locations ---------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(dirname "$SCRIPT_DIR")"          # /opt/rave-menu
. "${SCRIPT_DIR}/vivado_env.sh"
DESCRIPTOR_TOOL="${APP_ROOT}/descriptor_tool.py"
APP_SLOTS="${APP_ROOT}/app_slots.py"
APP_REGION="${APP_ROOT}/app_region.py"
CACHE_DIR="${APP_CACHE_DIR:-/var/lib/rave-menu/app-cache}"
# Master GRUB template shipped with the menu OS; used to re-render the live ESP
# grub.cfg so the app boot entry tracks template changes (e.g. the slot-relative
# app-kernel entry) on already-installed boards. Mirrors install_ubuntu.sh.
GRUB_TEMPLATE="${GRUB_TEMPLATE:-/usr/share/grub/grub-nvme-rauc.cfg}"

# --- Helpers -----------------------------------------------------------------
die() { echo "[app-install] ERROR: $*" >&2; exit 1; }
log() { echo "[app-install] $*"; }

# Copy stdin to stdout and to $1, at pipe speed.
#
# busybox tee moves a byte per syscall: measured on the unit, 500 MB takes 0.47s
# through a plain pipe and 2m05 through tee — 268x slower, and nearly all of it
# system time. A 5 GB app image would spend ~20 minutes there alone. python3 is
# already a hard dependency of this script, and a 1 MiB buffer restores full
# speed.
fast_tee() {
    python3 -u -c '
import sys
with open(sys.argv[1], "wb") as extra:
    read = sys.stdin.buffer.read
    write = sys.stdout.buffer.write
    while True:
        chunk = read(1 << 20)
        if not chunk:
            break
        extra.write(chunk)
        write(chunk)
    sys.stdout.buffer.flush()
' "$1"
}

normalize_bundle_url() {
    python3 - "$1" <<'PY'
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

url = sys.argv[1]
parts = urlsplit(url)
if parts.hostname in {"dropbox.com", "www.dropbox.com"}:
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key not in {"dl", "raw"}]
    query.append(("dl", "1"))
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
print(url)
PY
}

stream_url() {
    case "$1" in
        https://www.dropbox.com/*|https://dropbox.com/*)
            python3 - "$1" <<'PY'
import shutil
import sys
import urllib.request

request = urllib.request.Request(sys.argv[1], headers={"User-Agent": "RAVE2-App-Installer/1"})
try:
    with urllib.request.urlopen(request) as response:
        shutil.copyfileobj(response, sys.stdout.buffer, length=1024 * 1024)
except BrokenPipeError:
    pass
PY
            ;;
        *) wget -q -O - "$1" 2>/dev/null ;;
    esac
}

# Keep each pipeline stage's diagnostics/status: POSIX sh otherwise only sees
# tar's exit code, hiding a gzip CRC error or a failed download behind a missing
# descriptor. URL reads intentionally stop after the header, so upstream broken
# pipes are expected when tar successfully extracted a non-empty descriptor.
read_bundle_descriptor() {
    _read_dir="${WORK_DIR}/descriptor-read"
    mkdir -p "$_read_dir"
    if (
        _rc=0
        if [ "$IS_URL" = "1" ]; then
            stream_url "$BUNDLE_SRC" || _rc=$?
        else
            cat "$BUNDLE" || _rc=$?
        fi
        printf '%s\n' "$_rc" > "$_read_dir/source.status"
    ) 2> "$_read_dir/source.err" | (
        _rc=0
        LC_ALL=C gzip -dc || _rc=$?
        printf '%s\n' "$_rc" > "$_read_dir/gzip.status"
    ) 2> "$_read_dir/gzip.err" | (
        if [ "$IS_URL" = "1" ]; then
            LC_ALL=C tar -xO --occurrence=1 appimage.yaml
        else
            LC_ALL=C tar -xO appimage.yaml
        fi
    ) > "$DESCRIPTOR" 2> "$_read_dir/tar.err"; then
        _tar_rc=0
    else
        _tar_rc=$?
    fi
    _gzip_rc=$(cat "$_read_dir/gzip.status")
    _source_rc=$(cat "$_read_dir/source.status")

    # A successful header-only read may deliberately interrupt wget/gzip. Do
    # not mistake that for a damaged download, but retain actual gzip errors.
    if [ "$_tar_rc" = "0" ] && [ ! -s "$_read_dir/gzip.err" ]; then
        if [ "$IS_URL" = "1" ] \
           || { [ "$_gzip_rc" = "0" ] && [ "$_source_rc" = "0" ]; }; then
            [ -s "$DESCRIPTOR" ] \
                || die "bundle contains an empty appimage.yaml. Rebuild it with a valid descriptor."
            return 0
        fi
    fi

    for _diag in source gzip tar; do
        if [ -s "$_read_dir/${_diag}.err" ]; then
            sed "s/^/[app-install] ${_diag}: /" "$_read_dir/${_diag}.err" >&2
        fi
    done

    # wget's network/server failures often leave gzip an empty or truncated
    # stream. Exit 3 is a write error (e.g. tar rejected the archive and closed
    # its pipe); 141 is SIGPIPE. Neither proves a network failure.
    if [ "$IS_URL" = "1" ] && [ "$_source_rc" != "0" ] \
       && [ "$_source_rc" != "3" ] && [ "$_source_rc" != "141" ]; then
        die "bundle download failed or was interrupted. Check the URL and connection, then retry."
    fi
    if [ -s "$_read_dir/gzip.err" ] || [ "$_gzip_rc" != "0" ]; then
        die "bundle is corrupt, incomplete, or not a gzip-compressed .rave2app archive. Download it again or rebuild it, then retry."
    fi
    if [ "$IS_URL" = "0" ] && [ "$_source_rc" != "0" ]; then
        die "could not read the bundle file. Check that it is accessible and retry."
    fi
    if [ "$_tar_rc" != "0" ]; then
        # GNU tar reports these two lines for a healthy archive whose requested
        # member is absent. Any additional diagnostic means an archive error.
        if grep -q '^tar: appimage.yaml: Not found in archive$' "$_read_dir/tar.err" \
           && ! grep -Ev '^tar: (appimage.yaml: Not found in archive|Exiting with failure status due to previous errors)$' \
                "$_read_dir/tar.err" >/dev/null; then
            die "bundle does not contain appimage.yaml at the archive root. Rebuild it with appimage.yaml as the first member."
        fi
        die "bundle tar archive is corrupt or incomplete. Download it again or rebuild it, then retry."
    fi
    die "bundle contains an empty appimage.yaml. Rebuild it with a valid descriptor."
}

# Open a new named tab in the rave-menu terminal modal.
emit_tab() { printf '=-=-=-=\n%s\n=-=-=-=\n' "$1"; }

# Read a per-component field value by index from the cached TSV files.
comp_field() {  # <field> <index>
    awk -F'\t' -v i="$2" '$1==i {print $2}' "${WORK_DIR}/f_$1"
}

# Mount the app slot (reusing an existing mount if one is already present).
mount_app_slot() {
    EXISTING_MNT=$(awk -v d="$SLOT_DEV" '$1==d {print $2; exit}' /proc/mounts 2>/dev/null || true)
    if [ -n "$EXISTING_MNT" ]; then
        APP_MNT="$EXISTING_MNT"
        APP_MNT_OWNED=0
        log "slot ${SLOT_DEV} already mounted at ${APP_MNT}; reusing"
    else
        APP_MNT="$(mktemp -d "${CACHE_DIR}/app-mnt.XXXXXX")"
        APP_MNT_OWNED=1
        log "mounting ${SLOT_DEV} at ${APP_MNT}"
        mount "$SLOT_DEV" "$APP_MNT" || {
            rmdir "$APP_MNT" 2>/dev/null || true
            APP_MNT=""
            die "failed to mount ${SLOT_DEV}"
        }
    fi
}

# Unmount the app slot if we mounted it; always sync first.
unmount_app_slot() {
    [ -n "${APP_MNT:-}" ] || return 0
    sync
    if [ "${APP_MNT_OWNED:-0}" = "1" ]; then
        log "unmounting ${APP_MNT}"
        umount "$APP_MNT" 2>/dev/null || umount -l "$APP_MNT" 2>/dev/null || true
        rmdir "$APP_MNT" 2>/dev/null || true
    fi
    APP_MNT=""
}

# True (0) if the bundle carries any versal-* component.
has_versal_components() {
    grep -qE 'versal-(ospi|wic)' "${WORK_DIR}/f_kind" 2>/dev/null
}

# True (0) if the bundle carries any overlay or hook-script component.
has_init_components() {
    grep -qE 'overlay|hook-script' "${WORK_DIR}/f_kind" 2>/dev/null
}

# Resolve the on-slot path for a Versal basename ("boot.bin" or "wic.ufs").
# Echoes the existing .gz or .zst sibling; with a <declared_path> argument,
# falls back to the suffix that path implies when neither sibling exists yet
# (first persist). Echoes nothing if neither exists and no declared path was
# given. Dies if both compressions are present — that state should never
# occur, and silently picking one risks programming a stale image.
versal_image_path() {  # <basename> [<declared_path>]
    _base="$1"; _declared="${2:-}"
    _gz="${VDIR}/${_base}.gz"; _zst="${VDIR}/${_base}.zst"
    if [ -f "$_gz" ] && [ -f "$_zst" ]; then
        die "both ${_gz} and ${_zst} are present on the slot; refusing to guess which is current"
    elif [ -f "$_gz" ]; then
        printf '%s\n' "$_gz"
    elif [ -f "$_zst" ]; then
        printf '%s\n' "$_zst"
    elif [ -n "$_declared" ]; then
        case "$_declared" in
            *.zst) printf '%s\n' "$_zst" ;;
            *.gz)  printf '%s\n' "$_gz" ;;
            *) die "Versal member ${_declared} has unsupported suffix (expected .gz or .zst)" ;;
        esac
    fi
}

# Extract one Versal member from the bundle to $VDIR and verify sha256. The
# member's compression (gzip or zstd) is whatever the bundle shipped; nothing
# here recompresses it.
persist_one_versal() {  # <member_path> <expected_sha256> <dest_filename>
    _mp="$1"; _sha="$2"; _dest="${VDIR}/$3"
    log "persisting ${_mp} -> ${_dest}"
    gzip -dc "$BUNDLE" | tar -xO "$_mp" > "$_dest" 2>/dev/null || {
        rm -f "$_dest" 2>/dev/null || true
        die "could not extract Versal member ${_mp} from bundle"
    }
    _actual=$(sha256sum "$_dest" | awk '{print $1}')
    if [ "$_actual" != "$_sha" ]; then
        rm -f "$_dest" 2>/dev/null || true
        die "sha256 mismatch for ${_mp}: expected ${_sha}, got ${_actual}"
    fi
    log "  sha256 OK (${3})"
}

# Persist every versal-* component in the bundle into $VDIR, in whichever
# compression it shipped.
persist_versal_images() {
    mkdir -p "$VDIR"
    while IFS="$(printf '\t')" read -r idx kind; do
        case "$kind" in
            versal-ospi) base="boot.bin" ;;
            versal-wic)  base="wic.ufs" ;;
            *) continue ;;
        esac
        _mp=$(comp_field path "$idx")
        case "$_mp" in
            *.zst) _suffix=zst ;;
            *.gz)  _suffix=gz ;;
            *) die "Versal member ${_mp} has unsupported suffix (expected .gz or .zst)" ;;
        esac
        # Stale-sibling safety: never leave both compressions on the slot.
        rm -f "${VDIR}/${base}.gz" "${VDIR}/${base}.zst"
        persist_one_versal "$_mp" "$(comp_field sha256 "$idx")" "${base}.${_suffix}"
    done < "${WORK_DIR}/f_kind"
}

# The streaming dispatcher (_stream_bundle_members) writes Versal members
# straight through with no sha256 check, unlike persist_one_versal above.
# Call this after streaming completes and before program_versal() runs.
verify_streamed_versal_images() {
    while IFS="$(printf '\t')" read -r idx kind; do
        case "$kind" in
            versal-ospi) base="boot.bin" ;;
            versal-wic)  base="wic.ufs" ;;
            *) continue ;;
        esac
        _path=$(versal_image_path "$base")
        [ -n "$_path" ] || die "expected a streamed Versal image for ${base} but none is present on the slot"
        _exp=$(comp_field sha256 "$idx")
        _got=$(sha256sum "$_path" | awk '{print $1}')
        [ "$_got" = "$_exp" ] \
            || die "sha256 mismatch for streamed $(comp_field path "$idx"): expected ${_exp}, got ${_got}"
        log "  sha256 OK (streamed $(basename "$_path"))"
    done < "${WORK_DIR}/f_kind"
}

# Fail early (with an actionable message) if the Vivado/HW Server tools that the
# reprogram path needs are not installed.
versal_settings_preflight() {
    if vivado_find_settings >/dev/null; then
        vivado_announce app-install
        return 0
    fi
    die "Vivado/HW Server tools not found under $(vivado_base_dir)/. Install the HW Server using the Vivado installer action before installing an app that programs the Versal."
}

# Program the Versal from the persisted images on the app slot, whichever
# compression each one is in.
program_versal() {
    OSPI_ARG="-"
    _p=$(versal_image_path boot.bin); [ -n "$_p" ] && OSPI_ARG="$_p"
    WIC_ARG="-"
    _p=$(versal_image_path wic.ufs); [ -n "$_p" ] && WIC_ARG="$_p"
    if [ "$OSPI_ARG" = "-" ] && [ "$WIC_ARG" = "-" ]; then
        log "no Versal images persisted — skipping Versal programming"
        return 0
    fi
    versal_settings_preflight
    # program_remote_versal_payload.sh takes <WIC> <OSPI> (arg1=WIC, arg2=OSPI).
    log "programming Versal (WIC=${WIC_ARG}, OSPI=${OSPI_ARG})"
    "${SCRIPT_DIR}/program_remote_versal_payload.sh" "$WIC_ARG" "$OSPI_ARG" \
        || die "Versal programming failed"
}

# Write/refresh <app-slot>/lib/versal/installed.json (x86 + versal blocks).
write_installed_json() {
    mkdir -p "$VDIR"
    _ospi_present=false; _ospi_sha=""
    _wic_present=false;  _wic_sha=""
    _ospi_path=$(versal_image_path boot.bin)
    if [ -n "$_ospi_path" ]; then
        _ospi_present=true
        _ospi_sha=$(sha256sum "$_ospi_path" | awk '{print $1}')
    fi
    _wic_path=$(versal_image_path wic.ufs)
    if [ -n "$_wic_path" ]; then
        _wic_present=true
        _wic_sha=$(sha256sum "$_wic_path" | awk '{print $1}')
    fi
    # x86 block — populated once the application image has been deployed.
    if [ "${X86_DEPLOYED:-0}" = "1" ]; then
        if [ "${X86_KIND}" = "x86-disk-image" ]; then
            # No legacy slot is involved; record the partitions actually written.
            _x86=$(printf '{ "kind": "%s", "root_device": "%s", "root_partuuid": "%s", "boot_device": "%s", "sha256": "%s" }' \
                "${X86_KIND}" "${APP_ROOT_DEV:-}" "${APP_ROOT_PARTUUID:-}" \
                "${APP_BOOT_DEV:-}" "${X86_SHA}")
        else
            _x86=$(printf '{ "kind": "%s", "slot": "%s", "device": "%s", "label": "%s", "sha256": "%s" }' \
                "${X86_KIND}" "${SLOT}" "${SLOT_DEV}" "${SLOT_DESC_LABEL:-$SLOT_LABEL}" "${X86_SHA}")
        fi
    else
        _x86="null"
    fi
    _now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    cat > "${VDIR}/installed.json" <<JSON
{
  "id": "${APP_ID}",
  "name": "${APP_NAME}",
  "version": "${APP_VERSION}",
  "x86": ${_x86},
  "versal": {
    "ospi_present": ${_ospi_present}, "ospi_sha256": "${_ospi_sha}",
    "wic_present": ${_wic_present}, "wic_sha256": "${_wic_sha}"
  },
  "installed_at": "${_now}"
}
JSON
    log "wrote ${VDIR}/installed.json"
    # A disk-image app's rootfs is not automounted and has no legacy p4 for the
    # menu to read, so keep a copy on rave-data. It survives a menu slot update
    # and lets the UI report the installed app without mounting anything.
    if mkdir -p /var/lib/rave-menu/storage 2>/dev/null; then
        cp "${VDIR}/installed.json" /var/lib/rave-menu/storage/installed-app.json 2>/dev/null \
            && log "wrote /var/lib/rave-menu/storage/installed-app.json" \
            || log "WARNING: could not write the rave-data copy of installed.json"
    fi
    # Also drop a copy on the application rootfs at /etc so a user logged into
    # the booted app can see exactly which image is installed.
    if [ -d "${APP_MNT}/etc" ] || mkdir -p "${APP_MNT}/etc" 2>/dev/null; then
        if cp "${VDIR}/installed.json" "${APP_MNT}/etc/rave2-app.json" 2>/dev/null; then
            log "wrote ${APP_MNT}/etc/rave2-app.json"
        else
            log "WARNING: could not write ${APP_MNT}/etc/rave2-app.json"
        fi
    fi
}

# =============================================================================
# Recovery flow (--recovery): reprogram the Versal side from the images already
# persisted on the app slot at <app-slot>/lib/versal, without touching the x86
# application. Driven by the App Store "Reprogram Versal" button when an
# installed app carries Versal images. No bundle is downloaded or read.
# =============================================================================

# Read a dotted key out of a JSON file (e.g. installed.json). Prints the value
# or an empty string; never fails the script (so `set -e` stays happy).
json_field() {  # <json_file> <dotted.key>
    python3 - "$1" "$2" <<'PY' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for k in sys.argv[2].split("."):
    if isinstance(d, dict):
        d = d.get(k)
    else:
        d = None
        break
if d is not None and not isinstance(d, (dict, list)):
    print(d)
PY
}

# Verify one persisted Versal image against the sha256 recorded in
# installed.json (when present), aborting on mismatch.
verify_recovery_image() {  # <path> <expected_sha> <label>
    _p="$1"; _exp="$2"; _lbl="$3"
    _got=$(sha256sum "$_p" | awk '{print $1}')
    if [ -n "$_exp" ] && [ "$_got" != "$_exp" ]; then
        die "${_lbl} image sha256 mismatch (expected ${_exp}, got ${_got}); reinstall the application"
    fi
    log "${_lbl} image present ($(basename "$_p")) sha256 OK"
}

# Device the installed app slot actually occupies, or empty when the region
# manager has nothing installed. Neither a jumbo nor a disk-image app uses the
# legacy p4 slot, so the manifest is authoritative: a disk-image layout keeps
# the app root on `app-root`, a rootfs-style one on `app-a`.
installed_slot_row() {  # prints "<partlabel> <device>"
    [ -f "$APP_REGION" ] || return 0
    python3 "$APP_REGION" devices 2>/dev/null | awk -F'\t' '
        $1 == "app-root" { print $1, $2; found = 1; exit }
        $1 == "app-a" && alt == "" { alt = $1 " " $2 }
        END { if (!found && alt != "") print alt }
    '
}

# Reprogram the Versal from the images already on the app slot, then exit.
run_recovery() {
    emit_tab "Recovery"
    log "=== Versal recovery: reprogram from on-disk images ==="

    SLOT_DEV=$(python3 "$APP_SLOTS" device "$SLOT")  || die "could not resolve slot device"
    SLOT_LABEL=$(python3 "$APP_SLOTS" label "$SLOT") || die "could not resolve slot label"
    _region_row=$(installed_slot_row)
    if [ -n "$_region_row" ]; then
        SLOT_LABEL=${_region_row% *}
        SLOT_DEV=${_region_row#* }
    fi
    log "app slot: ${SLOT} -> ${SLOT_DEV} (label ${SLOT_LABEL})"

    mount_app_slot
    VDIR="${APP_MNT}/lib/versal"

    _manifest="${VDIR}/installed.json"
    [ -f "$_manifest" ] \
        || die "no installed app found on ${SLOT_DEV} (missing ${_manifest#${APP_MNT}}); install an application first"

    APP_ID=$(json_field "$_manifest" id)
    APP_NAME=$(json_field "$_manifest" name)
    APP_VERSION=$(json_field "$_manifest" version)
    log "installed app: ${APP_NAME:-${APP_ID:-unknown}} version ${APP_VERSION:-?}"

    _ospi_sha=$(json_field "$_manifest" versal.ospi_sha256)
    _wic_sha=$(json_field "$_manifest" versal.wic_sha256)

    _have_any=0
    _ospi_path=$(versal_image_path boot.bin)
    if [ -n "$_ospi_path" ]; then
        verify_recovery_image "$_ospi_path" "$_ospi_sha" "OSPI"
        _have_any=1
    fi
    _wic_path=$(versal_image_path wic.ufs)
    if [ -n "$_wic_path" ]; then
        verify_recovery_image "$_wic_path" "$_wic_sha" "UFS"
        _have_any=1
    fi
    [ "$_have_any" = "1" ] \
        || die "no Versal images on ${SLOT_LABEL}:/lib/versal; this application has nothing to reprogram"

    emit_tab "Versal Program"
    program_versal

    sync
    unmount_app_slot

    emit_tab "Done"
    log "Versal recovery complete for ${APP_ID:-app} ${APP_VERSION}."
    exit 0
}

# =============================================================================
# Phase 3 helpers: x86 application-image deploy, overlays/hooks, boot wiring.
# =============================================================================

# Locate the single application-image component; sets APP_IMG_IDX / APP_IMG_KIND.
APP_IMG_IDX=""
APP_IMG_KIND=""
find_app_image() {
    while IFS="$(printf '\t')" read -r idx kind; do
        case "$kind" in
            x86-rootfs-tar|x86-rootfs-ext4|custom-build|x86-disk-image)
                APP_IMG_IDX="$idx"; APP_IMG_KIND="$kind"; return 0 ;;
        esac
    done < "${WORK_DIR}/f_kind"
    die "no application-image component found in bundle"
}

# Force-unmount every current mount of a device (e.g. the udev automount under
# /run/media) so it can be written to safely.
release_dev() {  # <device>
    while :; do
        _rmp=$(awk -v d="$1" '$1==d {print $2; exit}' /proc/mounts 2>/dev/null || true)
        [ -n "$_rmp" ] || break
        log "unmounting $1 from ${_rmp}"
        umount "$_rmp" 2>/dev/null || umount -l "$_rmp" 2>/dev/null \
            || die "could not unmount $1 (busy)"
    done
}

# Force-unmount every current mount of the app slot device (e.g. the udev
# automount under /run/media) so the partition can be rewritten.
#
# This is the choke point for every path that writes the legacy slot — there are
# four of them (cached and streamed x rootfs and custom-build) — so the
# disk-image teardown lives here rather than at each call site.
release_app_slot() {
    if [ "${APP_TYPE:-standard}" = "jumbo" ]; then
        ensure_jumbo_slot
    else
        ensure_legacy_slot
    fi
    release_dev "$SLOT_DEV"
}

# The rootfs-style kinds all write to the single legacy slot partition. A
# disk-image app replaces it with its own partitions, so put the region back
# before deploying one of those over it. Called from every path that expects
# SLOT_DEV to exist.
ensure_legacy_slot() {
    [ -b "$SLOT_DEV" ] && return 0
    [ -f "$APP_REGION" ] || return 0
    log "app region holds a disk-image app; restoring the ${SLOT_DEV} layout"
    python3 "$APP_REGION" legacy \
        || die "could not restore the legacy app slot layout"
    _wait=0
    while [ ! -b "$SLOT_DEV" ] && [ "$_wait" -lt 15 ]; do
        _wait=$((_wait + 1)); sleep 1
    done
    [ -b "$SLOT_DEV" ] || die "${SLOT_DEV} did not appear after restoring the layout"
}

# A jumbo app does not use p4 at all: its slot is one partition carved out of
# the disk tail, sized from the minimum/desired the bundle declares. Claiming it
# also clears whatever standard app was installed, since both regions share
# partition indices.
ensure_jumbo_slot() {
    [ "${JUMBO_SLOT_READY:-0}" = "1" ] && return 0
    [ -f "$APP_REGION" ] || die "app_region.py not found at $APP_REGION"
    printf '[{"part": "app-a", "grow": true, "filesystem": "ext4"}]\n' \
        > "${WORK_DIR}/jumbo_layout.json"
    log "claiming the jumbo region for ${APP_ID:-the application}"
    # shellcheck disable=SC2086
    python3 "$APP_REGION" apply "${WORK_DIR}/jumbo_layout.json" \
        --app-id "${APP_ID:-}" --app-version "${APP_VERSION:-}" $REGION_ARGS \
        || die "could not claim the jumbo region"
    SLOT_DEV=$(python3 "$APP_REGION" devices \
        | awk -F'\t' '$1=="app-a" {print $2; exit}')
    [ -n "$SLOT_DEV" ] || die "the region manager created no app-a partition"
    JUMBO_SLOT_READY=1
    log "jumbo slot: ${SLOT_DEV}"
}

# Make a fresh filesystem on the app slot per the descriptor's `slot` block.
format_app_slot() {
    _fs="${SLOT_FS:-ext4}"
    _label="${SLOT_DESC_LABEL:-$SLOT_LABEL}"
    _opts=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" slot.mkfs_options)
    [ "$_fs" = "ext4" ] || die "unsupported slot filesystem: ${_fs}"
    release_app_slot
    log "formatting ${SLOT_DEV} as ext4 (label ${_label}, opts ${_opts})"
    # shellcheck disable=SC2086
    mkfs.ext4 -F -L "$_label" $_opts "$SLOT_DEV" >/dev/null 2>&1 \
        || die "mkfs.ext4 failed on ${SLOT_DEV}"
}

# Mount the (freshly written) app slot at a temp dir we own.
mount_fresh_slot() {
    APP_MNT="$(mktemp -d "${CACHE_DIR}/app-mnt.XXXXXX")"
    APP_MNT_OWNED=1
    log "mounting ${SLOT_DEV} at ${APP_MNT}"
    mount "$SLOT_DEV" "$APP_MNT" || {
        rmdir "$APP_MNT" 2>/dev/null || true
        APP_MNT=""
        die "failed to mount ${SLOT_DEV}"
    }
}

# Confirm the deployed rootfs carries a bootable kernel under /boot.
verify_rootfs_kernel() {
    for k in "${APP_MNT}/boot/bzImage" "${APP_MNT}/boot/vmlinuz"; do
        [ -f "$k" ] && { log "kernel present: ${k#${APP_MNT}}"; return 0; }
    done
    if ls "${APP_MNT}/boot/"bzImage* "${APP_MNT}/boot/"vmlinuz* >/dev/null 2>&1; then
        log "kernel present under /boot"
        return 0
    fi
    log "WARNING: no kernel (bzImage/vmlinuz) found under /boot — the app may not boot"
}

# Patch the deployed rootfs /etc/fstab so its root entry matches the app slot.
patch_rootfs_fstab() {
    _label="${SLOT_DESC_LABEL:-$SLOT_LABEL}"
    _fstab="${APP_MNT}/etc/fstab"
    mkdir -p "${APP_MNT}/etc"
    if [ -f "$_fstab" ]; then
        # Drop any existing root ("/" mount) line, keep everything else.
        awk '$1 !~ /^#/ && $2=="/" {next} {print}' "$_fstab" > "${_fstab}.new" 2>/dev/null || \
            cp "$_fstab" "${_fstab}.new"
        printf 'LABEL=%s\t/\text4\tdefaults\t1 1\n' "$_label" >> "${_fstab}.new"
        mv "${_fstab}.new" "$_fstab"
    else
        printf 'LABEL=%s\t/\text4\tdefaults\t1 1\n' "$_label" > "$_fstab"
    fi
    log "patched ${_fstab#${APP_MNT}} root -> LABEL=${_label}"
}

# PRIMARY path: stream-extract an uncompressed rootfs tar member onto the
# freshly-formatted slot in a single pass, verifying its sha256 inline.
deploy_rootfs_tar() {
    _mp=$(comp_field path "$APP_IMG_IDX")
    _sha=$(comp_field sha256 "$APP_IMG_IDX")
    format_app_slot
    mount_fresh_slot
    log "stream-extracting ${_mp} -> ${APP_MNT} (verifying sha256)"
    _shapipe="${WORK_DIR}/shapipe"
    rm -f "$_shapipe"; mkfifo "$_shapipe"
    ( sha256sum < "$_shapipe" | awk '{print $1}' > "${WORK_DIR}/sha.out" ) &
    _shapid=$!
    # Preserve file mtimes, but avoid one warning per file when the board clock
    # is behind the image. All other tar warnings and extraction errors remain.
    if gzip -dc "$BUNDLE" | tar -xO "$_mp" | fast_tee "$_shapipe" | tar --warning=no-timestamp -x -C "$APP_MNT"; then
        wait "$_shapid" 2>/dev/null || true
    else
        wait "$_shapid" 2>/dev/null || true
        die "failed to stream-extract ${_mp}"
    fi
    _got=$(cat "${WORK_DIR}/sha.out" 2>/dev/null || true)
    [ "$_got" = "$_sha" ] || die "sha256 mismatch for ${_mp}: expected ${_sha}, got ${_got}"
    log "  sha256 OK (${_mp})"
    verify_rootfs_kernel
    patch_rootfs_fstab
}

# ALT path: stream a prebuilt ext4 image member straight to the slot device via
# x86-replace-application (which writes, resizes, relabels and patches fstab).
deploy_rootfs_ext4() {
    _mp=$(comp_field path "$APP_IMG_IDX")
    _sha=$(comp_field sha256 "$APP_IMG_IDX")
    _tool=$(command -v x86-replace-application 2>/dev/null || echo /usr/sbin/x86-replace-application)
    [ -x "$_tool" ] || die "x86-replace-application not found (need recipes-utils)"
    release_app_slot
    # A jumbo slot is not p4, so the tool has to be told where to write.
    _rs_args=""
    [ "${APP_TYPE:-standard}" = "jumbo" ] \
        && _rs_args="--device ${SLOT_DEV} --label ${SLOT_DESC_LABEL:-$SLOT_LABEL}"
    log "streaming ext4 member ${_mp} -> ${SLOT_DEV} via ${_tool}"
    # shellcheck disable=SC2086
    gzip -dc "$BUNDLE" | tar -xO "$_mp" | "$_tool" "$SLOT" - --sha256 "$_sha" $_rs_args \
        || die "ext4 deploy failed"
    # Tool leaves the slot unmounted; mount it for the later Versal/overlay work.
    mount_fresh_slot
    verify_rootfs_kernel
}

# custom-build: the bundle ships a device-time build script (x86/build.sh) that
# installs the x86 host straight onto the app slot at install time — no rootfs is
# carried in the bundle (e.g. it debootstraps Ubuntu on-device). We extract the
# script, verify it, run it with the slot contract in the environment (it formats
# + mounts the slot itself and unmounts on exit), then mount the slot it produced
# so the subsequent Versal / overlay / boot steps work like the other kinds.
deploy_custom_build() {
    _mp=$(comp_field path "$APP_IMG_IDX")
    _sha=$(comp_field sha256 "$APP_IMG_IDX")
    [ -n "${BUNDLE:-}" ] \
        || die "custom-build requires the full bundle on disk (not a stream)"

    _build="${WORK_DIR}/build.sh"
    log "extracting build script ${_mp} from bundle"
    gzip -dc "$BUNDLE" | tar -xO "$_mp" > "$_build" \
        || die "could not extract build script ${_mp} from bundle"
    _got=$(sha256sum "$_build" | awk '{print $1}')
    [ "$_got" = "$_sha" ] \
        || die "sha256 mismatch for ${_mp}: expected ${_sha}, got ${_got}"
    log "  sha256 OK (${_mp})"
    chmod 0755 "$_build"

    # The script formats + mounts the slot itself; make sure nothing else holds
    # it first, and hand it the slot contract so it can reuse our label / mkfs
    # options for menu-GRUB compatibility.
    release_app_slot
    _mkfs_opts=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" slot.mkfs_options)
    _label="${SLOT_DESC_LABEL:-$SLOT_LABEL}"
    log "running custom-build script ${_mp} against ${SLOT_DEV}"
    APP_SLOT_DEV="$SLOT_DEV" \
    APP_SLOT_LABEL="$_label" \
    APP_SLOT_MKFS_OPTIONS="$_mkfs_opts" \
    BUNDLE="${BUNDLE:-}" \
        sh "$_build" || die "custom-build script ${_mp} failed"

    # The script left the slot unmounted (its EXIT trap); mount it for the rest.
    mount_fresh_slot
    verify_rootfs_kernel
}

# x86-disk-image: the bundle ships a whole disk image, exploded at pack time
# into one raw member per partition plus a `layout`. The region manager recreates
# that layout as real GPT entries inside the app region (deleting the previous
# app partitions, p4 included), then each member is streamed onto its partition.
# The app keeps its own bootloader; the menu's GRUB chainloads it.
deploy_disk_image() {
    [ -n "${BUNDLE:-}" ] \
        || die "x86-disk-image requires the full bundle on disk (not a stream)"
    [ -f "$APP_REGION" ] || die "app_region.py not found at $APP_REGION"

    _layout="${WORK_DIR}/layout.json"
    python3 "$DESCRIPTOR_TOOL" layout "$DESCRIPTOR" > "$_layout" \
        || die "could not read the disk-image layout from the descriptor"

    log "repartitioning the app region from the bundle layout"
    # shellcheck disable=SC2086
    python3 "$APP_REGION" apply "$_layout" \
        --app-id "${APP_ID:-}" --app-version "${APP_VERSION:-}" $REGION_ARGS \
        || die "region repartition failed (nothing written; see the error above)"

    # Copy each member onto the partition the region manager created for it.
    python3 "$APP_REGION" devices > "${WORK_DIR}/region_devices" \
        || die "region manager reported no installed partitions"
    while IFS="$(printf '\t')" read -r _name _dev _partuuid _fs _grow; do
        [ -n "$_name" ] || continue
        _path=$(_layout_field "$_layout" "$_name" path)
        _sha=$(_layout_field "$_layout" "$_name" sha256)
        [ -n "$_path" ] || die "layout entry ${_name} has no member path"

        # udev automounts a partition the moment it appears, and writing under a
        # mounted filesystem leaves the kernel's cached view inconsistent with
        # the disk - which then breaks the grow.
        release_dev "$_dev"

        # Discard first. Writing a smaller image over a partition that held a
        # larger filesystem leaves the old metadata beyond the new end; a later
        # grow walks into it and corrupts the result (observed as a kernel BUG
        # in ext4_flex_group_add). -f because the partition still carries the
        # previous app's filesystem signature, which blkdiscard refuses without.
        _discarded=1
        if ! blkdiscard -f "$_dev" 2>/dev/null; then
            _discarded=0
            log "  blkdiscard unavailable on ${_dev}; zeroing head and tail"
            dd if=/dev/zero of="$_dev" bs=1M count=16 conv=fsync status=none 2>/dev/null || true
            _sz=$(blockdev --getsz "$_dev" 2>/dev/null || echo 0)
            [ "$_sz" -gt 32768 ] && dd if=/dev/zero of="$_dev" bs=512 \
                seek=$((_sz - 32768)) count=32768 conv=fsync status=none 2>/dev/null || true
        fi

        log "writing ${_path} -> ${_dev} (${_name})"
        _shapipe="${WORK_DIR}/shapipe.${_name}"
        rm -f "$_shapipe"; mkfifo "$_shapipe"
        ( sha256sum < "$_shapipe" | awk '{print $1}' > "${WORK_DIR}/sha.${_name}" ) &
        _shapid=$!
        if gzip -dc "$BUNDLE" | tar -xO "$_path" | fast_tee "$_shapipe" \
                | LC_ALL=C dd of="$_dev" bs=4M conv=fsync 2>"${WORK_DIR}/dd.${_name}"; then
            wait "$_shapid" 2>/dev/null || true
        else
            wait "$_shapid" 2>/dev/null || true
            # dd stops at the end of the partition, so the usual cause is a
            # member built bigger than the size_mib it was paired with.
            if grep -qi "no space left" "${WORK_DIR}/dd.${_name}" 2>/dev/null; then
                die "${_path} is larger than the ${_name} partition; raise its size_mib"
            fi
            die "failed to write ${_path} to ${_dev}"
        fi
        _got=$(cat "${WORK_DIR}/sha.${_name}" 2>/dev/null || true)
        [ "$_got" = "$_sha" ] \
            || die "sha256 mismatch for ${_path}: expected ${_sha}, got ${_got}"
        log "  sha256 OK (${_path})"

        # A partition created larger than the filesystem written into it has
        # unusable space at the end; ext4 can be stretched over it. This is not
        # only the `grow` entry: a fixed size_mib may also exceed its member.
        if [ "$_fs" = "ext4" ]; then
            # Without a discard, anything the previous app left between the end
            # of this image and the end of the partition is still there, and
            # resize2fs will walk into it. Erase that span first; it is slow,
            # but only on hardware that cannot discard.
            if [ "$_discarded" = "0" ]; then
                _written=$(LC_ALL=C awk '/bytes/ {print $1; exit}' \
                               "${WORK_DIR}/dd.${_name}" 2>/dev/null || echo 0)
                _partsz=$(blockdev --getsize64 "$_dev" 2>/dev/null || echo 0)
                if [ "$_written" -gt 0 ] && [ "$_partsz" -gt "$_written" ]; then
                    # Round the start UP to the next MiB: truncating division
                    # would begin inside the image and erase its tail.
                    _skip_mib=$(( (_written + 1048575) / 1048576 ))
                    log "  erasing $(( _partsz / 1048576 - _skip_mib )) MiB of stale data past the image"
                    dd if=/dev/zero of="$_dev" bs=1M seek="$_skip_mib" \
                        conv=fsync status=none 2>/dev/null || true
                fi
            fi
            log "growing ${_dev} to fill its partition"
            e2fsck -fp "$_dev" >/dev/null 2>&1 || true
            resize2fs "$_dev" >/dev/null 2>&1 \
                || log "WARNING: resize2fs failed on ${_dev}; the app keeps its shipped size"
        fi
    done < "${WORK_DIR}/region_devices"

    # The app's root partition is the one named app-root; mount it so overlays,
    # hooks and the Versal steps work exactly as for the other kinds.
    APP_ROOT_DEV=$(awk -F'\t' '$1=="app-root" {print $2; exit}' "${WORK_DIR}/region_devices")
    [ -n "$APP_ROOT_DEV" ] || die "layout has no 'app-root' partition to mount"
    APP_MNT="$(mktemp -d "${CACHE_DIR}/app-mnt.XXXXXX")"
    APP_MNT_OWNED=1
    mount "$APP_ROOT_DEV" "$APP_MNT" || {
        rmdir "$APP_MNT" 2>/dev/null || true
        APP_MNT=""
        die "failed to mount ${APP_ROOT_DEV}"
    }
    log "mounted ${APP_ROOT_DEV} at ${APP_MNT}"

    # Hand the fixup hook everything it needs to rewrite the app's own boot
    # config: device nodes, the filesystem UUID and the PARTUUID. UUID is the
    # form to prefer, since it survives any future renumbering.
    APP_BOOT_DEV=$(awk -F'\t' '$1=="app-boot" {print $2; exit}' "${WORK_DIR}/region_devices")
    APP_ROOT_PARTUUID=$(awk -F'\t' '$1=="app-root" {print $3; exit}' "${WORK_DIR}/region_devices")
    APP_ROOT_UUID=$(blkid -o value -s UUID "$APP_ROOT_DEV" 2>/dev/null || true)
    export APP_ROOT_DEV APP_BOOT_DEV APP_ROOT_PARTUUID APP_ROOT_UUID
    log "app-root UUID=${APP_ROOT_UUID:-unknown} PARTUUID=${APP_ROOT_PARTUUID:-unknown}"
}

# Streamed deploy (x86-disk-image): repartition the app region from the layout,
# then stream the whole bundle exactly once, writing each partition member as it
# arrives in the tar stream. Eliminates the O(N²) re-decompression cost of the
# non-streamed deploy_disk_image() path when the bundle has multiple large members.
deploy_disk_image_streamed() {
    emit_tab "Init Partition"
    find_app_image
    X86_KIND="$APP_IMG_KIND"
    X86_SHA=$(comp_field sha256 "$APP_IMG_IDX")
    [ -f "$APP_REGION" ] || die "app_region.py not found at $APP_REGION"

    _layout="${WORK_DIR}/layout.json"
    python3 "$DESCRIPTOR_TOOL" layout "$DESCRIPTOR" > "$_layout" \
        || die "could not read the disk-image layout from the descriptor"

    log "repartitioning the app region from the bundle layout"
    # shellcheck disable=SC2086
    python3 "$APP_REGION" apply "$_layout" \
        --app-id "${APP_ID:-}" --app-version "${APP_VERSION:-}" $REGION_ARGS \
        || die "region repartition failed (nothing written; see the error above)"

    python3 "$APP_REGION" devices > "${WORK_DIR}/region_devices" \
        || die "region manager reported no installed partitions"

    # Pre-discard / zero each partition before streaming — release_dev, blkdiscard,
    # and the zero-fill fallback don't touch bundle content so they run up front.
    # Also build disk_members (name<TAB>dev<TAB>fs<TAB>grow<TAB>sha256<TAB>discarded)
    # for the per-member dispatcher to look up without forking python3 per member.
    : > "${WORK_DIR}/disk_members"
    : > "${WORK_DIR}/disk_map"
    while IFS="$(printf '\t')" read -r _name _dev _partuuid _fs _grow; do
        [ -n "$_name" ] || continue
        _path=$(_layout_field "$_layout" "$_name" path)
        _sha=$(_layout_field "$_layout" "$_name" sha256)
        [ -n "$_path" ] || die "layout entry ${_name} has no member path"

        release_dev "$_dev"

        _discarded=1
        if ! blkdiscard -f "$_dev" 2>/dev/null; then
            _discarded=0
            log "  blkdiscard unavailable on ${_dev}; zeroing head and tail"
            dd if=/dev/zero of="$_dev" bs=1M count=16 conv=fsync status=none 2>/dev/null || true
            _sz=$(blockdev --getsz "$_dev" 2>/dev/null || echo 0)
            [ "$_sz" -gt 32768 ] && dd if=/dev/zero of="$_dev" bs=512 \
                seek=$((_sz - 32768)) count=32768 conv=fsync status=none 2>/dev/null || true
        fi

        printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$_name" "$_dev" "$_fs" "$_grow" "$_sha" "$_discarded" \
            >> "${WORK_DIR}/disk_members"

        # disk_map is appended to stream_map by _stream_bundle_members after it
        # builds the component entries, so partition members don't collide with
        # the x86-disk-image component entry from f_kind.
        printf '%s\tx86-disk-image-member\t%s\n' "$_path" "$_name" \
            >> "${WORK_DIR}/disk_map"
    done < "${WORK_DIR}/region_devices"

    # app-root device and PARTUUID are knowable now (before any streaming).
    APP_ROOT_DEV=$(awk -F'\t' '$1=="app-root" {print $2; exit}' "${WORK_DIR}/region_devices")
    [ -n "$APP_ROOT_DEV" ] || die "layout has no 'app-root' partition"
    APP_BOOT_DEV=$(awk -F'\t' '$1=="app-boot" {print $2; exit}' "${WORK_DIR}/region_devices")
    APP_ROOT_PARTUUID=$(awk -F'\t' '$1=="app-root" {print $3; exit}' "${WORK_DIR}/region_devices")
    export APP_ROOT_DEV APP_BOOT_DEV APP_ROOT_PARTUUID

    APP_MNT="$(mktemp -d "${CACHE_DIR}/app-mnt.XXXXXX")"
    APP_MNT_OWNED=1
    export APP_MNT

    DISK_MEMBERS="${WORK_DIR}/disk_members"
    export DISK_MEMBERS

    _stream_bundle_members

    # app-root is now written and mounted (the dispatcher does it inline for the
    # app-root member). UUID needs the live filesystem — same timing as the
    # non-streamed path.
    APP_ROOT_UUID=$(blkid -o value -s UUID "$APP_ROOT_DEV" 2>/dev/null || true)
    export APP_ROOT_UUID
    log "app-root UUID=${APP_ROOT_UUID:-unknown} PARTUUID=${APP_ROOT_PARTUUID:-unknown}"
    X86_DEPLOYED=1
    log "x86 disk image streamed into app region (${APP_IMG_KIND})"
}

# Read one field of a named layout entry, without needing a YAML/JSON tool in sh.
_layout_field() {  # <layout.json> <part-name> <field>
    python3 - "$1" "$2" "$3" <<'PY'
import json, sys
layout = json.load(open(sys.argv[1]))
for part in layout:
    if part.get("part") == sys.argv[2]:
        print(part.get(sys.argv[3], ""))
        break
PY
}

# Deploy the bundle's application image into the app slot (sets APP_MNT).
X86_DEPLOYED=0
X86_KIND=""
X86_SHA=""
deploy_app_image() {
    emit_tab "Init Partition"
    find_app_image
    X86_KIND="$APP_IMG_KIND"
    X86_SHA=$(comp_field sha256 "$APP_IMG_IDX")

    case "$APP_IMG_KIND" in
        x86-rootfs-tar)  deploy_rootfs_tar ;;
        x86-rootfs-ext4) deploy_rootfs_ext4 ;;
        custom-build)    deploy_custom_build ;;
        x86-disk-image)  deploy_disk_image ;;
        *)               die "unsupported application-image kind: ${APP_IMG_KIND}" ;;
    esac
    X86_DEPLOYED=1
    log "x86 application image deployed (${APP_IMG_KIND})"
}

# Write the bundle source to stdout without staging another copy.
stream_bundle_source() {
    if [ "$IS_URL" = "1" ]; then
        stream_url "$BUNDLE_SRC"
    else
        cat "$BUNDLE_SRC"
    fi
}

# Stream the whole bundle in a SINGLE pass and dispatch each
# member to its destination on the already-mounted slot: rootfs unpacked into
# the slot, Versal images landed under /lib/versal, overlays/hooks staged in
# WORK_DIR. Requires APP_MNT to be set (slot mounted). The multi-hundred-MB
# payload never touches the menu filesystem.
_stream_bundle_members() {
    VDIR="${APP_MNT}/lib/versal"
    # Created lazily by the dispatcher: on a merged-/usr rootfs the incoming tar
    # carries ./lib as a symlink to usr/lib, which tar cannot lay down if we have
    # already made lib/ a real directory here.
    mkdir -p "${WORK_DIR}/streamed" "${WORK_DIR}/tarcwd"

    # path<TAB>kind<TAB>idx lookup consulted by the per-member dispatcher.
    : > "${WORK_DIR}/stream_map"
    while IFS="$(printf '\t')" read -r _i _k; do
        printf '%s\t%s\t%s\n' "$(comp_field path "$_i")" "$_k" "$_i" \
            >> "${WORK_DIR}/stream_map"
    done < "${WORK_DIR}/f_kind"
    # For x86-disk-image: append one entry per layout partition (path<TAB>
    # x86-disk-image-member<TAB>name). Populated by deploy_disk_image_streamed
    # into DISK_MAP before calling us; no-op for other kinds.
    if [ -s "${WORK_DIR}/disk_map" ]; then
        cat "${WORK_DIR}/disk_map" >> "${WORK_DIR}/stream_map"
    fi

    _disp="${WORK_DIR}/dispatch.sh"
    cat > "$_disp" <<'DISP'
#!/bin/bash
# Invoked by `tar --to-command` once per member; the member's (already
# outer-gunzipped) content arrives on stdin, its name in $TAR_FILENAME.
# bash required: process substitution (tee >(sha256sum)) is used for the
# x86-disk-image-member branch.
set -e

_fail() { printf '%s\n' "$*" > "${WORK_DIR}/error"; exit 1; }

# Ensure app-root is mounted before writing into VDIR (which lives under it).
# Safe to call from any dispatcher branch; no-op if already mounted. Required
# when disk-image members arrive before Versal members (the normal order), and
# also provides a clear failure if the order is inverted and app-root was never
# written.
_ensure_app_root_mounted() { grep -q " ${APP_MNT} " /proc/mounts 2>/dev/null && return 0
    [ -n "${APP_ROOT_DEV:-}" ] && mount "$APP_ROOT_DEV" "$APP_MNT" && return 0
    _fail "app-root not mounted and APP_ROOT_DEV is unknown"; }

_line=$(awk -F'\t' -v p="$TAR_FILENAME" '$1==p {print; exit}' "$STREAM_MAP")
_kind=$(printf '%s' "$_line" | cut -f2)
_idx=$(printf '%s' "$_line" | cut -f3)
_path=$(printf '%s' "$_line" | cut -f1)
_suffix=$(printf '%s' "$_path" | sed -n 's/.*\.\(gz\|zst\)$/\1/p')
case "$_kind" in
    # A board without synced time can otherwise emit thousands of warnings.
    x86-rootfs-tar) exec tar --warning=no-timestamp -x -C "$APP_MNT" ;;

    x86-disk-image-member)
        # _idx is the partition name; look up its device/fs/sha256/discarded.
        _dmline=$(awk -F'\t' -v n="$_idx" '$1==n {print; exit}' "$DISK_MEMBERS")
        _dev=$(printf '%s' "$_dmline" | cut -f2)
        _fs=$(printf '%s' "$_dmline" | cut -f3)
        _sha=$(printf '%s' "$_dmline" | cut -f5)
        _discarded=$(printf '%s' "$_dmline" | cut -f6)
        [ -n "$_dev" ] || _fail "no device found for partition ${_idx} in DISK_MEMBERS"

        printf '[app-install] writing %s -> %s (%s)\n' "$TAR_FILENAME" "$_dev" "$_idx" >&2
        # tee to an anonymous pipe feeding sha256sum — GNU coreutils tee (present on
        # the menu OS where this installer runs) handles this at full pipe speed.
        # Named-fifo + python3 fast_tee was benchmarked at 113 MB/s vs 243 MB/s here.
        if tee >(sha256sum | awk '{print $1}' > "${WORK_DIR}/sha.${_idx}") \
                | LC_ALL=C dd of="$_dev" bs=4M conv=fsync 2>"${WORK_DIR}/dd.${_idx}"; then
            :
        else
            if grep -qi "no space left" "${WORK_DIR}/dd.${_idx}" 2>/dev/null; then
                _fail "${TAR_FILENAME} is larger than the ${_idx} partition; raise its size_mib"
            fi
            _fail "failed to write ${TAR_FILENAME} to ${_dev}"
        fi
        _got=$(cat "${WORK_DIR}/sha.${_idx}" 2>/dev/null || true)
        [ "$_got" = "$_sha" ] \
            || _fail "sha256 mismatch for ${TAR_FILENAME}: expected ${_sha}, got ${_got}"
        printf '[app-install]   sha256 OK (%s)\n' "$TAR_FILENAME" >&2

        # A partition created larger than the filesystem written into it has
        # unusable space at the end; ext4 can be stretched over it. Attempted for
        # every ext4-flagged partition, not just ones marked grow, because a fixed
        # size_mib may also exceed its member's filesystem size.
        if [ "$_fs" = "ext4" ]; then
            if [ "$_discarded" = "0" ]; then
                _written=$(LC_ALL=C awk '/bytes/ {print $1; exit}' \
                               "${WORK_DIR}/dd.${_idx}" 2>/dev/null || echo 0)
                _partsz=$(blockdev --getsize64 "$_dev" 2>/dev/null || echo 0)
                if [ "$_written" -gt 0 ] && [ "$_partsz" -gt "$_written" ]; then
                    _skip_mib=$(( (_written + 1048575) / 1048576 ))
                    printf '[app-install]   erasing stale data past image on %s\n' "$_dev" >&2
                    dd if=/dev/zero of="$_dev" bs=1M seek="$_skip_mib" \
                        conv=fsync status=none 2>/dev/null || true
                fi
            fi
            printf '[app-install]   growing %s to fill its partition\n' "$_dev" >&2
            e2fsck -fp "$_dev" >/dev/null 2>&1 || true
            resize2fs "$_dev" >/dev/null 2>&1 \
                || printf '[app-install] WARNING: resize2fs failed on %s\n' "$_dev" >&2
        fi

        # Mount app-root immediately after writing so subsequent Versal members
        # can write into VDIR (which lives under it). Mounts are kernel-wide so
        # this is visible to the parent process and all later dispatcher forks.
        if [ "$_idx" = "app-root" ]; then
            mkdir -p "$APP_MNT"
            mount "$_dev" "$APP_MNT" \
                || _fail "failed to mount app-root (${_dev}) at ${APP_MNT}"
            printf '[app-install] mounted %s at %s\n' "$_dev" "$APP_MNT" >&2
        fi
        ;;

    versal-ospi)    _ensure_app_root_mounted
                    mkdir -p "$VDIR"; rm -f "$VDIR/boot.bin.gz" "$VDIR/boot.bin.zst"
                    exec cat > "$VDIR/boot.bin.${_suffix}" ;;
    versal-wic)     _ensure_app_root_mounted
                    mkdir -p "$VDIR"; rm -f "$VDIR/wic.ufs.gz" "$VDIR/wic.ufs.zst"
                    exec cat > "$VDIR/wic.ufs.${_suffix}" ;;
    overlay)        exec cat > "$STREAM_TMP/${_idx}.overlay" ;;
    hook-script)    exec cat > "$STREAM_TMP/${_idx}.hook" ;;
    *)              exec cat > /dev/null ;;
esac
DISP
    chmod 0755 "$_disp"

    STREAM_MAP="${WORK_DIR}/stream_map"
    STREAM_TMP="${WORK_DIR}/streamed"
    export APP_MNT VDIR STREAM_MAP STREAM_TMP WORK_DIR
    # DISK_MEMBERS already exported by deploy_disk_image_streamed when used;
    # harmless (empty) for other kinds since the x86-disk-image-member branch
    # is never reached if the stream_map contains no such entries.
    DISK_MEMBERS="${DISK_MEMBERS:-/dev/null}"
    export DISK_MEMBERS

    log "streaming ${BUNDLE_SRC} into the slot (single pass, no cache)"
    if stream_bundle_source \
        | gzip -dc \
        | tar --warning=no-timestamp -x -C "${WORK_DIR}/tarcwd" --to-command="$_disp"; then
        :
    else
        _errmsg=$(cat "${WORK_DIR}/error" 2>/dev/null || true)
        if [ -n "$_errmsg" ]; then
            die "streaming install failed: ${_errmsg}"
        else
            die "streaming install failed (network or archive error)"
        fi
    fi
}

# Streamed deploy (x86-rootfs-tar): format the slot, then read the whole bundle
# in a SINGLE pass from its URL or local file and dispatch each member to its
# destination. No additional bundle copy is staged in app-cache.
deploy_app_image_streamed() {
    emit_tab "Init Partition"
    find_app_image
    X86_KIND="$APP_IMG_KIND"
    X86_SHA=$(comp_field sha256 "$APP_IMG_IDX")
    [ "$APP_IMG_KIND" = "x86-rootfs-tar" ] \
        || die "streaming deploy only supports x86-rootfs-tar (got ${APP_IMG_KIND})"

    format_app_slot
    mount_fresh_slot
    _stream_bundle_members
    # Confirm the rootfs member actually landed something on the slot (tar -x
    # already fails hard on a real extraction error; this catches a silently
    # empty archive). verify_rootfs_kernel below still warns on a missing kernel.
    if [ -z "$(ls -A "$APP_MNT" 2>/dev/null | grep -v '^lost+found$' || true)" ]; then
        die "streamed rootfs is empty — extract produced no files"
    fi

    X86_DEPLOYED=1
    verify_rootfs_kernel
    patch_rootfs_fstab
    log "x86 application image streamed into slot (${APP_IMG_KIND})"
}

# Streamed deploy (custom-build): the bundle's only large members are the
# Versal images; the x86 side is a tiny build script that self-installs over the
# network. So we make TWO streaming passes and never copy the bundle:
#   1) stream just x86/build.sh (for URLs, tar quitting after the first match
#      stops wget before the large payload), verify it,
#      and run it (it formats + mounts + populates the slot, unmounts on exit);
#   2) re-mount the slot and stream the remaining members (Versal/overlay/hook)
#      straight onto it. The build-script member falls to the dispatcher default
#      and is discarded on the second pass.
deploy_custom_build_streamed() {
    emit_tab "Init Partition"
    find_app_image
    X86_KIND="$APP_IMG_KIND"
    X86_SHA=$(comp_field sha256 "$APP_IMG_IDX")
    _mp=$(comp_field path "$APP_IMG_IDX")

    # Pass 1: stream just the build script from the selected source.
    _build="${WORK_DIR}/build.sh"
    log "streaming build script ${_mp} from ${BUNDLE_SRC}"
    stream_bundle_source \
        | gzip -dc 2>/dev/null \
        | tar -xO --occurrence=1 "$_mp" > "$_build" 2>/dev/null || true
    [ -s "$_build" ] || die "could not stream build script ${_mp} from ${BUNDLE_SRC}"
    _got=$(sha256sum "$_build" | awk '{print $1}')
    [ "$_got" = "$X86_SHA" ] \
        || die "sha256 mismatch for ${_mp}: expected ${X86_SHA}, got ${_got}"
    log "  sha256 OK (${_mp})"
    chmod 0755 "$_build"

    # Run the build script against the slot (it formats + mounts + populates the
    # slot itself, then unmounts on exit), then re-mount what it produced.
    release_app_slot
    _mkfs_opts=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" slot.mkfs_options)
    _label="${SLOT_DESC_LABEL:-$SLOT_LABEL}"
    log "running custom-build script ${_mp} against ${SLOT_DEV}"
    APP_SLOT_DEV="$SLOT_DEV" \
    APP_SLOT_LABEL="$_label" \
    APP_SLOT_MKFS_OPTIONS="$_mkfs_opts" \
        sh "$_build" || die "custom-build script ${_mp} failed"
    mount_fresh_slot
    verify_rootfs_kernel

    # Pass 2: stream the remaining members (Versal images, overlays, hooks)
    # straight onto the freshly-built slot. Skip if there are none.
    if has_versal_components || has_init_components; then
        _stream_bundle_members
    fi

    X86_DEPLOYED=1
    log "x86 application image deployed (custom-build, streamed)"
}

# Apply overlay + hook-script components for a given stage onto the mounted slot.
apply_init_components() {  # <stage>
    _stage="$1"
    _opened=0
    while IFS="$(printf '\t')" read -r idx kind; do
        case "$kind" in overlay|hook-script) ;; *) continue ;; esac
        _cstage=$(comp_field stage "$idx"); [ -n "$_cstage" ] || _cstage="post-deploy"
        [ "$_cstage" = "$_stage" ] || continue
        if [ "$_opened" = "0" ]; then
            emit_tab "Init (${_stage})"
            _opened=1
        fi
        _mp=$(comp_field path "$idx")
        case "$kind" in
            overlay)
                _tgt=$(comp_field target "$idx"); [ -n "$_tgt" ] || _tgt="/"
                _dest="${APP_MNT}/${_tgt#/}"
                mkdir -p "$_dest"
                log "applying overlay ${_mp} -> ${_tgt}"
                if [ "${STREAMING:-0}" = "1" ]; then
                    _ov="${WORK_DIR}/streamed/${idx}.overlay"
                    [ -f "$_ov" ] || die "overlay ${_mp} was not streamed from bundle"
                    tar --warning=no-timestamp -x -C "$_dest" < "$_ov" \
                        || die "failed to apply overlay ${_mp}"
                else
                    gzip -dc "$BUNDLE" | tar -xO "$_mp" | tar --warning=no-timestamp -x -C "$_dest" \
                        || die "failed to apply overlay ${_mp}"
                fi
                ;;
            hook-script)
                _hook="${WORK_DIR}/hook.${idx}"
                if [ "${STREAMING:-0}" = "1" ]; then
                    _hk="${WORK_DIR}/streamed/${idx}.hook"
                    [ -f "$_hk" ] || die "hook-script ${_mp} was not streamed from bundle"
                    cp "$_hk" "$_hook"
                else
                    gzip -dc "$BUNDLE" | tar -xO "$_mp" > "$_hook" \
                        || die "could not extract hook-script ${_mp}"
                fi
                chmod 0755 "$_hook"
                log "running hook-script ${_mp} (stage ${_stage})"
                APP_MNT="$APP_MNT" APP_SLOT_DEV="$SLOT_DEV" BUNDLE="${BUNDLE:-}" \
                    APP_ROOT_DEV="${APP_ROOT_DEV:-}" APP_BOOT_DEV="${APP_BOOT_DEV:-}" \
                    APP_ROOT_UUID="${APP_ROOT_UUID:-}" APP_ROOT_PARTUUID="${APP_ROOT_PARTUUID:-}" \
                    sh "$_hook" || die "hook-script ${_mp} failed (stage ${_stage})"
                ;;
        esac
    done < "${WORK_DIR}/f_kind"
}

# Tell the ESP grub.cfg how to start this app. Always written, even for
# rootfs-style apps, so a previous disk-image install cannot leave stale
# chainload settings behind. Sourced by the template's Application A entry.
# Take one menuentry out of the app's own GRUB entries file and normalise it the
# way the app's vendor installer does. Read from the *deployed* partition, after
# hooks have run, so anything a hook rewrote is what gets booted.
#
# The entries file is already GRUB syntax, so nothing is translated: only the
# three substitutions the vendor's own installer performs are applied, plus a
# `set root` so the entry's bare paths resolve against the app's boot partition
# rather than the menu's.
adopt_boot_entry() {  # <mounted boot dir> <config path> <entry name> <root partuuid> <grub device>
    python3 - "$@" <<'PY'
import os, re, sys

root, rel, want, partuuid, device = sys.argv[1:6]
# udev's /dev/disk/by-partuuid/ symlinks are lower case, and this image's initrd
# builds that path from the argument rather than resolving it, so an upper-case
# PARTUUID leaves root unmountable and the boot hangs with no message.
partuuid = partuuid.lower()
# `rel` comes from the bundle's descriptor, so keep it inside the app's own
# boot partition; realpath also collapses any symlink pointing back out.
real_root = os.path.realpath(root)
path = os.path.realpath(os.path.join(root, rel.lstrip("/")))
if path != real_root and not path.startswith(real_root + os.sep):
    sys.stderr.write(f"{rel} escapes the boot partition\n")
    sys.exit(1)
try:
    text = open(path, "r", errors="replace").read()
except OSError as exc:
    sys.stderr.write(f"cannot read {rel}: {exc}\n")
    sys.exit(1)

# menuentry 'name' { ... } — brace may or may not be preceded by whitespace.
pattern = re.compile(
    r"^\s*menuentry\s+(['\"])(?P<name>.*?)\1\s*\{(?P<body>.*?)^\s*\}",
    re.S | re.M,
)
bodies = {m.group("name"): m.group("body") for m in pattern.finditer(text)}
if want not in bodies:
    sys.stderr.write(
        f"entry {want!r} not in {rel}; available: {', '.join(sorted(bodies)) or 'none'}\n")
    sys.exit(1)

lines = []
for line in bodies[want].splitlines():
    line = line.strip()
    if not line:
        continue
    # Any `set root` in the body is stale: the installer decided where the app
    # went, and ours is prepended below. Left in place it would sit after ours
    # and win. Dropping it also makes re-adopting an already-adopted entry a
    # no-op, which is what cloning needs.
    if re.match(r"set\s+root=", line):
        continue
    line = re.sub(r"\s+LABEL=\S*", " ", line)
    # The optional tail is our own signature: consuming it means re-adoption
    # substitutes the PARTUUID rather than appending a second copy.
    line = re.sub(r"\s+root=\S*(\s+rw\s+rootwait\s+quiet)?",
                  f" root=PARTUUID={partuuid} rw rootwait quiet", line)
    lines.append(" ".join(line.split()))
if not lines:
    sys.stderr.write(f"entry {want!r} is empty\n")
    sys.exit(1)

# Anything that could break out of the generated GRUB script, or introduce a
# substitution, is refused rather than escaped. The `set root=(...)` line below
# is ours and is added after this check.
joined = "\n".join(lines)
if re.search(r'[`"\'${}\\\r]', joined):
    sys.stderr.write(f"unsafe characters in entry {want!r}\n")
    sys.exit(1)

print(f"set root=({device})")
print(joined)
PY
}

# Build an app-entry.cfg from a declared boot.cmdline.
#
# The counterpart to adopt_boot_entry(): that one lifts an entry the app already
# has, this one writes the entry an app described. Both end at the same artefact,
# so the boot menu has a single thing to source. Values were already checked by
# descriptor_tool for the characters GRUB cares about; the quoting here is the
# device's own, not the app's.
render_boot_entry() {
    _rk=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" boot.cmdline.kernel 2>/dev/null || true)
    [ -n "$_rk" ] || return 1
    _rka=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" boot.cmdline.kernel_args 2>/dev/null || true)
    _rh=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" boot.cmdline.hypervisor 2>/dev/null || true)
    _rha=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" boot.cmdline.hypervisor_args 2>/dev/null || true)
    _rr=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" boot.cmdline.ramdisk 2>/dev/null || true)

    # root= is ours: the app cannot know which partition it landed on. Same
    # wording adopt_boot_entry() substitutes, so both paths boot alike.
    _rroot="root=PARTUUID=$(echo "$_cmdline_uuid" | tr 'A-Z' 'a-z') rw rootwait quiet"

    # Collapse runs of spaces and drop trailing ones, so an absent hypervisor_args
    # or kernel_args does not leave gaps. adopt_boot_entry() normalises the same
    # way, which keeps the two kinds of generated entry looking alike.
    _emit() { echo "$*" | tr -s ' ' | sed 's/[[:space:]]*$//'; }

    _emit "set root=(${_cmdline_part})"
    if [ -n "$_rh" ]; then
        # Xen: hypervisor first, kernel and ramdisk as multiboot2 modules.
        _emit "multiboot2 ${_rh} placeholder ${_rha}"
        _emit "module2 ${_rk} ${_rka} ${_rroot}"
        [ -n "$_rr" ] && _emit "module2 ${_rr}"
    else
        _emit "linux ${_rk} ${_rka} ${_rroot}"
        [ -n "$_rr" ] && _emit "initrd ${_rr}"
    fi
    return 0
}

write_app_boot_cfg() {  # <esp dir>
    _dir="$1"
    _cfg="${_dir}/app-boot.cfg"
    _chain=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" boot.chainload 2>/dev/null || true)
    _entries=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" boot.entries_cfg 2>/dev/null || true)
    _entry=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" boot.entry 2>/dev/null || true)
    _cmdline_kernel=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" boot.cmdline.kernel 2>/dev/null || true)

    if [ "$X86_KIND" = "x86-disk-image" ] \
       && { [ -n "$_chain" ] || [ -n "$_entries" ] || [ -n "$_cmdline_kernel" ]; }; then
        # The app's boot partition is the first index of the app region; the
        # template falls back to a file search if the table ever moves. Take the
        # trailing digits of the device node rather than a sed pattern, which
        # busybox sed would not parse the same way.
        _bootdev=$(awk -F'\t' '$1=="app-boot" {print $2; exit}' \
                       "${WORK_DIR}/region_devices" 2>/dev/null)
        _idx="${_bootdev##*[!0-9]}"
        [ -n "$_idx" ] || _idx=20
    fi

    if [ -n "$_entries" ]; then
        [ -n "$_entry" ] || _entry=boot
        if [ "$X86_KIND" = "x86-disk-image" ]; then
            # The entries file is on the app's own boot partition, which is not
            # mounted, so read it in place.
            _bmnt="${WORK_DIR}/bootmnt"
            mkdir -p "$_bmnt"
            mount -o ro "$_bootdev" "$_bmnt" 2>/dev/null \
                || die "cannot mount ${_bootdev} to read ${_entries}"
            _entry_root="$_bmnt"
            _entry_part="\$boot_disk,gpt${_idx}"
            _entry_uuid="${APP_ROOT_PARTUUID:-}"
        else
            # A rootfs app keeps its entries file inside the slot, which is
            # already mounted, and boots from the slot itself. Paths in the entry
            # are therefore slot-relative: /boot/xen.gz, not /xen.
            _entry_root="$APP_MNT"
            _entry_part="\$boot_disk,gpt4"
            _entry_uuid=$(blkid -s PARTUUID -o value "$SLOT_DEV" 2>/dev/null || true)
        fi
        _adopted=$(adopt_boot_entry "$_entry_root" "$_entries" "$_entry" \
                                    "$_entry_uuid" "$_entry_part") || {
            [ "$X86_KIND" = "x86-disk-image" ] && umount "$_bmnt" 2>/dev/null
            die "could not adopt entry '${_entry}' from ${_entries}"
        }
        [ "$X86_KIND" = "x86-disk-image" ] && umount "$_bmnt" 2>/dev/null

        printf '%s\n' \
            "# generated by install_app_bundle.sh - do not edit" \
            "$_adopted" > "${_dir}/app-entry.cfg"
        {
            echo "# generated by install_app_bundle.sh - do not edit"
            echo "set app_mode=entries"
            echo "set app_part=${_entry_part}"
        } > "$_cfg"
        log "app boot: adopted entry '${_entry}' from ${_entries} on ${_entry_part#*,}"
    elif [ -n "$_cmdline_kernel" ]; then
        if [ "$X86_KIND" = "x86-disk-image" ]; then
            _cmdline_part="\$boot_disk,gpt${_idx}"
            _cmdline_uuid="${APP_ROOT_PARTUUID:-}"
        else
            _cmdline_part="\$boot_disk,gpt4"
            _cmdline_uuid=$(blkid -s PARTUUID -o value "$SLOT_DEV" 2>/dev/null || true)
        fi
        _adopted=$(render_boot_entry) \
            || die "could not render a boot entry from boot.cmdline"
        printf '%s\n' \
            "# generated by install_app_bundle.sh - do not edit" \
            "$_adopted" > "${_dir}/app-entry.cfg"
        {
            echo "# generated by install_app_bundle.sh - do not edit"
            echo "set app_mode=entries"
            echo "set app_part=${_cmdline_part}"
        } > "$_cfg"
        log "app boot: rendered entry from boot.cmdline on ${_cmdline_part#*,}"
    elif [ "$X86_KIND" = "x86-disk-image" ] && [ -n "$_chain" ]; then
        {
            echo "# generated by install_app_bundle.sh - do not edit"
            echo "set app_mode=chainload"
            echo "set app_part=\$boot_disk,gpt${_idx}"
            echo "set app_chainload=${_chain}"
        } > "$_cfg"
        log "app boot: chainload ${_chain} from gpt${_idx}"
    else
        {
            echo "# generated by install_app_bundle.sh - do not edit"
            echo "set app_mode=direct"
            echo "set app_part=\$boot_disk,gpt4"
            echo "set app_chainload="
        } > "$_cfg"
        log "app boot: direct (app kernel from gpt4)"
        log "WARNING: ${APP_ID:-this app} declares no boot contract, so it will"
        log "         be booted with the menu OS's kernel arguments. Add"
        log "         boot.entries_cfg or boot.cmdline to its recipe."
    fi
    sync 2>/dev/null || true
}

# True if a grub.cfg is one of ours. The ESP is located by trying a few paths,
# which is a guess about where things are mounted, not proof of ownership: an
# app's own boot partition can be mounted at /boot, and rewriting a stranger's
# bootloader config would be a bad way to find that out. Every rave2-generated
# config carries at least one of these markers; a foreign one carries none.
esp_cfg_is_ours() {  # <path>
    grep -qE "ESP_GRUB_TEMPLATE_REVISION|--id app-a|RAVE2 Buddy|rauc\.slot=" \
        "$1" 2>/dev/null
}

# Which PARTUUID the regenerated grub.cfg should use for the app entry.
#
# The menu PARTUUIDs can safely be carried over from the config we are replacing,
# but app-a cannot: the partition may have been destroyed and recreated by the
# install that just ran. Both deploy paths know better than the old file does, so
# prefer what they report and fall back to the scraped value only when neither
# has anything to say.
#
#   disk-image  -> APP_ROOT_PARTUUID, exported by deploy_disk_image()
#   rootfs      -> read off SLOT_DEV; APP_ROOT_PARTUUID is never set here, and
#                  the scraped value would be the *previous* app's app-root if
#                  that app was a disk-image one
resolve_app_partuuid() {  # <partuuid scraped from the old config>
    _scraped="$1"
    if [ -n "${APP_ROOT_PARTUUID:-}" ]; then
        echo "$APP_ROOT_PARTUUID" | tr 'A-Z' 'a-z'
        return 0
    fi
    if [ -n "${SLOT_DEV:-}" ]; then
        _slot_uuid=$(blkid -s PARTUUID -o value "$SLOT_DEV" 2>/dev/null || true)
        if [ -n "$_slot_uuid" ]; then
            echo "$_slot_uuid" | tr 'A-Z' 'a-z'
            return 0
        fi
    fi
    echo "$_scraped" | tr 'A-Z' 'a-z'
}

# Register the deployed app with the ESP boot manager. Re-renders the live ESP
# grub.cfg from the menu OS template (preserving the rave2 PARTUUIDs) so the
# slot-relative 'Application A' entry — which boots the app's OWN kernel from the
# app slot — is present even on boards whose grub.cfg was baked from an older
# template at NVMe-install time. Then sets the grubenv flags that mark the app
# installed and, when requested, make it the default boot target.
configure_boot() {
    _make_default=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" boot.make_default)
    _esp_cfg=""
    for c in /boot/efi/EFI/BOOT/grub.cfg /boot/efi/EFI/rave2/grub.cfg \
             /boot/EFI/BOOT/grub.cfg; do
        [ -f "$c" ] || continue
        if esp_cfg_is_ours "$c"; then
            _esp_cfg="$c"
            break
        fi
        log "ignoring ${c}: not a rave2 boot config"
    done
    if [ -z "$_esp_cfg" ]; then
        log "WARNING: ESP grub.cfg not found; skipping boot registration"
        return 0
    fi

    # Re-render grub.cfg from the template, preserving the existing rave2
    # PARTUUIDs (menu-a, menu-b, app-a — in first-seen order), so the app entry
    # picks up the slot-relative app-kernel layout. Same technique as
    # install_ubuntu.sh::configure_grub() and the RAUC post-install hook.
    if [ -f "$GRUB_TEMPLATE" ]; then
        _uuids=$(grep -o 'PARTUUID=[0-9a-fA-F-]*' "$_esp_cfg" \
                   | sed 's/PARTUUID=//' | awk '!seen[$0]++')
        _menu_a=$(echo "$_uuids" | sed -n '1p')
        _menu_b=$(echo "$_uuids" | sed -n '2p')
        _app_a=$(resolve_app_partuuid "$(echo "$_uuids" | sed -n '3p')")
        if [ -n "$_menu_a" ] && [ -n "$_menu_b" ]; then
            log "re-rendering ${_esp_cfg} from template (slot-relative app entry)"
            _tmpcfg="${_esp_cfg}.new.$$"
            if sed -e "s/@@MENU_A_PARTUUID@@/$_menu_a/g" \
                   -e "s/@@MENU_B_PARTUUID@@/$_menu_b/g" \
                   -e "s/@@APP_A_PARTUUID@@/${_app_a:-00000000-0000-0000-0000-000000000000}/g" \
                   "$GRUB_TEMPLATE" > "$_tmpcfg"; then
                mv "$_tmpcfg" "$_esp_cfg"
            else
                rm -f "$_tmpcfg" 2>/dev/null || true
                log "WARNING: grub.cfg re-render failed; leaving existing config"
            fi
        else
            log "WARNING: could not read rave2 PARTUUIDs; leaving grub.cfg as-is"
        fi
    else
        log "WARNING: GRUB template ${GRUB_TEMPLATE} missing; leaving grub.cfg as-is"
    fi

    write_app_boot_cfg "$(dirname "$_esp_cfg")"

    command -v grub-editenv >/dev/null 2>&1 || {
        log "WARNING: grub-editenv missing; skipping grubenv registration"
        return 0
    }
    _grubenv="${_esp_cfg%/grub.cfg}/grubenv"
    [ -f "$_grubenv" ] || grub-editenv "$_grubenv" create
    grub-editenv "$_grubenv" set app_installed=1
    log "marked app_installed=1 in ${_grubenv}"
    # Drive the GRUB 'Application A' entry title (and the menu default label)
    # from the installed app's name. The template falls back to a generic label
    # when this is unset.
    if [ -n "${APP_NAME:-}" ]; then
        grub-editenv "$_grubenv" set app_title="$APP_NAME"
        log "set grub app_title='${APP_NAME}'"
    fi
    if [ "$_make_default" = "true" ]; then
        grub-editenv "$_grubenv" set boot_default=app
        log "set app slot as the default boot entry"
    fi
}

# --- Argument parsing --------------------------------------------------------
BUNDLE_SRC=""
SHA256_EXPECT=""
SLOT="a"
DRY_RUN=0
RECOVERY=0
NO_CACHE=0
KEEP_BUNDLE=0
JUMBO_SIZE_GIB="${RAVE_JUMBO_SIZE_GIB:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --sha256)   SHA256_EXPECT="${2:-}"; shift 2 ;;
        --slot)     SLOT="${2:-}"; shift 2 ;;
        --jumbo-size-gib) JUMBO_SIZE_GIB="${2:-}"; shift 2 ;;
        --dry-run)  DRY_RUN=1; shift ;;
        --recovery) RECOVERY=1; shift ;;
        --no-cache) NO_CACHE=1; shift ;;
        --keep-bundle) KEEP_BUNDLE=1; shift ;;
        -h|--help)
            sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        --*)        die "unknown option: $1" ;;
        *)
            [ -z "$BUNDLE_SRC" ] || die "unexpected extra argument: $1"
            BUNDLE_SRC="$1"; shift ;;
    esac
done

# Only slot "a" is supported today (open-questions D1). Reject anything else
# rather than silently writing the reserved partition.
[ "$SLOT" = "a" ] || die "unsupported --slot '$SLOT' (only 'a' is available)"

[ -x "$(command -v python3)" ] || die "python3 not found"

# A real install needs a bundle source and the descriptor tool; recovery reads
# neither (it reprograms Versal from images already on the slot), so only demand
# them when NOT recovering.
if [ "$RECOVERY" != "1" ]; then
    [ -n "$BUNDLE_SRC" ] || die "no bundle path or URL given"
    [ -f "$DESCRIPTOR_TOOL" ] || die "descriptor_tool.py not found at $DESCRIPTOR_TOOL"
elif [ -n "$BUNDLE_SRC" ]; then
    die "--recovery does not take a bundle (it reprograms Versal from the installed images)"
fi

mkdir -p "$CACHE_DIR"

# --- Cleanup trap ------------------------------------------------------------
APP_MNT=""
APP_MNT_OWNED=0
WORK_DIR="$(mktemp -d "${CACHE_DIR}/work.XXXXXX")"
cleanup() {
    if [ -n "${APP_MNT:-}" ] && [ "${APP_MNT_OWNED:-0}" = "1" ]; then
        sync 2>/dev/null || true
        umount "$APP_MNT" 2>/dev/null || umount -l "$APP_MNT" 2>/dev/null || true
        rmdir "$APP_MNT" 2>/dev/null || true
    fi
    rm -rf "$WORK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

# Recovery: reprogram the Versal from the images already on the app slot, then
# exit. None of the install stages below (descriptor resolution, download, x86
# deploy, boot wiring) run.
if [ "$RECOVERY" = "1" ]; then
    run_recovery
fi

# =============================================================================
# Stage: Resolve the descriptor.
#   For a URL we fetch ONLY the leading appimage.yaml member: `tar` quits after
#   the first match, so `wget` stops early and the big payload never lands on the
#   menu filesystem. Local bundles read the descriptor from the file in place.
# =============================================================================

free_space_precheck() {
    # Best-effort: if we can learn the remote size, ensure CACHE_DIR has room.
    # Only used for the cached fallback path (non-streaming).
    src="$1"
    case "$src" in
        http://*|https://*) ;;
        *) return 0 ;;
    esac
    content_length=$(wget --spider -S -q "$src" 2>&1 \
        | grep -i content-length | awk '{print $2}' | tr -d '\r' | tail -n1)
    [ -n "$content_length" ] && [ "$content_length" -gt 0 ] 2>/dev/null || {
        log "could not determine remote size, skipping space pre-check"
        return 0
    }
    free_mb=$(df -Pm "$CACHE_DIR" | awk 'NR==2 {print $4}')
    needed_mb=$(( (content_length * 11 / 10) / 1048576 ))
    log "space check: need ${needed_mb} MiB, ${free_mb} MiB free on ${CACHE_DIR}"
    [ "$free_mb" -ge "$needed_mb" ] \
        || die "not enough space on ${CACHE_DIR}: need ${needed_mb} MiB, have ${free_mb} MiB"
}

DESCRIPTOR="${WORK_DIR}/appimage.yaml"
STREAMING=0
BUNDLE=""
BUNDLE_DOWNLOADED=0

case "$BUNDLE_SRC" in
    http://*|https://*)
        IS_URL=1
        BUNDLE_SRC=$(normalize_bundle_url "$BUNDLE_SRC")
        log "fetching descriptor from ${BUNDLE_SRC} (header only)"
        ;;
    *)
        IS_URL=0
        [ -f "$BUNDLE_SRC" ] || die "bundle file not found: ${BUNDLE_SRC}"
        BUNDLE="$BUNDLE_SRC"
        log "using local bundle ${BUNDLE}"
        ;;
esac
read_bundle_descriptor

# =============================================================================
# Stage: Verify — validate the envelope, resolve the slot, cache fields, and
# pick a delivery strategy.
# =============================================================================

# Menu version for the optional compatibility gate.
MENU_VERSION=""
[ -f /etc/rauc/version ] && MENU_VERSION="$(cat /etc/rauc/version 2>/dev/null || true)"

log "validating envelope"
python3 "$DESCRIPTOR_TOOL" validate "$DESCRIPTOR" --menu-version "$MENU_VERSION" \
    || die "bundle descriptor failed validation"

# Resolve slot details from the single source of truth.
SLOT_DEV=$(python3 "$APP_SLOTS" device "$SLOT") || die "could not resolve slot device"
SLOT_LABEL=$(python3 "$APP_SLOTS" label "$SLOT") || die "could not resolve slot label"

# A jumbo bundle declares it cannot fit the 50 GiB standard slot. It installs
# into the disk tail after rave-data instead, reusing the same partition
# indices, so claiming it clears any standard app and vice versa.
# --jumbo-size-gib (or RAVE_JUMBO_SIZE_GIB) takes more or less than the
# bundle's desired_storage, anywhere from its minimum up to the whole tail;
# whatever is left over is what a later Ubuntu install gets.
APP_TYPE=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" type 2>/dev/null || echo standard)
[ -n "$APP_TYPE" ] || APP_TYPE=standard
REGION_ARGS=""
if [ "$APP_TYPE" = "jumbo" ]; then
    MIN_STORAGE_GIB=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" minimum_storage 2>/dev/null || echo 0)
    DESIRED_STORAGE_GIB=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" desired_storage 2>/dev/null || echo 0)
    REGION_ARGS="--region jumbo --min-gib ${MIN_STORAGE_GIB:-0} --desired-gib ${DESIRED_STORAGE_GIB:-0}"
    if [ -n "${JUMBO_SIZE_GIB:-}" ]; then
        case "$JUMBO_SIZE_GIB" in
            ''|*[!0-9]*) die "--jumbo-size-gib must be a whole number of GiB, got '${JUMBO_SIZE_GIB}'" ;;
        esac
        REGION_ARGS="${REGION_ARGS} --size-gib ${JUMBO_SIZE_GIB}"
        log "jumbo application: needs >= ${MIN_STORAGE_GIB} GiB, wants ${DESIRED_STORAGE_GIB} GiB, taking ${JUMBO_SIZE_GIB} GiB"
    else
        log "jumbo application: needs >= ${MIN_STORAGE_GIB} GiB, wants ${DESIRED_STORAGE_GIB} GiB"
    fi
fi

# Cache per-component fields as TSV (index<TAB>value) for the plan + installer.
python3 "$DESCRIPTOR_TOOL" components "$DESCRIPTOR" --field kind   > "${WORK_DIR}/f_kind"
python3 "$DESCRIPTOR_TOOL" components "$DESCRIPTOR" --field path   > "${WORK_DIR}/f_path"
python3 "$DESCRIPTOR_TOOL" components "$DESCRIPTOR" --field sha256 > "${WORK_DIR}/f_sha256"
python3 "$DESCRIPTOR_TOOL" components "$DESCRIPTOR" --field stage  > "${WORK_DIR}/f_stage"
python3 "$DESCRIPTOR_TOOL" components "$DESCRIPTOR" --field target > "${WORK_DIR}/f_target"

# Identify the application-image component up front so we can choose how to
# deliver it.
find_app_image

# Delivery strategy:
#   - x86-rootfs-tar -> stream URL or local file straight into the slot without
#     staging another copy in app-cache.
#   - custom-build   -> stream too: read the tiny build script, run it, then
#     stream the Versal payload straight onto the slot without another copy.
#   - URL + x86-rootfs-ext4 -> must land the full bundle first (legacy).
#   - local x86-rootfs-ext4 -> read members from the file in place.
if [ "$APP_IMG_KIND" = "x86-rootfs-tar" ] || [ "$APP_IMG_KIND" = "custom-build" ] \
        || [ "$APP_IMG_KIND" = "x86-disk-image" ]; then
    STREAMING=1
    log "delivery: streaming selected source into the app slot (no app-cache copy)"
elif [ "$IS_URL" = "1" ]; then
    log "delivery: ${APP_IMG_KIND} requires a cached bundle; downloading"
    free_space_precheck "$BUNDLE_SRC"
    fname=$(echo "$BUNDLE_SRC" | sed 's|.*/||')
    [ -n "$fname" ] || fname="bundle.rave2app"
    BUNDLE="${CACHE_DIR}/${fname}"
    BUNDLE_DOWNLOADED=1
    log "downloading ${BUNDLE_SRC} -> ${BUNDLE}"
    stream_url "$BUNDLE_SRC" > "$BUNDLE" || die "failed to download ${BUNDLE_SRC}"
fi

# Cross-check that every referenced member exists. Needs the full archive
# listing, so it only runs when we have the whole bundle on disk.
if [ "$STREAMING" = "0" ]; then
    MEMBER_LIST="${WORK_DIR}/members.txt"
    gzip -dc "$BUNDLE" | tar -t > "${WORK_DIR}/tar_listing.txt" 2>/dev/null \
        || die "could not list bundle tar contents"
    python3 "$DESCRIPTOR_TOOL" members "$DESCRIPTOR" > "$MEMBER_LIST"
    while IFS= read -r member; do
        [ -n "$member" ] || continue
        grep -Fxq "$member" "${WORK_DIR}/tar_listing.txt" \
            || die "descriptor references member not present in bundle: ${member}"
    done < "$MEMBER_LIST"
    log "all referenced members present in bundle"
fi

# =============================================================================
# Install plan (printed for both dry-run and real installs).
# =============================================================================
APP_ID=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" id)
APP_NAME=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" name)
APP_VERSION=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" version)
SLOT_FS=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" slot.filesystem)
SLOT_DESC_LABEL=$(python3 "$DESCRIPTOR_TOOL" get "$DESCRIPTOR" slot.label)

echo
log "=== Install plan ==="
log "  app:     ${APP_NAME} (${APP_ID}) version ${APP_VERSION}"
if [ "$APP_IMG_KIND" = "x86-disk-image" ]; then
    # The legacy slot is not used: this kind repartitions the region instead.
    log "  target:  the app region, repartitioned from the bundle layout"
elif [ "$APP_TYPE" = "jumbo" ]; then
    log "  target:  a jumbo slot carved from the disk tail (p4 is not used)"
    log "  slot fs: ${SLOT_FS:-ext4}, descriptor label ${SLOT_DESC_LABEL:-app-a}"
else
    log "  slot:    ${SLOT} -> ${SLOT_DEV} (label ${SLOT_LABEL})"
    log "  slot fs: ${SLOT_FS:-ext4}, descriptor label ${SLOT_DESC_LABEL:-app-a}"
fi
log "  components (in descriptor order):"

# Print "kind<TAB>path" per component and the action that would run.
while IFS="$(printf '\t')" read -r idx kind; do
    path=$(comp_field path "$idx")
    stage=$(comp_field stage "$idx")
    case "$kind" in
        x86-rootfs-tar)  if [ "${STREAMING:-0}" = "1" ]; then
                             action="format ${SLOT_DEV} + stream rootfs tar straight into slot (no cache)"
                         else
                             action="format ${SLOT_DEV} + stream-extract rootfs tar"
                         fi ;;
        x86-rootfs-ext4) action="write ext4 image to ${SLOT_DEV} via x86-replace-application" ;;
        x86-disk-image)  action="repartition the app region and write each layout member to its partition" ;;
        custom-build)    action="run x86/build.sh to self-install onto ${SLOT_DEV}" ;;
        versal-ospi)     action="persist ${path##*.}-compressed to ${SLOT_LABEL}:/lib/versal/boot.bin.${path##*.} + program OSPI (Phase 2)" ;;
        versal-wic)      action="persist ${path##*.}-compressed to ${SLOT_LABEL}:/lib/versal/wic.ufs.${path##*.} + program UFS (Phase 2)" ;;
        overlay)         action="extract onto slot (stage ${stage:-post-deploy})" ;;
        hook-script)     action="run against slot (stage ${stage:-post-deploy})" ;;
        *)               action="unknown kind — would skip unless required" ;;
    esac
    log "    - ${kind}  [${path}]  -> ${action}"
done < "${WORK_DIR}/f_kind"

echo
if [ "$DRY_RUN" = "1" ]; then
    log "dry-run complete; no changes made."
    emit_tab "Done"
    log "dry-run finished for ${APP_ID} ${APP_VERSION}."
    exit 0
fi

# =============================================================================
# Real install (Phase 1 + 2 + 3). Ordering matters: the x86 deploy reformats
# the slot, so it must run first; the Versal images are then persisted onto the
# freshly-deployed slot and programmed, and finally the boot entry is wired up.
# =============================================================================

# 1) Deploy the x86 application image (formats + mounts the slot, sets APP_MNT).
if [ "${STREAMING:-0}" = "1" ]; then
    if [ "$APP_IMG_KIND" = "custom-build" ]; then
        deploy_custom_build_streamed
    elif [ "$APP_IMG_KIND" = "x86-disk-image" ]; then
        deploy_disk_image_streamed
    else
        deploy_app_image_streamed
    fi
else
    deploy_app_image
fi
VDIR="${APP_MNT}/lib/versal"
mkdir -p "$VDIR"

# 2) Post-deploy overlays / hook-scripts onto the new rootfs.
apply_init_components post-deploy

# 3) Persist + program the Versal images (if the bundle carries any). When
# streaming, the Versal images were already landed on the slot during deploy.
# The persist step folds under the "Init Partition" tab; only the (long)
# programming step gets its own tab.
if has_versal_components; then
    if [ "${STREAMING:-0}" = "1" ]; then
        log "Versal images were streamed onto the slot during deploy"
        verify_streamed_versal_images
    else
        persist_versal_images
    fi

    emit_tab "Versal Program"
    program_versal
else
    log "no Versal components in bundle — skipping Versal persist/program"
fi

# 4) Finalize-stage hook-scripts (run after the Versal side is in place).
apply_init_components finalize

# 5) Register the boot entry and write the install manifest.
emit_tab "Boot Config"
configure_boot
write_installed_json

sync
unmount_app_slot

# Drop the downloaded bundle from the cache unless explicitly retained. Local
# bundles (BUNDLE_DOWNLOADED=0) are never touched.
if [ "${BUNDLE_DOWNLOADED:-0}" = "1" ] && [ "${KEEP_BUNDLE:-0}" != "1" ]; then
    log "removing cached bundle ${BUNDLE}"
    rm -f "$BUNDLE"
fi

emit_tab "Done"
log "install complete for ${APP_ID} ${APP_VERSION}."
exit 0
