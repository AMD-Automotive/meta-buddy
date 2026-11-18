# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Interactive serial console API for /dev/ttyUSB1 and /dev/ttyUSB2.

Design notes
------------
* A single :class:`SerialSession` per physical port is shared across **all**
  connected browser clients. Each /stream request registers a subscriber
  queue; the reader thread fans every chunk out to every subscriber, so
  multiple browsers see a unified live view of the same port.
* A small ring-buffer of recent bytes is replayed to each newcomer so they
  pick up mid-stream context instead of an empty terminal.
* The OS device is held open only while at least one subscriber is
  attached (plus a short idle grace period). Once everyone disconnects
  the port is released so it can be used over ssh / minicom / etc.
* A keepalive comment is sent on the SSE stream every second; if the
  client goes away the generator notices via the writer raising and the
  subscriber is removed.

Endpoints:
    POST /api/serial/<n>/open    -- open the port (idempotent)
    POST /api/serial/<n>/close   -- forcefully close the port now
    POST /api/serial/<n>/write   -- raw body bytes are written to the port
    GET  /api/serial/<n>/stream  -- SSE stream of decoded bytes from the port
"""

import json
import logging
import os
import queue
import threading
import time
from glob import glob as _glob

import serial  # pyserial
from flask import Blueprint, Response, jsonify, request, stream_with_context

log = logging.getLogger(__name__)
serial_bp = Blueprint("serial_bp", __name__)

PORTS = {
    1: "/dev/ttyUSB1",
    2: "/dev/ttyUSB2",
}
DEFAULT_BAUD = 115200

# Replay this much recent traffic to a newly-attached subscriber so they
# see context rather than a blank screen. Sized so that a user who has
# enabled "Keep alive" can return after a while and still see what
# happened on the line in the meantime.
HISTORY_BYTES = 256 * 1024

# Clients ping every 1 s. We tolerate 30 consecutive missed pings
# (~30 s of silence) before we consider a viewer gone -- this is what
# matters on flaky connections. The OS device is released only after
# this much time has elapsed with no subscribers and no keepalive
# pings since the last interaction.
KEEPALIVE_INTERVAL_SECONDS = 1.0
KEEPALIVE_MAX_MISSES = 30
IDLE_GRACE_SECONDS = KEEPALIVE_INTERVAL_SECONDS * KEEPALIVE_MAX_MISSES


class PortBusyError(RuntimeError):
    """Raised when the OS serial device is held open by another process.

    Carries a list of ``{"pid": int, "name": str}`` records describing
    the holders so the UI can tell the user which application to stop.
    """

    def __init__(self, dev: str, holders: list):
        self.dev = dev
        self.holders = holders
        names = ", ".join("%s (pid %d)" % (h["name"], h["pid"]) for h in holders) or "another process"
        super().__init__(f"{dev} is in use by {names}")


def _find_holders(dev: str, exclude_pid: int | None = None) -> list:
    """Return a list of {pid, name} for every process holding `dev` open.

    Reads /proc/<pid>/fd to find file descriptors that resolve to the
    same path as `dev`. Permission errors on individual /proc entries
    are silently skipped (we may not be able to inspect every process,
    but ones we can't read aren't ones holding our tty either, modulo
    the rare case of another root-owned holder — in which case the user
    will get a generic 'in use' message and can use lsof manually).

    `exclude_pid`, if given, is filtered out (used to ignore our own
    rave-menu process so the check doesn't flag our own shared session).
    """
    try:
        target = os.path.realpath(dev)
    except OSError:
        target = dev
    holders = []
    seen = set()
    for pid_dir in _glob("/proc/[0-9]*"):
        try:
            pid = int(os.path.basename(pid_dir))
        except ValueError:
            continue
        if exclude_pid is not None and pid == exclude_pid:
            continue
        fd_dir = os.path.join(pid_dir, "fd")
        try:
            fds = os.listdir(fd_dir)
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue
        for fd in fds:
            link = os.path.join(fd_dir, fd)
            try:
                rl = os.readlink(link)
            except OSError:
                continue
            if rl == dev or rl == target:
                if pid in seen:
                    break
                seen.add(pid)
                try:
                    with open(os.path.join(pid_dir, "comm"), "r") as f:
                        name = f.read().strip() or "?"
                except OSError:
                    name = "?"
                holders.append({"pid": pid, "name": name})
                break
    return holders


class SerialSession:
    """Shared serial port with multi-subscriber fan-out."""

    def __init__(self, dev: str, baud: int = DEFAULT_BAUD):
        self.dev = dev
        self.baud = baud
        self.ser: "serial.Serial | None" = None
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.reader: "threading.Thread | None" = None
        self.watchdog: "threading.Thread | None" = None
        self.subscribers: "list[queue.Queue[bytes]]" = []
        self.history = bytearray()
        # Updated on subscribe/unsubscribe, every successful SSE ping,
        # every write, and every explicit /keepalive POST. The watchdog
        # uses this to decide when to release the port.
        self.last_activity = time.monotonic()

    # ── lifecycle ────────────────────────────────────────────────
    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def open(self) -> None:
        with self.lock:
            if self.is_open():
                return
            # Refuse to open if another process already holds the
            # device. This keeps us from racing pyserial against
            # ssh-launched picocom / minicom / etc., and lets us tell
            # the user exactly which application to stop.
            holders = _find_holders(self.dev, exclude_pid=os.getpid())
            if holders:
                raise PortBusyError(self.dev, holders)
            self.ser = serial.Serial(self.dev, self.baud, timeout=0.1)
            self.stop_evt.clear()
            self.reader = threading.Thread(
                target=self._read_loop, name=f"serial-rd-{self.dev}", daemon=True
            )
            self.reader.start()
            if self.watchdog is None or not self.watchdog.is_alive():
                self.watchdog = threading.Thread(
                    target=self._watchdog_loop,
                    name=f"serial-wd-{self.dev}",
                    daemon=True,
                )
                self.watchdog.start()
            log.info("serial %s opened @%d", self.dev, self.baud)

    def close(self) -> None:
        with self.lock:
            self.stop_evt.set()
            ser = self.ser
            self.ser = None
            # Notify subscribers so their generators can exit promptly.
            for q in self.subscribers:
                try:
                    q.put_nowait(b"")  # sentinel: zero-length wakes the gen
                except queue.Full:
                    pass
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
            log.info("serial %s closed", self.dev)

    # ── threads ──────────────────────────────────────────────────
    def _read_loop(self) -> None:
        while not self.stop_evt.is_set():
            ser = self.ser
            if ser is None:
                return
            try:
                data = ser.read(4096)
            except Exception as exc:
                log.warning("serial %s read error: %s", self.dev, exc)
                time.sleep(0.5)
                continue
            if not data:
                continue
            with self.lock:
                # Keep a rolling history for replay.
                self.history.extend(data)
                if len(self.history) > HISTORY_BYTES:
                    del self.history[: len(self.history) - HISTORY_BYTES]
                subs = list(self.subscribers)
            for q in subs:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    # drop oldest for this slow consumer
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        pass

    def _watchdog_loop(self) -> None:
        """Release the OS device when nobody has been listening for a while."""
        while not self.stop_evt.is_set():
            time.sleep(2.0)
            with self.lock:
                if not self.is_open():
                    return
                idle = (
                    not self.subscribers
                    and (time.monotonic() - self.last_activity)
                    > IDLE_GRACE_SECONDS
                )
            if idle:
                log.info(
                    "serial %s idle > %.0fs (no subscribers, no keepalive); releasing port",
                    self.dev,
                    IDLE_GRACE_SECONDS,
                )
                self.close()
                return

    # ── subscriber management ───────────────────────────────────
    def subscribe(self, replay: bool = True) -> "tuple[queue.Queue[bytes], bytes]":
        """Register a new subscriber.

        :param replay: If True (the default, used by SSE clients) the
            caller receives a snapshot of the recent history buffer so
            it can paint mid-stream context. Internal consumers like
            the board-test runner pass False to start clean and avoid
            matching stale prompts.
        """
        q: "queue.Queue[bytes]" = queue.Queue(maxsize=4096)
        with self.lock:
            self.subscribers.append(q)
            self.last_activity = time.monotonic()
            replay_bytes = bytes(self.history) if replay else b""
        return q, replay_bytes

    def unsubscribe(self, q: "queue.Queue[bytes]") -> None:
        with self.lock:
            try:
                self.subscribers.remove(q)
            except ValueError:
                pass
            self.last_activity = time.monotonic()

    def keepalive(self) -> None:
        with self.lock:
            self.last_activity = time.monotonic()

    # ── writes ───────────────────────────────────────────────────
    def write(self, data: bytes) -> int:
        with self.lock:
            if not self.is_open() or self.ser is None:
                return 0
            self.last_activity = time.monotonic()
            return self.ser.write(data) or 0


_SESSIONS: "dict[int, SerialSession]" = {}
_SESSIONS_LOCK = threading.Lock()


def _get_session(n: int) -> "SerialSession | None":
    if n not in PORTS:
        return None
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(n)
        if s is None:
            s = SerialSession(PORTS[n])
            _SESSIONS[n] = s
        return s


def get_session(n: int) -> "SerialSession | None":
    """Public accessor: return the shared SerialSession for port number `n`."""
    return _get_session(n)


def get_session_for_device(dev: str) -> "SerialSession | None":
    """Public accessor: return the shared SerialSession that owns `dev`
    (e.g. ``/dev/ttyUSB1``), or None if no port is configured for it.
    Internal users (board-test runner, etc.) call this to participate
    in the unified RS-232 fan-out instead of opening pyserial directly.
    """
    for n, d in PORTS.items():
        if d == dev:
            return _get_session(n)
    return None


@serial_bp.post("/api/serial/<int:n>/open")
def serial_open(n: int):
    s = _get_session(n)
    if s is None:
        return jsonify({"ok": False, "error": "no such port"}), 404
    try:
        s.open()
    except PortBusyError as exc:
        return jsonify({
            "ok": False,
            "error": str(exc),
            "busy": True,
            "device": exc.dev,
            "holders": exc.holders,
        }), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "device": s.dev, "baud": s.baud})


@serial_bp.post("/api/serial/<int:n>/close")
def serial_close(n: int):
    s = _get_session(n)
    if s is None:
        return jsonify({"ok": False, "error": "no such port"}), 404
    s.close()
    return jsonify({"ok": True})


@serial_bp.post("/api/serial/<int:n>/keepalive")
def serial_keepalive(n: int):
    """Posted by the browser every second while "Keep alive" is enabled.

    Resets the idle countdown so the port stays open even when the user
    has dismissed the console UI but wants to come back later and see
    what happened in the meantime.
    """
    s = _get_session(n)
    if s is None:
        return jsonify({"ok": False, "error": "no such port"}), 404
    # Make sure the port is open so traffic gets buffered into history.
    try:
        s.open()
    except PortBusyError as exc:
        return jsonify({
            "ok": False,
            "error": str(exc),
            "busy": True,
            "device": exc.dev,
            "holders": exc.holders,
        }), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    s.keepalive()
    return jsonify({"ok": True})


@serial_bp.post("/api/serial/<int:n>/write")
def serial_write(n: int):
    s = _get_session(n)
    if s is None:
        return jsonify({"ok": False, "error": "no such port"}), 404
    if not s.is_open():
        return jsonify({"ok": False, "error": "port not open"}), 400

    body: bytes
    if request.is_json:
        try:
            payload = request.get_json(silent=True) or {}
            data = payload.get("data", "")
            body = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        except Exception:
            body = b""
    else:
        body = request.get_data() or b""
    try:
        wrote = s.write(body)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "wrote": wrote})


@serial_bp.get("/api/serial/<int:n>/stream")
def serial_stream(n: int):
    s = _get_session(n)
    if s is None:
        return "no such port", 404

    # Auto-open on first subscriber so the page works without an explicit
    # open() call and re-attaches transparently after an idle close.
    try:
        s.open()
    except PortBusyError as exc:
        # Tell the client over SSE who is holding the port and end the
        # stream cleanly so EventSource doesn't keep retrying behind
        # the scenes.
        payload = {
            "error": str(exc),
            "device": exc.dev,
            "holders": exc.holders,
        }
        body = (
            ": connected\n\n"
            "event: busy\ndata: " + json.dumps(payload) + "\n\n"
            "event: status\ndata: closed\n\n"
        )
        return Response(body, mimetype="text/event-stream")
    except Exception as exc:
        return f"open failed: {exc}", 500

    replay_history = request.args.get("replay", "1") != "0"
    q, replay = s.subscribe(replay=replay_history)

    def _decode(b: bytes) -> str:
        try:
            return b.decode("utf-8", errors="replace")
        except Exception:
            return b.decode("latin-1", errors="replace")

    @stream_with_context
    def gen():
        try:
            yield ": connected\n\n"
            if replay:
                yield "data: " + json.dumps(_decode(replay)) + "\n\n"
            while True:
                try:
                    data = q.get(timeout=KEEPALIVE_INTERVAL_SECONDS)
                except queue.Empty:
                    # Keepalive comment; if the client is gone the WSGI
                    # server will raise on the next yield and we'll fall
                    # through to the finally: block.
                    yield ": ping\n\n"
                    s.keepalive()
                    continue
                if not data:  # close sentinel
                    yield "event: status\ndata: closed\n\n"
                    return
                yield "data: " + json.dumps(_decode(data)) + "\n\n"
                s.keepalive()
        finally:
            s.unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream")
