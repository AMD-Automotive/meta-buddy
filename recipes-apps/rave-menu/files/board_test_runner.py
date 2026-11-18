#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

import ipaddress
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

import support_log

try:
    import serial
except Exception:
    serial = None

log = logging.getLogger(__name__)

INCORRECT_BITSTREAM_MSG = (
    "Board test image not detected - the tests below are available in the "
    "current image"
)

SERIAL_PORT = os.environ.get("RAVE_BOARD_TEST_SERIAL", "/dev/ttyUSB1")
SERIAL_BAUD = int(os.environ.get("RAVE_BOARD_TEST_BAUD", "115200"))
TEST_TIMEOUT_S = int(os.environ.get("RAVE_BOARD_TEST_TIMEOUT", "60"))
PROMPT_SYNC_TIMEOUT_S = 12
DISCOVERY_TIMEOUT_S = int(os.environ.get("RAVE_BOARD_TEST_DISCOVERY_TIMEOUT", "15"))

IPERF_DEFAULT_SERVER = "192.168.0.10"
IPERF_DEFAULT_MIN_BW = 80

IPERF10G_DEFAULT_SERVER = "192.168.0.10"
IPERF10G_DEFAULT_MIN_BW = 8000

# Tests that should never be shown in the UI even if discovered.
HIDDEN_TEST_IDS = {
    "cpu-test",
    "memory-test",
}

# Match simple shell prompts such as:
# root@host:~#
# host$
PROMPT_REGEX = re.compile(r"(?:^|\n)\s*(?:root@[^\n]*[#$]|[^\s\n]+[#$])\s*$")
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
TEST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")

_STATE_LOCK = threading.Lock()
_STATE = {
    "started_once": False,
    "running": False,
    "current_test": None,
    "last_error": "",
    "discovered": False,
    "discovering": False,
    "discover_started_at": 0,
    "pcie": {"ok": None, "message": "", "details": ""},
    "tests_order": [],
    "tests": {},
    "settings": {
        "iperf": {
            "server_ip": IPERF_DEFAULT_SERVER,
            "min_bw": IPERF_DEFAULT_MIN_BW,
        },
        "iperf10g": {
            "server_ip": IPERF10G_DEFAULT_SERVER,
            "min_bw": IPERF10G_DEFAULT_MIN_BW,
        },
    },
}


def _now() -> int:
    return int(time.time())


def _normalize_console_text(text: str) -> str:
    clean = text.replace("\r", "\n").replace("\x00", "")
    return ANSI_ESCAPE_RE.sub("", clean)


def _new_test_record(test_id: str):
    return {
        "id": test_id,
        "title": test_id,
        "command": f"ptest-runner {test_id}",
        "status": "pending",
        "result": "",
        "output": "",
        "has_run": False,
        "updated_at": 0,
    }


def _set_test_state(test_id: str, **kwargs):
    with _STATE_LOCK:
        test = _STATE["tests"].get(test_id)
        if not test:
            test = _new_test_record(test_id)
            _STATE["tests"][test_id] = test
            if test_id not in _STATE["tests_order"]:
                _STATE["tests_order"].append(test_id)
        for k, v in kwargs.items():
            test[k] = v
        test["updated_at"] = _now()


def _set_worker_running(running: bool):
    with _STATE_LOCK:
        _STATE["running"] = bool(running)
        if not running:
            _STATE["current_test"] = None


def _set_current_test(test_id: str | None):
    with _STATE_LOCK:
        _STATE["current_test"] = test_id


def _set_pcie(ok: bool, message: str, details: str = ""):
    with _STATE_LOCK:
        _STATE["pcie"] = {"ok": ok, "message": message, "details": details}


def _set_started_once():
    with _STATE_LOCK:
        _STATE["started_once"] = True


def _set_last_error(msg: str):
    with _STATE_LOCK:
        _STATE["last_error"] = msg


def _set_iperf_settings(server_ip: str, min_bw: int):
    with _STATE_LOCK:
        _STATE["settings"]["iperf"] = {
            "server_ip": server_ip,
            "min_bw": min_bw,
        }


def _set_iperf10g_settings(server_ip: str, min_bw: int):
    with _STATE_LOCK:
        _STATE["settings"]["iperf10g"] = {
            "server_ip": server_ip,
            "min_bw": min_bw,
        }


def _set_tests_order(test_ids: list[str]):
    with _STATE_LOCK:
        existing = _STATE["tests"]
        new_map = {}
        for test_id in test_ids:
            rec = dict(existing.get(test_id) or _new_test_record(test_id))
            rec["id"] = test_id
            rec["title"] = test_id
            rec["command"] = f"ptest-runner {test_id}"
            new_map[test_id] = rec
        _STATE["tests"] = new_map
        _STATE["tests_order"] = list(test_ids)


def _get_tests_order() -> list[str]:
    with _STATE_LOCK:
        return list(_STATE["tests_order"])


def snapshot():
    with _STATE_LOCK:
        tests = [dict(_STATE["tests"].get(tid) or _new_test_record(tid)) for tid in _STATE["tests_order"]]
        return {
            "started_once": _STATE["started_once"],
            "running": _STATE["running"],
            "current_test": _STATE["current_test"],
            "last_error": _STATE["last_error"],
            "discovered": _STATE["discovered"],
            "discovering": _STATE["discovering"],
            "pcie": dict(_STATE["pcie"]),
            "settings": dict(_STATE["settings"]),
            "tests": tests,
        }


def get_test_output(test_id: str):
    with _STATE_LOCK:
        test = _STATE["tests"].get(test_id)
        if not test:
            return None
        return test["output"]


def _run_cmd(cmd: list[str], timeout_s: int):
    env = os.environ.copy()
    env.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, env=env)
    except Exception:
        return None


def _parse_ptest_list(output: str) -> list[str]:
    """Parse 'ptest-runner -l' output (local or serial) into test IDs.

    Output format is typically:
        Available ptests:
        can-test\t/usr/lib/can-test/ptest/run-ptest
        cpu-test\t/usr/lib/cpu-test/ptest/run-ptest
        ...
    """
    text = _normalize_console_text(output)
    found = []
    seen = set()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Skip command echo
        if "ptest-runner" in line:
            continue
        # Skip prompt lines
        if _looks_like_prompt(line):
            continue

        low_line = line.lower()
        if low_line.startswith("available"):
            continue

        # ptest-runner -l outputs "name<TAB>path" -- take first column
        token = line.split("\t")[0].strip()
        if not token or not TEST_NAME_RE.match(token):
            continue
        if token in seen:
            continue
        low = token.lower()
        if low in {"available", "tests", "test", "ptests"}:
            continue
        seen.add(token)
        found.append(token)

    return [t for t in found if t not in HIDDEN_TEST_IDS]


def _discover_available_tests() -> list[str]:
    result = _run_cmd(["ptest-runner", "-l"], timeout_s=DISCOVERY_TIMEOUT_S)
    if not result:
        return []

    text = (result.stdout or "") + "\n" + (result.stderr or "")
    return _parse_ptest_list(text)


def _discover_tests_via_serial() -> list[str]:
    """Open serial to Versal, run 'ptest-runner -l', return discovered test IDs."""
    try:
        sess = SerialSession(SERIAL_PORT, SERIAL_BAUD)
    except Exception as exc:
        _set_last_error(f"Serial open failed during discovery: {exc}")
        return []

    try:
        output, ok, _prompt_fail = sess.run_command(
            "ptest-runner -l", DISCOVERY_TIMEOUT_S
        )
        if not ok:
            return []
        return _parse_ptest_list(output)
    finally:
        sess.close()


def _parse_pcie_device_sysfs() -> tuple[bool, str, str]:
    pci_root = Path("/sys/bus/pci/devices")
    if not pci_root.exists():
        return False, INCORRECT_BITSTREAM_MSG, "PCI sysfs path is missing"

    best_detail = ""
    for dev in sorted(pci_root.iterdir()):
        try:
            vendor = (dev / "vendor").read_text(encoding="utf-8", errors="ignore").strip().lower()
            device = (dev / "device").read_text(encoding="utf-8", errors="ignore").strip().lower()
            revision = (dev / "revision").read_text(encoding="utf-8", errors="ignore").strip().lower()
            class_code = (dev / "class").read_text(encoding="utf-8", errors="ignore").strip().lower()
        except Exception:
            continue

        if vendor != "0x10ee" or device != "0x9038":
            continue

        is_serial_controller = class_code.startswith("0x07")
        rev = revision.replace("0x", "")
        revision_ok = rev in {"", "0", "00"}
        best_detail = f"{dev.name} vendor={vendor} device={device} class={class_code} revision={revision or '-'}"

        if is_serial_controller and revision_ok:
            return True, "PCIe check passed", best_detail

    if best_detail:
        return False, INCORRECT_BITSTREAM_MSG, best_detail
    return False, INCORRECT_BITSTREAM_MSG, "No matching 10ee:9038 PCIe serial controller found"


def _parse_result(output: str) -> tuple[str, str]:
    normalized = _normalize_console_text(output)
    matches = re.findall(r"TOTAL:\s*(\d+)\s+FAIL:\s*(\d+)", normalized, flags=re.IGNORECASE)
    if matches:
        _total_text, fail_text = matches[-1]
        try:
            fail_count = int(fail_text)
        except ValueError:
            return "unknown", "Unknown"
        if fail_count == 0:
            return "pass", "Pass"
        return "fail", "Fail"

    upper = normalized.upper()
    if "PASS" in upper and "FAIL" not in upper:
        return "pass", "Pass"
    if "FAIL" in upper:
        return "fail", "Fail"
    return "unknown", "Unknown"


def _looks_like_prompt(text: str) -> bool:
    normalized = _normalize_console_text(text)
    if PROMPT_REGEX.search(normalized):
        return True
    lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]
    if not lines:
        return False
    tail = lines[-1]
    return tail.endswith("#") or tail.endswith("$")


def _normalize_iperf_options(iperf_options: dict | None, default_server: str = IPERF_DEFAULT_SERVER, default_bw: int = IPERF_DEFAULT_MIN_BW):
    options = iperf_options if isinstance(iperf_options, dict) else {}
    server_ip = str(options.get("server_ip", default_server)).strip() or default_server
    min_bw_raw = options.get("min_bw", default_bw)

    try:
        ipaddress.ip_address(server_ip)
    except ValueError:
        return False, "Invalid iperf server IP address", None

    try:
        min_bw = int(min_bw_raw)
    except Exception:
        return False, "Invalid iperf required speed", None

    if min_bw <= 0:
        return False, "iperf required speed must be greater than zero", None

    return True, "", {"server_ip": server_ip, "min_bw": min_bw}


def _build_test_command(test_id: str, iperf_settings: dict, iperf10g_settings: dict):
    if test_id == "iperf-test":
        ip = shlex.quote(str(iperf_settings["server_ip"]))
        min_bw = int(iperf_settings["min_bw"])
        return f"IPERF_SERVER={ip} IPERF_MIN_BW={min_bw} ptest-runner iperf-test"
    if test_id == "iperf10g-test":
        ip = shlex.quote(str(iperf10g_settings["server_ip"]))
        min_bw = int(iperf10g_settings["min_bw"])
        return f"IPERF10_SERVER={ip} IPERF10_MIN_BW={min_bw} ptest-runner iperf10g-test"
    return f"ptest-runner {shlex.quote(test_id)}"


class SerialSession:
    """Board-test wrapper that participates in the shared RS-232 fan-out.

    Rather than opening the serial port directly (which would conflict
    with the live web console subscribers in routes_serial.py), this
    class registers itself as just another subscriber of the same
    routes_serial.SerialSession that the browsers use. Effects:

    * Test prompts, commands, and device output are visible live in any
      open Serial Console window for the same /dev/ttyUSB.
    * The OS device is held open for the duration of the test and then
      released by the routes_serial watchdog ~30 s after the last
      subscriber leaves -- so once the test completes AND no browser
      is still watching (and no Keep-alive timer is running), the port
      goes free for ssh / minicom.
    * Multiple browsers and the test runner share one OS file handle;
      pyserial is never opened twice for the same device.

    The public surface (``run_command``, ``sync_prompt``, ``close``)
    matches the previous direct-serial implementation so the rest of
    the runner is unchanged.
    """

    def __init__(self, port: str, baud: int):
        # ``baud`` is accepted for API compatibility; the shared session
        # is created at the routes_serial-defined baud (115200).
        del baud
        try:
            import routes_serial  # imported lazily to avoid Flask import at module load
        except Exception as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"routes_serial unavailable: {exc}")

        shared = routes_serial.get_session_for_device(port)
        if shared is None:
            raise RuntimeError(
                f"no shared RS-232 session is configured for {port}"
            )
        # Ensure the OS device is open. Idempotent; if a browser already
        # opened it we just attach as another subscriber.
        shared.open()
        # Subscribe WITHOUT history replay -- we don't want to match a
        # stale prompt from buffered data when synchronising.
        q, _replay = shared.subscribe(replay=False)
        self._shared = shared
        self._queue = q
        self._closed = False

    # ── lifecycle ───────────────────────────────────────────────
    def close(self):
        """Drop our subscription. Does NOT force-close the OS device --
        the routes_serial watchdog releases it once the last subscriber
        leaves AND the keepalive grace window expires."""
        if self._closed:
            return
        self._closed = True
        try:
            self._shared.unsubscribe(self._queue)
        except Exception:
            pass

    # ── reads ───────────────────────────────────────────────────
    def _drain_queue_nowait(self) -> bytes:
        """Pull any chunks already buffered in our subscriber queue."""
        import queue as _queue
        out = bytearray()
        try:
            while True:
                out.extend(self._queue.get_nowait())
        except _queue.Empty:
            pass
        return bytes(out)

    def _read_until_prompt(self, timeout_s: int) -> tuple[str, bool]:
        import queue as _queue
        deadline = time.time() + max(1, timeout_s)
        chunks: list[str] = []

        # Pick up anything that arrived between the previous step and now.
        pending = self._drain_queue_nowait()
        if pending:
            chunks.append(pending.decode("utf-8", errors="ignore"))
            if _looks_like_prompt("".join(chunks)[-1024:]):
                return "".join(chunks), True

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return "".join(chunks), False
            try:
                data = self._queue.get(timeout=min(remaining, 0.5))
            except _queue.Empty:
                continue
            if not data:
                # Sentinel from a forced shared.close() -- give up.
                return "".join(chunks), False
            chunks.append(data.decode("utf-8", errors="ignore"))
            if _looks_like_prompt("".join(chunks)[-1024:]):
                return "".join(chunks), True

    # ── writes ──────────────────────────────────────────────────
    def _safe_write(self, payload: bytes):
        try:
            self._shared.write(payload)
        except Exception:
            pass

    # ── high-level helpers (unchanged behaviour) ────────────────
    def sync_prompt(self) -> tuple[bool, str]:
        sync_log: list[str] = []
        for attempt in range(2):
            if attempt > 0:
                self._safe_write(b"\x03")
                time.sleep(0.15)

            for _ in range(3):
                self._safe_write(b"\n")
                time.sleep(0.12)

            out, ok = self._read_until_prompt(
                PROMPT_SYNC_TIMEOUT_S + (3 if attempt > 0 else 0)
            )
            sync_log.append(out)
            if ok:
                return True, "".join(sync_log)

        return False, "".join(sync_log)

    def run_command(self, command: str, timeout_s: int) -> tuple[str, bool, bool]:
        synced, sync_out = self.sync_prompt()
        if not synced:
            return f"{sync_out}\n[board-test] prompt sync failed before command\n", False, True

        try:
            self._shared.write((command + "\n").encode("utf-8"))
        except Exception as exc:
            return f"{sync_out}\n[board-test] failed to write command: {exc}\n", False, False

        cmd_out, got_prompt = self._read_until_prompt(timeout_s)
        if not got_prompt:
            return f"{sync_out}{cmd_out}\n[board-test] timeout waiting for prompt\n", False, False
        return f"{sync_out}{cmd_out}", True, False


def _run_single_test(sess: SerialSession, test_id: str, iperf_settings: dict, iperf10g_settings: dict):
    command = _build_test_command(test_id, iperf_settings, iperf10g_settings)
    _set_test_state(test_id, status="running", result="Running", output="", command=command)
    _set_current_test(test_id)

    output, _ok, prompt_fail = sess.run_command(command, TEST_TIMEOUT_S)
    status, result = _parse_result(output)
    if prompt_fail:
        status, result = "fail", "Fail"

    _set_test_state(
        test_id,
        status=status,
        result=result,
        output=output,
        has_run=True,
    )
    _set_current_test(None)


def _record_run(test_ids: list[str], event: str, client: str) -> None:
    """Write the run to the support log: verdicts, plus output for failures."""
    try:
        state = snapshot()
        wanted = set(test_ids)
        tests = [t for t in state["tests"] if t["id"] in wanted]
        failed = [t for t in tests if t["status"] == "fail"]
        lines = [f"{(t['result'] or t['status']):<8} {t['id']}" for t in tests]
        if state["last_error"]:
            # A serial-open failure fails every test with the same message;
            # record it once rather than repeating it per test.
            lines += ["", state["last_error"]]
        else:
            for test in failed:
                lines += ["", f"--- {test['id']} ---"]
                lines += _normalize_console_text(test["output"] or "").splitlines()
        support_log.record(
            event,
            {"tests": list(test_ids), "pcie": state["pcie"].get("message", "")},
            "\n".join(lines),
            1 if (failed or state["last_error"]) else 0,
            client=client,
        )
    except Exception:
        log.exception("could not record the board test run")


def _run_tests_worker(test_ids: list[str], iperf_settings: dict, iperf10g_settings: dict,
                      event: str = "board-test.run", client: str = "system"):
    try:
        _set_started_once()
        _set_last_error("")

        # Report the detected PCIe/bitstream state but do NOT block the board
        # system test on a mismatch: the result is surfaced in the UI (warning
        # banner) while the tests still run.
        ok, msg, details = _parse_pcie_device_sysfs()
        _set_pcie(ok, msg, details)

        try:
            sess = SerialSession(SERIAL_PORT, SERIAL_BAUD)
        except Exception as exc:
            err = f"Serial open failed on {SERIAL_PORT}: {exc}"
            _set_last_error(err)
            for test_id in test_ids:
                _set_test_state(test_id, status="fail", result="Fail", output=err + "\n", has_run=True)
            return

        selected = set(test_ids)
        try:
            for test_id in _get_tests_order():
                if test_id not in selected:
                    continue
                _run_single_test(sess, test_id, iperf_settings, iperf10g_settings)
        finally:
            sess.close()
    finally:
        _set_worker_running(False)
        _record_run(test_ids, event, client)


def _refresh_tests() -> list[str]:
    test_ids = _discover_available_tests()
    _set_tests_order(test_ids)
    return test_ids


def _get_current_tests() -> list[str]:
    """Return already-discovered tests, or fall back to local discovery."""
    with _STATE_LOCK:
        if _STATE["discovered"] and _STATE["tests_order"]:
            return list(_STATE["tests_order"])
    return _refresh_tests()


# If discovery has been running longer than this, consider it stale.
_DISCOVER_STALE_S = 45


def _discover_worker():
    """Background worker that runs PCIe check + serial discovery."""
    try:
        # Report the PCIe/bitstream state, but continue with discovery even on a
        # mismatch so the board system test is not blocked (report, don't block).
        ok, msg, details = _parse_pcie_device_sysfs()
        _set_pcie(ok, msg, details)

        test_ids = _discover_tests_via_serial()
        if not test_ids:
            # Fall back to local ptest-runner if serial discovery fails
            test_ids = _discover_available_tests()
        _set_tests_order(test_ids)

        with _STATE_LOCK:
            _STATE["discovered"] = True
            _STATE["discovering"] = False
    except Exception as exc:
        with _STATE_LOCK:
            _STATE["discovering"] = False
        _set_last_error(f"Discovery failed: {exc}")


def discover(force: bool = False):
    """Kick off PCIe check + test discovery in a background thread.

    Returns the current snapshot immediately (non-blocking).
    The frontend should poll /api/board-test/status and check the
    ``discovered`` / ``discovering`` flags for completion.

    If *force* is True, a previously completed or stale discovery is
    reset so a fresh attempt can start.
    """
    now = _now()
    should_start = False
    with _STATE_LOCK:
        if _STATE["running"]:
            pass
        elif _STATE["discovering"]:
            elapsed = now - _STATE["discover_started_at"]
            if elapsed >= _DISCOVER_STALE_S or force:
                _STATE["discovering"] = True
                _STATE["discovered"] = False
                _STATE["discover_started_at"] = now
                should_start = True
        elif _STATE["discovered"] and not force:
            pass
        else:
            _STATE["discovering"] = True
            _STATE["discovered"] = False
            _STATE["discover_started_at"] = now
            should_start = True

    if should_start:
        t = threading.Thread(target=_discover_worker, daemon=True)
        t.start()
    return snapshot()


def _reset_tests_for_run(test_ids: list[str], iperf_settings: dict, iperf10g_settings: dict):
    for test_id in test_ids:
        _set_test_state(
            test_id,
            status="pending",
            result="",
            output="",
            has_run=False,
            command=_build_test_command(test_id, iperf_settings, iperf10g_settings),
        )


def start_all(force: bool = False, iperf_options: dict | None = None, iperf10g_options: dict | None = None):
    ok, msg, iperf_settings = _normalize_iperf_options(iperf_options)
    if not ok:
        return False, msg, snapshot()
    ok, msg, iperf10g_settings = _normalize_iperf_options(
        iperf10g_options, default_server=IPERF10G_DEFAULT_SERVER, default_bw=IPERF10G_DEFAULT_MIN_BW
    )
    if not ok:
        return False, msg, snapshot()

    test_ids = _get_current_tests()
    _set_iperf_settings(iperf_settings["server_ip"], iperf_settings["min_bw"])
    _set_iperf10g_settings(iperf10g_settings["server_ip"], iperf10g_settings["min_bw"])

    should_return = None
    with _STATE_LOCK:
        if _STATE["running"]:
            should_return = (False, "board test run already in progress")
        elif _STATE["started_once"] and not force:
            should_return = (True, "already started")
        else:
            _STATE["pcie"] = {"ok": None, "message": "", "details": ""}
            _STATE["running"] = True
            _STATE["current_test"] = None

    if should_return is not None:
        ok_val, message = should_return
        return ok_val, message, snapshot()

    _reset_tests_for_run(test_ids, iperf_settings, iperf10g_settings)

    t = threading.Thread(target=_run_tests_worker,
                         args=(test_ids, iperf_settings, iperf10g_settings,
                               "board-test.run", support_log.client_addr()),
                         daemon=True)
    t.start()
    return True, "started", snapshot()


def rerun_one(test_id: str, iperf_options: dict | None = None, iperf10g_options: dict | None = None):
    test_ids = _get_current_tests()
    if test_id not in test_ids:
        return False, f"unknown test: {test_id}", snapshot()

    ok, msg, iperf_settings = _normalize_iperf_options(iperf_options)
    if not ok:
        return False, msg, snapshot()
    ok, msg, iperf10g_settings = _normalize_iperf_options(
        iperf10g_options, default_server=IPERF10G_DEFAULT_SERVER, default_bw=IPERF10G_DEFAULT_MIN_BW
    )
    if not ok:
        return False, msg, snapshot()

    _set_iperf_settings(iperf_settings["server_ip"], iperf_settings["min_bw"])
    _set_iperf10g_settings(iperf10g_settings["server_ip"], iperf10g_settings["min_bw"])

    running_now = False
    with _STATE_LOCK:
        if _STATE["running"]:
            running_now = True
        else:
            _STATE["running"] = True
            _STATE["current_test"] = None

    if running_now:
        return False, "board test run already in progress", snapshot()

    _reset_tests_for_run([test_id], iperf_settings, iperf10g_settings)

    t = threading.Thread(target=_run_tests_worker,
                         args=([test_id], iperf_settings, iperf10g_settings,
                               "board-test.rerun", support_log.client_addr()),
                         daemon=True)
    t.start()
    return True, f"rerun started: {test_id}", snapshot()


# Tests are discovered dynamically via discover() when the board-test
# page is loaded, so we do not prime the list at module load.
