<!--
Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
SPDX-License-Identifier: MIT
Contributors: KC
Maintainer: KC
-->

# RAVE2 Buddy — agent orientation

> You are on, or connected to, a RAVE2 "Buddy" board: an x86 host carrying an
> AMD Versal adaptive SoC on PCIe. Buddy is the local web menu and control
> plane that runs on the x86 host and drives everything else on the board.

This file tells an automated agent what this device is, what it can be asked to
do, and what must never be done casually. The full HTTP reference is in
[api.md](api.md) — also served at `http://<board-ip>/api.md`.

## Read this before acting

**The HTTP API has no authentication of any kind.** There is no login, no
token, no API key, no CORS restriction and no IP allow-list. The service runs
as **root** on **port 80, bound to `0.0.0.0`**. Every endpoint described in
[api.md](api.md) is reachable by anyone who can route to the board — including
endpoints that reboot it, power it off, write BIOS flash, reprogram the Versal,
repartition NVMe, and open an unauthenticated interactive root-capable shell.

Treat the whole API as privileged. Do not expose the board to an untrusted
network, and do not proxy it to the public internet.

## What the board is

| Thing | Value |
|---|---|
| Web menu | `http://<board-ip>/` (port 80, HTTP only) |
| Service | `rave-menu.service` (systemd, `Restart=on-failure`) |
| Application root | `/opt/rave-menu` |
| Runs as | `root` |
| Framework | Flask, single process, `threaded=True` (the dev server, not gunicorn) |
| Login user | `amd-edf` (has `sudo`) |
| Persistent state | `/var/lib/rave-menu/` |
| Runtime state | `/run/rave-menu/` (tmpfs, lost on reboot) |
| Versal tools | `/opt/tools/<version>/` (e.g. `2025.2`, `2026.1`) |
| Updates | RAUC A/B slots; bundles are `.raucb` |

## Layout

```
/opt/rave-menu/
  app.py              Flask entry point, blueprint registration
  helpers.py          ACTIONS registry, bulk lock, subprocess wrapper
  routes_*.py         one blueprint per functional area
  scripts/            the shell scripts that jobs actually run
  templates/ static/  UI
/usr/share/doc/buddy/ this file, api.md, llms.txt
```

## The two concepts you need

**Jobs.** Long-running work is not synchronous. `POST /api/run/<action>`
returns `{"job_id": "<uuid>"}` immediately and you read progress from
`GET /api/stream/<job_id>`, a Server-Sent Events stream. The stream ends with
`event: status` / `data: exited:<returncode>`. Jobs live in an in-memory dict,
so a `rave-menu` restart loses every `job_id` while the child keeps running.

**The bulk lock.** A single `flock` on `/run/lock/rave-bulk-operation.lock`
serialises operations that write flash or repartition storage. Endpoints that
would corrupt an in-flight operation refuse with HTTP `409` while it is held.
Check for it, back off, and retry — never work around it.

**UFS re-provisioning is irreversible.** `POST /api/versal/luns/apply` with
`confirm: true` makes every byte on the Versal's UFS unrecoverable, including
the rootfs, and the board will not boot until an image is reprogrammed. Never
call it speculatively or to "see what happens". Without `confirm` it is a dry
run that reports the resulting layout and writes nothing — use that. `POST
/api/versal/luns/scan` is read-only and is the right way to find out what a
board currently looks like.

The `luns` array fully describes the result: a LUN you leave out is disabled.
Read the current layout with `scan` and send it back with only the fields you
mean to change — anything you omit from an entry is carried over from the
device, but a LUN you omit entirely is gone.

## Safe to call freely

All `GET` endpoints listed in [api.md](api.md) are read-only, with two
exceptions that mount filesystems as a side effect: `GET /api/rauc-status` and
`GET /api/browse?source=ubuntu`.

Good starting points for orientation:

- `GET /api/tools/version` — which Versal toolchain is selected and effective
- `GET /api/hw-server/status` — is a Vivado hardware server running, and at what release
- `GET /api/vivado/status` — what is installed under `/opt/tools`
- `GET /api/rauc-status` — A/B slot state and versions
- `GET /api/boot-default` — what the board boots by default
- `GET /api/storage-status`, `GET /api/uploads` — storage health and staged files
- `GET /api/network/services` — network state
- `GET /api/board-test/status` — hardware test results
- `GET /api/localization` — keyboard layout and language, and which locales are installed
- `GET /api/support-logs` — how much operation history the board has recorded

## Reading the support log

The board keeps an append-only record of everything that has been programmed,
installed or changed on it, under `/var/lib/rave-menu/storage/support/`.
`GET /api/support-logs/download` returns it as a `.tar.gz`, which may be large.
It is the fastest way to find out what has actually happened to a board.

Each record is framed:

```
##### BEGIN <id> <ts> <event>
{"id": …, "ts": …, "event": …, "client": …, "input": {…}, "context": {…}}
|<id>| a line of the operation's output
##### END <id> <ts> exit=0 status=ok duration_s=412
```

The line after `BEGIN` is a complete JSON object, so take structured fields
from there rather than parsing the frame. Every output line is prefixed with
its record id: concurrent operations interleave in the file, and a line is
always attributable to the record that produced it. Output is sanitised, so a
line beginning `#####` is always a real frame.

Two other top-level lines appear:

- `##### BOOT <ts> version=… slot=…` — the menu started.
- `##### MARK <id> <ts> <note>` — something happened mid-record, e.g. a reboot
  was requested.

A `BEGIN` with no matching `END` is normal, not corruption: several logged
operations reboot the board. If a `BOOT` line follows it, the operation did not
survive that boot. The file is never rewritten to tidy these up.

The log does **not** cover the interactive shells (`/api/console/*`,
`/api/serial/<n>/write`) or JTAG traffic through a running hardware server. If
someone programmed the board by hand through those, the log shows the gap but
not the content.

## Ask a human first

Do not call these on your own initiative. Each one either interrupts the user,
destroys data, or writes non-volatile memory where a failure can leave the
board unbootable.

**Interrupts everything:** `POST /api/poweroff`, `POST /api/reboot`,
`POST /api/reboot-to`, `POST /api/install-nvme`.

**Blanks the local display:** `POST /api/localization/keyboard` and
`POST /api/localization/revert` restart the compositor, so the screen goes dark
for several seconds and the kiosk browser relaunches. A keyboard change also
reverts itself after 60 seconds unless `POST /api/localization/confirm`
arrives, so do not apply one and walk away. `POST /api/localization/language`
only relaunches the browser.

**Writes flash / reprograms hardware:** `POST /api/run/board-test`,
`POST /api/run/versal-jtag`, `POST /api/run/bios-update`,
`POST /api/run/install-app`, `POST /api/apps/install`,
`POST /api/apps/install-url`, `POST /api/vivado/install`,
`POST /api/install-update`.

**Destroys data:** `POST /api/uploads/clear`, `POST /api/uploads/delete`,
`POST /api/run/uninstall-ubuntu`.

**Resets the Versal:** `POST /api/versal/layout/scan` and
`POST /api/run/reconfigure-versal`. Neither writes to the target, but both
reboot the Versal and interrupt whatever it is running.

**Can cut your own connection:** `POST /api/network/toggle`,
`POST /api/network/disconnect`, `POST /api/network/configure_ipv4`,
`POST /api/network/interface_power`.

**Arbitrary code execution:** `POST /api/console/write` writes to a live login
shell running as `amd-edf`, who has `sudo`. `POST /api/serial/<n>/write` writes
raw bytes to a UART attached to the Versal's console.

**Raw hardware register writes:** `POST /api/adau1860/register` has no address
allow-list and no range check, and an empty body silently writes `addr=0,
value=0`.

**Interrupts a live JTAG session:** `POST /api/hw-server/stop` and
`POST /api/tools/version` both stop the Vivado hardware server and the
ChipScope server. They probe the
bulk lock and return `409` during a programming run, but outside one they will
disconnect a customer's remote Vivado session without further warning.

## Working on the source

The board's files are installed artefacts, not a source tree. The source of
record is the Yocto layer `meta-buddy`, recipe
`recipes-apps/rave-menu/rave-menu_0.1.bb`. Anything changed directly on the
board is lost at the next RAUC update, so mirror every fix back into the layer.

- Every new file must be added to both `SRC_URI` and `do_install` in the recipe.
- After editing files under `/opt/rave-menu`, restart the service:
  `sudo systemctl restart rave-menu.service`.
- Python, shell, templates and the Vivado manifest ship together in one image.
  A partial hand-deploy over `scp` creates mixed-generation state that a real
  RAUC update never produces — copy the complete set of changed files.
- `ss`, `timeout` and several other GNU utilities are **not** on this image
  (BusyBox build). `netstat`, `lsof` and `dmidecode` are.

## Conventions and gotchas

- Most handlers parse bodies with `request.get_json(silent=True) or {}`, so a
  malformed body is indistinguishable from an empty one and silently falls back
  to defaults. Always set `Content-Type: application/json`.
- Error envelopes are not uniform. Most endpoints return
  `{"ok": false, "error": "..."}`; the app-store blueprint returns a bare
  `{"error": "..."}`; `start_job` failures return **HTML** Werkzeug error pages;
  two serial-stream failures return **plain text**.
- HTTP `409` is overloaded. `{"error": "busy"}` and
  `"another bulk operation is already running"` mean the bulk lock. `busy: true`
  with a `holders[]` array means a serial device is held by another process.
  `"not_installed"`, `"sha256_mismatch"` and `"wrong_version"` are unrelated.
- Serial and console sessions are **process-global and shared**. Closing one
  disconnects every other viewer.
- `xsdb` must come from the same install tree as the `hw_server` it connects
  to. Mixing releases is not a supported configuration.
