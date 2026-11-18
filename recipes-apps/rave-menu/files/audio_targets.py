# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Generic audio input metering + loopback engine.

A single registry (``TARGETS``) of capture-capable audio targets — the DMIC
array, analog mics, and anything added later. Each target declares its ALSA
capture device, channel layout, optional codec routing, and record/playback
behaviour.

Presence is detected from the ALSA capture device itself
(``/proc/asound/cardN/pcmDc``), so a target appears in the UI whenever its
sound card is actually present — no dependency on a vendor debugfs node.

Add a new input by appending an ``AudioTarget`` to ``TARGETS``; the metering
UI, loopback test, and unified audio state all pick it up automatically.
"""

import math
import os
import struct
import subprocess
import threading
import time
import wave
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


# -- Format table --

_FORMAT_INFO = {
    "S16_LE": {"bytes": 2, "max_val": 32768.0, "struct_fmt": "<h"},
    "S32_LE": {"bytes": 4, "max_val": 2147483648.0, "struct_fmt": "<i"},
}

LOOPBACK_DURATION = int(os.environ.get("RAVE_LOOPBACK_DURATION", "5"))
SILENCE_THRESHOLD = int(os.environ.get("RAVE_SILENCE_THRESHOLD", "32"))


@dataclass
class AudioTarget:
    """Declarative description of a capture-capable audio input."""

    key: str
    label: str
    capture_dev: str                  # ALSA device, e.g. "hw:1,1"
    channels: int                     # number of capture channels to meter
    channel_labels: List[str]         # per-channel display labels
    fmt: str = "S16_LE"
    rate: int = 48000
    # amixer cset routing applied before capture, one (control, value) per slot
    routing: List[Tuple[str, str]] = field(default_factory=list)
    # amixer cset capture-gain controls applied in full before capture
    capture_gain: List[Tuple[str, str]] = field(default_factory=list)
    # hold the bit clock with a short silence playback while capturing
    keep_bclk: bool = False
    # fixed 2-channel pairs for record/playback (None => use channels as-is)
    record_pairs: Optional[List[List[int]]] = None
    # playback path for the loopback test: "default" (system output) or "nau8822"
    playback: str = "default"
    # case-insensitive substring matched against a sink's node.name OR
    # node.description to pick the playback sink (used when playback != default)
    playback_match: str = ""

    @property
    def card_index(self) -> str:
        """Card index parsed from ``capture_dev`` ("hw:1,1" -> "1")."""
        try:
            return self.capture_dev.split(":", 1)[1].split(",", 1)[0]
        except (IndexError, ValueError):
            return "0"

    @property
    def device_index(self) -> str:
        """PCM device index parsed from ``capture_dev`` ("hw:1,1" -> "1")."""
        try:
            return self.capture_dev.split(",", 1)[1]
        except (IndexError, ValueError):
            return "0"

    @property
    def fmt_info(self) -> dict:
        return _FORMAT_INFO.get(self.fmt, _FORMAT_INFO["S16_LE"])


# -- Registry --

TARGETS = {
    "adau1860_dmic": AudioTarget(
        key="adau1860_dmic",
        label="ADAU1860 DMIC Array",
        capture_dev=os.environ.get("ADAU1860_CAPTURE_DEV", "hw:1,1"),
        channels=int(os.environ.get("ADAU1860_DMIC_CHANNELS", "4")),
        channel_labels=["Ch 1", "Ch 2", "Ch 3", "Ch 4"],
        fmt=os.environ.get("ADAU1860_DMIC_FORMAT", "S16_LE"),
        rate=48000,
        routing=[
            ("SPT0_OUT_SLOT0 Data Mux", "DMIC0"),
            ("SPT0_OUT_SLOT1 Data Mux", "DMIC1"),
            ("SPT0_OUT_SLOT2 Data Mux", "DMIC2"),
            ("SPT0_OUT_SLOT3 Data Mux", "DMIC3"),
        ],
        # DMIC digital capture gain. Codec default (191) is ~0 dB unity, which
        # records ambient/voice too quietly to hear on playback. 238 ~= +18 dB
        # (scale: 0=-71.25 dB .. 255=+24 dB) gives an audible level with
        # headroom. Override via ADAU1860_DMIC_GAIN.
        capture_gain=[
            ("DMIC0 Capture Volume", os.environ.get("ADAU1860_DMIC_GAIN", "238")),
            ("DMIC1 Capture Volume", os.environ.get("ADAU1860_DMIC_GAIN", "238")),
            ("DMIC2 Capture Volume", os.environ.get("ADAU1860_DMIC_GAIN", "238")),
            ("DMIC3 Capture Volume", os.environ.get("ADAU1860_DMIC_GAIN", "238")),
        ],
        keep_bclk=True,
        record_pairs=[[0, 1], [2, 3]],
        # The board's audible analog output is the NAU8822; the ADAU1860 DAC
        # sink is not wired to the listening output. Route the DMIC monitor
        # playback there so the recording is heard.
        playback=os.environ.get("ADAU1860_PLAYBACK", "nau8822"),
        playback_match="nau8822 analog",
    ),
    "nau8822_mic": AudioTarget(
        key="nau8822_mic",
        label="NAU8822 Analog Mic",
        capture_dev=os.environ.get("NAU8822_CAPTURE_DEV", "hw:1,0"),
        channels=2,
        channel_labels=["L", "R"],
        fmt=os.environ.get("NAU8822_FORMAT", "S16_LE"),
        rate=48000,
        playback="nau8822",
        playback_match="nau8822 analog",
    ),
}


def get_target(key: str) -> Optional[AudioTarget]:
    return TARGETS.get(key)


def list_keys() -> List[str]:
    return list(TARGETS.keys())


# -- Presence detection (ALSA capture device) --

def is_present(target: AudioTarget) -> bool:
    """True if the target's ALSA capture device exists on this system."""
    proc_path = Path(
        f"/proc/asound/card{target.card_index}/pcm{target.device_index}c"
    )
    return proc_path.exists()


# -- Codec routing --

def _ensure_routing(target: AudioTarget):
    """Apply the target's ALSA mux routing for the active channel count."""
    card = target.card_index
    for ctl, val in target.capture_gain:
        subprocess.run(
            ["amixer", "-c", card, "cset", f"name={ctl}", val],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    if not target.routing:
        return
    for ctl, val in target.routing[: target.channels]:
        subprocess.run(
            ["amixer", "-c", card, "cset", f"name={ctl}", val],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


# -- Streaming meter --

class Meter:
    """Persistent streaming level meter for one audio target.

    Reads PCM from the capture device in a background thread and computes
    per-channel RMS / peak. The frontend polls the cached levels.
    """

    def __init__(self, target: AudioTarget):
        self._t = target
        self._channels = target.channels
        self._lock = threading.Lock()
        self._proc = None
        self._bclk_proc = None
        self._thread = None
        self._running = False
        self._levels = self._empty_levels()
        self._error = None
        self._last_update = 0.0
        self._peak_hold = [0.0] * self._channels
        self._peak_decay = 0.95

    def _empty_levels(self) -> list:
        return [
            {"channel": ch, "rms": 0.0, "db": -96.0, "peak": 0.0, "peak_db": -96.0}
            for ch in range(self._channels)
        ]

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return True
            _ensure_routing(self._t)
            try:
                if self._t.keep_bclk:
                    # Hold the bit clock with a short silence playback so the
                    # codec PLL stays locked while capturing.
                    self._bclk_proc = subprocess.Popen(
                        ["aplay", "-D", self._t.capture_dev, "-f", self._t.fmt,
                         "-r", str(self._t.rate), "-c", "2",
                         "-t", "raw", "-q", "/dev/zero"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    time.sleep(0.1)

                cmd = [
                    "arecord",
                    "-D", self._t.capture_dev,
                    "-f", self._t.fmt,
                    "-r", str(self._t.rate),
                    "-c", str(self._channels),
                    "-t", "raw",
                    "--period-size", "256",
                    "--buffer-size", "1024",
                    "-q",
                ]
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                )
                self._running = True
                self._error = None
                self._peak_hold = [0.0] * self._channels
                self._thread = threading.Thread(target=self._reader_loop, daemon=True)
                self._thread.start()
                return True
            except Exception as e:
                self._error = str(e)
                return False

    def stop(self):
        with self._lock:
            self._running = False
            if self._proc:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                self._proc = None
            if self._bclk_proc:
                self._bclk_proc.terminate()
                try:
                    self._bclk_proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._bclk_proc.kill()
                self._bclk_proc = None

    def _reader_loop(self):
        fmt_info = self._t.fmt_info
        bytes_per_sample = fmt_info["bytes"]
        bytes_per_frame = self._channels * bytes_per_sample
        max_val = fmt_info["max_val"]
        struct_fmt = fmt_info["struct_fmt"]

        chunk_frames = int(self._t.rate * 0.1)
        chunk_bytes = chunk_frames * bytes_per_frame

        while self._running and self._proc and self._proc.poll() is None:
            try:
                data = self._proc.stdout.read(chunk_bytes)
                if not data:
                    break

                actual_frames = len(data) // bytes_per_frame
                if actual_frames < 10:
                    continue

                new_levels = []
                for ch in range(self._channels):
                    sum_sq = 0.0
                    peak = 0.0
                    count = 0
                    for i in range(actual_frames):
                        offset = i * bytes_per_frame + ch * bytes_per_sample
                        if offset + bytes_per_sample > len(data):
                            break
                        raw = struct.unpack_from(struct_fmt, data, offset)[0]
                        sample = raw / max_val
                        sum_sq += sample * sample
                        abs_s = abs(sample)
                        if abs_s > peak:
                            peak = abs_s
                        count += 1

                    if count == 0:
                        new_levels.append(
                            self._levels[ch] if ch < len(self._levels) else
                            {"channel": ch, "rms": 0.0, "db": -96.0,
                             "peak": 0.0, "peak_db": -96.0})
                        continue

                    rms = math.sqrt(sum_sq / count)
                    if peak > self._peak_hold[ch]:
                        self._peak_hold[ch] = peak
                    else:
                        self._peak_hold[ch] *= self._peak_decay

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

        with self._lock:
            if self._running and self._proc:
                try:
                    stderr = self._proc.stderr.read().decode(
                        "utf-8", errors="replace").strip()
                except Exception:
                    stderr = "unknown"
                self._error = f"arecord exited: {stderr}"
            self._running = False

    def get_levels(self) -> dict:
        with self._lock:
            return {
                "channels": [lv["rms"] for lv in self._levels],
                "peak": [lv["peak"] for lv in self._levels],
                "details": list(self._levels),
                "streaming": self._running,
                "error": self._error,
            }

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running


# -- Per-target meter instances --

_meters = {}
_meters_lock = threading.Lock()


def get_meter(key: str) -> Optional[Meter]:
    target = TARGETS.get(key)
    if target is None:
        return None
    with _meters_lock:
        m = _meters.get(key)
        if m is None:
            m = Meter(target)
            _meters[key] = m
        return m


def start_metering(key: str) -> dict:
    m = get_meter(key)
    if m is None:
        return {"ok": False, "error": f"unknown input: {key}"}
    ok = m.start()
    return {"ok": ok, "error": None if ok else "Failed to start"}


def stop_metering(key: str) -> dict:
    m = get_meter(key)
    if m is None:
        return {"ok": False, "error": f"unknown input: {key}"}
    m.stop()
    return {"ok": True}


def get_levels(key: str) -> dict:
    m = get_meter(key)
    if m is None:
        return {"channels": [], "peak": [], "details": [],
                "streaming": False, "error": f"unknown input: {key}"}
    return m.get_levels()


def input_state(key: str) -> dict:
    """Base state for the unified audio endpoint (presence + metering)."""
    target = TARGETS.get(key)
    if target is None:
        return {"available": False, "channels": 0, "metering_active": False}
    m = get_meter(key)
    return {
        "available": is_present(target),
        "channels": target.channels,
        "channel_labels": list(target.channel_labels[: target.channels]),
        "record_pairs": target.record_pairs,
        "metering_active": bool(m and m.is_running),
    }


# -- Loopback record / playback --

_loopback_locks = {}
_loopback_states = {}
_loopback_registry_lock = threading.Lock()


def _loopback_slot(key: str):
    with _loopback_registry_lock:
        if key not in _loopback_locks:
            _loopback_locks[key] = threading.Lock()
            _loopback_states[key] = {
                "status": "idle", "progress": 0, "error": None,
                "channel_results": None, "thread": None,
            }
        return _loopback_locks[key], _loopback_states[key]


def _find_pw_sink_node(user: str, uid: int, match: str):
    """Return a PipeWire sink node id whose node.name or node.description
    contains ``match`` (case-insensitive).

    Matching description as well as name is important on this board: every
    ACP sink's node.name contains the card name "acp70nau8822", so a bare
    "nau8822" name match is ambiguous between the NAU8822 analog output and
    the ADAU1860 DAC. The description ("NAU8822 analog speaker ...") is unique.
    """
    needle = match.lower()
    try:
        import json
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", "-u", user, "env",
             f"XDG_RUNTIME_DIR=/run/user/{uid}", "pw-dump"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        for node in data:
            if not isinstance(node, dict):
                continue
            props = (node.get("info") or {}).get("props") or {}
            if props.get("media.class") != "Audio/Sink":
                continue
            name = (props.get("node.name") or "").lower()
            desc = (props.get("node.description") or "").lower()
            if needle in name or needle in desc:
                return node["id"]
    except Exception:
        pass
    return None


def _build_play_cmd(target: AudioTarget, wav_path: str) -> list:
    """Build the playback command for the loopback test."""
    import pwd as pwd_mod
    user = os.environ.get("RAVE_AUDIO_USER", "weston")
    try:
        uid = pwd_mod.getpwnam(user).pw_uid
    except KeyError:
        uid = None

    if target.playback == "nau8822":
        u = uid if uid is not None else 1000
        cmd = ["/usr/bin/sudo", "-n", "-u", user, "env",
               f"XDG_RUNTIME_DIR=/run/user/{u}", "pw-play"]
        match = target.playback_match or "nau8822 analog"
        node = _find_pw_sink_node(user, u, match)
        if node:
            cmd += ["--target", str(node)]
        cmd.append(wav_path)
        return cmd

    # default: route through the session user's PulseAudio/PipeWire
    playback_dev = os.environ.get("RAVE_PLAYBACK_DEV", "")
    if playback_dev:
        return ["aplay", "-D", playback_dev, "-q", wav_path]
    if uid is None:
        return ["aplay", "-q", wav_path]
    return [
        "/usr/bin/sudo", "-n", "-u", user, "env",
        f"XDG_RUNTIME_DIR=/run/user/{uid}",
        f"PULSE_RUNTIME_PATH=/run/user/{uid}/pulse",
        "/usr/bin/paplay", wav_path,
    ]


def _analyze_recording(target: AudioTarget, wav_path: str) -> list:
    fmt_info = target.fmt_info
    bytes_per_sample = fmt_info["bytes"]
    struct_fmt = fmt_info["struct_fmt"]
    try:
        with wave.open(wav_path, "rb") as wf:
            n_channels = wf.getnchannels()
            n_frames = wf.getnframes()
            raw_data = wf.readframes(n_frames)
    except Exception:
        return [{"channel": ch, "peak": 0, "status": "error"}
                for ch in range(target.channels)]

    results = []
    bytes_per_frame = n_channels * bytes_per_sample
    for ch in range(min(n_channels, target.channels)):
        peak = 0
        for i in range(0, n_frames, 16):
            offset = i * bytes_per_frame + ch * bytes_per_sample
            if offset + bytes_per_sample > len(raw_data):
                break
            a = abs(struct.unpack_from(struct_fmt, raw_data, offset)[0])
            if a > peak:
                peak = a
        if peak < SILENCE_THRESHOLD:
            status = "silent"
        elif peak < 500:
            status = "low"
        else:
            status = "good"
        results.append({"channel": ch, "peak": peak, "status": status})
    return results


def _extract_pair_wav(target: AudioTarget, src_wav: str, pair: List[int]) -> str:
    """Write a stereo WAV containing only the two channels named in ``pair``."""
    with wave.open(src_wav, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    bytes_per_frame = n_channels * sampwidth
    out_fd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(out_fd)
    # Readable by the playback user (service is root, playback is session user).
    os.chmod(out_path, 0o644)
    out_frames = bytearray()
    a, b = pair[0], pair[1]
    for i in range(n_frames):
        base = i * bytes_per_frame
        oa = base + a * sampwidth
        ob = base + b * sampwidth
        if ob + sampwidth > len(raw):
            break
        out_frames += raw[oa:oa + sampwidth]
        out_frames += raw[ob:ob + sampwidth]

    with wave.open(out_path, "wb") as ow:
        ow.setnchannels(2)
        ow.setsampwidth(sampwidth)
        ow.setframerate(framerate)
        ow.writeframes(bytes(out_frames))
    return out_path


def _loopback_thread(key: str, pair: Optional[List[int]]):
    target = TARGETS[key]
    lock, state = _loopback_slot(key)
    tmp_file = None
    pair_file = None
    try:
        tmp_fd, tmp_file = tempfile.mkstemp(suffix=".wav")
        os.close(tmp_fd)
        # The service runs as root; playback runs as the session user via sudo.
        # mkstemp creates 0600 (root-only), so make it readable for playback.
        os.chmod(tmp_file, 0o644)

        meter = get_meter(key)
        was_metering = bool(meter and meter.is_running)
        if was_metering:
            meter.stop()
            time.sleep(0.2)

        _ensure_routing(target)

        with lock:
            state["status"] = "recording"
            state["progress"] = 0

        record_cmd = [
            "arecord", "-D", target.capture_dev, "-f", target.fmt,
            "-r", str(target.rate), "-c", str(target.channels),
            "-d", str(LOOPBACK_DURATION), "-q", tmp_file,
        ]
        proc = subprocess.Popen(record_cmd, stderr=subprocess.PIPE)
        start_time = time.time()
        while proc.poll() is None:
            elapsed = time.time() - start_time
            with lock:
                state["progress"] = min(50, int((elapsed / LOOPBACK_DURATION) * 50))
            time.sleep(0.1)
        if proc.returncode != 0:
            stderr = proc.stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Recording failed: {stderr}")

        with lock:
            state["progress"] = 50

        channel_results = _analyze_recording(target, tmp_file)
        with lock:
            state["channel_results"] = channel_results

        if all(r["status"] == "silent" for r in channel_results):
            raise RuntimeError("All channels silent — input not capturing")

        # Select what to play back: a fixed 2-channel pair or the full capture.
        # For multi-channel targets, default to the first pair so playback is
        # always stereo (the analog output is 2-channel).
        if (pair is None and target.channels > 2 and
                target.record_pairs):
            pair = target.record_pairs[0]
        play_file = tmp_file
        if pair and len(pair) == 2 and target.channels > 2:
            pair_file = _extract_pair_wav(target, tmp_file, pair)
            play_file = pair_file

        with lock:
            state["status"] = "playing"
            state["progress"] = 50

        play_cmd = _build_play_cmd(target, play_file)
        proc = subprocess.Popen(play_cmd, stderr=subprocess.PIPE)
        start_time = time.time()
        while proc.poll() is None:
            elapsed = time.time() - start_time
            with lock:
                state["progress"] = min(100, 50 + int((elapsed / LOOPBACK_DURATION) * 50))
            time.sleep(0.1)

        with lock:
            state["status"] = "done"
            state["progress"] = 100

        if was_metering:
            time.sleep(0.2)
            meter.start()

    except Exception as e:
        with lock:
            state["status"] = "error"
            state["error"] = str(e)
    finally:
        for f in (tmp_file, pair_file):
            if f and os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass
        with lock:
            state["thread"] = None


def start_loopback(key: str, pair: Optional[List[int]] = None) -> dict:
    target = TARGETS.get(key)
    if target is None:
        return {"ok": False, "error": f"unknown input: {key}"}
    lock, state = _loopback_slot(key)
    with lock:
        if state["thread"] is not None and state["thread"].is_alive():
            return {"ok": False, "error": "Test already in progress"}
        state["status"] = "starting"
        state["progress"] = 0
        state["error"] = None
        state["channel_results"] = None
        thread = threading.Thread(
            target=_loopback_thread, args=(key, pair), daemon=True)
        state["thread"] = thread
        thread.start()
    return {"ok": True}


def get_loopback_status(key: str) -> dict:
    target = TARGETS.get(key)
    if target is None:
        return {"status": "error", "progress": 0,
                "error": f"unknown input: {key}", "channel_results": None,
                "duration": LOOPBACK_DURATION}
    lock, state = _loopback_slot(key)
    with lock:
        return {
            "status": state["status"],
            "progress": state["progress"],
            "error": state["error"],
            "channel_results": state["channel_results"],
            "duration": LOOPBACK_DURATION,
        }
