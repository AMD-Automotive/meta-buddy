#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""RAVE2 Buddy -- Flask application entry point.

This file creates the Flask app and registers all route blueprints.
The actual route handlers and business logic live in dedicated modules:

    helpers.py           -- shared constants, subprocess wrapper, sysfs reader
    network.py           -- ConnMan / ip / iw wrappers, network snapshots
    board_test_runner.py -- serial-based hardware test orchestration
    routes_pages.py      -- HTML page routes (/, /board-test, etc.)
    routes_actions.py    -- script execution, SSE streaming, file browser, power
    routes_board_test.py -- board test start/status/rerun/log API
    routes_network.py    -- WiFi/ethernet scan/connect/configure API

See README.md for architecture overview.
"""

from flask import Flask, jsonify, request, send_from_directory
from helpers import APP_ROOT

# Shipped by the buddy-docs recipe; served here so the copy on disk is the copy
# on the wire and the two cannot drift.
DOCS_DIR = "/usr/share/doc/buddy"
# Flask appends charset=utf-8 to any text/* mimetype, so do not spell it here.
MARKDOWN = "text/markdown"

# llms.txt (llmstxt.org) is the closest thing to a discovery convention for
# agent-readable docs; the Link header is how it is advertised on non-HTML
# responses.
DOCS_LINK_HEADER = (
    '</api.md>; rel="alternate"; type="text/markdown", '
    '</llms.txt>; rel="describedby"'
)

app = Flask(
    __name__,
    static_folder=str(APP_ROOT / "static"),
    template_folder=str(APP_ROOT / "templates"),
)

# -- Internationalisation ----------------------------------------------------
# Source strings are British English, matching the factory en_GB.UTF-8 locale.
# The selector follows the configured system language rather than the browser's
# Accept-Language so the remote view matches what the kiosk is showing; the
# ?lang= override exists so a field engineer is not stuck in a language they
# cannot read. Guarded because a missing Flask-Babel must degrade to English,
# not take the whole menu down.
import localization as _localization

app.config["BABEL_DEFAULT_LOCALE"] = "en_GB"
app.config["BABEL_TRANSLATION_DIRECTORIES"] = str(APP_ROOT / "translations")

SUPPORTED_UI_LOCALES = _localization.ui_locales()


def _select_locale():
    try:
        configured = _localization.load_config()["language"]
    except Exception:  # noqa: BLE001 - never block a page render on config
        configured = None
    return _localization.resolve_ui_locale(
        requested=request.args.get("lang") or request.cookies.get("rave_lang"),
        configured=configured)


try:
    from flask_babel import Babel

    babel = Babel(app, locale_selector=_select_locale)
except ImportError:  # pragma: no cover - only on an image without flask-babel
    babel = None

    @app.template_global("_")
    def _passthrough(text, **kwargs):
        return text % kwargs if kwargs else text


@app.context_processor
def inject_locale():
    import i18n_strings

    return {"active_locale": _select_locale(), "js_strings": i18n_strings.common()}


# -- Cross-site request protection -------------------------------------------
# The menu has no authentication, so any page a browser on this network visits
# can POST here. That already mattered for /api/poweroff; it matters more now
# that a request can write settings which survive a reboot and are carried into
# the next slot by the RAUC hook. Origin/Sec-Fetch-Site costs nothing, needs no
# session or token, and covers every endpoint at once. Requests with no Origin
# at all (curl, scripts, the kiosk's own probes) are not browser-driven and are
# left alone.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.before_request
def block_cross_site_writes():
    if request.method in SAFE_METHODS:
        return None

    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site and fetch_site not in ("same-origin", "same-site", "none"):
        return jsonify({"ok": False, "error": "cross-site request blocked"}), 403

    origin = request.headers.get("Origin")
    if origin:
        from urllib.parse import urlsplit

        if urlsplit(origin).netloc != request.host:
            return jsonify({"ok": False,
                            "error": "cross-site request blocked"}), 403
    return None


@app.get("/healthz")
def healthz():
    """Readiness probe for rave-cog-launch.sh.

    The launcher used to poll "/", which rendered the whole menu up to sixty
    times per boot and, because urlopen follows redirects, would also consume
    the one-shot post-restart redirect meant for the browser.
    """
    return jsonify({"ok": True})


@app.after_request
def advertise_docs(resp):
    resp.headers.setdefault("Link", DOCS_LINK_HEADER)
    return resp


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "favicon.ico",
                               mimetype="image/vnd.microsoft.icon")


@app.route("/llms.txt")
def llms_txt():
    return send_from_directory(DOCS_DIR, "llms.txt", mimetype="text/plain")


@app.route("/AGENTS.md")
def agents_md():
    return send_from_directory(DOCS_DIR, "AGENTS.md", mimetype=MARKDOWN)


@app.route("/api.md")
def api_md():
    return send_from_directory(DOCS_DIR, "api.md", mimetype=MARKDOWN)

# Register route blueprints
from routes_pages import pages
from routes_actions import actions
from routes_board_test import board_test
from routes_network import net
from routes_audio import audio_bp
from routes_hw_server import hw_server_bp
from routes_serial import serial_bp
from routes_console import console_bp
from routes_vivado_install import vivado_bp
from routes_tools import tools_bp
from routes_adau1860 import adau1860_bp
from routes_app_store import app_store_bp
from routes_versal_layout import versal_layout_bp
from routes_versal_luns import versal_luns_bp
from routes_localization import localization_bp
from routes_support_log import support_logs_bp

app.register_blueprint(pages)
app.register_blueprint(actions)
app.register_blueprint(board_test)
app.register_blueprint(net)
app.register_blueprint(audio_bp)
app.register_blueprint(hw_server_bp)
app.register_blueprint(serial_bp)
app.register_blueprint(console_bp)
app.register_blueprint(vivado_bp)
app.register_blueprint(tools_bp)
app.register_blueprint(adau1860_bp)
app.register_blueprint(app_store_bp)
app.register_blueprint(versal_layout_bp)
app.register_blueprint(versal_luns_bp)
app.register_blueprint(localization_bp)
app.register_blueprint(support_logs_bp)

if __name__ == "__main__":
    import support_log

    # Marks the start of a boot in the support log, so a record left without an
    # end line can be attributed to the boot that interrupted it.
    support_log.note_boot()
    app.run(host="0.0.0.0", port=80, threaded=True)
