# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Page routes -- serve HTML templates.

Each route renders a full page.  No API logic here.
"""

from flask import Blueprint, abort, redirect, render_template, url_for
from helpers import get_boot_default, get_rauc_version, read_displays, read_hw_info, running_from_nvme

import localization
import i18n_strings

try:
    from flask_babel import gettext as _
except ImportError:  # pragma: no cover - image without flask-babel
    def _(text, **kwargs):
        return text % kwargs if kwargs else text

pages = Blueprint("pages", __name__)

# A redirect written by the API apply path is only interesting for the few
# seconds it takes the kiosk to come back; past that it is stale state that
# would ambush whoever next opens the menu.
VERIFY_MAX_AGE_SECONDS = 120


def _consume_verify_flag():
    """True exactly once after a localisation change restarted the kiosk."""
    import json
    import time

    try:
        raw = localization.VERIFY_FLAG.read_text(encoding="utf-8")
        localization.VERIFY_FLAG.unlink()
    except OSError:
        return False
    try:
        age = time.time() - float(json.loads(raw).get("at", 0))
    except (ValueError, AttributeError):
        return False
    return 0 <= age <= VERIFY_MAX_AGE_SECONDS


@pages.get("/")
def index():
    if _consume_verify_flag():
        return redirect(url_for("pages.localisation_page", verify=1))
    on_nvme = running_from_nvme()
    boot = get_boot_default()
    return render_template(
        "index.html",
        show_install_nvme=not on_nvme,
        show_system_update=on_nvme,
        menu_version=get_rauc_version(),
        ubuntu_installed=boot["installed"],
        ubuntu_is_default=(boot["default"] == "ubuntu"),
        hw_info=read_hw_info(),
        displays=read_displays(),
    )


@pages.get("/board-test")
def board_test_page():
    return render_template("board_test.html")


@pages.get("/power-off")
def power_off_page():
    return render_template("power_off.html")


@pages.get("/install-nvme")
def install_nvme_page():
    if running_from_nvme():
        abort(404, "Already running from NVMe")
    return render_template("install_nvme.html")


@pages.get("/app-store")
def app_store_page():
    return render_template(
        "app_store.html",
        ubuntu_installed=get_boot_default()["installed"],
    )


@pages.get("/ubuntu-install")
def ubuntu_install_page():
    return render_template(
        "ubuntu_install.html",
        on_nvme=running_from_nvme(),
        ubuntu_installed=get_boot_default()["installed"],
    )


@pages.get("/ubuntu-uninstall")
def ubuntu_uninstall_page():
    return render_template(
        "ubuntu_uninstall.html",
        on_nvme=running_from_nvme(),
        ubuntu_installed=get_boot_default()["installed"],
    )


@pages.get("/system-update")
def system_update_page():
    return render_template("system_update.html",
                           menu_version=get_rauc_version(),
                           js_strings=i18n_strings.system_update())


@pages.get("/vivado-install")
def vivado_install_page():
    return render_template("vivado_install.html")


@pages.get("/audio")
def audio_page():
    return render_template("audio.html")


@pages.get("/localisation")
def localisation_page():
    return render_template("localisation.html",
                           current=localization.load_config(),
                           js_strings=i18n_strings.localisation())


@pages.get("/plugins")
def plugins_page_moved():
    """The plugins page became localisation; keep the documented route alive."""
    return redirect(url_for("pages.localisation_page"), code=301)


@pages.get("/audio/developer")
def audio_developer_page():
    return render_template("audio_developer.html")
