# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""ADAU1860 Codec control via kernel debugfs.

This module provides register access to the ADAU1860 codec through the
in-kernel ASoC driver's debugfs interface. The driver owns the SPI bus;
userspace accesses registers via:

  /sys/kernel/debug/adau1860/poke     — write "ADDR VAL" to set a register
  /sys/kernel/debug/adau1860/peek     — write "ADDR" to latch address
  /sys/kernel/debug/adau1860/peek_val — read "addr: val" result

High-level functions:
  - Reading codec status (PLL lock, vendor ID, power states)
  - DAC volume control (hardware level, independent of PulseAudio)
  - Output mode selection (headphone vs line-out)
  - Hardware mute
  - 4-channel DMIC configuration and level monitoring
  - Codec reinitialization
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

# --- Debugfs paths ---
DEBUGFS_BASE = Path("/sys/kernel/debug/adau1860")
DEBUGFS_POKE = DEBUGFS_BASE / "poke"
DEBUGFS_PEEK = DEBUGFS_BASE / "peek"
DEBUGFS_PEEK_VAL = DEBUGFS_BASE / "peek_val"

# --- Register addresses ---
REG_VENDOR_ID       = 0x4000C000
REG_CHIP_ID         = 0x4000C001
REG_ADC_DAC_HP_PWR  = 0x4000C004
REG_SAI_CLK_PWR     = 0x4000C007
REG_DSP_PWR         = 0x4000C008
REG_ASRC_PWR        = 0x4000C009
REG_CHIP_PWR        = 0x4000C00E
REG_CLK_CTRL1       = 0x4000C010
REG_CLK_CTRL2       = 0x4000C011
REG_CLK_CTRL3       = 0x4000C012
REG_CLK_CTRL13      = 0x4000C01C
REG_DAC_CTRL1       = 0x4000C050
REG_DAC_CTRL2       = 0x4000C051
REG_DAC_VOL0        = 0x4000C052
REG_DAC_ROUTE0      = 0x4000C053
REG_DAC_ROUTE1      = 0x4000C054
REG_HP0_CTRL        = 0x4000C060
REG_HP_LVMODE_CTRL2 = 0x4000C062
REG_HPLDO_CTRL      = 0x4000C066
REG_SPT0_CTRL1      = 0x4000C0E0
REG_SPT0_CTRL2      = 0x4000C0E1
REG_SPT0_CTRL3      = 0x4000C0E2
REG_SPT0_ROUTE0     = 0x4000C0E3
REG_SPT0_ROUTE1     = 0x4000C0E4
REG_SPT0_ROUTE2     = 0x4000C0E5
REG_SPT0_ROUTE3     = 0x4000C0E6
REG_DMIC_CTRL1      = 0x4000C030
REG_DMIC_CTRL2      = 0x4000C031
REG_DMIC_CTRL3      = 0x4000C032
REG_STATUS1         = 0x4000C401
REG_STATUS2         = 0x4000C402

# Expected values
VENDOR_ID_EXPECT = 0x41
STATUS2_PLL_LOCKED = 0xF1

# DAC volume constants
# 0x00 = +24dB (max), 0x40 = 0dB, 0xFF = -71.25dB (min)
DAC_VOL_MAX_DB = 24.0
DAC_VOL_MIN_DB = -71.25
DAC_VOL_STEP_DB = 0.375  # each step is 0.375 dB

# Thread lock for debugfs access
_lock = threading.Lock()


def _debugfs_available() -> tuple[bool, str]:
    """Check if debugfs interface is available."""
    if not DEBUGFS_BASE.exists():
        return False, "Codec driver not loaded (debugfs not found)"
    if not DEBUGFS_POKE.exists():
        return False, "debugfs/poke not found"
    if not DEBUGFS_PEEK.exists():
        return False, "debugfs/peek not found"
    return True, "ok"


def _read_reg(addr: int) -> int:
    """Read a register via debugfs peek interface."""
    # Write address to peek, then read result from peek_val
    DEBUGFS_PEEK.write_text(f"0x{addr:08X}\n")
    # Result format: "4000c000: 41\n" or "0x4000c000: 0x41\n"
    result = DEBUGFS_PEEK_VAL.read_text().strip()
    # Parse "addr: val" format - value is after the colon
    match = re.search(r":\s*(?:0x)?([0-9a-fA-F]+)", result)
    if match:
        return int(match.group(1), 16)
    raise ValueError(f"Cannot parse peek_val: {result}")


def _write_reg(addr: int, value: int) -> None:
    """Write a register via debugfs poke interface."""
    # Format: "ADDR VAL" - both hex
    DEBUGFS_POKE.write_text(f"0x{addr:08X} 0x{value:02X}\n")


# --- High-level API ---

def get_codec_status() -> dict:
    """Return comprehensive codec status."""
    result = {
        "available": False,
        "error": None,
        "vendor_id": None,
        "chip_id": None,
        "pll_locked": False,
        "status1": None,
        "status2": None,
        "chip_pwr": None,
        "dac_pwr": None,
        "hp_pwr": None,
        "dsp_pwr": None,
        "asrc_pwr": None,
        "clk_ctrl1": None,
        "clk_ctrl3": None,
        "dac_vol_raw": None,
        "dac_vol_db": None,
        "dac_muted": None,
        "output_mode": None,  # "headphone" or "lineout"
        "spt0_ctrl1": None,
        "dmic_enabled": False,
    }

    # Check debugfs availability first
    ok, err = _debugfs_available()
    if not ok:
        result["error"] = err
        return result

    with _lock:
        try:
            # Basic identification
            result["vendor_id"] = _read_reg(REG_VENDOR_ID)
            result["chip_id"] = _read_reg(REG_CHIP_ID)

            if result["vendor_id"] != VENDOR_ID_EXPECT:
                result["error"] = f"VENDOR_ID mismatch: 0x{result['vendor_id']:02X}"
                return result

            result["available"] = True

            # Status registers
            result["status1"] = _read_reg(REG_STATUS1)
            result["status2"] = _read_reg(REG_STATUS2)
            result["pll_locked"] = (result["status2"] & 0x01) == 1

            # Power registers
            result["chip_pwr"] = _read_reg(REG_CHIP_PWR)
            result["dac_pwr"] = _read_reg(REG_ADC_DAC_HP_PWR)
            result["hp_pwr"] = _read_reg(REG_HPLDO_CTRL)
            result["dsp_pwr"] = _read_reg(REG_DSP_PWR)
            result["asrc_pwr"] = _read_reg(REG_ASRC_PWR)

            # Clock config
            result["clk_ctrl1"] = _read_reg(REG_CLK_CTRL1)
            result["clk_ctrl3"] = _read_reg(REG_CLK_CTRL3)

            # DAC volume
            vol_raw = _read_reg(REG_DAC_VOL0)
            result["dac_vol_raw"] = vol_raw
            # Convert to dB: 0x00=+24dB, 0x40=0dB, each step is -0.375dB
            result["dac_vol_db"] = round(DAC_VOL_MAX_DB - (vol_raw * DAC_VOL_STEP_DB), 2)

            # DAC mute status (bit 6 = mute, bit 7 = force_mute)
            dac_ctrl2 = _read_reg(REG_DAC_CTRL2)
            result["dac_muted"] = bool(dac_ctrl2 & 0xC0)

            # Output mode: HP0_CTRL bit0: 0=headphone, 1=lineout
            hp_ctrl = _read_reg(REG_HP0_CTRL)
            result["output_mode"] = "lineout" if (hp_ctrl & 0x01) else "headphone"

            # SPT0 config
            result["spt0_ctrl1"] = _read_reg(REG_SPT0_CTRL1)

            # DMIC status
            sai_clk_pwr = _read_reg(REG_SAI_CLK_PWR)
            result["dmic_enabled"] = bool(sai_clk_pwr & 0x10)  # DMIC_CLK_EN

        except PermissionError:
            result["error"] = "Permission denied (need root for debugfs)"
        except Exception as e:
            result["error"] = str(e)

    return result


def set_dac_volume_db(db: float) -> tuple[bool, str]:
    """Set DAC volume in dB. Range: -71.25 to +24."""
    ok, err = _debugfs_available()
    if not ok:
        return False, err

    db = max(DAC_VOL_MIN_DB, min(DAC_VOL_MAX_DB, db))
    # Convert dB to raw: 0dB = 0x40, +24dB = 0x00, -71.25dB = 0xBF (clamped)
    raw = int(round((DAC_VOL_MAX_DB - db) / DAC_VOL_STEP_DB))
    raw = max(0, min(0xFF, raw))

    with _lock:
        try:
            _write_reg(REG_DAC_VOL0, raw)
            # Readback verify
            readback = _read_reg(REG_DAC_VOL0)
            if readback != raw:
                return False, f"Readback mismatch: wrote 0x{raw:02X}, got 0x{readback:02X}"
            return True, f"Volume set to {db:.1f} dB (0x{raw:02X})"
        except Exception as e:
            return False, str(e)


def set_dac_volume_percent(percent: int) -> tuple[bool, str]:
    """Set DAC volume as percentage (0-100). 100% = 0dB, 0% = -71.25dB."""
    percent = max(0, min(100, percent))
    # Map 0-100% to -71.25dB to 0dB (not +24dB to avoid clipping)
    db = DAC_VOL_MIN_DB + (percent / 100.0) * (0 - DAC_VOL_MIN_DB)
    return set_dac_volume_db(db)


def set_dac_mute(mute: bool) -> tuple[bool, str]:
    """Set DAC hardware mute."""
    ok, err = _debugfs_available()
    if not ok:
        return False, err

    with _lock:
        try:
            current = _read_reg(REG_DAC_CTRL2)
            if mute:
                new_val = current | 0x40  # Set DAC0_MUTE bit
            else:
                new_val = current & ~0xC0  # Clear both MUTE and FORCE_MUTE
            _write_reg(REG_DAC_CTRL2, new_val)
            return True, "Muted" if mute else "Unmuted"
        except Exception as e:
            return False, str(e)


def set_output_mode(mode: str) -> tuple[bool, str]:
    """Set output mode: 'headphone' or 'lineout'."""
    if mode not in ("headphone", "lineout"):
        return False, "Invalid mode. Use 'headphone' or 'lineout'."

    ok, err = _debugfs_available()
    if not ok:
        return False, err

    with _lock:
        try:
            current = _read_reg(REG_HP0_CTRL)
            if mode == "lineout":
                new_val = current | 0x01
            else:
                new_val = current & ~0x01
            _write_reg(REG_HP0_CTRL, new_val)
            return True, f"Output mode set to {mode}"
        except Exception as e:
            return False, str(e)


def get_dmic_levels() -> dict:
    """Get DMIC configuration and basic status."""
    result = {
        "available": False,
        "enabled": False,
        "routes": [],
        "error": None,
    }

    ok, err = _debugfs_available()
    if not ok:
        result["error"] = err
        return result

    with _lock:
        try:
            vendor = _read_reg(REG_VENDOR_ID)
            if vendor != VENDOR_ID_EXPECT:
                result["error"] = "Codec not responding"
                return result

            result["available"] = True

            sai_clk = _read_reg(REG_SAI_CLK_PWR)
            result["enabled"] = bool(sai_clk & 0x10)

            # Read SPT0 routing
            routes = []
            for i, addr in enumerate([REG_SPT0_ROUTE0, REG_SPT0_ROUTE1,
                                      REG_SPT0_ROUTE2, REG_SPT0_ROUTE3]):
                val = _read_reg(addr)
                # Decode source
                source_map = {
                    0x27: "DMIC0", 0x28: "DMIC1", 0x29: "DMIC2", 0x2A: "DMIC3",
                    0x24: "ADC0", 0x25: "ADC1",
                    0x10: "TDSP0", 0x11: "TDSP1", 0x12: "TDSP2", 0x13: "TDSP3",
                    0x00: "SAI0_0", 0x01: "SAI0_1",
                }
                source = source_map.get(val, f"0x{val:02X}")
                routes.append({"slot": i, "source": source, "raw": val})
            result["routes"] = routes

        except Exception as e:
            result["error"] = str(e)

    return result


def set_asrc_bypass(bypass: bool) -> tuple[bool, str]:
    """Enable or disable ASRC. bypass=True means ASRC off (direct path)."""
    ok, err = _debugfs_available()
    if not ok:
        return False, err

    with _lock:
        try:
            if bypass:
                _write_reg(REG_ASRC_PWR, 0x00)
                # Route DAC directly from SAI
                _write_reg(REG_DAC_ROUTE0, 0x00)  # SAI0 slot 0
                _write_reg(REG_DAC_ROUTE1, 0x01)  # SAI0 slot 1
            else:
                _write_reg(REG_ASRC_PWR, 0x01)  # Enable ASRC
                # Route DAC through ASRC
                _write_reg(REG_DAC_ROUTE0, 0x08)  # ASRCI0
                _write_reg(REG_DAC_ROUTE1, 0x09)  # ASRCI1
            return True, "ASRC bypassed" if bypass else "ASRC enabled"
        except Exception as e:
            return False, str(e)


def reinit_codec() -> tuple[bool, str]:
    """Reinitialize codec with known-good configuration.

    This applies the critical registers for BCLK0 slave mode with
    PLL locked to the incoming bit clock from Ryzen ACP.
    """
    ok, err = _debugfs_available()
    if not ok:
        return False, err

    # Core init sequence (from BCLK0_LOCK_SEQUENCE in lark-spi.py)
    init_regs = [
        # PLL configuration
        (REG_CLK_CTRL2, 0x01),   # PLL_INPUT_PRESCALER=1
        (REG_CLK_CTRL3, 0x10),   # PLL_INTEGER_DIVIDER=16 (1.536MHz x16 = 24.576MHz)
        (0x4000C013, 0x00),      # CLK_CTRL4 MSB=0
        (0x4000C014, 0x00),      # CLK_CTRL5 PLL_NUMERATOR LSB
        (0x4000C015, 0x00),      # CLK_CTRL6 PLL_NUMERATOR MSB
        (0x4000C016, 0x00),      # CLK_CTRL7 PLL_DENOMINATOR LSB
        (0x4000C017, 0x00),      # CLK_CTRL8 PLL_DENOMINATOR MSB
        (REG_CLK_CTRL13, 0x10),  # MCLK_FREQ_INDEX=24.576MHz
        (REG_CLK_CTRL1, 0xC2),   # SYNC=INTERN, PLL_TYPE=INT, PLL_SOURCE=BCLK0
        (0x4000C018, 0x01),      # CLK_CTRL9 PLL_UPDATE
        # DAC routing
        (REG_DAC_ROUTE0, 0x00),  # SAI0 slot 0 -> Left DAC
        (REG_DAC_ROUTE1, 0x01),  # SAI0 slot 1 -> Right DAC
        (REG_ASRC_PWR, 0x00),    # ASRC off
        # I2S config
        (REG_SPT0_CTRL1, 0x80),  # 16-bit slot, 16-bit data, I2S, stereo
        # DMIC routing
        (REG_SAI_CLK_PWR, 0x13), # SPT0_IN_EN | SPT0_OUT_EN | DMIC_CLK_EN
        (REG_SPT0_ROUTE0, 0x29), # DMIC2 (top-left)
        (REG_SPT0_ROUTE1, 0x2A), # DMIC3 (bottom-left)
        (REG_SPT0_ROUTE2, 0x27), # DMIC0 (top-right)
        (REG_SPT0_ROUTE3, 0x28), # DMIC1 (bottom-right)
        # Power/unmute
        (REG_CHIP_PWR, 0x07),    # Enable
        (REG_ADC_DAC_HP_PWR, 0x10),  # PB0 (DAC) on
        (REG_DAC_CTRL2, 0x04),   # DAC unmuted
        (REG_DAC_VOL0, 0x40),    # 0 dB
    ]

    with _lock:
        try:
            # Check vendor ID first
            vendor = _read_reg(REG_VENDOR_ID)
            if vendor != VENDOR_ID_EXPECT:
                return False, f"Codec not responding (VENDOR_ID=0x{vendor:02X})"

            # Apply init sequence
            import time
            for addr, val in init_regs:
                _write_reg(addr, val)
                time.sleep(0.002)  # 2ms inter-write delay

            # Check PLL lock
            time.sleep(0.1)
            status2 = _read_reg(REG_STATUS2)
            pll_locked = (status2 & 0x01) == 1

            if pll_locked:
                return True, f"Codec reinitialized, PLL locked (STATUS2=0x{status2:02X})"
            else:
                return True, f"Codec reinitialized, PLL NOT locked (STATUS2=0x{status2:02X}) - check BCLK"

        except Exception as e:
            return False, str(e)


def read_register(addr: int) -> tuple[bool, int, str]:
    """Read a single register. Returns (ok, value, message)."""
    ok, err = _debugfs_available()
    if not ok:
        return False, 0, err

    with _lock:
        try:
            val = _read_reg(addr)
            return True, val, f"0x{addr:08X} = 0x{val:02X}"
        except Exception as e:
            return False, 0, str(e)


def write_register(addr: int, value: int) -> tuple[bool, str]:
    """Write a single register."""
    ok, err = _debugfs_available()
    if not ok:
        return False, err

    with _lock:
        try:
            _write_reg(addr, value & 0xFF)
            readback = _read_reg(addr)
            if readback == (value & 0xFF):
                return True, f"0x{addr:08X} <= 0x{value:02X} (OK)"
            else:
                return False, f"0x{addr:08X} <= 0x{value:02X}, readback 0x{readback:02X} (MISMATCH)"
        except Exception as e:
            return False, str(e)


def codec_snapshot() -> dict:
    """Return full snapshot for UI."""
    status = get_codec_status()
    dmic = get_dmic_levels()
    return {
        "status": status,
        "dmic": dmic,
    }


# --- DMIC Audio Level Metering (Streaming) ---

import threading
import struct
import math
import subprocess
import time

# ALSA capture device for DMIC array (hw:card,device)
# hw:1,1 = adau1860-sai0 (DMIC), hw:1,0 = nau8822-hifi (analog)
DMIC_CAPTURE_DEV = os.environ.get("ADAU1860_CAPTURE_DEV", "hw:1,1")
DMIC_SAMPLE_RATE = 48000
DMIC_CHANNELS = 2  # Capture device is stereo
DMIC_PERIOD_FRAMES = 256  # ~5.3ms periods for low latency

# DMIC mux routing — driver defaults route through TDSP which needs firmware.
# Set DMIC direct passthrough via ALSA mixer before any capture.
_DMIC_MUX_SETTINGS = [
    ("SPT0_OUT_SLOT0 Data Mux", "DMIC0"),
    ("SPT0_OUT_SLOT1 Data Mux", "DMIC1"),
]


def _ensure_dmic_routing():
    """Set ALSA mux controls to route DMICs directly to SAI0 capture slots."""
    for ctl, val in _DMIC_MUX_SETTINGS:
        subprocess.run(
            ["amixer", "-c1", "cset", f"name={ctl}", val],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


class DmicMeter:
    """Persistent streaming DMIC level meter using arecord pipe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._thread = None
        self._running = False
        self._levels = [
            {"channel": 0, "rms": 0.0, "db": -96.0, "peak": 0.0, "peak_db": -96.0},
            {"channel": 1, "rms": 0.0, "db": -96.0, "peak": 0.0, "peak_db": -96.0},
        ]
        self._error = None
        self._last_update = 0
        # Peak hold with decay
        self._peak_hold = [0.0, 0.0]
        self._peak_decay = 0.95  # Decay factor per update

    def start(self):
        """Start the streaming capture."""
        with self._lock:
            if self._running:
                return True

            _ensure_dmic_routing()

            try:
                # Start arecord in streaming mode
                cmd = [
                    "arecord",
                    "-D", DMIC_CAPTURE_DEV,
                    "-f", "S16_LE",
                    "-r", str(DMIC_SAMPLE_RATE),
                    "-c", str(DMIC_CHANNELS),
                    "-t", "raw",
                    "--period-size", str(DMIC_PERIOD_FRAMES),
                    "--buffer-size", str(DMIC_PERIOD_FRAMES * 4),
                    "-q",
                ]
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
                self._running = True
                self._error = None

                # Start reader thread
                self._thread = threading.Thread(target=self._reader_loop, daemon=True)
                self._thread.start()
                return True

            except Exception as e:
                self._error = str(e)
                return False

    def stop(self):
        """Stop the streaming capture."""
        with self._lock:
            self._running = False
            if self._proc:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1.0)
                except:
                    self._proc.kill()
                self._proc = None

    def _reader_loop(self):
        """Background thread that reads audio and calculates levels."""
        bytes_per_frame = DMIC_CHANNELS * 2  # S16_LE
        chunk_frames = DMIC_PERIOD_FRAMES * 2  # ~10ms chunks
        chunk_bytes = chunk_frames * bytes_per_frame

        while self._running and self._proc and self._proc.poll() is None:
            try:
                data = self._proc.stdout.read(chunk_bytes)
                if not data:
                    break

                # Parse and calculate levels
                actual_frames = len(data) // bytes_per_frame
                if actual_frames < 10:
                    continue

                # De-interleave channels
                ch_samples = [[], []]
                for i in range(actual_frames):
                    offset = i * bytes_per_frame
                    for ch in range(DMIC_CHANNELS):
                        sample = struct.unpack_from("<h", data, offset + ch * 2)[0]
                        ch_samples[ch].append(sample / 32768.0)

                # Calculate levels for each channel
                new_levels = []
                for ch in range(DMIC_CHANNELS):
                    samples = ch_samples[ch]
                    if not samples:
                        new_levels.append(self._levels[ch])
                        continue

                    # RMS
                    sum_sq = sum(s * s for s in samples)
                    rms = math.sqrt(sum_sq / len(samples))

                    # Peak
                    peak = max(abs(s) for s in samples)

                    # Update peak hold with decay
                    if peak > self._peak_hold[ch]:
                        self._peak_hold[ch] = peak
                    else:
                        self._peak_hold[ch] *= self._peak_decay

                    # Convert to dB
                    db = 20.0 * math.log10(max(rms, 1e-10))
                    peak_db = 20.0 * math.log10(max(self._peak_hold[ch], 1e-10))
                    db = max(-96.0, min(0.0, db))
                    peak_db = max(-96.0, min(0.0, peak_db))

                    new_levels.append({
                        "channel": ch,
                        "rms": round(rms, 6),
                        "db": round(db, 1),
                        "peak": round(self._peak_hold[ch], 6),
                        "peak_db": round(peak_db, 1),
                    })

                with self._lock:
                    self._levels = new_levels
                    self._last_update = time.time()

            except Exception as e:
                with self._lock:
                    self._error = str(e)
                break

        # Process ended
        with self._lock:
            if self._running:
                # Unexpected termination
                if self._proc:
                    stderr = self._proc.stderr.read().decode("utf-8", errors="replace").strip()
                    self._error = f"arecord exited: {stderr or 'unknown'}"
            self._running = False

    def get_levels(self) -> dict:
        """Get current levels (non-blocking)."""
        with self._lock:
            return {
                "ok": self._running and self._error is None,
                "error": self._error,
                "channels": list(self._levels),
                "streaming": self._running,
                "sample_rate": DMIC_SAMPLE_RATE,
            }

    @property
    def is_running(self):
        with self._lock:
            return self._running


# Global meter instance
_dmic_meter = None
_dmic_meter_lock = threading.Lock()


def get_dmic_meter() -> DmicMeter:
    """Get or create the global DMIC meter instance."""
    global _dmic_meter
    with _dmic_meter_lock:
        if _dmic_meter is None:
            _dmic_meter = DmicMeter()
        return _dmic_meter


def start_dmic_metering() -> dict:
    """Start the streaming DMIC meter."""
    meter = get_dmic_meter()
    ok = meter.start()
    return {"ok": ok, "error": None if ok else "Failed to start"}


def stop_dmic_metering() -> dict:
    """Stop the streaming DMIC meter."""
    meter = get_dmic_meter()
    meter.stop()
    return {"ok": True}


def get_dmic_levels() -> dict:
    """Get current DMIC levels from the streaming meter."""
    meter = get_dmic_meter()
    return meter.get_levels()


# --- Loopback Test (Record + Playback) ---

# hw:1,1 = adau1860-sai0 for DMIC capture.
# Playback goes through the user's PulseAudio sink (whichever one is
# currently selected in the Audio menu) so users hear the loopback
# through the same output as YouTube / other apps. Set
# ADAU1860_PLAYBACK_DEV to override with an ALSA hw:X,Y device for a
# bypass-PA path (e.g. when validating a specific codec output).
LOOPBACK_CAPTURE_DEV = os.environ.get("ADAU1860_CAPTURE_DEV", "hw:1,1")
LOOPBACK_PLAYBACK_DEV = os.environ.get("ADAU1860_PLAYBACK_DEV", "")  # "" = paplay via PA
LOOPBACK_PA_USER = os.environ.get("ADAU1860_PA_USER", "weston")
LOOPBACK_SILENCE_THRESHOLD = int(os.environ.get("ADAU1860_SILENCE_THRESHOLD", "32"))  # peak abs S16 value
LOOPBACK_DURATION = 5  # seconds
LOOPBACK_SAMPLE_RATE = 48000
LOOPBACK_CHANNELS = 2
LOOPBACK_FORMAT = "S16_LE"

_loopback_lock = threading.Lock()
_loopback_state = {
    "status": "idle",  # idle, recording, playing, done, error
    "progress": 0,
    "error": None,
    "thread": None,
}


def _wav_peak_s16le(path: str) -> int:
    """Return the maximum absolute S16_LE sample value in a WAV file.

    Used to detect silent captures (no microphone signal reaching the
    SAI stream) so the loopback test reports a meaningful error instead
    of silently playing a zero-amplitude buffer.
    """
    try:
        import wave
        with wave.open(path, "rb") as wf:
            if wf.getsampwidth() != 2:
                return -1  # not S16; skip the check
            frames = wf.readframes(wf.getnframes())
    except Exception:
        return -1
    if not frames:
        return 0
    # Sample 1 in every 16 frames is plenty for a peak estimate.
    stride = 32  # 16 frames * 2 bytes
    peak = 0
    for i in range(0, len(frames) - 1, stride):
        v = frames[i] | (frames[i + 1] << 8)
        if v & 0x8000:
            v -= 0x10000
        a = -v if v < 0 else v
        if a > peak:
            peak = a
    return peak


def _build_playback_cmd(path: str) -> tuple[list[str], dict]:
    """Build the playback subprocess command + env.

    When LOOPBACK_PLAYBACK_DEV is set, use aplay with that ALSA device.
    Otherwise route through the PA session user's PulseAudio so the
    audio comes out of whichever sink is selected in the Audio menu.
    """
    if LOOPBACK_PLAYBACK_DEV:
        return (["aplay", "-D", LOOPBACK_PLAYBACK_DEV, "-q", path], dict(os.environ))

    # paplay against the session user's PulseAudio
    try:
        import pwd
        uid = pwd.getpwnam(LOOPBACK_PA_USER).pw_uid
    except Exception as e:
        raise RuntimeError(f"PulseAudio user '{LOOPBACK_PA_USER}' not found: {e}")

    cmd = [
        "/usr/bin/sudo", "-n",
        "-u", LOOPBACK_PA_USER,
        "env",
        f"XDG_RUNTIME_DIR=/run/user/{uid}",
        f"PULSE_RUNTIME_PATH=/run/user/{uid}/pulse",
        "/usr/bin/paplay",
        path,
    ]
    return cmd, dict(os.environ)


def _loopback_thread():
    """Background thread for record + playback loopback test."""
    global _loopback_state
    import tempfile

    tmp_file = None
    try:
        # Create temp file for the recording
        tmp_fd, tmp_file = tempfile.mkstemp(suffix=".wav")
        os.close(tmp_fd)

        # Stop the streaming meter if running (to free the device)
        meter = get_dmic_meter()
        was_metering = meter.is_running
        if was_metering:
            meter.stop()
            time.sleep(0.2)

        _ensure_dmic_routing()

        # Phase 1: Record
        with _loopback_lock:
            _loopback_state["status"] = "recording"
            _loopback_state["progress"] = 0

        record_cmd = [
            "arecord",
            "-D", LOOPBACK_CAPTURE_DEV,
            "-f", LOOPBACK_FORMAT,
            "-r", str(LOOPBACK_SAMPLE_RATE),
            "-c", str(LOOPBACK_CHANNELS),
            "-d", str(LOOPBACK_DURATION),
            "-q",
            tmp_file,
        ]

        proc = subprocess.Popen(record_cmd, stderr=subprocess.PIPE)

        # Wait for recording to complete, updating progress
        start_time = time.time()
        while proc.poll() is None:
            elapsed = time.time() - start_time
            with _loopback_lock:
                _loopback_state["progress"] = min(50, int((elapsed / LOOPBACK_DURATION) * 50))
            time.sleep(0.1)

        if proc.returncode != 0:
            stderr = proc.stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Recording failed: {stderr}")

        with _loopback_lock:
            _loopback_state["progress"] = 50

        # Sanity check the recorded buffer so a silent capture path is
        # reported clearly instead of producing an inaudible playback.
        peak = _wav_peak_s16le(tmp_file)
        if 0 <= peak < LOOPBACK_SILENCE_THRESHOLD:
            raise RuntimeError(
                f"Recorded buffer is silent (peak={peak} < {LOOPBACK_SILENCE_THRESHOLD}). "
                "DMIC frames are not reaching the SAI capture stream; check "
                "ADAU1860 DMIC clock + SAI0 routing."
            )

        # Phase 2: Playback
        with _loopback_lock:
            _loopback_state["status"] = "playing"

        play_cmd, play_env = _build_playback_cmd(tmp_file)
        proc = subprocess.Popen(play_cmd, stderr=subprocess.PIPE, env=play_env)

        # Wait for playback to complete
        start_time = time.time()
        while proc.poll() is None:
            elapsed = time.time() - start_time
            with _loopback_lock:
                _loopback_state["progress"] = min(100, 50 + int((elapsed / LOOPBACK_DURATION) * 50))
            time.sleep(0.1)

        if proc.returncode != 0:
            stderr = proc.stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Playback failed: {stderr}")

        with _loopback_lock:
            _loopback_state["status"] = "done"
            _loopback_state["progress"] = 100

        # Restart metering if it was running
        if was_metering:
            time.sleep(0.2)
            meter.start()

    except Exception as e:
        with _loopback_lock:
            _loopback_state["status"] = "error"
            _loopback_state["error"] = str(e)

    finally:
        # Cleanup temp file
        if tmp_file and os.path.exists(tmp_file):
            try:
                os.unlink(tmp_file)
            except:
                pass

        with _loopback_lock:
            _loopback_state["thread"] = None


def start_loopback_test() -> dict:
    """Start a 5-second record + playback loopback test."""
    global _loopback_state

    with _loopback_lock:
        if _loopback_state["thread"] is not None and _loopback_state["thread"].is_alive():
            return {"ok": False, "error": "Loopback test already in progress"}

        _loopback_state["status"] = "starting"
        _loopback_state["progress"] = 0
        _loopback_state["error"] = None

        thread = threading.Thread(target=_loopback_thread, daemon=True)
        _loopback_state["thread"] = thread
        thread.start()

    return {"ok": True, "message": "Loopback test started"}


def get_loopback_status() -> dict:
    """Get the current loopback test status."""
    with _loopback_lock:
        return {
            "ok": True,
            "status": _loopback_state["status"],
            "progress": _loopback_state["progress"],
            "error": _loopback_state["error"],
            "duration": LOOPBACK_DURATION,
        }
