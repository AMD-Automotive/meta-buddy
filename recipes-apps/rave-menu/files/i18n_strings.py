# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Strings the client-side scripts build at runtime.

Declared in Python so `pybabel extract` sees them with the Jinja strings; the
JS files themselves are not scanned, which keeps one catalogue as the single
source instead of two that drift.

Separate module so both app.py (which injects the common set into every
template) and routes_pages.py (which adds page-specific ones) can use it
without importing each other.
"""


def _gettext():
    try:
        from flask_babel import gettext
        return gettext
    except ImportError:  # pragma: no cover - image without flask-babel
        return lambda text: text


def common():
    """Used by the shared confirm dialog in app.js, so needed on every page."""
    t = _gettext()
    return {
        "Please confirm": t("Please confirm"),
        "Continue": t("Continue"),
        "Cancel": t("Cancel"),
        "Reboot": t("Reboot"),
        "Reboot the Versal device?": t("Reboot the Versal device?"),
        "Reprogramming the Versal OSPI/UFS will erase current contents. Are you sure?":
            t("Reprogramming the Versal OSPI/UFS will erase current contents. "
              "Are you sure?"),
        "Copied!": t("Copied!"),
        "Loading…": t("Loading…"),
        # UFS Layout tab in the Versal lab modal, built at runtime by lab.js.
        "Reading the LUN layout from the target…": t("Reading the LUN layout from the target…"),
        "Could not read the LUN layout.": t("Could not read the LUN layout."),
        "Target read. Nothing has been changed.": t("Target read. Nothing has been changed."),
        "LUN scan failed: ": t("LUN scan failed: "),
        "BOOT": t("BOOT"),
        "used": t("used"),
        "New layout:": t("New layout:"),
        "remainder": t("remainder"),
        "remainder assigned": t("remainder assigned"),
        "unallocated": t("unallocated"),
        "nothing left for the remainder LUN": t("nothing left for the remainder LUN"),
        "over capacity by ": t("over capacity by "),
        "Re-provisioning the UFS device…": t("Re-provisioning the UFS device…"),
        "Re-provisioning failed.": t("Re-provisioning failed."),
        "Re-provisioning failed: ": t("Re-provisioning failed: "),
        "Erase and re-provision": t("Erase and re-provision"),
        "boot LUN": t("boot LUN"),
        "boot": t("boot"),
        "Mark this LUN as a UFS boot LUN. Optional: the Versal boots from OSPI and does not use them.":
            t("Mark this LUN as a UFS boot LUN. Optional: the Versal boots "
              "from OSPI and does not use them."),
        "allocation unit": t("allocation unit"),
        "allocatable": t("allocatable"),
        "PROVISIONING LOCKED: this device's layout can never be changed":
            t("PROVISIONING LOCKED: this device's layout can never be changed"),
        "UFS re-provisioned. The Versal will not boot until an image is programmed from the Flash Program tab.":
            t("UFS re-provisioned. The Versal will not boot until an image is "
              "programmed from the Flash Program tab."),
        "This re-provisions the Versal's UFS device. All data on it, including the root filesystem, becomes unrecoverable. The board will not boot afterwards until you program an image.":
            t("This re-provisions the Versal's UFS device. All data on it, "
              "including the root filesystem, becomes unrecoverable. The "
              "board will not boot afterwards until you program an image."),
        "This restores the factory LUN layout for this device. All data on the UFS device becomes unrecoverable. The board will not boot afterwards until you program an image.":
            t("This restores the factory LUN layout for this device. All "
              "data on the UFS device becomes unrecoverable. The board "
              "will not boot afterwards until you program an image."),
    }


def system_update():
    """Extra strings for the system update page, merged over common()."""
    t = _gettext()
    strings = common()
    strings.update({
        "No entries yet.": t("No entries yet."),
        "{n} entries": t("{n} entries"),
        "last entry": t("last entry"),
        "Could not read the support log status.": t("Could not read the support log status."),
        "Could not change the setting.": t("Could not change the setting."),
        "Recording is off.": t("Recording is off."),
        "Logging stopped: ": t("Logging stopped: "),
        "Persistent storage is unavailable, so these logs will not survive a system update.":
            t("Persistent storage is unavailable, so these logs will not "
              "survive a system update."),
    })
    return strings


def localisation():
    """Extra strings for the localisation page, merged over common()."""
    t = _gettext()
    strings = common()
    strings.update({
        "Default": t("Default"),
        "(not installed)": t("(not installed)"),
        "No matching layouts": t("No matching layouts"),
        "Reverting automatically in {n}s.": t("Reverting automatically in {n}s."),
        "Reverting…": t("Reverting…"),
        "Apply the {layout} keyboard layout?": t("Apply the {layout} keyboard layout?"),
        "The display on the appliance will go blank for a few seconds. If the new layout is unusable it reverts automatically after {n} seconds.":
            t("The display on the appliance will go blank for a few seconds. "
              "If the new layout is unusable it reverts automatically after {n} seconds."),
        "request failed": t("request failed"),
    })
    return strings
