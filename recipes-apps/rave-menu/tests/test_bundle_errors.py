#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: BM
# Maintainer: BM

"""Exercise descriptor-read failures through the installer's safe --dry-run path.

The URL cases replace wget with a local producer, including its broken-pipe
behavior, so these tests need neither a network connection nor a target disk.
"""

import gzip
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "files/scripts/install_app_bundle.sh"
DESCRIPTOR = b"""format: rave2-appimage
format_version: 2
id: test
name: Test
version: '1'
boot:
  make_default: false
  cmdline:
    kernel: /boot/bzImage
components:
  - kind: x86-rootfs-tar
    path: x86/rootfs.tar
    sha256: placeholder
"""


def archive(members):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in members:
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
    return raw.getvalue()


class BundleErrors(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "test.rave2app"
        self.env = dict(os.environ, APP_CACHE_DIR=str(self.root / "cache"))
        self.env["PATH"] = str(self.root) + os.pathsep + self.env["PATH"]
        self.env["TEST_BUNDLE"] = str(self.bundle)
        self.env["TEST_BYTES_READ"] = str(self.root / "bytes-read")
        wget = self.root / "wget"
        wget.write_text("""#!/usr/bin/env python3
import os
from pathlib import Path
import sys

if os.environ.get('TEST_DOWNLOAD_FAILURE'):
    sys.exit(8)
count = 0
status = 0
try:
    with open(os.environ['TEST_BUNDLE'], 'rb') as source:
        while chunk := source.read(4096):
            count += os.write(1, chunk)
except BrokenPipeError:
    status = 3
Path(os.environ['TEST_BYTES_READ']).write_text(str(count))
sys.exit(status)
""")
        wget.chmod(0o755)

    def run_install(self, content, remote=False):
        self.bundle.write_bytes(content)
        source = "https://example.invalid/test.rave2app" if remote else str(self.bundle)
        result = subprocess.run(
            ["sh", str(INSTALLER), source, "--dry-run"],
            env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=15,
        )
        return result.returncode, result.stdout

    def assert_failure(self, content, message, remote=False):
        code, output = self.run_install(content, remote)
        self.assertNotEqual(code, 0, output)
        self.assertIn(message, output)
        self.assertNotIn("validating envelope", output)
        self.assertNotIn("Init Partition", output)
        return output

    def test_valid_local_bundle(self):
        code, output = self.run_install(gzip.compress(archive([
            ("appimage.yaml", DESCRIPTOR), ("x86/rootfs.tar", b"payload"),
        ])))
        self.assertEqual(code, 0, output)

    def test_non_gzip_response(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                self.assert_failure(b"<html>not a bundle</html>",
                                    "not a gzip-compressed .rave2app", remote)

    def test_truncated_descriptor(self):
        # The member header declares a full descriptor, but only a prefix is
        # available. Non-empty extraction output must not count as success.
        raw = archive([("appimage.yaml", DESCRIPTOR)])[:520]
        for remote in (False, True):
            with self.subTest(remote=remote):
                self.assert_failure(gzip.compress(raw), "corrupt", remote)

    def test_gzip_crc_failure(self):
        damaged = bytearray(gzip.compress(archive([("appimage.yaml", DESCRIPTOR)])))
        damaged[-8] ^= 0xFF
        for remote in (False, True):
            with self.subTest(remote=remote):
                output = self.assert_failure(bytes(damaged), "bundle is corrupt", remote)
                self.assertIn("gzip:", output)

    def test_truncated_gzip(self):
        data = gzip.compress(archive([("appimage.yaml", DESCRIPTOR)]))[:-8]
        self.assert_failure(data, "bundle is corrupt")

    def test_invalid_tar_inside_valid_gzip(self):
        self.assert_failure(gzip.compress(b"not a tar archive"), "corrupt")

    def test_missing_descriptor(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                self.assert_failure(gzip.compress(archive([("other.txt", b"ok")])),
                                    "does not contain appimage.yaml", remote)

    def test_empty_descriptor(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                self.assert_failure(gzip.compress(archive([("appimage.yaml", b"")])),
                                    "empty appimage.yaml", remote)

    def test_failed_download(self):
        self.env["TEST_DOWNLOAD_FAILURE"] = "1"
        self.assert_failure(b"", "bundle download failed or was interrupted", True)

    def test_empty_remote_descriptor_before_large_payload(self):
        data = gzip.compress(archive([
            ("appimage.yaml", b""), ("x86/rootfs.tar", os.urandom(2 * 1024 * 1024)),
        ]))
        self.assert_failure(data, "empty appimage.yaml", True)

    def test_valid_remote_header_stops_before_large_payload(self):
        data = gzip.compress(archive([
            ("appimage.yaml", DESCRIPTOR),
            ("x86/rootfs.tar", os.urandom(2 * 1024 * 1024)),
        ]))
        code, output = self.run_install(data, remote=True)
        self.assertEqual(code, 0, output)
        self.assertLess(int((self.root / "bytes-read").read_text()), len(data))


if __name__ == "__main__":
    unittest.main()
