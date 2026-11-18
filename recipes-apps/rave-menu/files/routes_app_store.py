# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""App Store routes -- remote catalog fetch + bundle install (Phase 4).

Endpoints:
    GET  /api/apps             -- remote catalog + which app is installed
    GET  /api/apps/installed   -- installed.json summary (or {installed: false})
    POST /api/apps/install     -- install a catalog app by id (streaming job)
    POST /api/apps/install-url -- manual https:// bundle install (streaming job)
    POST /api/apps/recover     -- (Phase 6) reprogram Versal only -- stub

Design (see docs/app-bundle-installer/phase-4-catalog-and-download.md):
  - The catalog (``appstore.json``) is the *strategy* layer: it carries only
    outer metadata + the whole-file sha256 download gate. It is fetched online
    only -- there is NO on-device fallback catalog. On a fetch failure the
    endpoints surface an ``offline`` status the UI can render.
  - The bundle's own ``appimage.yaml`` is authoritative for component structure
    and is validated by the installer (Phases 1-3). We never branch install
    logic on the catalog's advisory badges (e.g. ``has_versal``).
  - Installs reuse the existing job runner (``routes_actions.start_job``) so the
    SSE streaming/heartbeat machinery is shared; this module just resolves a URL
    (+ sha256) and forwards it as argv to the ``install-app`` action.
"""

import json
import logging
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request

from helpers import (APP_CACHE_DIR, APP_CATALOG_URL, catalog_url_for,
                     find_installed_app, free_bytes, get_jumbo_free_mib,
                     get_region_capacity, load_app_stores, run_cmd,
                     save_app_stores)
from routes_actions import start_job

log = logging.getLogger(__name__)
app_store_bp = Blueprint("app_store", __name__)

CATALOG_TIMEOUT_S = 15
# Reading just the descriptor off the front of a bundle is a few hundred KB, but
# it crosses the network, so it still gets a ceiling.
BUNDLE_PEEK_TIMEOUT_S = 60
DESCRIPTOR_TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "descriptor_tool.py")
# Headroom multiplier applied to the catalog size for the pre-download check.
SPACE_FACTOR = 1.1
# Upper bound on how many extra stores a user may register.
MAX_USER_STORES = 16


def _fetch_one(catalog_url):
    """Fetch + parse a single catalog URL. Returns ``(catalog, error)``.

    ``catalog`` is the parsed dict on success (``error`` empty), or ``None`` on
    failure with ``error`` describing why (unreachable host or invalid JSON).
    There is deliberately no on-disk fallback -- offline means nothing to show.
    """
    if not catalog_url:
        return None, "empty catalog URL"
    try:
        req = urllib.request.Request(
            catalog_url, method="GET",
            headers={"User-Agent": "RAVE2-BUDDY/1.0"})
        with urllib.request.urlopen(req, timeout=CATALOG_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode()), ""
    except (urllib.error.URLError, OSError) as exc:
        return None, f"Cannot reach app catalog: {exc}"
    except (json.JSONDecodeError, ValueError):
        return None, "Invalid catalog format"


def _store_label(root) -> str:
    """A short, human display name for a store root (its host, else the root)."""
    try:
        netloc = urlparse(root).netloc
    except Exception:
        netloc = ""
    return netloc or root


# Distinct accent colours cycled across stores so each store -- and the apps it
# delivers -- can be told apart at a glance. Index 0 is reserved for the
# built-in store; user stores take the rest in the order they were added.
STORE_COLORS = [
    "#7fb2ff",  # built-in: blue
    "#ff9d5c",  # orange
    "#5fd08a",  # green
    "#d78bff",  # violet
    "#ffd35c",  # amber
    "#ff7f9e",  # pink
    "#5fd6d0",  # teal
    "#b6c96b",  # olive
]


def _store_color(index) -> str:
    """Return the accent colour for the store at ``index`` (cycles the palette)."""
    return STORE_COLORS[index % len(STORE_COLORS)]


def _configured_stores() -> list:
    """Ordered stores: the built-in default first, then user-added roots.

    Each entry is ``{"root", "label", "color", "default", "removable"}``.
    ``root`` is the value the user entered (or ``APP_CATALOG_URL`` for the
    built-in); the catalog URL is derived on demand with ``catalog_url_for``.
    """
    stores = [{
        "root": APP_CATALOG_URL,
        "label": _store_label(APP_CATALOG_URL),
        "color": _store_color(0),
        "default": True,
        "removable": False,
    }]
    for i, root in enumerate(load_app_stores(), start=1):
        stores.append({
            "root": root,
            "label": _store_label(root),
            "color": _store_color(i),
            "default": False,
            "removable": True,
        })
    return stores


def _gather_catalog():
    """Fetch every configured store and merge their apps.

    Returns ``(apps, stores, any_ok)`` where ``apps`` is the merged list (each
    app tagged with its ``store``/``store_label``/``store_color``), ``stores`` is
    a per-source status list for the UI, and ``any_ok`` is ``True`` if at least
    one store was reachable. A single unreachable store never sinks the others.
    """
    apps, stores, any_ok = [], [], False
    for store in _configured_stores():
        catalog, error = _fetch_one(catalog_url_for(store["root"]))
        status = {
            "root": store["root"],
            "label": store["label"],
            "color": store["color"],
            "default": store["default"],
            "removable": store["removable"],
            "ok": catalog is not None,
            "error": error,
            "count": 0,
        }
        if catalog is not None:
            any_ok = True
            entries = catalog.get("apps", []) if isinstance(catalog, dict) else []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                tagged = dict(entry)
                tagged["store"] = store["root"]
                tagged["store_label"] = store["label"]
                tagged["store_color"] = store["color"]
                apps.append(tagged)
                status["count"] += 1
        stores.append(status)
    return apps, stores, any_ok


def _is_https_url(url) -> bool:
    """True for an absolute http(s):// URL with no embedded credentials.

    NOTE: ``http`` is accepted only to support the ad-hoc dev catalog/bundle
    host (http://192.168.0.10/ad-hoc/copilot/). Restrict this to ``https`` only
    before release, alongside switching APP_CATALOG_URL back to the production
    https endpoint.
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlparse(url)
    except Exception:
        return False
    if parts.scheme not in ("https", "http") or not parts.netloc:
        return False
    if "@" in parts.netloc:  # reject user:pass@host
        return False
    return True


def _is_sha256(value) -> bool:
    """True for a 64-char hex string."""
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdefABCDEF" for c in value))


def _installed_summary() -> dict:
    """Summarise the installed app (or ``{installed: False}``)."""
    info = find_installed_app()
    if not info:
        return {"installed": False}
    versal = info.get("versal") or {}
    # The region manager, not the app record, knows where the app actually
    # landed - a jumbo app holds the disk tail rather than the standard slot.
    capacity = get_region_capacity() or {}
    jumbo = bool(capacity.get("jumbo_installed"))
    jumbo_gib = 0
    if jumbo:
        jumbo_gib = max(capacity.get("app_tail_end_mib", 0)
                        - capacity.get("jumbo_first_mib", 0), 0) // 1024
    return {
        "installed": True,
        "id": info.get("id", ""),
        "name": info.get("name", ""),
        "version": info.get("version", ""),
        "has_versal": bool(versal.get("ospi_present") or versal.get("wic_present")),
        "installed_at": info.get("installed_at", ""),
        "jumbo": jumbo,
        "jumbo_gib": jumbo_gib,
    }


def _start_install(args: list):
    """Forward ``args`` to the shared ``install-app`` job runner."""
    job_id, cmd = start_job("install-app", args)
    return jsonify({"job_id": job_id, "cmd": cmd})


@app_store_bp.get("/api/apps")
def api_apps():
    """Return the merged catalog (all stores) plus which app is installed."""
    apps, stores, any_ok = _gather_catalog()
    installed = _installed_summary()
    if not any_ok:
        error = next((s["error"] for s in stores if s["error"]),
                     "No app stores are reachable.")
        return jsonify({"ok": False, "status": "offline", "error": error,
                        "stores": stores, "installed": installed}), 502
    partial = any(not s["ok"] for s in stores)
    return jsonify({
        "ok": True,
        "status": "partial" if partial else "online",
        "schema_version": 1,
        "apps": apps,
        "stores": stores,
        "installed": installed,
    })


@app_store_bp.get("/api/apps/stores")
def api_apps_stores():
    """List configured stores (built-in + user-added) without fetching them."""
    return jsonify({"stores": _configured_stores()})


@app_store_bp.post("/api/apps/stores")
def api_apps_stores_add():
    """Register an additional store root. Body: ``{"url": "<root>"}``."""
    body = request.get_json(silent=True) or {}
    root = (body.get("url") or "").strip()
    if not _is_https_url(root):
        return jsonify({"error": "store must be an absolute http(s):// URL"}), 400

    existing = load_app_stores()
    if root == APP_CATALOG_URL or root in existing:
        return jsonify({"error": "that store is already configured"}), 409
    if len(existing) >= MAX_USER_STORES:
        return jsonify({"error": f"at most {MAX_USER_STORES} stores are allowed"}), 409

    existing.append(root)
    if not save_app_stores(existing):
        return jsonify({"error": "could not save the store list"}), 500
    return jsonify({"ok": True, "stores": _configured_stores()})


@app_store_bp.delete("/api/apps/stores")
def api_apps_stores_remove():
    """Remove a user-added store root. Body: ``{"url": "<root>"}``."""
    body = request.get_json(silent=True) or {}
    root = (body.get("url") or "").strip()
    if not root:
        return jsonify({"error": "missing store url"}), 400
    if root == APP_CATALOG_URL:
        return jsonify({"error": "the built-in store cannot be removed"}), 403

    existing = load_app_stores()
    if root not in existing:
        return jsonify({"error": "no such store"}), 404
    existing = [s for s in existing if s != root]
    if not save_app_stores(existing):
        return jsonify({"error": "could not save the store list"}), 500
    return jsonify({"ok": True, "stores": _configured_stores()})


@app_store_bp.get("/api/apps/installed")
def api_apps_installed():
    """Return the installed app summary (no catalog fetch)."""
    return jsonify(_installed_summary())


@app_store_bp.post("/api/apps/inspect")
def api_apps_inspect():
    """Report what a bundle is — and what the device can give it — before install.

    The catalog's `jumbo` fields are only a copy; the bundle is authoritative,
    and a manual URL install has no catalog at all. So the descriptor is read
    from the source itself. That is cheap even for a multi-gigabyte bundle:
    appimage.yaml is the first tar member, and `tar` quits after it, so the
    download stops before the payload.
    """
    body = request.get_json(silent=True) or {}
    source = str(body.get("source") or "").strip()
    if not source:
        return jsonify({"error": "missing source"}), 400
    if source.startswith(("http://", "https://")):
        if not _is_https_url(source):
            return jsonify({"error": "source must be an absolute http(s) URL"}), 400
    elif not os.path.isfile(source):
        return jsonify({"error": "no such bundle file"}), 404

    info, error = _peek_descriptor(source)
    if info is None:
        return jsonify({"error": error}), 502

    capacity = get_region_capacity() or {}
    info["available_mib"] = capacity.get("jumbo_mib", 0)
    info["jumbo_installed"] = bool(capacity.get("jumbo_installed"))
    info["ubuntu_installed"] = bool(capacity.get("ubuntu_installed"))
    info["ubuntu_min_gib"] = capacity.get("ubuntu_min_gib", 0)
    return jsonify(info)


def _peek_descriptor(source):
    """Read only the leading appimage.yaml from `source`. Returns (info, error)."""
    with tempfile.TemporaryDirectory(prefix="inspect.") as work:
        descriptor = os.path.join(work, "appimage.yaml")
        if source.startswith(("http://", "https://")):
            reader = ["wget", "-q", "-O", "-", "--timeout", str(CATALOG_TIMEOUT_S), source]
        else:
            reader = ["cat", source]
        # --occurrence=1 makes tar exit on the first match, which closes the
        # pipe and stops the transfer; the payload is never fetched.
        try:
            with open(descriptor, "wb") as fh:
                head = subprocess.Popen(reader, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
                gunzip = subprocess.Popen(["gzip", "-dc"], stdin=head.stdout,
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL)
                head.stdout.close()
                untar = subprocess.Popen(
                    ["tar", "-xO", "--occurrence=1", "appimage.yaml"],
                    stdin=gunzip.stdout, stdout=fh, stderr=subprocess.DEVNULL)
                gunzip.stdout.close()
                untar.communicate(timeout=BUNDLE_PEEK_TIMEOUT_S)
                for proc in (head, gunzip):
                    proc.kill()
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"could not read the bundle descriptor: {exc}"

        if not os.path.getsize(descriptor):
            return None, "no appimage.yaml at the head of the bundle"
        result = run_cmd(["python3", DESCRIPTOR_TOOL, "info", descriptor], timeout=20)
        if result.returncode != 0:
            return None, "the bundle descriptor could not be parsed"
        try:
            return json.loads(result.stdout), ""
        except ValueError:
            return None, "the bundle descriptor could not be parsed"


@app_store_bp.post("/api/apps/install")
def api_apps_install():
    """Install a catalog app by id: resolve {url, sha256} then start the job."""
    body = request.get_json(silent=True) or {}
    app_id = body.get("id")
    if not app_id or not isinstance(app_id, str):
        return jsonify({"error": "missing app id"}), 400

    apps, _stores, any_ok = _gather_catalog()
    if not any_ok:
        return jsonify({"error": "no app stores are reachable",
                        "status": "offline"}), 502

    entry = next((a for a in apps
                  if isinstance(a, dict) and a.get("id") == app_id), None)
    if entry is None:
        return jsonify({"error": f"unknown app id '{app_id}'"}), 404

    url = entry.get("url", "")
    sha = entry.get("sha256", "")
    if not _is_https_url(url):
        return jsonify({"error": "catalog entry has no valid http(s):// url"}), 502
    if sha and not _is_sha256(sha):
        return jsonify({"error": "catalog entry has a malformed sha256"}), 502

    # Early free-space guard using the catalog's declared size (the installer
    # repeats a Content-Length based check before download).
    size = entry.get("size_bytes")
    if isinstance(size, (int, float)) and size > 0:
        free = free_bytes(APP_CACHE_DIR)
        needed = int(size * SPACE_FACTOR)
        if 0 <= free < needed:
            return jsonify({
                "error": f"insufficient space: need ~{needed} bytes, "
                         f"have {free} on {APP_CACHE_DIR}",
                "status": "no-space",
            }), 507

    # A jumbo app needs a slice of the disk tail that this unit may not have.
    # The bundle is authoritative, but checking the catalog's copy here saves a
    # multi-gigabyte download that could only end in a refusal.
    minimum = entry.get("minimum_storage")
    if entry.get("jumbo") and isinstance(minimum, int) and minimum > 0:
        available_mib = get_jumbo_free_mib()
        if available_mib is not None and available_mib < minimum * 1024:
            return jsonify({
                "error": f"'{app_id}' needs at least {minimum} GiB of jumbo "
                         f"storage; this device has {available_mib // 1024} GiB",
                "status": "no-space",
            }), 507

    args = [url]
    if sha:
        args += ["--sha256", sha]
    return _start_install(args)


@app_store_bp.post("/api/apps/install-url")
def api_apps_install_url():
    """Manual bring-your-own-URL install (works even if the catalog is offline).

    The URL is validated server-side as an absolute ``https://`` URL. ``sha256``
    is optional here; when absent the installer relies on the bundle envelope
    validation + per-component sha256 (Phases 1-3) and the UI must warn that the
    source is unverified (Phase 5).
    """
    body = request.get_json(silent=True) or {}
    url = body.get("url", "")
    sha = body.get("sha256", "")
    if not _is_https_url(url):
        return jsonify({"error": "url must be an absolute http(s):// URL"}), 400
    if sha and not _is_sha256(sha):
        return jsonify({"error": "sha256 must be 64 hex characters"}), 400

    args = [url]
    if sha:
        args += ["--sha256", sha]
    return _start_install(args)


@app_store_bp.post("/api/apps/recover")
def api_apps_recover():
    """Reprogram the Versal side only -- implemented in Phase 6."""
    return jsonify({"error": "recovery not implemented yet (Phase 6)"}), 501
