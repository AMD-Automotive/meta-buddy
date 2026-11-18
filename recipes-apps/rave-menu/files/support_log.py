# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Durable record of the operations a user performs on the board.

Writes an append-only log to the persistent ``rave-data`` partition so that a
support request can be answered from one file: what was run, when, from where,
with what input, and what came back.

Format -- each record is framed so it stays parseable no matter what a script
prints::

    ##### BEGIN <id> <ts> <event>
    {"id": …, "ts": …, "event": …, "client": …, "input": {…}, "context": {…}}
    |<id>| first line of output
    |<id>| second line of output
    ##### END <id> <ts> exit=0 status=ok duration_s=412

Every output line carries the record id, so concurrent operations may
interleave in the file and still be attributed, and a record interrupted by a
reboot or a power cut is partial rather than unparseable. Output is sanitised
before it is written, so a script can never forge a frame line.

Two further line types appear at the top level::

    ##### BOOT <ts> version=<menu version> slot=<boot slot>
    ##### MARK <id> <ts> reboot-requested

A ``BEGIN`` with no ``END`` followed by a ``BOOT`` means the operation did not
survive that boot. The file is never rewritten to tidy such records up:
append-only is the only safe discipline on a board that can lose power
mid-write.

Nothing in this module raises. Logging must never be the reason an install
fails, so every public function swallows its own errors and latches a reason
that ``summary()`` reports to the UI.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

# The persistent partition (rave-data, p5) is mounted here by
# rave-storage.service. Only this mount survives an A/B update; the rest of
# /var/lib is on the slot being replaced.
STORAGE_MOUNT = Path(os.environ.get("RAVE_STORAGE_MOUNT",
                                    "/var/lib/rave-menu/storage"))
STORAGE_STATUS = Path(os.environ.get("RAVE_STORAGE_STATUS",
                                     "/run/rave-menu/storage-status"))

LOG_DIR = STORAGE_MOUNT / "support"
# Used when rave-data is not mounted. Writing to LOG_DIR then would land under
# the mount point and be hidden the moment the partition appears.
DEGRADED_DIR = Path("/var/lib/rave-menu/support-degraded")

LOG_NAME = "events.log"
DISABLED_MARKER = "logging-disabled"

MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024
ROTATE_AT_BYTES = 32 * 1024 * 1024
KEEP_ROTATIONS = 5
# Leave room for the app cache, uploads and update staging, which share this
# partition. Logging is never worth failing an install for.
MIN_FREE_BYTES = 64 * 1024 * 1024

_LOCK = threading.Lock()
_broken = ""            # reason logging stopped, "" while healthy
_entry_count = None     # cached; None means "not counted yet"

_CSI_OSC = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _storage_mounted() -> bool:
    try:
        for line in STORAGE_STATUS.read_text(encoding="utf-8").splitlines():
            if line.startswith("state="):
                return line.split("=", 1)[1].strip() == "mounted"
    except Exception:
        pass
    return os.path.ismount(STORAGE_MOUNT)


def _active_dir() -> Path:
    return LOG_DIR if _storage_mounted() else DEGRADED_DIR


def _log_path() -> Path:
    return _active_dir() / LOG_NAME


def sanitise(text: str) -> str:
    """Make one line of script output safe to embed in the log.

    Strips terminal escapes and maps the remaining control characters to a
    visible escape, so the log cannot retitle a terminal when ``cat``-ed, and
    a stray NUL cannot make ``grep`` treat the file as binary.
    """
    text = _CSI_OSC.sub("", text)
    text = _CONTROL.sub(lambda m: "\\x%02x" % ord(m.group()), text)
    encoded = text.encode("utf-8", "replace")
    if len(encoded) > MAX_LINE_BYTES:
        text = encoded[:MAX_LINE_BYTES].decode("utf-8", "ignore") + " …line truncated…"
    return text


def enabled() -> bool:
    """Whether operations are being recorded. Defaults to on."""
    try:
        return not (_active_dir() / DISABLED_MARKER).exists()
    except Exception:
        return True


def set_enabled(value: bool, client: str = "") -> None:
    """Turn recording on or off, recording the transition either way.

    The transition is always written so that a gap in the file is explainable
    rather than looking like data loss.
    """
    try:
        want = bool(value)
        if want == enabled():
            return
        if not want:
            record("logging.disabled", {"enabled": False}, "", 0, client=client)
        _prepare_dir()
        marker = _active_dir() / DISABLED_MARKER
        if want:
            marker.unlink(missing_ok=True)
            record("logging.enabled", {"enabled": True}, "", 0, client=client)
        else:
            marker.write_text(_now() + "\n", encoding="utf-8")
    except Exception as exc:
        _break(f"could not change the logging setting: {exc}")


def client_addr() -> str:
    """The requesting address, or ``system`` outside a request context."""
    try:
        from flask import has_request_context, request
        if has_request_context():
            return request.remote_addr or "unknown"
    except Exception:
        pass
    return "system"


def _break(reason: str) -> None:
    global _broken
    if not _broken:
        _broken = reason
        log.warning("support log stopped: %s", reason)


def _prepare_dir() -> Path:
    directory = _active_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _open_append(path: Path):
    created = not path.exists()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    if created:
        _sync_dir(path.parent)
    return os.fdopen(fd, "a", encoding="utf-8")


def _sync_dir(directory: Path) -> None:
    """Make a new log file's directory entry durable."""
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        log.warning("support log directory sync failed: %s", exc)


def _write(text: str, durable: bool = False) -> None:
    """Append raw text. Caller holds ``_LOCK``.

    Record boundaries are flushed to disk; the output lines between them are
    not. Several of the operations being logged reboot the board or write NVMe
    for minutes, so a record that only exists in the page cache is the one most
    likely to be lost, while an fsync per output line would be thousands of
    them during a single install.
    """
    with _open_append(_log_path()) as handle:
        handle.write(text)
        if durable:
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError as exc:
                # The bytes reached the kernel; only durability is in doubt,
                # which is not worth giving up logging over.
                log.warning("support log sync failed: %s", exc)


def _rotate_if_needed() -> None:
    """Roll the log over if it has grown past the threshold.

    Only ever called before a record starts, never mid-record: renaming the
    file under an open record would split it across two files.
    """
    path = _log_path()
    try:
        if path.stat().st_size < ROTATE_AT_BYTES:
            return
    except FileNotFoundError:
        return
    oldest = path.with_suffix(path.suffix + f".{KEEP_ROTATIONS}")
    if oldest.exists():
        oldest.unlink()
    for index in range(KEEP_ROTATIONS - 1, 0, -1):
        source = path.with_suffix(path.suffix + f".{index}")
        if source.exists():
            source.rename(path.with_suffix(path.suffix + f".{index + 1}"))
    path.rename(path.with_suffix(path.suffix + ".1"))


def _have_room() -> bool:
    try:
        return shutil.disk_usage(_active_dir()).free >= MIN_FREE_BYTES
    except Exception:
        return True


def _context() -> dict:
    """Board identity at the time a record starts.

    Captured at begin rather than end because a system update changes both
    values before the record is closed.
    """
    out = {}
    try:
        from helpers import get_rauc_version
        out["menu_version"] = get_rauc_version() or ""
    except Exception:
        pass
    try:
        from helpers import run_cmd
        result = run_cmd(["rauc", "status", "--output-format=shell"], timeout=5)
        for line in (result.stdout or "").splitlines():
            if line.startswith("RAUC_BOOT_PRIMARY="):
                out["boot_slot"] = line.split("=", 1)[1].strip().strip("'\"")
                break
    except Exception:
        pass
    return out


class Record:
    """One in-progress log record. Obtained from :func:`begin`."""

    def __init__(self, record_id: str, event: str):
        self.id = record_id
        self.event = event
        self.started = time.monotonic()
        self.written = 0
        self.capped = False
        self.closed = False

    def line(self, text: str) -> None:
        """Append one line of the operation's output."""
        if self.closed or _broken:
            return
        try:
            if self.capped:
                return
            body = f"|{self.id}| {sanitise(text)}\n"
            self.written += len(body)
            if self.written > MAX_RECORD_BYTES:
                self.capped = True
                body = f"|{self.id}| …output truncated at {MAX_RECORD_BYTES // (1024 * 1024)} MiB…\n"
            with _LOCK:
                _write(body)
        except Exception as exc:
            _break(f"write failed: {exc}")

    def mark(self, note: str) -> None:
        """Note something about this record that is not output, e.g. a reboot."""
        if self.closed or _broken:
            return
        try:
            with _LOCK:
                _write(f"##### MARK {self.id} {_now()} {sanitise(note)}\n", durable=True)
        except Exception as exc:
            _break(f"write failed: {exc}")

    def finish(self, exit_code=None, status: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        if _broken:
            return
        try:
            if not status:
                status = "ok" if exit_code == 0 else "failed"
            duration = int(time.monotonic() - self.started)
            code = "none" if exit_code is None else exit_code
            with _LOCK:
                _write(f"##### END {self.id} {_now()} exit={code} "
                       f"status={status} duration_s={duration}\n", durable=True)
        except Exception as exc:
            _break(f"write failed: {exc}")


def begin(event: str, inputs=None, client: str = "") -> "Record | None":
    """Open a record for an operation that is about to run.

    Returns ``None`` when logging is off or unavailable, so callers can treat
    the result as optional without branching on configuration.
    """
    global _entry_count
    if _broken or not enabled():
        return None
    try:
        if not _have_room():
            _break("no space left on rave-data")
            return None
        _prepare_dir()
        record_id = uuid.uuid4().hex[:8]
        header = {
            "id": record_id,
            "ts": _now(),
            "event": event,
            "client": client or client_addr(),
            "storage_state": "mounted" if _storage_mounted() else "degraded",
            "context": _context(),
            "input": inputs if inputs is not None else {},
        }
        with _LOCK:
            _rotate_if_needed()
            _write(f"##### BEGIN {record_id} {header['ts']} {event}\n"
                   + json.dumps(header, ensure_ascii=True, default=str) + "\n",
                   durable=True)
            if _entry_count is not None:
                _entry_count += 1
        return Record(record_id, event)
    except Exception as exc:
        _break(f"could not start a record: {exc}")
        return None


def record(event: str, inputs=None, output: str = "", exit_code=None,
           client: str = "", status: str = "") -> None:
    """Write a complete record for an operation that has already finished."""
    handle = begin(event, inputs, client=client)
    if handle is None:
        return
    for line in (output or "").splitlines():
        handle.line(line)
    handle.finish(exit_code, status)


def note_boot() -> None:
    """Mark a menu start, so records with no END can be attributed to a boot."""
    if _broken or not enabled():
        return
    try:
        _prepare_dir()
        context = _context()
        with _LOCK:
            _write(f"##### BOOT {_now()} version={context.get('menu_version', '?')} "
                   f"slot={context.get('boot_slot', '?')}\n", durable=True)
    except Exception as exc:
        _break(f"could not record the boot: {exc}")


def log_files() -> list:
    """Every log file, oldest generation first, for the download archive."""
    found = []
    for directory in (LOG_DIR, DEGRADED_DIR):
        try:
            if not directory.is_dir():
                continue
        except Exception:
            continue
        for index in range(KEEP_ROTATIONS, 0, -1):
            candidate = directory / f"{LOG_NAME}.{index}"
            if candidate.exists():
                found.append(candidate)
        current = directory / LOG_NAME
        if current.exists():
            found.append(current)
    return found


def summary() -> dict:
    """Size, age and health of the log, for the System Update panel.

    Deliberately cheap: the entry count is cached and kept up to date by
    :func:`begin` rather than recounted by scanning the file.
    """
    global _entry_count
    out = {"enabled": enabled(), "bytes": 0, "entries": 0,
           "newest": "", "broken": _broken,
           "degraded": not _storage_mounted()}
    try:
        files = log_files()
        out["bytes"] = sum(f.stat().st_size for f in files)
        if files:
            out["newest"] = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(files[-1].stat().st_mtime))
        if _entry_count is None:
            _entry_count = 0
            for path in files:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if line.startswith("##### BEGIN "):
                            _entry_count += 1
        out["entries"] = _entry_count
    except Exception as exc:
        out["broken"] = _broken or f"could not read the log: {exc}"
    return out
