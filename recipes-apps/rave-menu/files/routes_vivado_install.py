# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Vivado HW Server (Lab Tools) installer route blueprint.

Endpoints:
    GET  /api/vivado/status            -- install state + candidate sources
    POST /api/vivado/upload            -- multipart upload of installer tar
    POST /api/vivado/eulas             -- extract EULA texts from a tar
    POST /api/vivado/install           -- launch install job; returns job_id

The install job streams its progress via the existing /api/stream/<job_id>
SSE channel (see routes_actions.stream_job).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_file
from werkzeug.utils import secure_filename

from helpers import APP_ROOT, register_job
import vivado_installer as vi

log = logging.getLogger(__name__)
vivado_bp = Blueprint("vivado_install", __name__)

INSTALL_SCRIPT = APP_ROOT / "scripts" / "vivado_install.sh"

# Cap a single uploaded chunk at 4 GiB (Vivado tar is ~250 MiB; this is just
# a sanity ceiling so a stuck upload doesn't fill the rootfs unbounded).
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024

# EULA files we surface to the user before install. Paths are relative to
# the single top-level directory inside the AMD installer tarball.
EULA_FILES = [
    ("data/unified_xilinx_eulas_hwserver.txt",
     "End User License Agreement for Hardware Server"),
    ("data/unified_3rd_party_eulas_hwserver.txt",
     "Third Party Software End User License Agreement for Hardware Server"),
]
# Per-file extraction cap (defensive -- these files are ~50 KiB each).
EULA_MAX_BYTES = 2 * 1024 * 1024


@vivado_bp.get("/api/vivado/status")
def status():
    effective = vi.effective_status()
    return jsonify({
        # "installed" gates the lab / App Store / board-test entry points. Any
        # usable release unlocks them -- gating on the base release alone would
        # lock a 2026.1-only board out of the lab card, which is where the
        # toolchain selector lives.
        "installed": bool(vi.installed_versions()),
        "base_installed": vi.is_installed(vi.BASE_VERSION),
        "hw_server": vi.hw_server_binary(effective["effective_version"]
                                         or vi.BASE_VERSION),
        "install_prefix": vi.INSTALL_PREFIX,
        "base_version": vi.BASE_VERSION,
        "default_version": vi.DEFAULT_VERSION,
        "selected_version": effective["selected_version"],
        "effective_version": effective["effective_version"],
        "effective_source": effective["effective_source"],
        "versions": vi.version_status(),
        "installed_versions": vi.installed_versions(),
        "sources": vi.discover_local_tars(),
    })


@vivado_bp.get("/vivado-installer-url.png")
def qr_png():
    version = request.args.get("version") or vi.DEFAULT_VERSION
    path = vi.qr_path(version)
    if path is not None and path.is_file():
        return send_file(str(path), mimetype="image/png")
    return ("QR not available", 404)


@vivado_bp.post("/api/vivado/upload")
def upload():
    """Accept a multipart upload and stash it under uploads/.

    Returns the absolute path so the install endpoint can be called next.
    """
    vi.ensure_dirs()
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"ok": False, "error": "no file in request"}), 400

    name = secure_filename(f.filename) or "upload.tar"
    dest = vi.UPLOADS_DIR / name

    # Werkzeug streams the upload to a temp file already; just move it.
    try:
        f.save(str(dest))
    except OSError as exc:
        return jsonify({"ok": False, "error": f"save failed: {exc}"}), 500

    try:
        size = dest.stat().st_size
    except OSError:
        size = 0
    if size > MAX_UPLOAD_BYTES:
        dest.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": "file exceeds size cap"}), 413

    sha_ok, digest = vi.verify_tar(dest)
    return jsonify({
        "ok": True,
        "path": str(dest),
        "size": size,
        "sha256": digest,
        "sha_ok": bool(sha_ok),
        "version": vi.version_for_sha(digest),
    })


@vivado_bp.post("/api/vivado/eulas")
def eulas():
    """Pull the HW Server EULA texts out of a candidate installer tarball.

    JSON body: {"path": "/abs/path/to/tar"}
    Returns {"ok": true, "eulas": [{"title": "...", "body": "..."}, ...]}.

    The user must agree to these before /api/vivado/install will run.
    """
    if not request.is_json:
        return jsonify({"ok": False, "error": "JSON body required"}), 400
    data = request.get_json(silent=True) or {}
    src = data.get("path") or ""
    if not src:
        return jsonify({"ok": False, "error": "missing 'path'"}), 400

    src_path = Path(os.path.realpath(src))
    if not src_path.is_file():
        return jsonify({"ok": False, "error": f"file not found: {src}"}), 404

    try:
        tf = tarfile.open(str(src_path), mode="r:*")
    except (tarfile.TarError, OSError) as exc:
        return jsonify({"ok": False, "error": f"tar open failed: {exc}"}), 400

    out: list[dict] = []
    missing: list[str] = []
    with tf:
        # The AMD tars contain a single top-level dir. Build a name -> member
        # map keyed by the suffix after that top dir so we don't have to
        # guess the version-stamped prefix.
        index: dict[str, tarfile.TarInfo] = {}
        for m in tf.getmembers():
            if not m.isfile():
                continue
            name = m.name
            parts = name.split("/", 1)
            tail = parts[1] if len(parts) == 2 else name
            index[tail] = m

        for rel, title in EULA_FILES:
            m = index.get(rel)
            if m is None:
                missing.append(rel)
                continue
            if m.size > EULA_MAX_BYTES:
                missing.append(rel + " (too large)")
                continue
            try:
                fp = tf.extractfile(m)
                if fp is None:
                    missing.append(rel)
                    continue
                raw = fp.read(EULA_MAX_BYTES + 1)
            except (tarfile.TarError, OSError) as exc:
                missing.append(f"{rel} ({exc})")
                continue
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = raw.decode("latin-1", errors="replace")
            out.append({"title": title, "body": text})

    if not out:
        return jsonify({
            "ok": False,
            "error": "EULA files not found in tar",
            "missing": missing,
        }), 422

    return jsonify({"ok": True, "eulas": out, "missing": missing})


@vivado_bp.post("/api/vivado/install")
def install():
    """Verify the chosen tar and launch the install script.

    JSON body: {"path": "/abs/path/to/tar", "version": "2026.1", "force": false}
    Each version installs into its own <prefix>/<version>/ tree, so installing
    a newer release leaves the existing ones alone.
    On SHA mismatch with force=false returns 409 with the computed SHA so the
    UI can surface a clear "install anyway (not recommended)" override.
    """
    if not request.is_json:
        return jsonify({"ok": False, "error": "JSON body required"}), 400
    data = request.get_json(silent=True) or {}
    src = data.get("path") or ""
    version = data.get("version") or vi.DEFAULT_VERSION
    force = bool(data.get("force", False))
    accepted = bool(data.get("accepted_eulas", False))

    if not src:
        return jsonify({"ok": False, "error": "missing 'path'"}), 400
    entry = vi.entry_for(version)
    if entry is None:
        return jsonify({
            "ok": False,
            "error": "unknown_version",
            "version": version,
            "known_versions": vi.catalog_versions(),
        }), 400
    if not accepted:
        return jsonify({
            "ok": False,
            "error": "eulas_not_accepted",
            "message": "User must accept the EULAs before install.",
        }), 403

    src_path = Path(os.path.realpath(src))
    if not src_path.is_file():
        return jsonify({"ok": False, "error": f"file not found: {src}"}), 404

    sha_ok, digest = vi.verify_tar(src_path, version)
    matched = vi.version_for_sha(digest)
    # A tar for a *different* catalog release would have xsetup install into
    # that release's directory instead, silently overwriting it. Never allow
    # this, force or not.
    if matched and matched != version:
        return jsonify({
            "ok": False,
            "error": "wrong_version",
            "version": version,
            "matched_version": matched,
            "computed_sha256": digest,
        }), 409
    if not sha_ok and not force:
        return jsonify({
            "ok": False,
            "error": "sha256_mismatch",
            "version": version,
            "expected_sha256": entry["sha256"],
            "computed_sha256": digest,
        }), 409

    # Stage the tar at the canonical per-version cache path so a future RAUC
    # slot hook can find it. Using a copy (not move) so the original on USB /
    # Downloads is untouched if the install fails halfway.
    vi.ensure_dirs()
    cache_path = vi.tar_cache_path(version)
    try:
        if src_path != cache_path:
            shutil.copy2(src_path, cache_path)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"cache copy failed: {exc}"}), 500

    env = os.environ.copy()
    env.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [
        "/bin/sh",
        str(INSTALL_SCRIPT),
        str(cache_path),
        vi.INSTALL_PREFIX,
        version,
        str(vi.state_path(version)),
        digest or entry["sha256"],
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as exc:
        return jsonify({"ok": False, "error": f"launch failed: {exc}"}), 500

    job_id = register_job(proc)

    # Selecting an uninstalled release in the lab card sends the user here
    # rather than committing the choice, so a cancelled or failed install can
    # never leave the board pointing at a missing toolchain. Commit it now
    # that the install has actually succeeded.
    def _adopt_when_installed(p, wanted):
        try:
            p.wait()
        except Exception:
            return
        if p.returncode == 0 and vi.is_installed(wanted):
            vi.set_selected_version(wanted)

    threading.Thread(target=_adopt_when_installed, args=(proc, version),
                     daemon=True).start()

    return jsonify({
        "ok": True,
        "job_id": job_id,
        "version": version,
        "sha_ok": bool(sha_ok),
        "force": force,
    })
