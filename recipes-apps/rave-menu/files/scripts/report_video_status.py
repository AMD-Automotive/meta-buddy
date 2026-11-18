#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""report_video_status.py - report display/video output status.

For each DRM connector on the AMD GPU it reports:
  * connection status (connected / disconnected)
  * sink type (HDMI vs DisplayPort, decoded from the EDID)
  * attached display name (from the EDID)
  * the active mode: resolution and refresh rate

Active mode + refresh come from the kernel's atomic state
(/sys/kernel/debug/dri/*/state), which needs root -- so the script re-execs
itself under sudo. EDID gives the sink type and monitor name; the DRM state
gives the real, currently-driven resolution and refresh.
"""
import glob
import os
import re
import sys

# debugfs (dri/*/state) needs root.
if os.geteuid() != 0:
    os.execvp("sudo", ["sudo", "-n", sys.executable, *sys.argv])

HDMI_OUIS = {0x000C03, 0xC45DD8}  # HDMI LLC (1.4), HDMI Forum (2.x)


def decode_edid(path):
    """Return (sink_type, display_name) from an EDID blob, or (None, None)."""
    try:
        with open(path, "rb") as f:
            edid = f.read()
    except OSError:
        return None, None
    if len(edid) < 128 or edid[0:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None, None

    # Monitor name: descriptor tag 0xFC in the four 18-byte descriptors.
    name = ""
    for base in (54, 72, 90, 108):
        d = edid[base:base + 18]
        if len(d) == 18 and d[0:2] == b"\x00\x00" and d[2] == 0 and d[3] == 0xFC:
            name = d[5:18].split(b"\n")[0].decode("ascii", "replace").strip()
            break

    # Sink type: look for an HDMI IEEE OUI in the CTA-861 extension block(s).
    sink = "DisplayPort"
    for ext_off in range(128, len(edid), 128):
        ext = edid[ext_off:ext_off + 128]
        if len(ext) < 128 or ext[0] != 0x02:  # CTA-861 tag
            continue
        dtd_off = ext[2]
        i = 4
        while i < dtd_off and i < 128:
            tag = ext[i] >> 5
            length = ext[i] & 0x1F
            if tag == 3 and length >= 3:  # vendor-specific data block
                oui = ext[i + 1] | (ext[i + 2] << 8) | (ext[i + 3] << 16)
                if oui in HDMI_OUIS:
                    sink = "HDMI"
            i += 1 + length
    return sink, name


def parse_drm_state():
    """Map connector name -> (resolution, refresh_hz) from the atomic state."""
    conn_to_crtc = {}
    crtc_to_mode = {}
    for state in glob.glob("/sys/kernel/debug/dri/*/state"):
        try:
            with open(state) as f:
                text = f.read()
        except OSError:
            continue

        # connector[NN]: DP-1  ... crtc=crtc-0
        cur = None
        for line in text.splitlines():
            m = re.match(r"connector\[\d+\]:\s+(\S+)", line)
            if m:
                cur = m.group(1)
                continue
            m = re.match(r"\s+crtc=(crtc-\d+)", line)
            if m and cur:
                conn_to_crtc[cur] = m.group(1)
                cur = None

        # crtc[NN]: crtc-0 ... mode: "3840x2160": 60 533280 ...
        cur = None
        for line in text.splitlines():
            m = re.match(r"crtc\[\d+\]:\s+(crtc-\d+)", line)
            if m:
                cur = m.group(1)
                continue
            m = re.search(r'mode:\s+"([^"]+)":\s+(\d+)', line)
            if m and cur:
                crtc_to_mode[cur] = (m.group(1), m.group(2))
                cur = None

    return {c: crtc_to_mode.get(crtc) for c, crtc in conn_to_crtc.items()}


def main():
    active = parse_drm_state()

    print("--- Video / Display Outputs ---")
    connectors = sorted(glob.glob("/sys/class/drm/card*-*"))
    any_connected = False
    for path in connectors:
        name = os.path.basename(path).split("-", 1)[1]  # card0-DP-1 -> DP-1
        try:
            status = open(os.path.join(path, "status")).read().strip()
        except OSError:
            continue
        if name.startswith("Writeback"):
            continue

        if status != "connected":
            print(f"  {name:<14}: disconnected")
            continue

        any_connected = True
        sink, disp = decode_edid(os.path.join(path, "edid"))
        sink = sink or "DisplayPort"
        mode = active.get(name)
        if mode:
            res, hz = mode
            mode_str = f"{res} @ {hz} Hz"
        else:
            # Fall back to the connector's preferred (first listed) mode.
            try:
                pref = open(os.path.join(path, "modes")).readline().strip()
            except OSError:
                pref = ""
            mode_str = f"{pref} (available)" if pref else "no active mode"

        label = f"{sink}"
        if disp:
            label += f" \u2014 {disp}"
        print(f"  {name:<14}: connected  [{label}]  {mode_str}")

    if not any_connected:
        print("  (no displays connected)")


if __name__ == "__main__":
    main()
