# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

SUMMARY = "RAVE2 local web menu + script launcher"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "\
    file://app.py \
    file://helpers.py \
    file://network.py \
    file://routes_pages.py \
    file://routes_actions.py \
    file://routes_board_test.py \
    file://routes_network.py \
    file://routes_audio.py \
    file://routes_hw_server.py \
    file://routes_serial.py \
    file://routes_console.py \
    file://routes_vivado_install.py \
    file://routes_tools.py \
    file://routes_adau1860.py \
    file://routes_app_store.py \
    file://routes_versal_layout.py \
    file://routes_versal_luns.py \
    file://routes_localization.py \
    file://localization.py \
    file://support_log.py \
    file://routes_support_log.py \
    file://i18n_strings.py \
    file://apply_localization.py \
    file://revert_localization.py \
    file://localization.defaults.json \
    file://babel.cfg \
    file://translations/messages.pot \
    file://translations/de/LC_MESSAGES/messages.po \
    file://translations/el/LC_MESSAGES/messages.po \
    file://translations/fr/LC_MESSAGES/messages.po \
    file://translations/es/LC_MESSAGES/messages.po \
    file://translations/it/LC_MESSAGES/messages.po \
    file://vivado_installer.py \
    file://audio.py \
    file://adau1860.py \
    file://pipewire.py \
    file://audio_targets.py \
    file://dmic_meter.py \
    file://nau8822_meter.py \
    file://acpi_ssdt.py \
    file://board_test_runner.py \
    file://descriptor_tool.py \
    file://app_slots.py \
    file://app_region.py \
    file://versal_partitions.py \
    file://templates/index.html \
    file://templates/base.html \
    file://templates/_versal_lab.html \
    file://templates/_serial_console.html \
    file://templates/_terminal_modal.html \
    file://templates/board_test.html \
    file://templates/power_off.html \
    file://templates/app_store.html \
    file://templates/install_nvme.html \
    file://templates/ubuntu_install.html \
    file://templates/ubuntu_uninstall.html \
    file://templates/system_update.html \
    file://templates/audio.html \
    file://templates/vivado_install.html \
    file://templates/audio_developer.html \
    file://templates/localisation.html \
    file://static/style.css \
    file://static/lab.css \
    file://static/terminal.js \
    file://static/network.js \
    file://static/file_browser.js \
    file://static/app.js \
    file://static/lab.js \
    file://static/serial_console.js \
    file://static/vendor/xterm/xterm.js \
    file://static/vendor/xterm/xterm.css \
    file://static/vendor/xterm/xterm-addon-fit.js \
    file://static/board_test.css \
    file://static/board_test.js \
    file://static/app_store.css \
    file://static/app_store.js \
    file://static/audio.css \
    file://static/vivado_install.css \
    file://static/audio.js \
    file://static/chime.wav \
    file://static/vivado_install.js \
    file://static/adau1860.js \
    file://static/adau1860.css \
    file://static/localization.css \
    file://static/localization.js \
    file://static/vivado_gate.js \
    file://static/bios_update.js \
    file://static/support_logs.js \
    file://static/app-store-reset.png \
    file://static/app-store-video.png \
    file://static/buddy_logo.svg \
    file://static/buddy_logo.png \
    file://static/favicon.svg \
    file://static/favicon.ico \
    file://static/apple-touch-icon.png \
    file://static/bg-futuristic-4k.png \
    file://scripts/program_remote_versal_payload.sh \
    file://scripts/scan_versal_layout.sh \
    file://scripts/scan_versal_luns.sh \
    file://scripts/provision_versal_luns.sh \
    file://scripts/restore_versal_ospi.sh \
    file://scripts/program_jtag_versal.sh \
    file://scripts/vivado_env.sh \
    file://scripts/launch_app_store.sh \
    file://scripts/reconfigure_versal.sh \
    file://scripts/vivado_install.sh \
    file://scripts/install_ubuntu.sh \
    file://scripts/uninstall_ubuntu.sh \
    file://scripts/install_app_bundle.sh \
    file://scripts/report_hw_info.sh \
    file://scripts/report_video_status.py \
    file://scripts/setup_bulk_storage.sh \
    file://scripts/program_bios_update.sh \
    file://rave-menu.service \
    file://rave-storage.service \
    file://rave-hwinfo.service \
    file://rave-localization.service \
    file://weston.ini \
    file://weston.ini.in \
    file://weston.service.d/10-rave-menu.conf \
    file://weston.service.d/20-rave-gpu-wait.conf \
    file://weston.service.d/30-rave-localization.conf \
    file://rave-cog-launch.sh \
    https://mkmartifactory.amd.com:8443/artifactory/SW-AutomotiveDevOps-DEV-LOCAL/ERICA-H/meta-erica-edf/353e23_fe5f25___amd-cortexa78-mali-common_2025.2-rave2-test-image.wic.ufs.gz;name=boardtest-wic;downloadfilename=boardtest-wic.ufs.gz \
    https://mkmartifactory.amd.com:8443/artifactory/SW-AutomotiveDevOps-DEV-LOCAL/ERICA-H/meta-erica-edf/353e23_fe5f25___rave2-baseline_2025.2-edf-ospi.bootbin.tar.gz;name=boardtest-bootbin;downloadfilename=boardtest-bootbin.tar.gz;subdir=boardtest-bootbin \
"
SRC_URI[boardtest-wic.sha256sum] = "850554d18348a92c7aa739ea56e3ab0ee8b11a3ca096ca2bd7c1ffbb736ed119"
SRC_URI[boardtest-bootbin.sha256sum] = "c9c236675fb5b406a8833f2afab39a46bb66af3d0f7c3295199c5ff0c522c82c"

# Title logo shown in the menu top bar. By default buddy_logo.svg (bundled) is
# used. To substitute a different logo at build time set:
#   RAVE_MENU_LOGO     — filename of the logo (must match what RAVE_MENU_LOGO_URI fetches)
#   RAVE_MENU_LOGO_URI — URL to fetch the logo from (plain URL, no BitBake params)
#   RAVE_MENU_LOGO_SHA256 — sha256sum of the fetched file
# The fetched file lands in ${WORKDIR}/static/ via subdir=static so do_install
# finds it at the same path regardless of which logo is active.
RAVE_MENU_LOGO ?= "buddy_logo.png"
RAVE_MENU_LOGO_URI ?= ""
RAVE_MENU_LOGO_SHA256 ?= ""

SRC_URI += "${@ (d.getVar('RAVE_MENU_LOGO_URI') + ';name=title-logo;subdir=static') \
               if d.getVar('RAVE_MENU_LOGO_URI') else ''}"

python () {
    sha256 = d.getVar('RAVE_MENU_LOGO_SHA256')
    if sha256:
        d.setVarFlag('SRC_URI', 'title-logo.sha256sum', sha256)
}

S = "${WORKDIR}"

inherit systemd

# For msgfmt. Note that `inherit gettext` would NOT do this job: it only adds
# gettext dependencies and --enable-nls handling for autotools recipes, and
# this recipe has no build system, so the catalogues would silently never be
# compiled and Flask-Babel would serve msgids forever.
DEPENDS += "gettext-native"

# Compile any translation catalogues into the .mo files Flask-Babel loads.
# There are none yet -- source strings are British English and serve as the
# msgids -- so this is a no-op until the first catalogue lands. It fails loudly
# rather than shipping a .po with no matching .mo, because that failure mode
# looks exactly like "not translated yet".
do_compile() {
    for po in ${WORKDIR}/translations/*/LC_MESSAGES/*.po; do
        [ -e "$po" ] || continue
        msgfmt -o "${po%.po}.mo" "$po" || bbfatal "msgfmt failed for $po"
    done
}

RDEPENDS:${PN} = "\
    python3-core \
    python3-flask \
    python3-werkzeug \
    python3-jinja2 \
    python3-itsdangerous \
    python3-click \
    python3-difflib \
    python3-pyserial \
    bash \
    buddy-docs \
    chromium-ozone-wayland \
    connman \
    connman-client \
    pipewire \
    pipewire-pulse \
    pipewire-alsa \
    pipewire-tools \
    wireplumber \
    pulseaudio-misc \
    sudo \
    tar \
    vivado-installer-assets \
    e2fsprogs \
    e2fsprogs-mke2fs \
    e2fsprogs-resize2fs \
    e2fsprogs-e2fsck \
    util-linux \
    util-linux-blkid \
    util-linux-swapon \
    util-linux-mkswap \
    util-linux-mount \
    util-linux-umount \
    util-linux-findmnt \
    util-linux-lsblk \
    util-linux-sfdisk \
    util-linux-blkdiscard \
    util-linux-partx \
    parted \
    gzip \
    zstd \
    xz \
    unzip \
    efibootmgr \
    grub-editenv \
    ca-certificates \
    python3-pyyaml \
    versal-reprogram \
    dmidecode \
    python3-flask-babel \
    python3-babel \
    xkeyboard-config \
    kbd \
    xkbcli \
    systemd-extra-utils \
"

# xkbcli test-compiles a candidate keymap before it is committed. Weston exits
# 1 on a keymap it cannot build and is Restart=on-failure, so without this a
# bad layout takes the display down instead of returning an error.
# systemd-extra-utils provides systemd-run, which the deferred restart and the
# revert timer both need; it is only an RRECOMMENDS of systemd, so it has to be
# named explicitly or BAD_RECOMMENDATIONS could drop it.

# Both packages ship a weston.service ExecStart= override and systemd cannot
# merge two resets, so an image may contain only one of them.
RCONFLICTS:${PN} = "weston-config-safed"

SYSTEMD_SERVICE:${PN} = "rave-menu.service rave-storage.service rave-hwinfo.service rave-localization.service"
SYSTEMD_AUTO_ENABLE:${PN} = "enable"

# Written at runtime by apply_localization.py. Declared so rpm leaves a user's
# choice alone on package upgrade.
CONFFILES:${PN} = "${sysconfdir}/locale.conf ${sysconfdir}/vconsole.conf"

do_install() {
    install -d ${D}/opt/rave-menu
    install -d ${D}/opt/rave-menu/templates
    install -d ${D}/opt/rave-menu/static
    install -d ${D}/opt/rave-menu/scripts
    install -d ${D}/var/lib/rave-menu/updates
    install -d ${D}/var/lib/rave-menu/payload-cache
    install -d ${D}/var/lib/rave-menu/vivado/uploads
    install -d ${D}/var/lib/rave-menu/uploads
    install -d ${D}/var/lib/rave-menu/app-cache

    # Python application modules
    install -m 0755 ${WORKDIR}/app.py               ${D}/opt/rave-menu/app.py
    install -m 0644 ${WORKDIR}/helpers.py            ${D}/opt/rave-menu/helpers.py
    install -m 0644 ${WORKDIR}/network.py            ${D}/opt/rave-menu/network.py
    install -m 0644 ${WORKDIR}/routes_pages.py       ${D}/opt/rave-menu/routes_pages.py
    install -m 0644 ${WORKDIR}/routes_actions.py     ${D}/opt/rave-menu/routes_actions.py
    install -m 0644 ${WORKDIR}/routes_board_test.py  ${D}/opt/rave-menu/routes_board_test.py
    install -m 0644 ${WORKDIR}/routes_network.py     ${D}/opt/rave-menu/routes_network.py
    install -m 0644 ${WORKDIR}/routes_audio.py       ${D}/opt/rave-menu/routes_audio.py
    install -m 0644 ${WORKDIR}/routes_hw_server.py   ${D}/opt/rave-menu/routes_hw_server.py
    install -m 0644 ${WORKDIR}/routes_serial.py      ${D}/opt/rave-menu/routes_serial.py
    install -m 0644 ${WORKDIR}/routes_console.py     ${D}/opt/rave-menu/routes_console.py
    install -m 0644 ${WORKDIR}/routes_vivado_install.py ${D}/opt/rave-menu/routes_vivado_install.py
    install -m 0644 ${WORKDIR}/routes_tools.py      ${D}/opt/rave-menu/routes_tools.py
    install -m 0644 ${WORKDIR}/routes_adau1860.py    ${D}/opt/rave-menu/routes_adau1860.py
    install -m 0644 ${WORKDIR}/routes_app_store.py   ${D}/opt/rave-menu/routes_app_store.py
    install -m 0644 ${WORKDIR}/routes_versal_layout.py ${D}/opt/rave-menu/routes_versal_layout.py
    install -m 0644 ${WORKDIR}/routes_versal_luns.py ${D}/opt/rave-menu/routes_versal_luns.py
    install -m 0644 ${WORKDIR}/routes_localization.py ${D}/opt/rave-menu/routes_localization.py
    install -m 0644 ${WORKDIR}/localization.py     ${D}/opt/rave-menu/localization.py
    install -m 0644 ${WORKDIR}/support_log.py      ${D}/opt/rave-menu/support_log.py
    install -m 0644 ${WORKDIR}/routes_support_log.py ${D}/opt/rave-menu/routes_support_log.py
    install -m 0644 ${WORKDIR}/i18n_strings.py    ${D}/opt/rave-menu/i18n_strings.py
    install -m 0755 ${WORKDIR}/apply_localization.py ${D}/opt/rave-menu/apply_localization.py
    install -m 0755 ${WORKDIR}/revert_localization.py ${D}/opt/rave-menu/revert_localization.py
    install -m 0644 ${WORKDIR}/localization.defaults.json ${D}/opt/rave-menu/localization.defaults.json
    install -m 0644 ${WORKDIR}/babel.cfg          ${D}/opt/rave-menu/babel.cfg

    # Ship only the compiled catalogues; the .po files are build inputs.
    install -d ${D}/opt/rave-menu/translations
    for mo in ${WORKDIR}/translations/*/LC_MESSAGES/messages.mo; do
        [ -e "$mo" ] || continue
        lang=$(basename $(dirname $(dirname "$mo")))
        install -d ${D}/opt/rave-menu/translations/$lang/LC_MESSAGES
        install -m 0644 "$mo" ${D}/opt/rave-menu/translations/$lang/LC_MESSAGES/messages.mo
    done
    install -m 0644 ${WORKDIR}/vivado_installer.py   ${D}/opt/rave-menu/vivado_installer.py
    install -m 0644 ${WORKDIR}/audio.py              ${D}/opt/rave-menu/audio.py
    install -m 0644 ${WORKDIR}/adau1860.py           ${D}/opt/rave-menu/adau1860.py
    install -m 0644 ${WORKDIR}/pipewire.py            ${D}/opt/rave-menu/pipewire.py
    install -m 0644 ${WORKDIR}/audio_targets.py       ${D}/opt/rave-menu/audio_targets.py
    install -m 0644 ${WORKDIR}/dmic_meter.py          ${D}/opt/rave-menu/dmic_meter.py
    install -m 0644 ${WORKDIR}/nau8822_meter.py        ${D}/opt/rave-menu/nau8822_meter.py
    install -m 0644 ${WORKDIR}/acpi_ssdt.py          ${D}/opt/rave-menu/acpi_ssdt.py
    install -m 0644 ${WORKDIR}/board_test_runner.py  ${D}/opt/rave-menu/board_test_runner.py
    install -m 0644 ${WORKDIR}/descriptor_tool.py    ${D}/opt/rave-menu/descriptor_tool.py
    install -m 0644 ${WORKDIR}/app_slots.py          ${D}/opt/rave-menu/app_slots.py
    install -m 0644 ${WORKDIR}/app_region.py         ${D}/opt/rave-menu/app_region.py
    install -m 0644 ${WORKDIR}/versal_partitions.py  ${D}/opt/rave-menu/versal_partitions.py

    # HTML templates
    install -m 0644 ${WORKDIR}/templates/index.html        ${D}/opt/rave-menu/templates/index.html
    install -m 0644 ${WORKDIR}/templates/base.html        ${D}/opt/rave-menu/templates/base.html
    install -m 0644 ${WORKDIR}/templates/_versal_lab.html  ${D}/opt/rave-menu/templates/_versal_lab.html
    install -m 0644 ${WORKDIR}/templates/_serial_console.html ${D}/opt/rave-menu/templates/_serial_console.html
    install -m 0644 ${WORKDIR}/templates/_terminal_modal.html ${D}/opt/rave-menu/templates/_terminal_modal.html
    install -m 0644 ${WORKDIR}/templates/board_test.html   ${D}/opt/rave-menu/templates/board_test.html
    install -m 0644 ${WORKDIR}/templates/power_off.html    ${D}/opt/rave-menu/templates/power_off.html
    install -m 0644 ${WORKDIR}/templates/app_store.html    ${D}/opt/rave-menu/templates/app_store.html
    install -m 0644 ${WORKDIR}/templates/ubuntu_install.html ${D}/opt/rave-menu/templates/ubuntu_install.html
    install -m 0644 ${WORKDIR}/templates/ubuntu_uninstall.html ${D}/opt/rave-menu/templates/ubuntu_uninstall.html
    install -m 0644 ${WORKDIR}/templates/install_nvme.html ${D}/opt/rave-menu/templates/install_nvme.html
    install -m 0644 ${WORKDIR}/templates/system_update.html ${D}/opt/rave-menu/templates/system_update.html
    install -m 0644 ${WORKDIR}/templates/audio.html        ${D}/opt/rave-menu/templates/audio.html
    install -m 0644 ${WORKDIR}/templates/vivado_install.html ${D}/opt/rave-menu/templates/vivado_install.html
    install -m 0644 ${WORKDIR}/templates/audio_developer.html ${D}/opt/rave-menu/templates/audio_developer.html
    install -m 0644 ${WORKDIR}/templates/localisation.html ${D}/opt/rave-menu/templates/localisation.html

    # JavaScript
    install -m 0644 ${WORKDIR}/static/terminal.js     ${D}/opt/rave-menu/static/terminal.js
    install -m 0644 ${WORKDIR}/static/network.js      ${D}/opt/rave-menu/static/network.js
    install -m 0644 ${WORKDIR}/static/file_browser.js  ${D}/opt/rave-menu/static/file_browser.js

    # Vendored xterm.js terminal emulator (MIT)
    install -d ${D}/opt/rave-menu/static/vendor/xterm
    install -m 0644 ${WORKDIR}/static/vendor/xterm/xterm.js            ${D}/opt/rave-menu/static/vendor/xterm/xterm.js
    install -m 0644 ${WORKDIR}/static/vendor/xterm/xterm.css           ${D}/opt/rave-menu/static/vendor/xterm/xterm.css
    install -m 0644 ${WORKDIR}/static/vendor/xterm/xterm-addon-fit.js  ${D}/opt/rave-menu/static/vendor/xterm/xterm-addon-fit.js
    install -m 0644 ${WORKDIR}/static/app.js           ${D}/opt/rave-menu/static/app.js
    install -m 0644 ${WORKDIR}/static/lab.js           ${D}/opt/rave-menu/static/lab.js
    install -m 0644 ${WORKDIR}/static/serial_console.js ${D}/opt/rave-menu/static/serial_console.js
    install -m 0644 ${WORKDIR}/static/board_test.js    ${D}/opt/rave-menu/static/board_test.js
    install -m 0644 ${WORKDIR}/static/audio.js         ${D}/opt/rave-menu/static/audio.js
    install -m 0644 ${WORKDIR}/static/vivado_install.js ${D}/opt/rave-menu/static/vivado_install.js
    install -m 0644 ${WORKDIR}/static/adau1860.js      ${D}/opt/rave-menu/static/adau1860.js
    install -m 0644 ${WORKDIR}/static/adau1860.css     ${D}/opt/rave-menu/static/adau1860.css
    install -m 0644 ${WORKDIR}/static/localization.css ${D}/opt/rave-menu/static/localization.css
    install -m 0644 ${WORKDIR}/static/localization.js  ${D}/opt/rave-menu/static/localization.js
    install -m 0644 ${WORKDIR}/static/vivado_gate.js    ${D}/opt/rave-menu/static/vivado_gate.js
    install -m 0644 ${WORKDIR}/static/bios_update.js    ${D}/opt/rave-menu/static/bios_update.js
    install -m 0644 ${WORKDIR}/static/support_logs.js   ${D}/opt/rave-menu/static/support_logs.js

    # CSS
    install -m 0644 ${WORKDIR}/static/style.css        ${D}/opt/rave-menu/static/style.css
    install -m 0644 ${WORKDIR}/static/lab.css          ${D}/opt/rave-menu/static/lab.css
    install -m 0644 ${WORKDIR}/static/board_test.css   ${D}/opt/rave-menu/static/board_test.css
    install -m 0644 ${WORKDIR}/static/app_store.css    ${D}/opt/rave-menu/static/app_store.css
    install -m 0644 ${WORKDIR}/static/app_store.js     ${D}/opt/rave-menu/static/app_store.js
    install -m 0644 ${WORKDIR}/static/audio.css        ${D}/opt/rave-menu/static/audio.css
    install -m 0644 ${WORKDIR}/static/chime.wav         ${D}/opt/rave-menu/static/chime.wav
    install -m 0644 ${WORKDIR}/static/vivado_install.css ${D}/opt/rave-menu/static/vivado_install.css

    # Images
    install -m 0644 ${WORKDIR}/static/app-store-reset.png  ${D}/opt/rave-menu/static/app-store-reset.png
    install -m 0644 ${WORKDIR}/static/app-store-video.png  ${D}/opt/rave-menu/static/app-store-video.png
    install -m 0644 ${WORKDIR}/static/${RAVE_MENU_LOGO}    ${D}/opt/rave-menu/static/title_logo.png
    install -m 0644 ${WORKDIR}/static/buddy_logo.svg       ${D}/opt/rave-menu/static/buddy_logo.svg
    install -m 0644 ${WORKDIR}/static/favicon.svg               ${D}/opt/rave-menu/static/favicon.svg
    install -m 0644 ${WORKDIR}/static/favicon.ico               ${D}/opt/rave-menu/static/favicon.ico
    install -m 0644 ${WORKDIR}/static/apple-touch-icon.png      ${D}/opt/rave-menu/static/apple-touch-icon.png
    install -m 0644 ${WORKDIR}/static/bg-futuristic-4k.png      ${D}/opt/rave-menu/static/bg-futuristic-4k.png

    # Shell scripts
    install -m 0755 ${WORKDIR}/scripts/program_remote_versal_payload.sh ${D}/opt/rave-menu/scripts/program_remote_versal_payload.sh
    install -m 0755 ${WORKDIR}/scripts/scan_versal_layout.sh            ${D}/opt/rave-menu/scripts/scan_versal_layout.sh
    install -m 0755 ${WORKDIR}/scripts/scan_versal_luns.sh              ${D}/opt/rave-menu/scripts/scan_versal_luns.sh
    install -m 0755 ${WORKDIR}/scripts/provision_versal_luns.sh         ${D}/opt/rave-menu/scripts/provision_versal_luns.sh
    install -m 0755 ${WORKDIR}/scripts/restore_versal_ospi.sh           ${D}/opt/rave-menu/scripts/restore_versal_ospi.sh
    install -m 0755 ${WORKDIR}/scripts/program_jtag_versal.sh           ${D}/opt/rave-menu/scripts/program_jtag_versal.sh
    install -m 0644 ${WORKDIR}/scripts/vivado_env.sh                    ${D}/opt/rave-menu/scripts/vivado_env.sh
    install -m 0755 ${WORKDIR}/scripts/launch_app_store.sh              ${D}/opt/rave-menu/scripts/launch_app_store.sh
    install -m 0755 ${WORKDIR}/scripts/reconfigure_versal.sh           ${D}/opt/rave-menu/scripts/reconfigure_versal.sh
    install -m 0755 ${WORKDIR}/scripts/vivado_install.sh                ${D}/opt/rave-menu/scripts/vivado_install.sh
    install -m 0755 ${WORKDIR}/scripts/install_ubuntu.sh                ${D}/opt/rave-menu/scripts/install_ubuntu.sh
    install -m 0755 ${WORKDIR}/scripts/uninstall_ubuntu.sh              ${D}/opt/rave-menu/scripts/uninstall_ubuntu.sh
    install -m 0755 ${WORKDIR}/scripts/install_app_bundle.sh            ${D}/opt/rave-menu/scripts/install_app_bundle.sh
    install -m 0755 ${WORKDIR}/scripts/report_hw_info.sh                ${D}/opt/rave-menu/scripts/report_hw_info.sh
    install -m 0755 ${WORKDIR}/scripts/report_video_status.py           ${D}/opt/rave-menu/scripts/report_video_status.py
    install -m 0755 ${WORKDIR}/scripts/setup_bulk_storage.sh             ${D}/opt/rave-menu/scripts/setup_bulk_storage.sh
    install -m 0755 ${WORKDIR}/scripts/program_bios_update.sh            ${D}/opt/rave-menu/scripts/program_bios_update.sh

    # Systemd service
    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/rave-menu.service ${D}${systemd_system_unitdir}/rave-menu.service
    install -m 0644 ${WORKDIR}/rave-storage.service ${D}${systemd_system_unitdir}/rave-storage.service
    install -m 0644 ${WORKDIR}/rave-hwinfo.service ${D}${systemd_system_unitdir}/rave-hwinfo.service
    install -m 0644 ${WORKDIR}/rave-localization.service ${D}${systemd_system_unitdir}/rave-localization.service

    # Weston configuration.
    #
    # Three files, deliberately: the package owns the template and the static
    # fallback, and the runtime owns the generated config. Nothing is written
    # by both. rave-menu.ini keeps the factory GB keymap and is only used if
    # apply_localization.py failed to produce /run/rave-menu/weston.ini --
    # Weston exits 1 on a missing config, so that path has to stay covered.
    install -d ${D}${sysconfdir}/xdg/weston
    install -m 0644 ${WORKDIR}/weston.ini ${D}${sysconfdir}/xdg/weston/rave-menu.ini
    install -m 0644 ${WORKDIR}/weston.ini.in ${D}/opt/rave-menu/weston.ini.in

    # User localisation choice lives here alone; the RAUC post-install hook
    # copies it into the new slot so a firmware update keeps the setting.
    install -d ${D}${sysconfdir}/rave-menu

    # Factory localisation: British English, GB keyboard. Owned by this package
    # so CONFFILES applies; apply_localization.py rewrites both on every boot
    # from the user's choice. Keep in step with localization.defaults.json.
    echo "LANG=en_GB.UTF-8" > ${D}${sysconfdir}/locale.conf
    echo "KEYMAP=uk" > ${D}${sysconfdir}/vconsole.conf
    chmod 0644 ${D}${sysconfdir}/locale.conf ${D}${sysconfdir}/vconsole.conf

    install -d ${D}${sysconfdir}/systemd/system/weston.service.d
    install -m 0644 ${WORKDIR}/weston.service.d/10-rave-menu.conf \
        ${D}${sysconfdir}/systemd/system/weston.service.d/10-rave-menu.conf
    install -m 0644 ${WORKDIR}/weston.service.d/20-rave-gpu-wait.conf \
        ${D}${sysconfdir}/systemd/system/weston.service.d/20-rave-gpu-wait.conf
    install -m 0644 ${WORKDIR}/weston.service.d/30-rave-localization.conf \
        ${D}${sysconfdir}/systemd/system/weston.service.d/30-rave-localization.conf

    install -d ${D}${bindir}
    install -m 0755 ${WORKDIR}/rave-cog-launch.sh ${D}${bindir}/rave-cog-launch.sh

    # Boardtest firmware
    install -d ${D}${nonarch_base_libdir}/firmware/boardtest
    install -m 0644 ${WORKDIR}/boardtest-wic.ufs ${D}${nonarch_base_libdir}/firmware/boardtest/wic.ufs
    install -m 0644 ${WORKDIR}/boardtest-bootbin/boot.bin ${D}${nonarch_base_libdir}/firmware/boardtest/boot.bin
}

FILES:${PN} += "\
    /opt/rave-menu \
    /var/lib/rave-menu \
    ${sysconfdir}/rave-menu \
    ${nonarch_base_libdir}/firmware/boardtest \
    ${systemd_system_unitdir}/rave-menu.service \
    ${systemd_system_unitdir}/rave-storage.service \
    ${systemd_system_unitdir}/rave-hwinfo.service \
    ${systemd_system_unitdir}/rave-localization.service \
"
