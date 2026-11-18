# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Versal UFS LUN layout query and re-provisioning.

Endpoints:
    POST /api/versal/luns/scan     read-only
    POST /api/versal/luns/apply    dry run, or irreversible with confirm

Both boot the recovery payload and reset the Versal, so both take the bulk
lock. `apply` without `confirm` writes nothing; with it, all data on every
LUN is destroyed and the board will not boot until it is reimaged.
"""

import json
import logging
import os
import subprocess

from flask import Blueprint, jsonify, request

import support_log
from helpers import SCRIPTS_DIR, BulkOperationBusy, acquire_bulk_lock

log = logging.getLogger(__name__)
versal_luns_bp = Blueprint("versal_luns", __name__)

SCAN_SCRIPT = SCRIPTS_DIR / "scan_versal_luns.sh"
PROVISION_SCRIPT = SCRIPTS_DIR / "provision_versal_luns.sh"
OUTPUT_DIR = "/run/rave-menu"
SCAN_OUTPUT = f"{OUTPUT_DIR}/versal-luns.json"
PROVISION_OUTPUT = f"{OUTPUT_DIR}/versal-luns-plan.json"
PLAN_INPUT = f"{OUTPUT_DIR}/versal-luns-request.json"

# Fields a request may set per LUN. Everything else about a LUN is carried
# over from the device, so a plan changes only what it names.
PLAN_FIELDS = {"lun", "size", "boot_lun_id", "memory_type", "data_reliability",
               "write_protect", "logical_block_size_exp", "provisioning_type"}

SCAN_TIMEOUT_S = 600
# A commit writes a descriptor, resets the device and re-reads the layout, all
# of which is quick; the budget is dominated by booting the payload twice.
PROVISION_TIMEOUT_S = 900


def _run(script, args, output, timeout, event="", log_ok=True):
    """Run a payload script and return (json, error_response_tuple).

    *event* names the support-log record. ``log_ok=False`` records only
    failures, which is what a read-only scan warrants.
    """
    if not script.exists() or not os.access(script, os.X_OK):
        return None, (jsonify({"error": f"script missing or not executable: {script}"}), 500)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if os.path.exists(output):
        os.unlink(output)

    try:
        result = subprocess.run(
            [str(script), output, *args],
            cwd=str(SCRIPTS_DIR), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if event:
            support_log.record(event, {"args": list(args)},
                               "timed out talking to the Versal",
                               None, status="timeout")
        return None, (jsonify({"error": "timed out talking to the Versal"}), 504)

    tail = (result.stdout + result.stderr)[-4000:]
    # The log gets the whole transcript; only the client's copy is trimmed.
    if event and (log_ok or result.returncode != 0):
        support_log.record(event, {"args": list(args)},
                           result.stdout + result.stderr, result.returncode)
    try:
        with open(output, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError):
        document = {}

    # The scripts always restore OSPI boot mode, so a non-zero status means the
    # operation failed rather than that the board was left mid-flight.
    if result.returncode != 0 and not document:
        log.warning("versal LUN operation failed: %s", tail)
        return None, (jsonify({
            "error": "the Versal did not complete the requested LUN operation",
            "log": tail,
        }), 500)

    document["log"] = tail
    document["returncode"] = result.returncode
    return document, None


@versal_luns_bp.post("/api/versal/luns/scan")
def scan_luns():
    try:
        lock_file = acquire_bulk_lock()
    except BulkOperationBusy as exc:
        return jsonify({"error": str(exc)}), 409

    try:
        document, error = _run(SCAN_SCRIPT, [], SCAN_OUTPUT, SCAN_TIMEOUT_S,
                               event="versal.lun-scan", log_ok=False)
    finally:
        lock_file.close()

    if error:
        return error
    report = document.get("report")
    if not report:
        return jsonify({"error": "the target did not return a LUN layout",
                        "log": document.get("log", "")}), 500
    return jsonify({"ok": True, "report": report, "log": document["log"]})


def _validate_luns(luns):
    """Reject a malformed layout request, or return None if it is usable.

    Only shape is checked here. Capacity, LUN count and boot LUN limits depend
    on the live device, so they are the planner's and the target's to judge.
    """
    if not isinstance(luns, list) or not luns:
        return jsonify({"error": "luns must be a non-empty list"}), 400
    seen = set()
    for item in luns:
        if not isinstance(item, dict):
            return jsonify({"error": "each LUN must be an object"}), 400
        unknown = set(item) - PLAN_FIELDS
        if unknown:
            return jsonify({"error": f"unknown LUN field(s): {', '.join(sorted(unknown))}"}), 400
        lun = item.get("lun")
        if not isinstance(lun, int) or lun < 0:
            return jsonify({"error": f"invalid LUN index {lun!r}"}), 400
        if lun in seen:
            return jsonify({"error": f"LUN {lun} named twice"}), 422
        seen.add(lun)
        size = item.get("size")
        if isinstance(size, str):
            if size.lower() != "rest":
                return jsonify({"error": f"LUN {lun} has an invalid size {size!r}"}), 400
        elif not isinstance(size, int) or size <= 0:
            return jsonify({"error": f"LUN {lun} has an invalid size {size!r}"}), 400
        for field in PLAN_FIELDS - {"lun", "size"}:
            if field in item and not isinstance(item[field], int):
                return jsonify({"error": f"LUN {lun}: {field} must be an integer"}), 400
    if sum(1 for item in luns if str(item.get("size")).lower() == "rest") > 1:
        return jsonify({"error": "at most one LUN may be sized 'rest'"}), 422
    return None


@versal_luns_bp.post("/api/versal/luns/apply")
def apply_luns():
    payload = request.get_json(silent=True) or {}
    luns = payload.get("luns")
    restore = bool(payload.get("restore_baseline"))
    confirm = bool(payload.get("confirm"))

    if restore and luns:
        return jsonify({"error": "give either luns or restore_baseline, not both"}), 400

    if restore:
        args = ["--restore-baseline"]
    else:
        error = _validate_luns(luns)
        if error:
            return error
        args = ["--plan", PLAN_INPUT]

    if confirm:
        args.append("--commit")

    try:
        lock_file = acquire_bulk_lock()
    except BulkOperationBusy as exc:
        return jsonify({"error": str(exc)}), 409

    try:
        if not restore:
            # Per-LUN descriptor fields do not survive being flattened into
            # command line arguments, so the plan goes across as a file. It is
            # written under the lock, so a request that is turned away cannot
            # replace the layout a running operation is about to apply.
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            with open(PLAN_INPUT, "w", encoding="utf-8") as handle:
                json.dump({"luns": luns, "manage_boot_luns": True}, handle)
        document, error = _run(PROVISION_SCRIPT, args, PROVISION_OUTPUT,
                               PROVISION_TIMEOUT_S, event="versal.ufs-geometry")
    finally:
        lock_file.close()

    if error:
        return error

    preview = document.get("preview")
    if not preview:
        # The target refuses bad plans, so this is the normal path for a plan
        # that does not fit; surface its reason rather than a generic 500.
        return jsonify({"error": "the target rejected the requested layout",
                        "report": document.get("report"),
                        "log": document.get("log", "")}), 422

    return jsonify({
        "ok": True,
        "committed": bool(preview.get("committed")),
        "report": document.get("report"),
        "preview": preview,
        "log": document["log"],
    })
