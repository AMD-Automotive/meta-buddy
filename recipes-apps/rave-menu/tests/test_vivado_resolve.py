#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Tests for the toolchain resolver in vivado_installer.py.

Runs on the build host with no root and no board: the install prefix, the
/run marker, the selection file and the port-owner lookup are all pointed at
temporary fakes.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

FILES = Path(__file__).resolve().parent.parent / "files"
sys.path.insert(0, str(FILES))

import vivado_installer as _vi_module

# The module is imported once for the whole run, so any function a test
# replaces has to be put back or it leaks into later tests.
_PRISTINE = {name: getattr(_vi_module, name) for name in (
    "_port_owner_pid", "_pid_alive", "_proc_field", "_version_of_pid",
)}


def _write_manifest(path: Path, prefix: str, versions, base, programming=None):
    payload = {
        "install_prefix": prefix,
        "base_version": base,
        "default_version": base,
        "versions": [{"version": v, "filename": f"{v}.tar", "url": "",
                      "sha256": "", "qr": ""} for v in versions],
    }
    if programming is not None:
        payload["programming_versions"] = programming
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_install(prefix: Path, version: str):
    """Create a tool tree that looks installed: hw_server + setupEnv.sh."""
    binary = prefix / version / "HWSRVR" / "bin" / "hw_server"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    settings = prefix / version / "HWSRVR" / "bin" / "setupEnv.sh"
    settings.write_text("# fake\n", encoding="utf-8")
    return binary


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.prefix = root / "opt" / "tools"
        self.prefix.mkdir(parents=True)
        self.manifest = root / "manifest.json"
        _write_manifest(self.manifest, str(self.prefix),
                        ["2025.2", "2026.1"], "2025.2")

        import vivado_installer as vi
        for name, func in _PRISTINE.items():
            setattr(vi, name, func)
        # Re-point every path constant at the fake tree, then reload the
        # manifest-derived globals.
        vi.MANIFEST_PATH = self.manifest
        vi._MANIFEST = vi._load_manifest()
        vi.INSTALL_PREFIX = str(self.prefix)
        vi.CATALOG = vi._parse_catalog(vi._MANIFEST)
        vi.BASE_VERSION = "2025.2"
        vi.DEFAULT_VERSION = "2025.2"
        vi.PROGRAMMING_VERSIONS = vi._parse_programming_versions(vi._MANIFEST)
        vi.VIVADO_STATE_DIR = root / "state"
        vi.SELECTION_PATH = vi.VIVADO_STATE_DIR / "selected-version.json"
        vi.RUNTIME_DIR = root / "run"
        vi.MARKER_PATH = vi.RUNTIME_DIR / "hw-server.json"
        self.vi = vi

        self._no_server()
        os.environ.pop("RAVE_VIVADO_VERSION", None)

    def tearDown(self):
        for name, func in _PRISTINE.items():
            setattr(self.vi, name, func)
        self.tmp.cleanup()
        os.environ.pop("RAVE_VIVADO_VERSION", None)

    # -- helpers ---------------------------------------------------------

    def _no_server(self):
        self.vi._port_owner_pid = lambda port=None: None

    def _server(self, version, pid=4242, alive=True, cmdline=True):
        """Fake a running server without spawning anything."""
        self.vi._port_owner_pid = lambda port=None: pid
        self.vi._pid_alive = lambda p: alive
        path = f"{self.prefix}/{version}/HWSRVR/bin/hw_server"
        if cmdline:
            self.vi._proc_field = lambda p, name: f"/bin/bash {path}"
        else:
            self.vi._proc_field = lambda p, name: ""

    def _install(self, *versions):
        for v in versions:
            _make_install(self.prefix, v)

    # -- precedence ------------------------------------------------------

    def test_base_when_nothing_else(self):
        self._install("2025.2", "2026.1")
        self.assertEqual(self.vi.resolve_version()[:2], ("2025.2", "base"))

    def test_newest_when_base_absent(self):
        self._install("2026.1")
        self.assertEqual(self.vi.resolve_version()[:2], ("2026.1", "newest"))

    def test_nothing_installed(self):
        version, source, _ = self.vi.resolve_version()
        self.assertIsNone(version)
        self.assertEqual(source, "none")

    def test_selection_beats_base(self):
        self._install("2025.2", "2026.1")
        self.assertTrue(self.vi.set_selected_version("2026.1"))
        self.assertEqual(self.vi.resolve_version()[:2], ("2026.1", "selection"))

    def test_running_server_beats_selection(self):
        self._install("2025.2", "2026.1")
        self.vi.set_selected_version("2025.2")
        self._server("2026.1")
        version, source, warnings = self.vi.resolve_version()
        self.assertEqual((version, source), ("2026.1", "running"))
        self.assertTrue(any("2026.1 hardware server is running" in w
                            for w in warnings))

    def test_override_beats_everything(self):
        self._install("2025.2", "2026.1")
        self.vi.set_selected_version("2025.2")
        self._server("2025.2")
        os.environ["RAVE_VIVADO_VERSION"] = "2026.1"
        self.assertEqual(self.vi.resolve_version()[:2], ("2026.1", "override"))

    def test_override_ignored_when_not_installed(self):
        self._install("2025.2")
        os.environ["RAVE_VIVADO_VERSION"] = "2026.1"
        version, source, warnings = self.vi.resolve_version()
        self.assertEqual((version, source), ("2025.2", "base"))
        self.assertTrue(any("not installed" in w for w in warnings))

    # -- marker / detection robustness ------------------------------------

    def test_marker_pid_with_bash_exe_is_accepted(self):
        # Regression: the recorded pid is a bash wrapper whose /proc/<pid>/exe
        # is /usr/bin/bash.bash, so validation must use cmdline.
        self._install("2026.1")
        self.vi.marker_write("2026.1", 4242, "x")
        self.vi._pid_alive = lambda p: True
        path = f"{self.prefix}/2026.1/HWSRVR/bin/hw_server"
        self.vi._proc_field = lambda p, name: f"/bin/bash {path}"
        server = self.vi.running_server()
        self.assertEqual(server["version"], "2026.1")
        self.assertEqual(server["source"], "marker")

    def test_stale_marker_dead_pid_falls_through(self):
        self._install("2025.2")
        self.vi.marker_write("2026.1", 4242, "x")
        self.vi._pid_alive = lambda p: False
        self._no_server()
        self.assertIsNone(self.vi.running_server())
        self.assertEqual(self.vi.resolve_version()[:2], ("2025.2", "base"))

    def test_pid_alive_uses_proc_not_signals(self):
        # Regression: kill(pid, 0) on a root-owned process raises EPERM from a
        # non-root caller, which previously read as "dead" and silently
        # dropped the running server from the decision.
        self.assertTrue(self.vi._pid_alive(os.getpid()))
        self.assertFalse(self.vi._pid_alive(999999))

    def test_unwrapped_port_owner_maps_to_version(self):
        # Regression: the real binary lives at bin/unwrapped/lnx64.o/hw_server,
        # which is not in HW_SERVER_SUBPATHS -- must prefix-match instead.
        self._install("2026.1")
        unwrapped = f"{self.prefix}/2026.1/HWSRVR/bin/unwrapped/lnx64.o/hw_server"
        self.vi._port_owner_pid = lambda port=None: 99
        self.vi._pid_alive = lambda p: True
        self.vi._proc_field = lambda p, name: unwrapped
        self.assertEqual(self.vi.running_server()["version"], "2026.1")

    def test_unmappable_port_owner_warns_and_falls_through(self):
        self._install("2025.2")
        self.vi._port_owner_pid = lambda port=None: 99
        self.vi._pid_alive = lambda p: True
        self.vi._proc_field = lambda p, name: "/usr/bin/something-else"
        self.vi._version_of_pid = lambda p: None
        version, source, warnings = self.vi.resolve_version()
        self.assertEqual((version, source), ("2025.2", "base"))
        self.assertTrue(any("could not be matched" in w for w in warnings))

    # -- selection file ---------------------------------------------------

    def test_selection_ignored_when_uninstalled(self):
        self._install("2025.2")
        self.vi.set_selected_version("2026.1")
        version, source, warnings = self.vi.resolve_version()
        self.assertEqual((version, source), ("2025.2", "base"))
        self.assertTrue(any("no longer installed" in w for w in warnings))
        # The file is left alone so re-installing restores the user's intent.
        self.assertEqual(self.vi.selected_version(), "2026.1")

    def test_selection_rejects_invalid(self):
        self.assertFalse(self.vi.set_selected_version("../../etc"))
        self.assertIsNone(self.vi.selected_version())

    def test_corrupt_selection_is_ignored(self):
        self.vi.SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.vi.SELECTION_PATH.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.vi.selected_version())

    def test_selection_round_trip_leaves_no_temp(self):
        self._install("2026.1")
        self.vi.set_selected_version("2026.1")
        self.assertEqual(self.vi.selected_version(), "2026.1")
        leftovers = list(self.vi.SELECTION_PATH.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    # -- settings + qualification -----------------------------------------

    def test_settings_script_found(self):
        self._install("2026.1")
        self.assertTrue(
            self.vi.settings_script("2026.1").endswith("HWSRVR/bin/setupEnv.sh"))

    def test_settings_script_missing(self):
        self.assertIsNone(self.vi.settings_script("2099.9"))

    def test_programming_qualification_defaults_to_all(self):
        self.assertTrue(self.vi.is_programming_qualified("2026.1"))

    def test_unqualified_release_warns_but_still_resolves(self):
        _write_manifest(self.manifest, str(self.prefix),
                        ["2025.2", "2026.1"], "2025.2", programming=["2025.2"])
        self.vi._MANIFEST = self.vi._load_manifest()
        self.vi.PROGRAMMING_VERSIONS = self.vi._parse_programming_versions(
            self.vi._MANIFEST)
        self._install("2025.2", "2026.1")
        self.vi.set_selected_version("2026.1")
        status = self.vi.effective_status()
        self.assertEqual(status["effective_version"], "2026.1")
        self.assertFalse(status["programming_qualified"])
        self.assertTrue(any("not listed as qualified" in w
                            for w in status["warnings"]))


if __name__ == "__main__":
    unittest.main()
