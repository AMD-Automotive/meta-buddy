<!--
Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
SPDX-License-Identifier: MIT
Contributors: KC
Maintainer: KC
-->

# rave-menu

Web-based board menu for the RAVE2 5050A recovery USB image.
Runs as a Flask service on port 80, served by the `rave-menu` systemd unit.

## Architecture

```
app.py                    ← entry point: creates Flask app, registers blueprints
helpers.py                ← shared constants (APP_ROOT, SCRIPTS_DIR, ACTIONS, …),
                            run_cmd(), sysfs_read(), running_from_nvme()
network.py                ← ConnMan / ip / iw wrappers, service parsing,
                            network_snapshot(), WiFi agent connect
board_test_runner.py      ← serial-based hardware test orchestration (unchanged)

routes_pages.py           ← page routes: /, /board-test, /power-off, /install-nvme, /app-store
routes_actions.py         ← /api/run/<action>, /api/stream/<job_id>, /api/browse,
                            /api/install-nvme, /api/poweroff, /api/reboot
routes_board_test.py       ← /api/board-test/start|status|rerun|log
routes_network.py          ← /api/network/services|scan|toggle|connect|disconnect|configure_ipv4
routes_versal_layout.py    ← /api/versal/layout/scan
versal_partitions.py       ← GPT parsing, image head fetch, partial-update planning

routes_localization.py     ← /api/localization/*  (keyboard layout, language)
localization.py            ← XKB catalogue parsing, allow-list validation,
                             config load/save, weston.ini rendering. No Flask
                             import, so it is testable on a build host.
apply_localization.py      ← renders the config into /run/rave-menu/weston.ini,
                             /etc/default/rave-localization, locale.conf and
                             vconsole.conf. Run at boot by
                             rave-localization.service and on every change.
revert_localization.py     ← auto-revert entry point for the confirm timer
i18n_strings.py            ← strings the client-side scripts build at runtime.
                             Declared in Python so pybabel extracts them with
                             the Jinja strings instead of needing a second
                             scanner for .js that would drift.

static/
  terminal.js             ← runAction(), tab management, SSE streaming, terminal modal
  network.js              ← fetchJson(), network modal IIFE (WiFi/ethernet forms)
  file_browser.js         ← file browser IIFE for USB media and an installed Ubuntu
  app.js                  ← menu button click handlers, custom URL card, autorun
  board_test.js           ← board test UI
  board_test.css          ← board test styles
  localization.js         ← localisation page: layout picker, search, confirm timer
  localization.css        ← localisation page styles
  style.css               ← main styles

templates/
  base.html               ← shared layout: <html lang>/dir from the active
                            locale, head, topbar, and the i18n data block.
                            Every page extends this.
  index.html              ← main menu page
  board_test.html         ← board test page
  power_off.html          ← power-off confirmation
  install_nvme.html       ← NVMe installer confirmation
  app_store.html          ← app store placeholder
  localisation.html       ← keyboard layout and language settings

translations/             ← gettext catalogues (de, el, es, fr, it). Source
                            strings are British English and act as the msgids,
                            so there is no en_GB catalogue. The recipe compiles
                            .po to .mo at build time and ships only the .mo.
babel.cfg                 ← pybabel extraction map (Jinja + Python)

scripts/                  ← shell scripts executed by /api/run/<action>
```

## API Endpoints

| Method | Path                          | Description                        |
|--------|-------------------------------|------------------------------------|
| GET    | /                             | Main menu page                     |
| GET    | /board-test                   | Board test page                    |
| GET    | /power-off                    | Power-off confirmation page        |
| GET    | /install-nvme                 | NVMe install confirmation page     |
| GET    | /app-store                    | App store page                     |
| POST   | /api/run/\<action\>           | Start script, returns job_id       |
| GET    | /api/stream/\<job_id\>        | SSE stream of script output        |
| GET    | /api/browse?source=&path=...  | Browse USB media or Ubuntu         |
| GET    | /api/browse-sources           | List browsable sources             |
| POST   | /api/install-nvme             | Set GRUB default=install & reboot  |
| POST   | /api/poweroff                 | Power off the board                |
| POST   | /api/reboot                   | Reboot the board                   |
| POST   | /api/board-test/start         | Start hardware test suite          |
| GET    | /api/board-test/status        | Current test suite snapshot        |
| POST   | /api/board-test/rerun/\<id\>  | Re-run a single test               |
| GET    | /api/board-test/log/\<id\>    | Get log output for a test          |
| GET    | /api/network/services         | Network snapshot (services, techs) |
| POST   | /api/network/scan             | Trigger ConnMan scan               |
| POST   | /api/network/toggle           | Enable/disable technology          |
| POST   | /api/network/connect          | Connect to a service               |
| POST   | /api/network/disconnect       | Disconnect from a service          |
| POST   | /api/network/configure_ipv4   | Set DHCP or manual IPv4            |
| POST   | /api/versal/layout/scan       | Read target GPT, plan partial write |

## Development

Deploy to target:
```bash
scp -o StrictHostKeyChecking=no *.py static/* templates/* amd-edf@192.168.0.78:/tmp/
ssh -o StrictHostKeyChecking=no amd-edf@192.168.0.78 \
  "sudo cp /tmp/*.py /opt/rave-menu/ && \
   sudo cp /tmp/*.js /tmp/*.css /opt/rave-menu/static/ && \
   sudo cp /tmp/*.html /opt/rave-menu/templates/ && \
   sudo systemctl restart rave-menu"
```
