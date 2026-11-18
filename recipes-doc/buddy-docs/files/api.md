<!--
Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
SPDX-License-Identifier: MIT
Contributors: KC
Maintainer: KC
-->

# RAVE2 Buddy HTTP API

Complete reference for the HTTP API exposed by `rave-menu` on a RAVE2 Buddy
board. For a shorter orientation aimed at automated agents, read
[AGENTS.md](AGENTS.md) first.

- **Base URL:** `http://<board-ip>/` — plain HTTP, port 80, bound to `0.0.0.0`
- **Runs as:** `root`, via `rave-menu.service`
- **Authentication:** **none** — see [Security model](#security-model)
- **Content type:** JSON in, JSON out, except where noted

---

## Contents

- [Security model](#security-model)
- [Conventions](#conventions)
- [The job model](#the-job-model)
- [The bulk lock](#the-bulk-lock)
- [HTML pages](#html-pages)
- [Actions, power and boot](#actions-power-and-boot)
- [Files, storage and updates](#files-storage-and-updates)
- [Versal flash programming](#versal-flash-programming)
- [Board test](#board-test)
- [Network](#network)
- [Audio](#audio)
- [ADAU1860 codec](#adau1860-codec)
- [Vivado hardware server](#vivado-hardware-server)
- [Programming toolchain selection](#programming-toolchain-selection)
- [Vivado installer](#vivado-installer)
- [App store](#app-store)
- [Serial ports](#serial-ports)
- [Local console](#local-console)
- [Support logs](#support-logs)
- [Destructive endpoint index](#destructive-endpoint-index)

---

## Security model

There is no authentication, authorisation, session, token, API key, CORS
restriction or IP allow-list anywhere in the application. The service runs as
root and binds all interfaces on port 80.

Anyone who can reach port 80 can, without credentials:

- power off or reboot the board (`/api/poweroff`, `/api/reboot`)
- write BIOS flash (`/api/run/bios-update`)
- reprogram the Versal's OSPI flash and rootfs (`/api/run/board-test`, `/api/run/versal-jtag`)
- repartition and overwrite NVMe (`/api/run/install-app`, `/api/apps/install`)
- overwrite the inactive RAUC slot from an arbitrary URL (`/api/install-update`)
- obtain an interactive shell as `amd-edf`, who has `sudo` (`/api/console/*`)
- write arbitrary bytes to the Versal UART (`/api/serial/<n>/write`)
- write arbitrary codec registers over I²C (`/api/adau1860/register`)
- read the board's whole operation history, hardware serials and MAC
  addresses (`/api/support-logs/download`)

Two endpoints fetch a caller-supplied URL server-side with no host restriction
(`/api/apps/inspect`, `/api/apps/install-url`), and both `http://` and
`https://` are accepted. `/api/vivado/eulas` opens any readable tar path on the
filesystem — the path is resolved but not confined to a directory.

This is a bench and lab tool. Keep it on a trusted network segment. Do not
expose it to the internet or proxy it without adding authentication in front.

**One exception: cross-site writes are blocked.** Every state-changing request
(anything other than `GET`, `HEAD`, `OPTIONS`) is rejected with `403` if
`Sec-Fetch-Site` says it is cross-site, or if `Origin` is present and its host
does not match the request host. Requests with no `Origin` at all — `curl`,
scripts, the kiosk's own probes — are unaffected, because they are not
browser-driven and so are not the attack this prevents.

This stops a drive-by: any page a browser on this network visits could
otherwise `POST` to every endpoint above, including `/api/poweroff`. It is not
authentication and does not change anything in the list above for a caller who
reaches the port directly.

---

## Conventions

**Request bodies.** Most handlers use `request.get_json(silent=True) or {}`. A
malformed body, a missing `Content-Type`, or an empty body are all
indistinguishable, and the handler silently falls back to its defaults. Always
send `Content-Type: application/json`. The exceptions — `/api/run/<action>`,
`/api/reboot-to`, `POST /api/boot-default` and `/api/boot-timeout` — gate on
`request.is_json` instead.

**Response envelopes are not uniform:**

| Shape | Used by |
|---|---|
| `{"ok": true\|false, "error": "..."}` | most endpoints |
| `{"error": "..."}` with no `ok` | the whole app-store blueprint, `/api/browse`, `/api/stream/<job_id>` |
| bare payload, no `ok` | `/api/hw-server/status`, `/api/tools/version`, `/api/board-test/status`, `/api/audio/*` passthroughs |
| **HTML** (Werkzeug error page) | `start_job` failures: unknown action, missing/non-executable script, bulk lock held |
| **plain text** | `GET /api/serial/<n>/stream` failures, `/vivado-installer-url.png` 404 |

**HTTP 409 is overloaded.** Four unrelated meanings:

| Response | Meaning |
|---|---|
| `{"error": "busy"}` | bulk lock held (hw-server / tools endpoints) |
| `another bulk operation is already running` | bulk lock held (everything else) |
| `{"busy": true, "holders": [...]}` | serial device held by another OS process |
| `"not_installed"`, `"sha256_mismatch"`, `"wrong_version"` | validation failures |

---

## The job model

Long-running work is asynchronous.

### `POST /api/run/<action>`

Starts one of the registered scripts.

**Body:** `{"args": ["...", ...]}` — optional list of scalars, stringified.
Default `[]`. A non-list gives `400 {"error": "args must be a list"}`.

**Registered actions** (`ACTIONS` in `helpers.py`, scripts under
`/opt/rave-menu/scripts/`):

| Action | Script | Effect |
|---|---|---|
| `board-test` | `program_remote_versal_payload.sh` | **Programs Versal OSPI + rootfs** |
| `versal-jtag` | `program_jtag_versal.sh` | **Programs Versal over JTAG** |
| `app-store` | `launch_app_store.sh` | Launches the app-store UI |
| `reconfigure-versal` | `reconfigure_versal.sh` | Re-triggers Versal boot, re-enumerates PCIe |
| `install-ubuntu` | `install_ubuntu.sh` | **Creates NVMe partitions** |
| `uninstall-ubuntu` | `uninstall_ubuntu.sh` | **Deletes NVMe partitions** |
| `install-app` | `install_app_bundle.sh` | **Repartitions/writes the NVMe app region** |
| `bios-update` | `program_bios_update.sh` | **Writes BIOS flash** |

`BULK_ACTIONS = {board-test, versal-jtag, install-app, bios-update}` take the
bulk lock. The other four do **not** — `install-ubuntu`, `uninstall-ubuntu`,
`reconfigure-versal` and `app-store` can run concurrently with anything.

**Execution.** The script is spawned as an argv list (never a shell string),
`cwd=/opt/rave-menu/scripts`, stderr merged into stdout, with
`RAVE_ACTION=<action>` and `PYTHONUNBUFFERED=1` added to the inherited
environment and `PATH` defaulted to `/usr/sbin:/usr/bin:/sbin:/bin`. When the
bulk lock is taken, the lock fd is passed to the child, so the child holds the
lock for its entire lifetime.

**Success:** `200 {"job_id": "<uuid>", "cmd": ["<script>", "<arg>", ...]}`

**Errors:** `400 {"error": "args must be a list"}`; and as **HTML** error
pages: `404 Unknown action '<action>'`, `500 Script missing: <path>`,
`500 Script not executable: <path>`, `409 another bulk operation is already running`.

### `GET /api/stream/<job_id>`

Server-Sent Events, `Content-Type: text/event-stream`.

| Frame | Meaning |
|---|---|
| `data: <line>\n\n` | one line of output (stdout and stderr merged, right-stripped) |
| `: keepalive\n\n` | heartbeat after 10 s of silence — an SSE comment, invisible to `EventSource` |
| `event: status\ndata: exited:<rc>\n\n` | **terminator** — the job finished with return code `<rc>` |
| `event: status\ndata: error:<message>\n\n` | the stream itself failed |

Unknown id → `404 {"error": "unknown job id"}`.

**Single-shot.** When a job exits it is removed from the in-memory `JOBS` dict,
so reconnecting to the same `job_id` returns 404. Disconnecting does **not**
kill the child — it runs to completion and is still reaped.

Jobs are held in memory only. A `rave-menu` restart loses every `job_id` while
orphaning the running children.

Also used by jobs created outside `/api/run`: `/api/install-update`,
`/api/vivado/install`, `/api/apps/install`, `/api/apps/install-url`.

---

## The bulk lock

An exclusive non-blocking `flock` on
`/run/lock/rave-bulk-operation.lock` (override with `RAVE_BULK_LOCK_FILE`)
serialises operations that write non-volatile memory.

**Held for the operation by:** `POST /api/run/<action>` for the four
`BULK_ACTIONS`; `POST /api/apps/install`; `POST /api/apps/install-url`;
`POST /api/install-update`; `PUT /api/upload-payload`;
`POST /api/uploads/clear`; `POST /api/uploads/delete`;
`POST /api/versal/layout/scan`.

**Probed, but not held, by:** `POST /api/hw-server/stop`,
`POST /api/tools/version` — these refuse rather than proceed, so they cannot
tear the hardware server out from under a live `xsdb`.

**Not guarded at all:** `/api/poweroff`, `/api/reboot`, `/api/reboot-to`,
`/api/install-nvme`, `/api/vivado/install`, and the four non-bulk actions.
Calling these during a programming run can brick the target.

---

## HTML pages

All `GET`, no body, no side effects.

| Path | Notes |
|---|---|
| `/` | main menu |
| `/board-test` | |
| `/power-off` | |
| `/install-nvme` | `404 Already running from NVMe` when already installed |
| `/app-store` | |
| `/ubuntu-install`, `/ubuntu-uninstall` | |
| `/system-update` | |
| `/vivado-install` | accepts `?version=` |
| `/audio`, `/audio/developer` | |
| `/localisation` | accepts `?verify=1` to focus the layout test box |
| `/plugins` | `301` to `/localisation` (the page it replaced) |
| `/healthz` | `{"ok": true}`; readiness probe used by the kiosk launcher |
| `/favicon.ico` | |

---

## Localisation

Keyboard layout and system language. The keyboard is the point of this page:
it sets the layout for the keyboard physically attached to the unit.

**A keyboard change restarts the compositor**, which blanks the local display
for roughly six seconds and relaunches the kiosk browser. A remote caller's own
keyboard and browser are unaffected. A language change does not restart the
compositor; it only relaunches Chromium.

Settings live in `/etc/rave-menu/localization.json` and are copied into the new
slot by the RAUC post-install hook, so they survive a firmware update.

### `GET /api/localization`
Current settings, the shortlist of common layouts, and the offered locales with
an `installed` flag. No body.

### Page language

Every HTML page renders in the configured system language, so a remote view
matches what the kiosk is showing. Append `?lang=` to override it for one
request — useful when the appliance is set to a language you cannot read:

    GET /localisation?lang=de
    GET /?lang=el_GR

Both the short (`de`) and full (`de_DE`) forms are accepted. An unrecognised
value is ignored rather than erroring. Only locales built into the image are
offered; `<html lang>` always reflects the locale actually used.

### `GET /api/localization/layouts`
The full XKB catalogue (~100 layouts with their variants), parsed from
`/usr/share/X11/xkb/rules/evdev.xml`. Served separately because it is far too
large to include in every page load.

### `POST /api/localization/keyboard`
`{"layout": "de", "variant": "nodeadkeys", "model": "pc105", "options": ""}`

Only `layout` is required. **Restarts the display.** On success the response
carries `revert_seconds`: the change reverts automatically unless
`POST /api/localization/confirm` arrives first.

Guards, in order:

| Condition | Result |
|---|---|
| Not in the XKB catalogue, or fails a `xkbcli` test-compile | `400`, nothing written |
| Identical to the current settings | `200`, no write, no restart |
| Within 30s of the previous apply | `429` with `Retry-After` |

The first guard is not cosmetic. Weston exits 1 on a keymap it cannot compile
and is `Restart=on-failure` with `StartLimitBurst=5` over 10s, so an unchecked
value takes the display down until someone clears the failed state over SSH.
The other two stop a repeated or replayed POST doing the same thing.

### `POST /api/localization/language`
`{"language": "de_DE.UTF-8"}` — must be one of the locales built into the
image. Sets `LANG`, the console keymap and Chromium's `--lang`. Does **not**
restart the compositor.

### `POST /api/localization/confirm`
No body. Keeps the pending keyboard change and cancels the auto-revert timer.

### `POST /api/localization/revert`
No body. Restores the previous keyboard settings immediately and restarts the
display. `404` if there is nothing to revert to.

---

## Actions, power and boot

### `POST /api/poweroff`
No body. **Powers the machine off** about one second later. Not lock-guarded —
will kill an in-flight flash job.
`{"ok": true, "message": "poweroff requested"}` / `500 {"ok": false, "error": "..."}`

### `POST /api/reboot`
No body. **Reboots** about one second later. Not lock-guarded.
`{"ok": true, "message": "reboot requested"}` / `500 {"ok": false, "error": "..."}`

### `POST /api/reboot-to`
**Body:** `entry` (string, required) — one of `menu`, `ubuntu`, `ubuntu-prev`,
`ubuntu-recovery`, `app-a`.

Writes a one-shot `next_entry` to the GRUB environment, then **reboots**.

`400 {"ok": false, "error": "invalid entry"}`,
`400 {"ok": false, "error": "Ubuntu is not installed"}`,
`400 {"ok": false, "error": "No application is installed"}`,
`500 {"ok": false, "error": "..."}`.
Success: `{"ok": true, "message": "rebooting once into <entry>"}`

### `GET /api/boot-default`
No input. Reads the GRUB environment.

Returns `installed`, `default` (`ubuntu`|`buddy`), `app_installed`,
`boot_default` (`menu`|`ubuntu`|`app`|`""`), `app_title`, `timeout` (seconds),
and `app_name` when an app is installed.

### `POST /api/boot-default`
**Body:** `target` (string, required) — `ubuntu`, `buddy` or `app`.
**Changes what the board boots by default.**

`400 {"ok": false, "error": "target must be 'buddy', 'ubuntu' or 'app'"}`,
`400 ... "Ubuntu is not installed"`, `400 ... "No application is installed"`,
`500 {"ok": false, "error": "Could not locate the ESP grubenv."}`.
Success: `{"ok": true, "default": "<target>"}`

### `POST /api/boot-timeout`
**Body:** `timeout` (integer seconds, required, 0–3600).

`400 {"ok": false, "error": "timeout is required"}`, or
`"timeout must be an integer number of seconds"`,
`"timeout must be between 0 and 3600 seconds"`,
`"Could not locate the ESP grubenv."`.
Success: `{"ok": true, "timeout": <int>}`

### `POST /api/install-nvme`
No body. **Rewrites `/boot/EFI/BOOT/grub.cfg` to select the NVMe installer,
unmounts `/boot/efi`, then reboots.** Not lock-guarded.

`400 {"ok": false, "error": "Already running from NVMe"}`,
`500 {"ok": false, "error": "Cannot find grub.cfg at /boot/EFI/BOOT/grub.cfg"}`,
`500 {"ok": false, "error": "Failed to update grub.cfg: <exc>"}`.
Success: `{"ok": true, "message": "Rebooting into NVMe installer..."}`

---

## Files, storage and updates

### `GET /api/browse-sources`
`{"ok": true, "sources": [{"id": "usb", "label": "USB Media", "available": bool},
{"id": "ubuntu", "label": "Ubuntu", "available": bool, "device": "<devnode>"}]}`

### `GET /api/browse`
**Query:** `path` (default `""`), `source` (default `usb`; `usb` → `/run/media`,
`ubuntu` → the installed Ubuntu root).

**Side effect:** `source=ubuntu` **mounts** the Ubuntu root read-only at
`/run/rave-menu/ubuntu-root` and never unmounts it.

`400 {"error": "..."}` (`Unknown browse source '<s>'`, `No installed Ubuntu was
found.`, `Could not mount <dev>: <err>`, `invalid path`),
`404 {"error": "not a directory"}`, `403 {"error": "permission denied"}`.
Success: `{"source", "root", "path", "abs_path", "entries": [{"name", "is_dir"}]}`

### `PUT /api/upload-payload`
**Raw binary body** — not multipart, not JSON. The filename comes from the
`X-Filename` header, percent-decoded then sanitised to a basename with
`[^A-Za-z0-9._-]` replaced by `_`, capped at 200 characters, defaulting to
`upload.bin`. Streamed to `/var/lib/rave-menu/uploads/<name>.part` and renamed
on completion, **overwriting any existing file of that name**.

**Lock-guarded** for the whole upload → `409 {"ok": false, "error": "another bulk operation is already running"}`.

`400 {"ok": false, "error": "missing filename"}`,
`500 {"ok": false, "error": "cannot create upload dir: <exc>"}`,
`500 {"ok": false, "error": "upload failed: <exc>"}`.
Success: `{"ok": true, "path": "...", "name": "...", "size": <bytes>}`

### `GET /api/storage-status`
No input. Always `200`. Returns `ok`, `state` (default `degraded`), `reason`
(default `status-unavailable`), any other keys present in
`/run/rave-menu/storage-status`, plus `free` and `total` in bytes (`null` on error).

### `GET /api/uploads`
`{"ok": true, "items": [{"name", "path", "size", "mtime"}, ...], "free", "total"}`,
newest first, `*.part` excluded. `500 {"ok": false, "error": "<OSError>"}`.

### `POST /api/uploads/clear`
No body. **Deletes every regular file and symlink in
`/var/lib/rave-menu/uploads`.** Lock-guarded.

`{"ok": true, "removed": <int>, "freed": <bytes>}`; per-file failures return
HTTP **200** with `{"ok": false, "removed", "freed", "errors": ["<name>: <exc>"]}`.

### `POST /api/uploads/delete`
**Body:** `name` (string, required). **Deletes that file.** Lock-guarded.

`400 {"ok": false, "error": "invalid filename"}`,
`400 ... "path outside upload dir"`, `404 ... "not found"`, `500 ... "<OSError>"`.
Success: `{"ok": true, "name": "<name>", "freed": <bytes>}`

### `GET /api/check-update`
Fetches `https://rave2.cicdstorage.com/updates/manifest.json` (15 s timeout) and
filters to entries compatible with `rave2-menu`.

`{"ok": true, "current_version": "...", "updates": [{"version", "description", "url", "experimental"}]}`;
`502 {"ok": false, "error": "Cannot reach update server: <exc>"}` or `"Invalid manifest format"`.

### `GET /api/usb-bundles`
`{"ok": true, "bundles": ["/run/media/<mount>/<file>.raucb", ...]}`

### `GET /api/bios-status`
Runs `dmidecode`. `{"ok": true, "vendor": "ami"|"coreboot"|"unknown",
"product_line": "rave2-core"|"rave2-pro"|"rave2-max"|"unknown", "version": "...",
"pending": <object|null>}`

### `POST /api/bios-pending/clear`
No body. Removes `/var/lib/rave-menu/bios-update-pending.json`. Always `{"ok": true}`.

### `GET /api/nvme-free-space`
`{"ok": true, "free_mib": <int>, "jumbo_installed": bool, "jumbo_app_mib": <int>}`;
`500 {"ok": false, "error": "Could not read the NVMe partition table."}`

### `GET /api/rauc-status`
No input. **Side effect: mounts every non-booted slot read-only to read
`etc/rauc/version`, then unmounts it.**

`{"ok": true, "status": <rauc JSON with a "version" key injected per slot>}`;
`500 {"ok": false, "error": "..."}` or `"Failed to parse rauc status"`.

### `POST /api/install-update`
**Body:** `url` **or** `path` (string). `url` wins if both are given. Local
paths must resolve under `/run/media/`. Dropbox share links are normalised.

**Highly destructive.** Resizes the live root filesystem, downloads the bundle
into `/var/lib/rave-menu/updates/`, runs **`rauc install`** — overwriting the
inactive rootfs slot and flipping the boot order — then fsck/resizes the updated
slot and deletes the staged bundle.

**Lock-guarded.**

`400 {"ok": false, "error": "JSON body required"}`, `"Provide 'url' or 'path'"`,
`"Local path must be under /run/media/"`, `"Only http/https URLs supported"`;
`404 {"ok": false, "error": "File not found: <source>"}`;
`409 {"ok": false, "error": "another bulk operation is already running"}`.
Success: `{"ok": true, "job_id": "<uuid>"}` — stream via `/api/stream/<job_id>`.

---

## Versal flash programming

The Versal is programmed over PCIe by the `board-test` action. A recovery
payload is booted on the target, which exposes its UFS device through a BAR
window; the host then streams the image into it and resets the Versal back to
OSPI boot.

### `POST /api/run/board-test` arguments

`args` is positional, `[wic, ospi, bmap, ranges]`, all optional:

| # | Argument | Meaning |
|---|---|---|
| 0 | `wic` | Rootfs image — URL or local path. `.wic.ufs`, `.wic.ufs.gz` or `.wic.ufs.zst`. |
| 1 | `ospi` | Boot image (`boot.bin`) — URL or local path. |
| 2 | `bmap` | Optional `.bmap` for the WIC. Enables sparse writes. |
| 3 | `ranges` | Optional partial-update spec, `"offset:size,offset:size"` in bytes. |

An omitted argument falls back to the packaged default; `""` or `"-"` skips
that phase explicitly. At least one of `wic`/`ospi` must be present.

**bmap.** Supplying a bmap makes the transfer sparse — only mapped blocks are
sent, and the bmap's per-range SHA-256 checksums are used to verify them, so
no separate per-chunk checksum pass is needed. A bmap requires a `wic`.

**ranges.** A partial update writes only the listed byte ranges and **never
touches the partition table**. It requires both a `wic` and a `bmap`, and is
rejected without them. Offsets and sizes must be plain decimal integers. Build
the spec from the partitions the caller selected after a layout scan.

### `POST /api/versal/layout/scan`

Reads the GPT from the target's UFS and compares it against the partition table
in a candidate image, so a caller can decide which partitions may be written on
their own. **Lock-guarded.**

> **Resets the Versal.** The scan boots the recovery payload, reads the head of
> the UFS, then resets the board back to OSPI boot. Whatever was running on the
> Versal is interrupted. It does not write to the target. Takes roughly 30 s.
>
> It deliberately does **not** wait for the Versal to re-enumerate on PCIe
> afterwards — the OSPI image cannot be assumed valid, so the endpoint may
> legitimately never come back, and the scan must not hang on that.

**Body:** `wic` (string, required) — URL or local path to the image to compare
against; `bmap` (string, optional) — presence alone decides whether partial
updates are offered, the contents are not read here.

Only the first few hundred KiB of `wic` are read. For an `http(s)` source a
`Range` request is used, escalating through 1/4/16/64 MiB until enough
uncompressed data is available, so a multi-GiB image is not downloaded.

`400 {"error": "a Rootfs WIC image is required to compare layouts"}`;
`409 {"error": "another bulk operation is already running"}`;
`422 {"error": "<layout error>"}` — unreadable or absent partition table on
either side;
`500 {"error": "could not read the partition table from the target", "log": "<tail>"}`;
`504 {"error": "timed out reading the partition table from the target"}`.

**Success:** `{"ok": true, "target": <gpt>, "image": <gpt>, "plan": <plan>}`

Each `<gpt>` is `{"lba_size", "disk_guid", "device_size", "first_usable",
"last_usable", "partitions": [...]}`, with each partition carrying `index`,
`name`, `type_guid`, `type_name`, `part_uuid`, `start` and `size`. All offsets
and sizes are **bytes**, not LBAs. `lba_size` is detected from the image, and
is `4096` on current RAVE2 UFS parts — do not assume 512.

`<plan>` is:

| Key | Meaning |
|---|---|
| `rows` | One entry per partition position, paired across image and target |
| `partial_supported` | `true` when at least one partition may be written alone |
| `blocker` | Why partial updates are unavailable, else `null` |

Each row is `{"index", "name", "type_name", "start", "image_size",
"target_size", "selectable", "reason", "warning"}`. `image_size` or
`target_size` is `null` where that side has no partition at that position.

`selectable` is `false`, with `reason` explaining why, when the name, type or
start offset differ, or when the sizes do not match — including a target
partition that has been **grown** to fill the device, which is the normal case
for a data partition and can only be rewritten by a full image write. A
PARTUUID mismatch is only a `warning`; the row stays selectable.

To act on the result, write back the `start` and `image_size` of each selected
row as the `ranges` argument to `POST /api/run/board-test`.

---

## Versal UFS LUN layout

The RAVE2 UFS device is divided into logical units. It ships with three: a
large rootfs LUN that the flash programming flow writes, and two 4 MiB boot
LUNs. These endpoints report that layout and let it be changed.

Everything is read from the device. LUN count, allocation-unit size, capacity
and capacity adjustment factors all come from the part, so a different device
simply reports different limits.

### `POST /api/versal/luns/scan`

Reads the UFS device, geometry and configuration descriptors and returns the
current LUN layout. **Read-only. Lock-guarded.**

> **Resets the Versal**, exactly as the layout scan does: it boots the recovery
> payload, queries the device, then restores OSPI boot mode. Nothing is
> written to the UFS.

On the first successful scan of a given device the configuration descriptor is
captured as that device's baseline, keyed by UFS serial. That baseline is only
used as a fallback for parts with no known factory layout — see
`restore_baseline` below.

**Success:** `{"ok": true, "report": <report>}` where `<report>` carries
`serial`, `product`, `spec_version`, `config_descr_lock`, `max_boot_luns`, a
`geometry` object and a `luns` array. Each LUN has `lun`, `enabled`,
`boot_lun_id`, `memory_type`, `num_alloc_units`, `capacity_bytes` and the
resolved `block_device`.

`boot_lun_id` is 0 for an ordinary LUN, 1 for Boot LUN A and 2 for Boot LUN B.
`max_boot_luns` is how many the device will accept, which is 2 because that is
all `bBootLunID` can encode.

`config_descr_lock` is the one field worth checking first: when non-zero the
device's layout was permanently frozen by an earlier write and can never be
changed, so every provisioning call will fail.

### `POST /api/versal/luns/apply`

Validates a requested layout and, with explicit confirmation, writes it.
**Lock-guarded.**

> **Makes all data on the UFS device unrecoverable, including the rootfs.** The Versal
> will not boot afterwards until an image is programmed. That is a separate
> operation — this endpoint does not chain into it.

**Body:** `luns` (array) — one object per LUN that should exist, each with
`lun` (the index it keeps) and `size` (a byte count, a string such as `32G`,
or `"rest"` on at most one LUN);
`restore_baseline` (boolean) — instead of `luns`, write back this device's
factory layout; `confirm` (boolean) — must be `true` to write
anything. Without it the call is a dry run that reports the resulting layout
and changes nothing.

Give either `luns` or `restore_baseline`, not both.

```json
{
  "luns": [
    { "lun": 0, "size": "rest" },
    { "lun": 1, "size": 12582912, "boot_lun_id": 1,
      "memory_type": 3, "data_reliability": 1 }
  ],
  "confirm": false
}
```

**A LUN keeps the index it is given.** Leaving one out disables it, and the
others are not renumbered, so a layout with a gap stays as written.

**A plan changes only what it names.** An entry may also carry `boot_lun_id`
(0, 1 for Boot LUN A, 2 for Boot LUN B), `memory_type`, `data_reliability`,
`write_protect`, `logical_block_size_exp` and `provisioning_type`. Anything
not given is copied from that LUN's current configuration, so sending the
current layout back unchanged is a no-op down to the byte.

That matters on the fitted part, whose boot LUNs are enhanced memory with
reliability set: 3 allocation units present 4 MiB there, and rebuilding them
as ordinary memory would turn them into 12 MiB LUNs.

Sizes are rounded **down** to whole allocation units, because the hardware has
no finer granularity, and the rounded result comes back in the response.

### Restoring the factory layout

`restore_baseline: true` writes the part's factory configuration descriptor
back verbatim, header and vendor fields included.

For a part with a known factory layout that descriptor is built in, so the
result does not depend on when the board was first scanned. The RAVE2 part,
`KLUEG8UHGC-B0E1`, is known: LUN 0 of 61016 allocation units plus 4 MiB
enhanced-memory boot LUNs A and B. It was read from two boards with different
UFS serials and was byte-identical on both, so for this part the layout
belongs to the part rather than the board.

A part with no known factory layout falls back to the descriptor captured the
first time that serial was scanned. That is weaker: if the board had already
been re-provisioned before its first scan, the capture records that layout and
nothing afterwards can tell the difference. The response reports which was
used in `restore_source`.

Matching is strict — product, manufacturer ID, allocation unit size,
allocatable units and maximum LUN count must all agree — because writing
another part's descriptor would describe a device that is not there.

**Success:** `{"ok": true, "committed": <bool>, "report": <report>,
"preview": <layout>, "log": "<tail>"}`. `committed` is `false` for a dry run.

`400` for a malformed body — a LUN that is not an object, an unknown field, an
invalid index or size;
`422 {"error": "the target rejected the requested layout"}` — over capacity, a
size that rounds to zero, more boot LUNs than the device allows, a repeated
index, or a stale view of the device;
`409 {"error": "another bulk operation is already running"}`.

---

## Board test

Drives the Versal over serial and runs `ptest-runner` suites on it. None of
these are lock-guarded.

The **snapshot** object returned by most of these contains: `started_once`,
`running`, `current_test`, `last_error`, `discovered`, `discovering`, `pcie`
(`{ok, message, details}`), `settings` (with `iperf` and `iperf10g` sub-objects
of `server_ip`/`min_bw`), and `tests` (per-test records).

### `POST /api/board-test/discover`
**Body:** `force` (bool, default `false`). Starts a background PCIe check and
ptest discovery. Non-blocking. Returns the snapshot, always `200`.

### `POST /api/board-test/start`
**Body:** `force` (bool, default `false`), `iperf` (object with `server_ip`,
`min_bw`), `iperf10g` (same shape).

**Spawns a worker thread that drives the Versal over serial** and resets all
test records.

`{"ok": bool, "message": str, "snapshot": {...}}` with `200` when `ok`, else
`409`. Messages include `started`, `already started`, `board test run already in
progress`, `Invalid iperf server IP address`, `Invalid iperf required speed`,
`iperf required speed must be greater than zero`.

### `GET /api/board-test/status`
Returns the snapshot directly, with no `ok` wrapper.

### `POST /api/board-test/rerun/<test_id>`
**Body:** `iperf`, `iperf10g` as above. Reruns one test.
`404 {"ok": false, "error": "unknown test: <id>", "snapshot": {...}}`, else
`{"ok", "message", "snapshot"}` at `200`/`409`.

### `GET /api/board-test/log/<test_id>`
`{"ok": true, "test_id": "<id>", "output": "<accumulated stdout>"}`;
`404 {"ok": false, "error": "unknown test: <id>"}`.

---

## Network

Wrappers around `connmanctl`. None are lock-guarded. **Several can disconnect
the caller.**

### `GET /api/network/services`
**Query:** `scan` — `1` triggers `connmanctl scan wifi` (25 s) first.

Returns `ok`, `technologies`, `services`, `interfaces`, `wifi_hint`, `errors`,
`viewer`, `captive_portal`. Always `200`.

### `POST /api/network/scan`
**Body:** `technology` (default `wifi`) — `wifi`, `ethernet`, `cellular`, `bluetooth`.
`400 {"ok": false, "error": "invalid technology"}`; `500 {"ok": false, "error": "..."}`.
Success: `{"ok": true, "message": "scan requested for <tech>"}`

### `POST /api/network/toggle`
**Body:** `technology` (default `wifi`), `enabled` (bool, default `true`).
**Disabling drops every interface of that technology.**
Success: `{"ok": true, "message": "<tech> enabled|disabled"}`

### `POST /api/network/interface_power`
**Body:** `ifname` (string, required), `enabled` (bool, default `true`).
**Brings a single NIC up or down.**
`400 {"ok": false, "error": "invalid or unknown interface"}`.
Success: `{"ok": true, "message": "<ifname> enabled|disabled"}`

### `POST /api/network/connect`
**Body:** `service_id` (string, required), `passphrase` (default `""`),
`identity` (falls back to `username`, default `""`), `security` (default `auto`)
— `auto`, `open`, `psk`, `wep`, `8021x`. `security=open` forces the passphrase
and identity empty.

Sets the passphrase/identity, connects, and finally sets `--autoconnect yes`,
**persisting the profile in ConnMan across reboots**.

`400 {"ok": false, "error": "invalid service_id"}` or `"invalid security type"`;
`500` with `"failed to set passphrase"`, `"failed to set identity"`, a raw
ConnMan message, or `"connect requested but service did not reach connected
state. Verify WiFi is enabled and credentials are correct, then retry."`.
Success: `{"ok": true, "message": "connected", "persistence": "..."}`

### `POST /api/network/disconnect`
**Body:** `service_id` (required), `disable_autoconnect` (bool, default `false`).
An already-disconnected service returns `200 {"ok": true, "message": "already disconnected"}`.
Success: `{"ok": true, "message": "disconnected"}`

### `POST /api/network/configure_ipv4`
**Body:** `service_id` (required), `method` (default `dhcp`) — `dhcp` or
`manual`; `dns_servers` (string, split on whitespace/commas); and for `manual`,
`address`, `netmask`, `gateway` (all required).

**Changes the address the caller may be talking to.**

`400 {"ok": false, "error": "method must be dhcp or manual"}` or
`"manual mode requires address, netmask, and gateway"`.
Success: `{"ok": true, "message": "IPv4 configuration applied", "persistence": "Saved via ConnMan profile."}`

---

## Audio

None are lock-guarded. Endpoints marked *passthrough* return a dict built by
`pipewire.py`, `dmic_meter.py`, `nau8822_meter.py`, `audio_targets.py` or
`acpi_ssdt.py`; consult those modules for the exact keys.

### Unified control

| Endpoint | Body | Notes |
|---|---|---|
| `GET /api/audio/state` | — | passthrough; sinks carry `id`, `name`, `muted`, `volume_percent`, `available`, `bus_path`, `card_name` |
| `POST /api/audio/set_default` | `id` (required) | changes the system default sink |
| `POST /api/audio/set_volume` | `id` (required), `percent` (int, default `0`) | `id="adau1860_lineout"` **writes codec registers over I²C** |
| `POST /api/audio/set_mute` | `id` (required), `mute` (bool, default `false`) | |
| `POST /api/audio/play_chime` | `id` (required) | **audible** |
| `POST /api/audio/mic_control` | `name` (or `control`), `value` | `pga_gain`→`PGA Volume`, `boost`/`boost_20db`→`PGA Boost`; any other name passes through as a literal ALSA control |

Errors are `400 {"ok": false, "error": "missing id"}` / `"invalid percent"` /
`"missing name"` / `"invalid value"`, or `500 {"ok": false, "error": "..."}`.
Successes are `{"ok": bool, "message": str}` at `200`/`500`.

### Metering and loopback

DMIC: `POST /api/audio/dmic/start_metering`, `.../stop_metering`,
`GET /api/audio/dmic/levels`, `POST /api/audio/dmic/test` (**audible
record+playback loopback**), `GET /api/audio/dmic/test/status`.

NAU8822: the same five under `/api/audio/nau8822/…`.

Generic registry-driven inputs, keyed by `<key>` from `audio_targets.TARGETS`:
`POST /api/audio/input/<key>/start_metering`, `.../stop_metering`,
`GET /api/audio/input/<key>/levels`,
`POST /api/audio/input/<key>/test` (body `pair`, optional, two int-coercible
elements; invalid → `400 {"ok": false, "error": "invalid pair"}`),
`GET /api/audio/input/<key>/test/status`.

All passthrough.

### Legacy compatibility

| Endpoint | Body |
|---|---|
| `GET /api/audio/snapshot` | — — returns `{"sinks": [{"name", "description", "mute", "volume_percent", "state", "available", "bus_path", "card_name"}], "default_sink": "..."}` |
| `POST /api/audio/default_sink` | `name` (required) |
| `POST /api/audio/sink_volume` | `name` (required), `percent` (int, default `0`) |
| `POST /api/audio/sink_mute` | `name` (required), `mute` (bool, default `false`) |
| `GET /api/audio/mic_controls` | — — `{"ok": true, "controls": {...}}` |

### ACPI SSDT

| Endpoint | Body | Notes |
|---|---|---|
| `GET /api/audio/ssdt/status` | — | passthrough |
| `POST /api/audio/ssdt/load` | `name` (required, e.g. `adau1860-ssdt`) | **loads an ACPI table into the running kernel — changes live device enumeration** |
| `POST /api/audio/ssdt/unload` | `name` (required) | **unloads it** |
| `POST /api/audio/adau1860/enable` | `enabled` (bool, default `false`) | **persists the setting for the next boot**; returns `{"ok", "message", "enabled"}` |
| `GET /api/audio/adau1860/status` | — | `{"enabled", "loaded", "message"}`, always `200` |

---

## ADAU1860 codec

Direct I²C register access. None are lock-guarded and none require
confirmation.

| Endpoint | Body | Notes |
|---|---|---|
| `GET /api/adau1860/snapshot` | — | passthrough |
| `GET /api/adau1860/status` | — | passthrough |
| `POST /api/adau1860/volume` | `db` (float) **or** `percent` (int); `db` wins | **writes registers** |
| `POST /api/adau1860/mute` | `mute` (bool, default `false`) | |
| `POST /api/adau1860/output_mode` | `mode` — `headphone` or `lineout` | |
| `POST /api/adau1860/asrc` | `bypass` (bool, **default `true`**) | |
| `POST /api/adau1860/reinit` | — | **full codec re-init; audio glitches/drops** |
| `GET /api/adau1860/dmic` | — | passthrough |
| `GET /api/adau1860/register/<addr>` | — | `<addr>` parsed with `int(addr, 0)`, so `0x10`, `16`, `0o20` all work. **Raw hardware read.** |
| `POST /api/adau1860/register` | `addr` (int, default `0`), `value` (int, default `0`) | **Dangerous.** Arbitrary raw register write, no allow-list, no range check. An empty body silently writes `addr=0, value=0`. Strings like `"0x10"` are rejected — send integers. |
| `POST /api/adau1860/dmic_meter/start` / `/stop` | — | passthrough |
| `GET /api/adau1860/dmic_levels` | — | passthrough |
| `POST /api/adau1860/loopback/start` | — | **~5 s record + audible playback** |
| `GET /api/adau1860/loopback/status` | — | passthrough |

Errors: `400 {"ok": false, "error": "invalid db value"|"invalid percent value"|
"missing 'db' or 'percent'"|"mode must be 'headphone' or 'lineout'"|
"invalid address"|"invalid addr or value"}`.

---

## Vivado hardware server

These endpoints drive **two** servers from one release, started and stopped
together so a ChipScope session and a JTAG session always agree on the
toolchain:

| Server | Port | Binary | Log | Marker |
|---|---:|---|---|---|
| `hw_server` | 3121 | `<tools>/HWSRVR/bin/hw_server` | `/var/log/rave-menu-hw-server.log` | `/run/rave-menu/hw-server.json` |
| `cs_server` (ChipScope) | 3042 | `<tools>/HWSRVR/bin/cs_server` | `/var/log/rave-menu-cs-server.log` | `/run/rave-menu/cs-server.json` |

Every response in this blueprint carries the same status payload: `running`,
`pid`, `port`, `version`, `binary`, `cs_running`, `cs_pid`, `cs_port`,
`cs_binary`, `cs_log`, `base_version`, `selected_version`,
`effective_version`, `effective_source`, `warnings`, `installed_versions`,
`versions`, `ips` (`[{"iface", "addr"}]`, populated only while running) and
`log` (`/var/log/rave-menu-hw-server.log`).

`cs_server` is **best-effort**: if it fails to start, `hw_server` is left
running and the reason is returned in `cs_error` with `ok` still `true`. There
is no endpoint to run one without the other.

### `GET /api/hw-server/status`
No input, no `ok` key. Reconciles in-memory state against the on-disk markers
and enumerates IPv4 addresses. Always `200`.

### `POST /api/hw-server/start`
**Body:** `version` (string, optional) — falls back to `?version=`, then the
global toolchain selection.

**Spawns `hw_server` and then `cs_server`, both detached
(`start_new_session=True`)**, logging to their own files and writing their own
markers. `cs_server` is given `-s TCP::3042` explicitly so the reported
`cs_port` stays correct if a release changes the default; it is deliberately
**not** given `-d`, which would daemonise and leave no pid to track.
**Not lock-guarded.**

> `start_new_session=True` detaches the session but not the cgroup, and the
> unit runs with the default `KillMode=control-group` — so `systemctl restart
> rave-menu` **does** take the hardware server down with it. The marker lives
> on tmpfs and cannot outlive a reboot. Reconciliation exists for servers the
> menu never started, not for orphans of its own.

`400 {"ok": false, "error": "invalid version"}`;
`409 {..., "ok": false, "error": "not_installed", "version": <v|null>, "message": "..."}`;
`500 {"ok": false, "error": "cannot open log file: <exc>"|"failed to start hw_server: <exc>"}`.
Already running → `200 {"ok": true, ..., "message": "already running"}`.
Success: `{"ok": true, ..., "message": "hw_server <version> started"}`

### `POST /api/hw-server/stop`
No body. **`SIGTERM` to the process group, escalating to `SIGKILL` after 5 s**,
for `cs_server` and then `hw_server`, then clears both markers. **This
disconnects any remote Vivado or ChipScope session.**

**Probes the bulk lock** → `409 {"ok": false, "error": "busy", "message": "a
programming operation is running; stopping the hardware server now would
interrupt it"}`.

Success: `{"ok": true, ..., "stopped_server": {"version", "pid"} | {},
"message": "hw_server stopped"|"not running"}`

---

## Programming toolchain selection

One global, reboot-sticky choice of Versal toolchain release governs both
programming and the hardware server. `xsdb` cannot drive a hardware server from
a different release, so the two ends must always match.

Shared payload keys: `versions`, `installed_versions`, `selected`, `effective`,
`effective_source`, `programming_qualified`, `programming_versions`,
`base_version`, `running`, `warnings`.

`effective` is resolved by a fixed precedence, and `effective_source` names the
rule that won:

| `effective_source` | Rule |
|---|---|
| `override` | `RAVE_VIVADO_VERSION` in the environment, or an explicit request argument. Must be installed or it is ignored with a warning. |
| `running` | The release of the hardware server currently on `:3121`. Outranks the selection because `xsdb` cannot drive a mismatched server. |
| `selection` | The persisted global choice. |
| `base` | `base_version` from the manifest, when nothing is selected. |
| `newest` | Newest installed release, when the base one is absent. |
| `none` | No usable install; `effective` is `null`. |

`warnings` is a list of human-readable strings explaining any divergence — a
selection that is no longer installed, a server overriding the selection, a
release outside `programming_versions`, or a `:3121` owner that could not be
mapped to an install. Empty in the normal case.

### `GET /api/tools/version`
No input. Returns the payload. Always `200`, not lock-guarded.

### `POST /api/tools/version`
**Body:** `version` (string, required).

**Stops the running hardware server and persists the selection.** Re-selecting
the already-selected release is a no-op **only when that selection is also in
effect**; if a different release is serving it is overriding the selection, so
the request proceeds and stops it. The no-op returns early, **before** the busy
check. `cs_server` is stopped alongside `hw_server`.

`400 {"ok": false, "error": "invalid version"}`;
`409 {..., "ok": false, "error": "not_installed", "version": "<v>", "message": "Vivado HW Server <v> is not installed."}`;
`409 {..., "ok": false, "error": "busy", "message": "a programming operation is running; changing the toolchain now would interrupt it"}`;
`500 {..., "ok": false, "error": "could not persist selection"}`.
Success: `{..., "ok": true, "stopped_server": {...}, "message": "programming tools set to <v>"}`,
with `"; <ver> server stopped"` appended when a server was actually stopped.

---

## Vivado installer

**None of these take the bulk lock**, including the install itself — it can race
a programming job.

### `GET /api/vivado/status`
`{"installed", "base_installed", "hw_server", "install_prefix", "base_version",
"default_version", "selected_version", "effective_version", "effective_source",
"versions", "installed_versions", "sources"}`

`installed` means "some release usable for programming is present";
`base_installed` means the qualified base release specifically.

### `GET /vivado-installer-url.png`
**Query:** `version` (default: the manifest default). Returns a QR PNG, or
`404` with the **plain-text** body `QR not available`.

### `POST /api/vivado/upload`
**`multipart/form-data`**, file field `file`. Filename sanitised with
`secure_filename`, defaulting to `upload.tar`. **Overwrites a same-named file.**
Computes a full SHA-256 of the (~250 MiB) tar.

`400 {"ok": false, "error": "no file in request"}`;
`413 {"ok": false, "error": "file exceeds size cap"}` above 4 GiB (the partial
file is deleted); `500 {"ok": false, "error": "save failed: <exc>"}`.
Success: `{"ok": true, "path", "size", "sha256", "sha_ok", "version"}`

### `POST /api/vivado/eulas`
**Body:** `path` (string, required) — absolute path to an installer tar.
Extracts the two EULA files from the tar's top-level directory.

> The path is resolved but **not confined to any directory** — any readable tar
> on the filesystem can be opened.

`400 {"ok": false, "error": "JSON body required"|"missing 'path'"|"tar open failed: <exc>"}`;
`404 {"ok": false, "error": "file not found: <src>"}`;
`422 {"ok": false, "error": "EULA files not found in tar", "missing": [...]}`.
Success: `{"ok": true, "eulas": [{"title", "body"}], "missing": [...]}`

### `POST /api/vivado/install`
**Body:** `path` (required), `version` (default: manifest default), `force`
(bool, default `false`), `accepted_eulas` (bool, default `false`).

**Copies a ~250 MiB tar into the per-version cache and installs a multi-GiB
toolchain.** On success it silently calls `set_selected_version(version)`,
changing the global toolchain selection.

> That commit deliberately does **not** stop a running server, unlike
> `POST /api/tools/version`. Installing a release is not a statement about what
> should be serving. If another release is up it keeps serving and keeps
> winning the `effective_version` precedence, so client and server still match;
> the selection simply takes effect once that server is stopped. This is the
> only route by which `selected` and `effective` can differ through ordinary
> UI use.

Errors, in order: `400 "JSON body required"`; `400 "missing 'path'"`;
`400 {"error": "unknown_version", "version", "known_versions"}`;
`403 {"error": "eulas_not_accepted", "message": "User must accept the EULAs before install."}`;
`404 "file not found: <src>"`;
`409 {"error": "wrong_version", "version", "matched_version", "computed_sha256"}` —
**never overridable by `force`**;
`409 {"error": "sha256_mismatch", "version", "expected_sha256", "computed_sha256"}` —
overridable by `force`;
`500 "cache copy failed: <exc>"|"launch failed: <exc>"`.
Success: `{"ok": true, "job_id": "<uuid>", "version", "sha_ok", "force"}`

---

## App store

Built-in catalog `https://rave2.cicdstorage.com/updates/appstore.json`; user
stores in `/var/lib/rave-menu/app-stores.json` (max 16); bundle cache
`/var/lib/rave-menu/app-cache`. **This blueprint returns bare `{"error": ...}`
with no `ok` key.**

### `GET /api/apps`
Fetches every configured store over the network.
All stores unreachable → `502 {"ok": false, "status": "offline", "error": "...",
"stores": [...], "installed": {...}}`.
Success: `{"ok": true, "status": "online"|"partial", "schema_version": 1,
"apps": [...], "stores": [...], "installed": {...}}`

`installed` is either `{"installed": false}` or `{"installed": true, "id",
"name", "version", "has_versal", "installed_at", "jumbo", "jumbo_gib"}`.

### `GET /api/apps/stores`
`{"stores": [{"root", "label", "color", "default", "removable"}, ...]}`

### `POST /api/apps/stores`
**Body:** `url` (required) — a store root (directory or `.json`). Both `http`
and `https` are accepted; `user:pass@host` is rejected.

`400 {"error": "store must be an absolute http(s):// URL"}`;
`409 {"error": "that store is already configured"}`;
`409 {"error": "at most 16 stores are allowed"}`;
`500 {"error": "could not save the store list"}`.
Success: `{"ok": true, "stores": [...]}`

### `DELETE /api/apps/stores`
**Body:** `url` (required).
`400 {"error": "missing store url"}`;
`403 {"error": "the built-in store cannot be removed"}`;
`404 {"error": "no such store"}`; `500 {"error": "could not save the store list"}`.

### `POST /api/apps/inspect`
**Body:** `source` (required) — an `http(s)://` URL or an absolute local path.

**Fetches a caller-supplied URL server-side with no host restriction** and
streams just the `appimage.yaml` member out of the bundle.

`400 {"error": "missing source"|"source must be an absolute http(s) URL"}`;
`404 {"error": "no such bundle file"}`;
`502 {"error": "could not read the bundle descriptor: <exc>"|"no appimage.yaml at
the head of the bundle"|"the bundle descriptor could not be parsed"}`.
Success: the descriptor JSON plus `available_mib`, `jumbo_installed`,
`ubuntu_installed`, `ubuntu_min_gib`.

### `POST /api/apps/install`
**Body:** `id` (required) — a catalog app id.

**Destructive:** runs the `install-app` job, which downloads and installs an
application image, repartitioning and writing the NVMe app region and, for
bundles with Versal components, programming OSPI. **Lock-guarded.**

`400 {"error": "missing app id"}`;
`502 {"error": "no app stores are reachable", "status": "offline"}`;
`404 {"error": "unknown app id '<id>'"}`;
`502 {"error": "catalog entry has no valid http(s):// url"|"catalog entry has a malformed sha256"}`;
`507 {"error": "insufficient space: ...", "status": "no-space"}`;
`507 {"error": "'<id>' needs at least <n> GiB of jumbo storage; ...", "status": "no-space"}`.
Success: `{"job_id": "<uuid>", "cmd": [...]}`

### `POST /api/apps/install-url`
**Body:** `url` (required), `sha256` (optional, 64 hex characters).

Same destructive install, but **with no catalog entry, no signature check, and
the checksum optional**. `http://` is accepted. Lock-guarded.

`400 {"error": "url must be an absolute http(s):// URL"|"sha256 must be 64 hex characters"}`.

### `POST /api/apps/recover`
Stub. Always `501 {"error": "recovery not implemented yet (Phase 6)"}`.

---

## Serial ports

`{1: /dev/ttyUSB1, 2: /dev/ttyUSB2}` at 115200 baud, 256 KiB of history, 30 s
idle grace. Sessions are **process-global and shared by all clients**. None are
lock-guarded. Any `n` outside the map gives "no such port".

### `POST /api/serial/<int:n>/open`
No body. Opens the device and starts reader/watchdog threads.

`404 {"ok": false, "error": "no such port"}`;
`409 {"ok": false, "error": "<dev> is in use by <name> (pid <pid>), …",
"busy": true, "device": "<dev>", "holders": [{"pid", "name"}]}` — this is the
**device-busy** 409, not the bulk lock;
`500 {"ok": false, "error": "<exc>"}`.
Success: `{"ok": true, "device": "<dev>", "baud": 115200}`

### `POST /api/serial/<int:n>/close`
No body. **Force-closes the shared port, cutting off every other viewer.**
`{"ok": true}`

### `POST /api/serial/<int:n>/keepalive`
No body. Opens the port if closed and resets the idle timer. Same error set as
`/open`.

### `POST /api/serial/<int:n>/write`
**Two body forms.** With a JSON content type: `data` (string, UTF-8 encoded).
Otherwise the **raw request body bytes** are written verbatim.

**Writes arbitrary bytes onto the UART** — i.e. arbitrary commands into
whatever console (U-Boot, Linux, Versal) is on the other end.

`404 {"ok": false, "error": "no such port"}`;
`400 {"ok": false, "error": "port not open"}`; `500 {"ok": false, "error": "<exc>"}`.
Success: `{"ok": true, "wrote": <int>}`

### `GET /api/serial/<int:n>/stream`
**Query:** `replay` — history is replayed unless `replay=0`.

SSE. Auto-opens the port on the first subscriber.

| Frame | Meaning |
|---|---|
| `: connected\n\n` | stream established |
| `data: <json-encoded string>\n\n` | output chunk — JSON-encoded so newlines survive |
| `: ping\n\n` | heartbeat every 1 s of silence |
| `event: busy\ndata: {"error", "device", "holders"}\n\n` | port held by another process |
| `event: status\ndata: closed\n\n` | **terminator** |

Failures here return **plain text**: `404 no such port`,
`500 open failed: <exc>`.

---

## Local console

A single process-global login shell shared by all viewers. **The highest-risk
surface in the API.** None are lock-guarded.

### `POST /api/console/open`
No body. **Allocates a PTY and spawns
`/usr/sbin/runuser -u amd-edf -- /usr/bin/bash -l`** in its own session.
Idempotent while open. `500 {"ok": false, "error": "<exc>"}` /
`{"ok": true, "user": "amd-edf"}`

The user is `RAVE_CONSOLE_USER`, default `amd-edf` — who has `sudo`.

### `POST /api/console/close`
No body. **`SIGHUP` to the process group, escalating to `SIGKILL` after 2 s.**
Disconnects every viewer. Always `{"ok": true}`.

### `POST /api/console/keepalive`
No body. Opens the session if needed and resets the 30 s idle timer.

### `POST /api/console/write`
**Body:** `data` (string, required).

**Arbitrary command execution as `amd-edf`.**

`400 {"ok": false, "error": "data must be a string"|"console not open"}`;
`500 {"ok": false, "error": "<exc>"}`.
Success: `{"ok": true, "wrote": <int>}`

### `POST /api/console/resize`
**Body:** `rows` (int, default `24`), `cols` (int, default `80`), both clamped
to 1–500. `400 {"ok": false, "error": "<exc>"}` / `{"ok": true}`

### `GET /api/console/stream`
No input. Auto-opens the session; a failure returns **JSON**, not SSE.

Frames: `: connected\n\n`, `data: <json string>\n\n`, `: ping\n\n` every 1 s,
and the terminator `event: status\ndata: closed\n\n` when the shell exits.

> **Known defect:** this route returns the generator without
> `mimetype="text/event-stream"`, so Flask serves it as
> `text/html; charset=utf-8` and browser `EventSource` will reject it. The other
> two SSE routes set the mimetype correctly.

---

## Support logs

An append-only record of what has been programmed, installed and changed on the
board, kept on the persistent `rave-data` partition so it survives an A/B
update. Written automatically; there is no endpoint that adds an entry.

The grammar of the log file itself is documented in `AGENTS.md`.

### `GET /api/support-logs`

Status for the System Update page.

Success: `{"ok": true, "enabled": true, "entries": 42, "bytes": 1258291,
"newest": "2026-10-19 14:03", "broken": "", "degraded": false}`

`broken` is non-empty when logging has stopped (no space, I/O error) and names
the reason. `degraded` is true when `rave-data` is not mounted, in which case
records are going to `/var/lib/rave-menu/support-degraded/` and **will not**
survive a system update.

### `POST /api/support-logs`

**Body:** `enabled` (bool, required).

Turns recording on or off. The transition is always written to the log, so a
gap in the file is explainable rather than looking like data loss. There is no
way to erase the log through the API; it is bounded by rotation instead.

Errors: `400 {"ok": false, "error": "Provide 'enabled'"}`

Success: the same body as `GET /api/support-logs`.

### `GET /api/support-logs/download`

Streams a `.tar.gz` named `buddy-support-<hostname>-<timestamp>.tar.gz`
containing `context.txt` (board identity), every log generation,
`hardware-report.txt`, `video-report.txt` and `storage-report.txt` (bulk
storage status and reason, block devices, partition tables, mounts and the
`rave-storage.service` journal).

This can be a large response — the log is capped at roughly 192 MiB before
compression. One archive is built at a time.

Errors: `429 {"ok": false, "error": "another support archive is being built"}`

---

## Destructive endpoint index

Call none of these without explicit human confirmation.

**Disrupts the local display:** `POST /api/localization/keyboard`,
`POST /api/localization/revert` (both blank the appliance's screen for several
seconds and relaunch the kiosk), `POST /api/localization/language` (relaunches
the browser only)

**Power and boot:** `POST /api/poweroff`, `POST /api/reboot`,
`POST /api/reboot-to`, `POST /api/install-nvme`, `POST /api/boot-default`,
`POST /api/boot-timeout`

**Writes non-volatile memory:** `POST /api/run/board-test`,
`POST /api/run/versal-jtag`, `POST /api/run/bios-update`,
`POST /api/run/install-app`, `POST /api/run/install-ubuntu`,
`POST /api/run/uninstall-ubuntu`, `POST /api/install-update`,
`POST /api/apps/install`, `POST /api/apps/install-url`,
`POST /api/vivado/install`

**Deletes data:** `POST /api/uploads/clear`, `POST /api/uploads/delete`,
`PUT /api/upload-payload` (overwrites)

**Arbitrary code or register access:** `POST /api/console/write`,
`POST /api/serial/<n>/write`, `POST /api/adau1860/register`,
`POST /api/audio/ssdt/load`, `POST /api/audio/ssdt/unload`

**Disconnects sessions:** `POST /api/hw-server/stop`, `POST /api/tools/version`,
`POST /api/console/close`, `POST /api/serial/<n>/close`,
`POST /api/network/toggle`, `POST /api/network/disconnect`,
`POST /api/network/configure_ipv4`, `POST /api/network/interface_power`

**Mounts filesystems as a side effect:** `GET /api/rauc-status`,
`GET /api/browse?source=ubuntu`

**Resets the Versal, interrupting whatever it is running:**
`POST /api/versal/layout/scan`, `POST /api/versal/luns/scan`,
`POST /api/versal/luns/apply`, `POST /api/run/reconfigure-versal`

**Destroys all data on the Versal's UFS, leaving it unbootable:**
`POST /api/versal/luns/apply` with `confirm: true`
