#!/bin/sh
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

set -eu

timestamp() {
    date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date
}

URL="${RAVE_MENU_URL:-http://127.0.0.1}"
PROFILE_DIR="/tmp/rave-chromium"
mkdir -p "$PROFILE_DIR"

set -- /usr/bin/chromium \
    --start-maximized \
    --new-window \
    --no-first-run \
    --no-default-browser-check \
    --disable-session-crashed-bubble \
    --disable-infobars \
    --user-data-dir="$PROFILE_DIR"

# Weston often runs as root in this image; Chromium requires no-sandbox in that mode.
if [ "$(id -u)" = "0" ]; then
    set -- "$@" --no-sandbox
fi

# Allow runtime overrides via /etc/default/weston without changing this script.
if [ -n "${RAVE_CHROMIUM_EXTRA_ARGS:-}" ]; then
    # shellcheck disable=SC2086
    set -- "$@" ${RAVE_CHROMIUM_EXTRA_ARGS}
fi

echo "[$(timestamp)] launch: $* $URL" >> /tmp/rave-cog-launch.debug.log

# Wait for the Flask backend to start accepting requests before opening the
# browser. Without this, Chromium loads $URL before the menu is listening and
# paints a "site can't be reached" error page for the first few seconds of
# boot (made worse by rave-hwinfo.service delaying the backend start). Poll for
# up to ~30s, then launch anyway so a broken backend never blocks the GUI.
#
# Probe /healthz rather than $URL: rendering the full menu up to sixty times a
# boot is wasteful, and urlopen follows redirects, so polling "/" would also
# swallow the one-shot redirect the localisation page relies on after a
# compositor restart.
HEALTH_URL="${RAVE_MENU_HEALTH_URL:-http://127.0.0.1/healthz}"

wait_for_backend() {
    i=0
    while [ "$i" -lt 60 ]; do
        if python3 -c "import sys,urllib.request; urllib.request.urlopen(sys.argv[1], timeout=1)" "$HEALTH_URL" 2>/dev/null; then
            echo "[$(timestamp)] backend ready after ${i} tries" >> /tmp/rave-cog-launch.debug.log
            return 0
        fi
        i=$((i + 1))
        sleep 0.5
    done
    echo "[$(timestamp)] backend not ready after ${i} tries, launching anyway" >> /tmp/rave-cog-launch.debug.log
    return 1
}

# Keep the browser alive: if the user closes the last tab (Ctrl-W twice) or
# Chromium crashes, relaunch it. A short sleep between attempts prevents a
# tight loop if Chromium is failing to start. The loop runs forever -- the
# only way to truly stop it is to terminate the script (which Weston will
# also restart via [autolaunch] watch=true).
rc=0
while :; do
    wait_for_backend || true
    set +e
    "$@" "$URL" >> /tmp/rave-cog-launch.stdout.log 2>> /tmp/rave-cog-launch.stderr.log
    rc=$?
    set -e
    echo "[$(timestamp)] chromium exited rc=$rc, relaunching" >> /tmp/rave-cog-launch.debug.log
    sleep 1
done

exit "$rc"
