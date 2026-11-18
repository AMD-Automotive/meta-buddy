# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Network routes -- WiFi/ethernet scan, connect, disconnect, configure.

Endpoints: /api/network/services, /api/network/scan, /api/network/toggle,
           /api/network/connect, /api/network/disconnect,
           /api/network/configure_ipv4
"""

import re

from flask import Blueprint, jsonify, request
from network import (
    connmanctl,
    connman_connect_with_agent,
    ifname_valid,
    network_snapshot,
    service_id_valid,
    set_interface_power,
    viewer_context,
    wait_for_service_connected,
)

net = Blueprint("net", __name__)


@net.get("/api/network/services")
def network_services():
    scan_wifi = request.args.get("scan", "0") == "1"
    return jsonify(network_snapshot(scan_wifi=scan_wifi, client_addr=request.remote_addr or ""))


@net.post("/api/network/scan")
def network_scan():
    data = request.get_json(silent=True) or {}
    tech = str(data.get("technology", "wifi"))
    if tech not in {"wifi", "ethernet", "cellular", "bluetooth"}:
        return jsonify({"ok": False, "error": "invalid technology"}), 400

    result = connmanctl(["scan", tech], timeout=25)
    if result.returncode != 0:
        return jsonify({"ok": False, "error": (result.stderr or result.stdout or "scan failed").strip()}), 500

    return jsonify({"ok": True, "message": f"scan requested for {tech}"})


@net.post("/api/network/toggle")
def network_toggle():
    data = request.get_json(silent=True) or {}
    tech = str(data.get("technology", "wifi"))
    enabled = bool(data.get("enabled", True))

    if tech not in {"wifi", "ethernet", "cellular", "bluetooth"}:
        return jsonify({"ok": False, "error": "invalid technology"}), 400

    cmd = "enable" if enabled else "disable"
    result = connmanctl([cmd, tech], timeout=12)
    if result.returncode != 0:
        return jsonify({"ok": False, "error": (result.stderr or result.stdout or f"{cmd} failed").strip()}), 500

    return jsonify({"ok": True, "message": f"{tech} {cmd}d"})


@net.post("/api/network/interface_power")
def network_interface_power():
    """Bring a single interface up or down (per-interface, unlike /toggle).

    ConnMan's technology-level enable/disable affects every wired NIC at once.
    This endpoint acts on one interface only, so disabling one wired port
    leaves the others connected.
    """
    data = request.get_json(silent=True) or {}
    ifname = str(data.get("ifname", "")).strip()
    enabled = bool(data.get("enabled", True))

    if not ifname_valid(ifname):
        return jsonify({"ok": False, "error": "invalid or unknown interface"}), 400

    result = set_interface_power(ifname, enabled)
    if result.returncode != 0:
        return jsonify({"ok": False, "error": (result.stderr or result.stdout or "failed to change interface state").strip()}), 500

    return jsonify({"ok": True, "message": f"{ifname} {'enabled' if enabled else 'disabled'}"})


@net.post("/api/network/connect")
def network_connect():
    data = request.get_json(silent=True) or {}
    service_id = str(data.get("service_id", "")).strip()
    passphrase = str(data.get("passphrase", "")).strip()
    identity = str(data.get("identity", data.get("username", ""))).strip()
    security = str(data.get("security", "auto")).strip().lower()

    if not service_id_valid(service_id):
        return jsonify({"ok": False, "error": "invalid service_id"}), 400

    if security not in {"auto", "open", "psk", "wep", "8021x"}:
        return jsonify({"ok": False, "error": "invalid security type"}), 400

    if security == "open":
        passphrase = ""
        identity = ""

    pass_cfg_unsupported = False
    if passphrase and security in {"auto", "psk", "wep", "8021x"}:
        pass_cfg = connmanctl(["config", service_id, "--passphrase", passphrase], timeout=15)
        if pass_cfg.returncode != 0:
            msg = (pass_cfg.stderr or pass_cfg.stdout or "failed to set passphrase").strip()
            if "Invalid argument" in msg:
                pass_cfg_unsupported = True
            else:
                return jsonify({"ok": False, "error": msg}), 500
    if identity and security == "8021x":
        id_cfg = connmanctl(["config", service_id, "--identity", identity], timeout=15)
        if id_cfg.returncode != 0:
            return jsonify({"ok": False, "error": (id_cfg.stderr or id_cfg.stdout or "failed to set identity").strip()}), 500

    result = connmanctl(["connect", service_id], timeout=35)
    result_text = (result.stderr or result.stdout or "").strip().lower()

    used_agent_fallback = False
    if service_id.startswith("wifi_") and passphrase and (pass_cfg_unsupported or "not registered" in result_text or result.returncode != 0):
        ok, agent_err = connman_connect_with_agent(service_id, passphrase, timeout_s=50)
        used_agent_fallback = True
        if not ok:
            return jsonify({"ok": False, "error": agent_err}), 500

    if result.returncode != 0 and not used_agent_fallback:
        err = (result.stderr or result.stdout or "connect failed").strip()
        if pass_cfg_unsupported and passphrase:
            err = f"{err}. This ConnMan build cannot set WiFi passphrase non-interactively; trying from UI requires valid passphrase entry."
        return jsonify({"ok": False, "error": err}), 500

    connected, snap = wait_for_service_connected(service_id, timeout_s=20)
    if not connected and service_id.startswith("wifi_"):
        connmanctl(["enable", "wifi"], timeout=12)
        connmanctl(["scan", "wifi"], timeout=20)
        retry = connmanctl(["connect", service_id], timeout=35)
        if retry.returncode == 0:
            connected, snap = wait_for_service_connected(service_id, timeout_s=20)

    if not connected:
        return jsonify({"ok": False, "error": "connect requested but service did not reach connected state. Verify WiFi is enabled and credentials are correct, then retry."}), 500

    connmanctl(["config", service_id, "--autoconnect", "yes"], timeout=12)

    return jsonify(
        {
            "ok": True,
            "message": "connected",
            "persistence": "Connection profile is managed by ConnMan and will be reused across reboot.",
        }
    )


@net.post("/api/network/disconnect")
def network_disconnect():
    data = request.get_json(silent=True) or {}
    service_id = str(data.get("service_id", "")).strip()
    disable_autoconnect = bool(data.get("disable_autoconnect", False))

    if not service_id_valid(service_id):
        return jsonify({"ok": False, "error": "invalid service_id"}), 400

    if disable_autoconnect:
        cfg = connmanctl(["config", service_id, "--autoconnect", "no"], timeout=12)
        if cfg.returncode != 0:
            return jsonify({"ok": False, "error": (cfg.stderr or cfg.stdout or "failed to disable autoconnect").strip()}), 500

    result = connmanctl(["disconnect", service_id], timeout=12)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "disconnect failed").strip()
        if "not connected" in err.lower():
            return jsonify({"ok": True, "message": "already disconnected"})
        return jsonify({"ok": False, "error": err}), 500
    return jsonify({"ok": True, "message": "disconnected"})


@net.post("/api/network/configure_ipv4")
def network_configure_ipv4():
    data = request.get_json(silent=True) or {}
    service_id = str(data.get("service_id", "")).strip()
    method = str(data.get("method", "dhcp")).strip().lower()

    if not service_id_valid(service_id):
        return jsonify({"ok": False, "error": "invalid service_id"}), 400
    if method not in {"dhcp", "manual"}:
        return jsonify({"ok": False, "error": "method must be dhcp or manual"}), 400

    dns_raw = str(data.get("dns_servers", "")).strip()
    dns_servers = [x for x in re.split(r"[\s,]+", dns_raw) if x]

    if method == "dhcp":
        result = connmanctl(["config", service_id, "--ipv4", "dhcp"], timeout=15)
        if result.returncode != 0:
            return jsonify({"ok": False, "error": (result.stderr or result.stdout or "failed to set dhcp").strip()}), 500
    else:
        address = str(data.get("address", "")).strip()
        netmask = str(data.get("netmask", "")).strip()
        gateway = str(data.get("gateway", "")).strip()

        if not address or not netmask or not gateway:
            return jsonify({"ok": False, "error": "manual mode requires address, netmask, and gateway"}), 400

        result = connmanctl(["config", service_id, "--ipv4", "manual", address, netmask, gateway], timeout=15)
        if result.returncode != 0:
            return jsonify({"ok": False, "error": (result.stderr or result.stdout or "failed to set manual ipv4").strip()}), 500

    if dns_servers:
        dns_csv = ",".join(dns_servers)
        dns_result = connmanctl(["config", service_id, "--nameservers", dns_csv], timeout=15)
        if dns_result.returncode != 0:
            dns_result = connmanctl(["config", service_id, "--nameservers"] + dns_servers, timeout=15)
        if dns_result.returncode != 0:
            return jsonify({"ok": False, "error": (dns_result.stderr or dns_result.stdout or "failed to set dns servers").strip()}), 500

    return jsonify({"ok": True, "message": "IPv4 configuration applied", "persistence": "Saved via ConnMan profile."})
