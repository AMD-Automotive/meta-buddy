#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: BM
# Maintainer: BM

"""Exercise the real extraction functions in temporary directories, without disks."""
import gzip
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "files/scripts/install_app_bundle.sh"
SOURCE = INSTALLER.read_text()


def function(name):
    return re.search(r"^" + re.escape(name) + r"\(\).*?^}", SOURCE, re.M | re.S)[0]


def tar_bytes(name, data, mtime):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(data)
        member.mtime = mtime
        archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


class Extraction(unittest.TestCase):
    def run_extraction(self, kind, streaming, payload):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        work = root / "work"
        work.mkdir()
        target = root / "target"
        target.mkdir()
        bundle = root / "bundle.rave2app"
        bundle.write_bytes(gzip.compress(tar_bytes("payload.tar", payload, 0)))
        for field, value in {
            "kind": kind, "path": "payload.tar", "stage": "post-deploy",
            "target": "/", "sha256": hashlib.sha256(payload).hexdigest(),
        }.items():
            (work / ("f_" + field)).write_text("0\t" + value + "\n")
        functions = ["comp_field", "fast_tee", "stream_bundle_source",
                     "_stream_bundle_members", "deploy_rootfs_tar", "apply_init_components"]
        script = "set -eu\n" + "\n".join(function(n) for n in functions)
        # Mount/format are intentionally replaced: every extraction destination
        # is an ordinary temporary directory and no device is ever opened.
        script += """
die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "$*"; }
emit_tab() { echo "$*"; }
format_app_slot() { :; }
mount_fresh_slot() { :; }
verify_rootfs_kernel() { :; }
patch_rootfs_fstab() { :; }
"""
        if streaming:
            script += "_stream_bundle_members\n"
        if kind == "overlay":
            script += "apply_init_components post-deploy\n"
        elif not streaming:
            script += "deploy_rootfs_tar\n"
        result = subprocess.run(
            ["sh", "-c", script], text=True, capture_output=True, timeout=10,
            env=dict(os.environ, WORK_DIR=str(work), APP_MNT=str(target),
                     BUNDLE=str(bundle), BUNDLE_SRC=str(bundle), IS_URL="0",
                     APP_IMG_IDX="0", SLOT_DEV="unused", STREAMING=str(int(streaming)),
                     LC_ALL="C"),
        )
        return result, target

    def test_future_timestamps_preserved_without_repetitive_warnings(self):
        mtime = int(time.time()) + 30 * 86400
        payload = tar_bytes("future-file", b"file content", mtime)
        for kind in ("x86-rootfs-tar", "overlay"):
            for streaming in (False, True):
                with self.subTest(kind=kind, streaming=streaming):
                    result, target = self.run_extraction(kind, streaming, payload)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("in the future", result.stderr)
                    self.assertEqual((target / "future-file").read_bytes(), b"file content")
                    self.assertEqual(int((target / "future-file").stat().st_mtime), mtime)

    def test_corrupt_payload_still_fails_with_tar_diagnostics(self):
        for kind in ("x86-rootfs-tar", "overlay"):
            for streaming in (False, True):
                with self.subTest(kind=kind, streaming=streaming):
                    result, _ = self.run_extraction(kind, streaming, b"not a tar archive")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("tar:", result.stderr)
                    self.assertIn("ERROR:", result.stderr)


if __name__ == "__main__":
    unittest.main()
