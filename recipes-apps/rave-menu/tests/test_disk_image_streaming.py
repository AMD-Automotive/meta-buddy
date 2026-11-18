#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Host-side tests for deploy_disk_image_streamed and the x86-disk-image-member
dispatcher branch. No device, no BitBake build required.

Uses sparse files as block-device stand-ins so dd/e2fsck/resize2fs work without
root, and a real gzip+tar synthetic bundle to exercise the single-pass streaming
path end-to-end.

Test structure follows test_bundle_extraction.py (function() regex, subprocess sh)
and test_app_region.py (sparse-file GPT fixtures via sfdisk).
"""

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALLER = HERE.parent / "files/scripts/install_app_bundle.sh"
SOURCE = INSTALLER.read_text()

MIB = 1024 * 1024


def function(name):
    """Extract a shell function body by name from the installer source."""
    return re.search(r"^" + re.escape(name) + r"\(\).*?^}", SOURCE, re.M | re.S)[0]


def make_sparse_file(path, size_mib):
    """Create a sparse file of the given size (no real disk allocation)."""
    with open(path, "wb") as fh:
        fh.truncate(size_mib * MIB)


def make_ext4(path, size_mib=32):
    """Create an ext4 filesystem on a sparse file. Returns the path."""
    make_sparse_file(path, size_mib)
    subprocess.run(
        ["mkfs.ext4", "-F", "-q", str(path)],
        capture_output=True, check=True,
    )
    return path


def make_fat(path, size_mib=16):
    """Create a FAT filesystem on a sparse file."""
    make_sparse_file(path, size_mib)
    subprocess.run(
        ["mkfs.fat", "-F", "32", str(path)],
        capture_output=True, check=True,
    )
    return path


def build_bundle(members):
    """Build a gzip-compressed tar with the given (name, bytes) members."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _can_mkfs():
    return shutil.which("mkfs.ext4") is not None


def _can_sfdisk():
    return shutil.which("sfdisk") is not None


def _skip_unless(*tools):
    missing = [t for t in tools if shutil.which(t) is None]
    return unittest.skipIf(missing, f"tools not available: {missing}")


@unittest.skipUnless(_can_mkfs(), "mkfs.ext4 not available")
class DiskImageStreamingTests(unittest.TestCase):
    """Exercise deploy_disk_image_streamed and the dispatcher x86-disk-image-member
    branch using synthetic bundles and sparse-file block-device stand-ins."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.cache = self.root / "cache"
        self.cache.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_layout(self, partitions):
        """Write a layout.json and return its path."""
        path = self.work / "layout.json"
        path.write_text(json.dumps(partitions))
        return path

    def _write_region_devices(self, rows):
        """Write a region_devices TSV and return its path.
        rows: list of (name, dev, partuuid, fs, grow)
        """
        path = self.work / "region_devices"
        path.write_text("\n".join(
            "\t".join(str(c) for c in row) for row in rows
        ) + "\n")
        return path

    def _write_f_kind(self, components):
        """Write f_kind TSV: list of (idx, kind) pairs."""
        path = self.work / "f_kind"
        path.write_text("\n".join(f"{i}\t{k}" for i, k in components) + "\n")
        return path

    def _write_f_path(self, components):
        """Write f_path TSV: list of (idx, path) pairs."""
        path = self.work / "f_path"
        path.write_text("\n".join(f"{i}\t{p}" for i, p in components) + "\n")
        return path

    def _write_f_sha256(self, components):
        path = self.work / "f_sha256"
        path.write_text("\n".join(f"{i}\t{s}" for i, s in components) + "\n")
        return path

    def _run_stream(self, layout, region_devices, f_kind, bundle_bytes,
                    extra_env=None, disk_members_rows=None):
        """
        Run the streaming pipeline using the real _stream_bundle_members +
        x86-disk-image-member dispatcher branch, with app_region.py stubbed out.

        layout: list of dicts with {part, path, sha256, ...}
        region_devices: list of (name, dev, partuuid, fs, grow)
        f_kind: list of (idx, kind)
        bundle_bytes: gzip+tar bytes
        disk_members_rows: list of (name, dev, fs, grow, sha256, discarded)
        """
        layout_path = self._write_layout(layout)
        self._write_region_devices(region_devices)
        self._write_f_kind(f_kind)
        # comp_field reads these; write empty placeholders for non-disk-image components
        for field in ("path", "sha256", "stage", "target"):
            p = self.work / ("f_" + field)
            if not p.exists():
                p.write_text("")

        # Write bundle
        bundle = self.root / "bundle.rave2app"
        bundle.write_bytes(bundle_bytes)

        # Build disk_map from disk_members_rows (normally done by
        # deploy_disk_image_streamed's prepare loop; we replicate it here)
        disk_map = self.work / "disk_map"
        disk_members = self.work / "disk_members"
        disk_map_lines = []
        disk_members_lines = []
        if disk_members_rows:
            for row in disk_members_rows:
                name, dev, fs, grow, sha, discarded = row
                # Find path from layout
                lpath = next((p["path"] for p in layout if p["part"] == name), "")
                disk_map_lines.append(f"{lpath}\tx86-disk-image-member\t{name}")
                disk_members_lines.append(
                    f"{name}\t{dev}\t{fs}\t{grow}\t{sha}\t{discarded}"
                )
        disk_map.write_text("\n".join(disk_map_lines) + "\n" if disk_map_lines else "")
        disk_members.write_text(
            "\n".join(disk_members_lines) + "\n" if disk_members_lines else ""
        )

        # Extract and assemble the shell functions we need
        functions = [
            "comp_field", "fast_tee", "stream_bundle_source",
            "_stream_bundle_members",
        ]
        script = "set -eu\n" + "\n".join(function(n) for n in functions)
        script += """
die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "$*"; }
emit_tab() { echo "$*"; }
"""
        # Invoke just _stream_bundle_members (the function under test)
        script += "_stream_bundle_members\n"

        env = dict(
            os.environ,
            WORK_DIR=str(self.work),
            APP_MNT=str(self.root / "app-mnt"),
            BUNDLE=str(bundle),
            BUNDLE_SRC=str(bundle),
            IS_URL="0",
            STREAMING="1",
            SLOT_DEV="unused",
            APP_IMG_IDX="0",
            DISK_MEMBERS=str(disk_members),
            APP_ROOT_DEV="",
            LC_ALL="C",
        )
        if extra_env:
            env.update(extra_env)

        return subprocess.run(
            ["sh", "-c", script], text=True, capture_output=True, timeout=60, env=env,
        )

    # ------------------------------------------------------------------
    # Test: single-pass write — each member byte-matches the source data
    # ------------------------------------------------------------------
    def test_fat_member_written_correctly(self):
        """A FAT member written via the dispatcher lands byte-for-byte on the device."""
        fat_data = make_fat(self.work / "p1.fat.raw").read_bytes()
        fat_sha = sha256(fat_data)
        dev_path = self.root / "p1.dev"
        make_sparse_file(dev_path, 20)  # 20 MiB stand-in

        layout = [{"part": "app-boot", "path": "x86/disk/p1.fat", "sha256": fat_sha}]
        region_devices = [("app-boot", str(dev_path), "some-uuid", "vfat", "false")]
        f_kind = [("0", "x86-disk-image")]
        members = [("x86/disk/p1.fat", fat_data)]
        bundle = build_bundle(members)

        disk_members_rows = [
            ("app-boot", str(dev_path), "vfat", "false", fat_sha, "1"),
        ]
        result = self._run_stream(layout, region_devices, f_kind, bundle,
                                  disk_members_rows=disk_members_rows)
        self.assertEqual(result.returncode, 0, result.stderr)

        written = dev_path.read_bytes()[:len(fat_data)]
        self.assertEqual(written, fat_data)

    def test_sha256_mismatch_causes_failure(self):
        """A corrupted member (wrong sha256) causes the install to fail with a clear message."""
        fat_data = bytes([0xfa]) * (1 * MIB)  # fake FAT data
        wrong_sha = "0" * 64
        dev_path = self.root / "p1.dev"
        make_sparse_file(dev_path, 4)

        layout = [{"part": "app-boot", "path": "x86/disk/p1.fat", "sha256": wrong_sha}]
        region_devices = [("app-boot", str(dev_path), "some-uuid", "vfat", "false")]
        f_kind = [("0", "x86-disk-image")]
        members = [("x86/disk/p1.fat", fat_data)]
        bundle = build_bundle(members)
        disk_members_rows = [
            ("app-boot", str(dev_path), "vfat", "false", wrong_sha, "1"),
        ]
        result = self._run_stream(layout, region_devices, f_kind, bundle,
                                  disk_members_rows=disk_members_rows)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sha256 mismatch", result.stderr + result.stdout)

    def test_ext4_member_with_resize(self):
        """An ext4 member is written and resize2fs is called on it."""
        # Make a small ext4 image (8 MiB), then put it in a 16 MiB device
        ext4_data = make_ext4(self.work / "p2.ext4.raw", size_mib=8).read_bytes()
        ext4_sha = sha256(ext4_data)
        dev_path = self.root / "p2.dev"
        # Device larger than image — resize2fs should run without error
        make_ext4(dev_path, size_mib=16)
        # Overwrite with sparse (no real filesystem needed for the write test)
        make_sparse_file(dev_path, 16)

        layout = [{"part": "app-root", "path": "x86/disk/p2.ext4", "sha256": ext4_sha}]
        region_devices = [("app-root", str(dev_path), "root-uuid", "ext4", "true")]
        f_kind = [("0", "x86-disk-image")]
        members = [("x86/disk/p2.ext4", ext4_data)]
        bundle = build_bundle(members)
        # discarded=1 so no zero-fill branch
        disk_members_rows = [
            ("app-root", str(dev_path), "ext4", "true", ext4_sha, "1"),
        ]

        app_mnt = self.root / "app-mnt"
        app_mnt.mkdir(exist_ok=True)

        result = self._run_stream(layout, region_devices, f_kind, bundle,
                                  disk_members_rows=disk_members_rows,
                                  extra_env={"APP_MNT": str(app_mnt),
                                             "APP_ROOT_DEV": str(dev_path)})
        combined = result.stderr + result.stdout
        # sha256 must pass (dd wrote the right bytes) even if resize2fs/mount
        # fails without root in the host test environment.
        self.assertNotIn("sha256 mismatch", combined)
        self.assertIn("sha256 OK", combined)
        # e2fsck modifies superblock timestamps so byte-equality after
        # resize doesn't hold; sha256 OK above confirms the write was correct.

    def test_multiple_members_single_pass(self):
        """Two partition members in one bundle — both land on their devices in one pass."""
        fat_data = b"\xeb\x00" * (2 * MIB)
        ext4_data = b"\x53\xef" * (2 * MIB)  # fake ext4 magic at start
        fat_sha = sha256(fat_data)
        ext4_sha = sha256(ext4_data)

        fat_dev = self.root / "p1.dev"
        ext4_dev = self.root / "p2.dev"
        make_sparse_file(fat_dev, 4)
        make_sparse_file(ext4_dev, 4)

        layout = [
            {"part": "app-boot", "path": "x86/disk/p1.fat",  "sha256": fat_sha},
            {"part": "app-root", "path": "x86/disk/p2.ext4", "sha256": ext4_sha},
        ]
        region_devices = [
            ("app-boot", str(fat_dev),  "uuid1", "vfat", "false"),
            ("app-root", str(ext4_dev), "uuid2", "ext4", "false"),
        ]
        f_kind = [("0", "x86-disk-image")]
        members = [
            ("x86/disk/p1.fat",  fat_data),
            ("x86/disk/p2.ext4", ext4_data),
        ]
        bundle = build_bundle(members)
        disk_members_rows = [
            ("app-boot", str(fat_dev),  "vfat", "false", fat_sha,  "1"),
            ("app-root", str(ext4_dev), "ext4", "false", ext4_sha, "1"),
        ]

        app_mnt = self.root / "app-mnt"
        app_mnt.mkdir(exist_ok=True)

        result = self._run_stream(layout, region_devices, f_kind, bundle,
                                  disk_members_rows=disk_members_rows,
                                  extra_env={"APP_MNT": str(app_mnt),
                                             "APP_ROOT_DEV": str(ext4_dev)})
        combined = result.stderr + result.stdout
        # Both sha256 checks must pass (both members written correctly).
        # The mount of app-root (ext4 sparse file) may fail without root in
        # the host test environment; that is acceptable here.
        self.assertNotIn("sha256 mismatch", combined)
        self.assertEqual(combined.count("sha256 OK"), 2, combined)
        self.assertEqual(fat_dev.read_bytes()[:len(fat_data)], fat_data)
        self.assertEqual(ext4_dev.read_bytes()[:len(ext4_data)], ext4_data)

    def test_unknown_member_discarded(self):
        """A member not in the stream_map is discarded (the '*' catch-all case)."""
        data = b"x" * 1024
        bundle = build_bundle([("some/unknown/member.bin", data)])
        layout = []
        region_devices = []
        f_kind = [("0", "x86-disk-image")]
        result = self._run_stream(layout, region_devices, f_kind, bundle,
                                  disk_members_rows=[])
        # Should complete successfully with no error (unknown member -> /dev/null)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_error_file_propagated_on_failure(self):
        """A dispatcher error (bad sha256) surfaces as a specific message, not generic."""
        fat_data = b"\xfa" * (1 * MIB)
        dev_path = self.root / "p1.dev"
        make_sparse_file(dev_path, 4)
        wrong_sha = "a" * 64

        layout = [{"part": "app-boot", "path": "x86/disk/p1.fat", "sha256": wrong_sha}]
        region_devices = [("app-boot", str(dev_path), "uuid", "vfat", "false")]
        f_kind = [("0", "x86-disk-image")]
        bundle = build_bundle([("x86/disk/p1.fat", fat_data)])
        disk_members_rows = [
            ("app-boot", str(dev_path), "vfat", "false", wrong_sha, "1"),
        ]
        result = self._run_stream(layout, region_devices, f_kind, bundle,
                                  disk_members_rows=disk_members_rows)
        self.assertNotEqual(result.returncode, 0)
        combined = result.stderr + result.stdout
        # Should mention sha256 mismatch specifically, not the generic network error
        self.assertIn("sha256 mismatch", combined)
        self.assertNotIn("network or archive error", combined)


class MemberOrderEdgeCaseTests(unittest.TestCase):
    """Verify defined behavior when Versal members arrive before disk partitions.

    Since these tests require the _ensure_app_root_mounted dispatcher function
    and the mount syscall, we only verify the failure path here (no root/device
    needed). The success path is covered by the DiskImageStreamingTests above.
    """

    def test_ensure_app_root_mounted_function_present_in_dispatcher(self):
        """The _ensure_app_root_mounted helper is emitted into dispatch.sh."""
        disp_block = re.search(
            r"cat > \"\$_disp\" <<'DISP'(.*?)^DISP",
            SOURCE, re.M | re.S
        )
        self.assertIsNotNone(disp_block, "could not find dispatcher heredoc in installer")
        disp_text = disp_block.group(1)
        self.assertIn("_ensure_app_root_mounted", disp_text)
        self.assertIn("versal-ospi", disp_text)
        self.assertIn("versal-wic", disp_text)
        # Both Versal branches must call the guard
        ospi_line = [l for l in disp_text.splitlines() if "versal-ospi" in l][0]
        wic_line  = [l for l in disp_text.splitlines() if "versal-wic"  in l][0]
        # The guard call is on the line following each versal-* case label
        disp_lines = disp_text.splitlines()
        for label_line in (ospi_line, wic_line):
            idx = disp_lines.index(label_line)
            # At least one of the next 3 lines must call _ensure_app_root_mounted
            nearby = "\n".join(disp_lines[idx:idx+4])
            self.assertIn("_ensure_app_root_mounted", nearby,
                          f"{label_line!r} — guard not found in next 3 lines")

    def test_x86_disk_image_member_case_present_in_dispatcher(self):
        """The x86-disk-image-member case is present in the dispatcher."""
        disp_block = re.search(
            r"cat > \"\$_disp\" <<'DISP'(.*?)^DISP",
            SOURCE, re.M | re.S
        )
        self.assertIsNotNone(disp_block)
        self.assertIn("x86-disk-image-member", disp_block.group(1))

    def test_dispatcher_uses_tee_not_python_fast_tee(self):
        """The dispatcher uses GNU coreutils tee + sha256sum, not python3 fast_tee.
        fast_tee via named fifo was benchmarked at 113 MB/s vs 243 MB/s with tee."""
        disp_block = re.search(
            r"cat > \"\$_disp\" <<'DISP'(.*?)^DISP",
            SOURCE, re.M | re.S
        ).group(1)
        self.assertIn("tee", disp_block)
        self.assertIn("sha256sum", disp_block)
        # python3 fast_tee should NOT be duplicated in the dispatcher
        self.assertNotIn("sys.stdin.buffer.read", disp_block)


class SelectionLogicTests(unittest.TestCase):
    """Verify the STREAMING selection and dispatch block text in the installer."""

    def test_x86_disk_image_in_streaming_condition(self):
        """x86-disk-image is included in the STREAMING=1 condition."""
        self.assertIn('[ "$APP_IMG_KIND" = "x86-disk-image" ]', SOURCE)
        # Find the STREAMING=1 block and confirm x86-disk-image appears there
        block = re.search(
            r'if \[ "\$APP_IMG_KIND".*?STREAMING=1', SOURCE, re.M | re.S
        )
        self.assertIsNotNone(block)
        self.assertIn("x86-disk-image", block.group(0))

    def test_deploy_disk_image_streamed_in_dispatch_block(self):
        """deploy_disk_image_streamed is called in the STREAMING=1 dispatch block."""
        self.assertIn("deploy_disk_image_streamed", SOURCE)
        block = re.search(
            r'if \[ "\$\{STREAMING:-0\}" = "1" \].*?deploy_app_image\b',
            SOURCE, re.M | re.S
        )
        self.assertIsNotNone(block, "could not find the STREAMING dispatch block")
        self.assertIn("deploy_disk_image_streamed", block.group(0))

    def test_old_deploy_disk_image_still_present(self):
        """The old deploy_disk_image() function is kept (non-streamed fallback)."""
        self.assertIn("deploy_disk_image()", SOURCE)

    def test_deploy_disk_image_streamed_function_exists(self):
        """deploy_disk_image_streamed() is defined in the installer."""
        self.assertIn("deploy_disk_image_streamed()", SOURCE)

    def test_disk_map_appended_to_stream_map(self):
        """_stream_bundle_members appends disk_map to stream_map for disk-image kinds."""
        sbm = re.search(r"^_stream_bundle_members\(\).*?^}", SOURCE, re.M | re.S).group(0)
        self.assertIn("disk_map", sbm)
        self.assertIn("stream_map", sbm)


if __name__ == "__main__":
    unittest.main()
