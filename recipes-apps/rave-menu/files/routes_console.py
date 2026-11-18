# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Interactive local x86 console backed by a shared pseudo-terminal."""

import json
import logging
import os
import queue
import signal
import struct
import subprocess
import threading
import time
from collections import deque
from fcntl import ioctl
from termios import TIOCSCTTY, TIOCSWINSZ

from flask import Blueprint, Response, jsonify, request, stream_with_context

log = logging.getLogger(__name__)
console_bp = Blueprint("console_bp", __name__)

CONSOLE_USER = os.environ.get("RAVE_CONSOLE_USER", "amd-edf")
HISTORY_BYTES = 256 * 1024
IDLE_GRACE_SECONDS = 30.0
SSE_HEARTBEAT_SECONDS = 1.0


class ConsoleSession:
    """One local shell shared by all connected browser viewers."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: "subprocess.Popen[bytes] | None" = None
        self.master_fd: "int | None" = None
        self.reader: "threading.Thread | None" = None
        self.watchdog: "threading.Thread | None" = None
        self.subscribers: "list[queue.Queue[bytes | None]]" = []
        self.history: "deque[bytes]" = deque()
        self.history_size = 0
        self.last_activity = time.monotonic()

    def is_open(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self.master_fd is not None

    def open(self) -> None:
        with self.lock:
            if self.is_open():
                return
            master_fd, slave_fd = os.openpty()
            env = os.environ.copy()
            env.update({"HOME": f"/home/{CONSOLE_USER}", "TERM": "xterm-256color"})

            def prepare_terminal() -> None:
                os.setsid()
                ioctl(slave_fd, TIOCSCTTY, 0)

            try:
                proc = subprocess.Popen(
                    ["/usr/sbin/runuser", "-u", CONSOLE_USER, "--", "/usr/bin/bash", "-l"],
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=f"/home/{CONSOLE_USER}",
                    env=env,
                    preexec_fn=prepare_terminal,
                )
            except Exception:
                os.close(master_fd)
                raise
            finally:
                os.close(slave_fd)
            self.proc = proc
            self.master_fd = master_fd
            self.history.clear()
            self.history_size = 0
            self.last_activity = time.monotonic()
            self.reader = threading.Thread(target=self._read_loop, name="x86-console-reader", daemon=True)
            self.reader.start()
            if self.watchdog is None or not self.watchdog.is_alive():
                self.watchdog = threading.Thread(target=self._watchdog_loop, name="x86-console-watchdog", daemon=True)
                self.watchdog.start()
            log.info("x86 console opened as %s", CONSOLE_USER)

    def close(self) -> None:
        with self.lock:
            proc = self.proc
            master_fd = self.master_fd
            self.proc = None
            self.master_fd = None
            subscribers = list(self.subscribers)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGHUP)
                proc.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(None)
            except queue.Full:
                pass
        log.info("x86 console closed")

    def _read_loop(self) -> None:
        while True:
            with self.lock:
                master_fd = self.master_fd
            if master_fd is None:
                return
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                data = b""
            if not data:
                self.close()
                return
            with self.lock:
                self.history.append(data)
                self.history_size += len(data)
                while self.history_size > HISTORY_BYTES and self.history:
                    self.history_size -= len(self.history.popleft())
                subscribers = list(self.subscribers)
            for subscriber in subscribers:
                try:
                    subscriber.put_nowait(data)
                except queue.Full:
                    pass

    def _watchdog_loop(self) -> None:
        while True:
            time.sleep(2)
            with self.lock:
                if not self.is_open():
                    return
                idle = not self.subscribers and time.monotonic() - self.last_activity > IDLE_GRACE_SECONDS
            if idle:
                self.close()
                return

    def subscribe(self) -> "tuple[queue.Queue[bytes | None], bytes]":
        subscriber: "queue.Queue[bytes | None]" = queue.Queue(maxsize=4096)
        with self.lock:
            self.subscribers.append(subscriber)
            self.last_activity = time.monotonic()
            replay = b"".join(self.history)
        return subscriber, replay

    def unsubscribe(self, subscriber: "queue.Queue[bytes | None]") -> None:
        with self.lock:
            try:
                self.subscribers.remove(subscriber)
            except ValueError:
                pass
            self.last_activity = time.monotonic()

    def keepalive(self) -> None:
        with self.lock:
            self.last_activity = time.monotonic()

    def write(self, data: bytes) -> int:
        with self.lock:
            master_fd = self.master_fd
            self.last_activity = time.monotonic()
        return os.write(master_fd, data) if master_fd is not None else 0

    def resize(self, rows: int, cols: int) -> None:
        with self.lock:
            master_fd = self.master_fd
        if master_fd is not None:
            ioctl(master_fd, TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


_SESSION = ConsoleSession()


@console_bp.post("/api/console/open")
def console_open():
    try:
        _SESSION.open()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "user": CONSOLE_USER})


@console_bp.post("/api/console/close")
def console_close():
    _SESSION.close()
    return jsonify({"ok": True})


@console_bp.post("/api/console/keepalive")
def console_keepalive():
    try:
        _SESSION.open()
        _SESSION.keepalive()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True})


@console_bp.post("/api/console/write")
def console_write():
    if not _SESSION.is_open():
        return jsonify({"ok": False, "error": "console not open"}), 400
    payload = request.get_json(silent=True) or {}
    data = payload.get("data", "")
    if not isinstance(data, str):
        return jsonify({"ok": False, "error": "data must be a string"}), 400
    try:
        wrote = _SESSION.write(data.encode("utf-8"))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "wrote": wrote})


@console_bp.post("/api/console/resize")
def console_resize():
    payload = request.get_json(silent=True) or {}
    try:
        rows = max(1, min(500, int(payload.get("rows", 24))))
        cols = max(1, min(500, int(payload.get("cols", 80))))
        _SESSION.resize(rows, cols)
    except (TypeError, ValueError, OSError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@console_bp.get("/api/console/stream")
def console_stream():
    try:
        _SESSION.open()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    subscriber, replay = _SESSION.subscribe()

    @stream_with_context
    def generate():
        try:
            yield ": connected\n\n"
            if replay:
                yield "data: " + json.dumps(replay.decode("utf-8", errors="replace")) + "\n\n"
            while True:
                try:
                    data = subscriber.get(timeout=SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield ": ping\n\n"
                    _SESSION.keepalive()
                    continue
                if data is None:
                    yield "event: status\ndata: closed\n\n"
                    return
                yield "data: " + json.dumps(data.decode("utf-8", errors="replace")) + "\n\n"
                _SESSION.keepalive()
        finally:
            _SESSION.unsubscribe(subscriber)

    return Response(generate(), mimetype="text/event-stream")