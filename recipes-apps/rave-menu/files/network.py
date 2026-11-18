# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Network management layer -- ConnMan, ip, iw wrappers and state queries.

All functions are pure Python (no Flask dependency).  The main entry point
is ``network_snapshot()`` which gathers technologies, services, interfaces,
and viewer context into a single dict consumed by the frontend.

ConnMan interactive agent flow (for WiFi WPA passphrase) is handled by
``connman_connect_with_agent()``.
"""

import ipaddress
import json
import os
import pty
import re
import select
import subprocess
import time

from helpers import run_cmd, sysfs_read, CONNMANCTL, IP_CMD, LSPCI_CMD, IW_CMD


# --- Low-level command wrappers ---

def connmanctl(args, timeout=20, input_text=None):
    """Run a connmanctl sub-command."""
    return run_cmd([CONNMANCTL] + args, timeout=timeout, input_text=input_text)


def ip_json(args, timeout=10):
    """Run ``ip -json <args>`` and return parsed list (or [] on failure)."""
    result = run_cmd([IP_CMD, "-json"] + args, timeout=timeout)
    if result.returncode != 0:
        return []
    try:
        parsed = json.loads(result.stdout or "[]")
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


# --- Validation helpers ---

def service_id_valid(service_id: str) -> bool:
    """Check that *service_id* matches the ConnMan naming pattern."""
    if not isinstance(service_id, str):
        return False
    return bool(re.match(
        r"^(wifi|ethernet|cellular|bluetooth)_[A-Za-z0-9_.:-]+$", service_id
    ))


def ifname_valid(ifname: str) -> bool:
    """Validate a Linux interface name and confirm it exists in sysfs."""
    if not isinstance(ifname, str) or not re.match(r"^[A-Za-z0-9._-]{1,15}$", ifname):
        return False
    return os.path.isdir(f"/sys/class/net/{ifname}")


def set_interface_power(ifname: str, enabled: bool):
    """Bring a single network interface administratively up or down.

    Operates on one interface only (via ``ip link set``), so toggling one
    wired NIC never affects the others.
    """
    state = "up" if enabled else "down"
    return run_cmd([IP_CMD, "link", "set", "dev", ifname, state], timeout=10)


def _parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "yes", "true", "on"}


# --- ConnMan output parsers ---

def parse_technologies(output: str):
    """Parse ``connmanctl technologies`` into {type: {...}} dict."""
    technologies = {}
    current = None

    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("/net/connman/technology/"):
            if current and current.get("type"):
                technologies[current["type"]] = current
            current = {"path": line}
            continue

        if current and "=" in line:
            key, value = [x.strip() for x in line.split("=", 1)]
            key_l = key.lower()
            if key_l == "type":
                current["type"] = value
            elif key_l == "name":
                current["name"] = value
            elif key_l in {"powered", "connected", "tethering"}:
                current[key_l] = _parse_bool(value)

    if current and current.get("type"):
        technologies[current["type"]] = current

    return technologies


def _parse_security(service_id: str) -> str:
    sid = service_id.lower()
    if sid.endswith("_psk"):
        return "psk"
    if sid.endswith("_ieee8021x"):
        return "8021x"
    if sid.endswith("_wep"):
        return "wep"
    return "open"


def parse_services(output: str):
    """Parse ``connmanctl services`` into a sorted list of service dicts."""
    services = []
    pattern = re.compile(
        r"^(?:(?P<prefix>[*AORFCc]{1,4})\s+)?(?P<name>.*?)\s+"
        r"(?P<id>(?:wifi|ethernet|cellular|bluetooth)_[^\s]+)$"
    )

    for raw in output.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("Error"):
            continue

        match = pattern.match(line)
        if not match:
            continue

        prefix = match.group("prefix") or ""
        name = match.group("name").strip() or "(unnamed)"
        service_id = match.group("id")

        services.append({
            "id": service_id,
            "name": name,
            "type": service_id.split("_", 1)[0],
            "connected": "*" in prefix,
            "ready": "R" in prefix,
            "online": "O" in prefix,
            "favorite": "F" in prefix,
            "auto_connect": "A" in prefix,
            "security": _parse_security(service_id),
            "raw_flags": prefix,
        })

    services.sort(key=lambda s: (0 if s["connected"] else 1, s["type"], s["name"].lower()))
    return services


# --- Interface / link helpers ---

def _compact_bitrate(raw: str) -> str:
    text = str(raw or "").strip()
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMG]Bit/s)", text, flags=re.IGNORECASE)
    if m:
        value = m.group(1)
        unit = m.group(2)
        if unit.lower().endswith("bit/s"):
            prefix = unit[0].upper()
            unit = f"{prefix}Bit/s"
        return f"{value} {unit}"
    parts = text.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    return text or "-"


def _wifi_link_info(ifname: str):
    """Query ``iw dev <ifname> link`` for SSID, speed, and signal quality."""
    result = run_cmd([IW_CMD, "dev", ifname, "link"], timeout=5)
    text = f"{result.stdout}\n{result.stderr}".strip()
    low = text.lower()

    if result.returncode != 0 or "not connected" in low:
        return "", "-", None

    ssid = ""
    speed = "-"
    signal_quality = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SSID:"):
            ssid = line.split(":", 1)[1].strip()
        elif line.startswith("tx bitrate:"):
            speed = _compact_bitrate(line.split(":", 1)[1].strip())
        elif line.startswith("rx bitrate:") and speed == "-":
            speed = _compact_bitrate(line.split(":", 1)[1].strip())
        elif line.startswith("signal:"):
            try:
                dbm = float(line.split(":", 1)[1].strip().split()[0])
                signal_quality = int(max(0, min(100, (dbm + 100.0) * 2.0)))
            except Exception:
                pass

    return ssid, speed, signal_quality


def _interface_link_details(ifname: str):
    if str(ifname).startswith("wl"):
        ssid, speed, signal_quality = _wifi_link_info(ifname)
        return speed, ssid, signal_quality

    raw_speed = sysfs_read(f"/sys/class/net/{ifname}/speed")
    speed = "-"
    if raw_speed and raw_speed not in {"-1", "unknown"}:
        speed = f"{raw_speed} Mb/s"

    return speed, "", None


def list_interfaces():
    """Return a sorted list of network interfaces with addresses and link info."""
    addr_data = ip_json(["addr", "show"])
    interfaces = []

    for item in addr_data:
        ifname = item.get("ifname", "")
        if not ifname or ifname == "lo":
            continue

        addresses = []
        for a in item.get("addr_info", []):
            local = a.get("local")
            family = a.get("family")
            if local and family in {"inet", "inet6"}:
                addresses.append({"family": family, "address": local})

        link_speed, ssid, wifi_signal = _interface_link_details(ifname)
        interfaces.append({
            "ifname": ifname,
            "operstate": item.get("operstate", "unknown"),
            "flags": item.get("flags", []),
            "addresses": addresses,
            "link_speed": link_speed,
            "ssid": ssid,
            "wifi_signal": wifi_signal,
        })

    interfaces.sort(key=lambda x: x["ifname"])
    return interfaces


# --- Viewer / captive-portal context ---

def request_client_ip(raw_addr: str) -> str:
    """Normalise the remote address from a request."""
    raw = str(raw_addr or "").strip()
    if raw.startswith("::ffff:"):
        raw = raw.split("::ffff:", 1)[1]
    try:
        return str(ipaddress.ip_address(raw)) if raw else ""
    except ValueError:
        return raw


def viewer_context(client_ip: str, interfaces):
    """Determine if the viewer is accessing from a local interface."""
    local_ips = {"127.0.0.1", "::1"}

    for itf in interfaces or []:
        for addr in itf.get("addresses", []):
            ip_text = str(addr.get("address") or "").strip()
            if not ip_text:
                continue
            try:
                local_ips.add(str(ipaddress.ip_address(ip_text)))
            except ValueError:
                local_ips.add(ip_text)

    is_local = client_ip in local_ips if client_ip else False
    return {
        "client_ip": client_ip,
        "is_local": is_local,
        "access": "local" if is_local else "remote",
    }


def _captive_portal_state(services, interfaces):
    wifi_services = [s for s in (services or []) if s.get("type") == "wifi"]
    connected_wifi = [s for s in wifi_services if s.get("connected")]
    wifi_has_ipv4 = any(
        str(itf.get("ifname") or "").startswith("wl")
        and any(a.get("family") == "inet" for a in (itf.get("addresses") or []))
        for itf in (interfaces or [])
    )
    has_online_wifi = any(bool(s.get("online")) for s in connected_wifi)
    has_connected_wifi = bool(connected_wifi)
    required = bool(has_connected_wifi and wifi_has_ipv4 and not has_online_wifi)

    return {
        "required": required,
        "has_connected_wifi": has_connected_wifi,
        "has_online_wifi": has_online_wifi,
        "wifi_has_ipv4": wifi_has_ipv4,
    }


def _wifi_hint(technologies):
    if technologies.get("wifi"):
        return ""

    lspci_result = run_cmd([LSPCI_CMD, "-nn"], timeout=8)
    text = f"{lspci_result.stdout}\n{lspci_result.stderr}".lower()
    if lspci_result.returncode == 0 and any(
        tok in text for tok in ["wi-fi", "wifi", "802.11", "network controller"]
    ):
        return (
            "WiFi PCI device detected but ConnMan has no WiFi technology. "
            "Check iwlwifi firmware/driver and ConnMan WiFi backend."
        )
    return "No WiFi hardware detected by system scan."


# --- High-level snapshot ---

def network_snapshot(scan_wifi=False, client_addr=""):
    """Gather a complete network state snapshot.

    Returns a dict with technologies, services, interfaces, viewer context,
    captive-portal state, wifi hint, and any errors encountered.
    """
    errors = []

    if scan_wifi:
        scan_result = connmanctl(["scan", "wifi"], timeout=25)
        if scan_result.returncode != 0:
            msg = (scan_result.stderr or scan_result.stdout or "wifi scan failed").strip()
            if msg:
                errors.append(msg)

    tech = connmanctl(["technologies"], timeout=10)
    svc = connmanctl(["services"], timeout=12)

    technologies = parse_technologies(tech.stdout) if tech.returncode == 0 else {}
    services = parse_services(svc.stdout) if svc.returncode == 0 else []

    if tech.returncode != 0:
        errors.append((tech.stderr or tech.stdout or "failed to query technologies").strip())
    if svc.returncode != 0:
        errors.append((svc.stderr or svc.stdout or "failed to query services").strip())

    interfaces = list_interfaces()
    client_ip = request_client_ip(client_addr)
    viewer = viewer_context(client_ip, interfaces)
    captive_portal = _captive_portal_state(services, interfaces)

    return {
        "ok": tech.returncode == 0 and svc.returncode == 0,
        "technologies": technologies,
        "services": services,
        "interfaces": interfaces,
        "wifi_hint": _wifi_hint(technologies),
        "errors": [e for e in errors if e],
        "viewer": viewer,
        "captive_portal": captive_portal,
    }


# --- WiFi interactive agent connect ---

def connman_connect_with_agent(service_id: str, passphrase: str, timeout_s: int = 45):
    """Connect to a WiFi service using the ConnMan interactive agent.

    Some ConnMan builds can't set the passphrase via ``config --passphrase``.
    This function spawns an interactive connmanctl session with a PTY, issues
    ``agent on`` then ``connect <service>``, and feeds the passphrase when
    prompted.

    Returns (success: bool, error_message: str).
    """
    if not passphrase:
        return False, "missing passphrase"

    master_fd = None
    proc = None
    try:
        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        proc = subprocess.Popen(
            [CONNMANCTL],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
            close_fds=True,
            env=env,
        )
        os.close(slave_fd)

        def _send(line: str):
            os.write(master_fd, (line + "\n").encode())
            time.sleep(0.15)

        _send("agent on")
        _send(f"connect {service_id}")

        sent_pass = False
        last_error = ""
        deadline = time.time() + max(10, timeout_s)

        while time.time() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 0.5)
            if not ready:
                continue

            data = os.read(master_fd, 4096)
            if not data:
                break

            text = data.decode(errors="ignore")
            low = text.lower()

            if ("passphrase?" in low or "password?" in low
                    or ("requestinput" in low and "passphrase" in low)) and not sent_pass:
                _send(passphrase)
                sent_pass = True

            if "retry (yes/no)?" in low:
                _send("no")

            if "invalid-key" in low:
                last_error = "invalid WiFi passphrase"
            elif "not registered" in low:
                last_error = "failed to register ConnMan agent for WiFi credentials"

            if "connected" in low:
                _send("quit")
                return True, ""

        if last_error:
            return False, last_error
        if not sent_pass:
            return False, "unable to provide WiFi passphrase to ConnMan agent"
        return False, "interactive WiFi connect timed out"
    except Exception as exc:
        return False, f"interactive WiFi connect failed: {exc}"
    finally:
        try:
            if master_fd is not None:
                os.write(master_fd, b"quit\n")
        except Exception:
            pass
        try:
            if master_fd is not None:
                os.close(master_fd)
        except Exception:
            pass
        try:
            if proc is not None:
                proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def wait_for_service_connected(service_id: str, timeout_s: int = 12):
    """Poll ConnMan until *service_id* shows as connected or timeout.

    Returns (connected: bool, last_snapshot: dict).
    """
    deadline = time.time() + max(1, timeout_s)
    last_snapshot = None

    while time.time() < deadline:
        snap = network_snapshot(scan_wifi=False)
        last_snapshot = snap

        for svc in snap.get("services", []):
            if svc.get("id") == service_id:
                if svc.get("connected"):
                    return True, snap
                break

        time.sleep(1)

    return False, last_snapshot
