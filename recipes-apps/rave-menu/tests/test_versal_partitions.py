# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
import zlib
from pathlib import Path

FILES_DIR = Path(__file__).resolve().parents[1] / "files"
sys.path.insert(0, str(FILES_DIR))

import versal_partitions as vp

LINUX_FS = "0fc63daf-8483-4772-8e79-3d69d8477de4"
MS_DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
DISK_GUID = "11111111-2222-3333-4444-555555555555"


def build_gpt(parts, lba=4096, device_lbas=4096, entry_count=128, entry_size=128):
    """Build a GPT image head with valid header and entry-array CRCs."""
    entries = bytearray(entry_count * entry_size)
    for i, (name, type_guid, part_uuid, first, last) in enumerate(parts):
        off = i * entry_size
        entries[off:off + 16] = uuid.UUID(type_guid).bytes_le
        entries[off + 16:off + 32] = uuid.UUID(part_uuid).bytes_le
        struct.pack_into("<QQQ", entries, off + 32, first, last, 0)
        encoded = name.encode("utf-16-le")
        entries[off + 56:off + 56 + len(encoded)] = encoded

    header = bytearray(92)
    header[0:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<Q", header, 24, 1)
    struct.pack_into("<Q", header, 32, device_lbas - 1)
    struct.pack_into("<QQ", header, 40, 6, device_lbas - 2)
    header[56:72] = uuid.UUID(DISK_GUID).bytes_le
    struct.pack_into("<QIII", header, 72, 2, entry_count, entry_size,
                     zlib.crc32(bytes(entries)))
    struct.pack_into("<I", header, 16, zlib.crc32(bytes(header)))

    end = 2 * lba + len(entries)
    image = bytearray(max(end, 4 * lba))
    image[lba:lba + len(header)] = header
    image[2 * lba:2 * lba + len(entries)] = entries
    return bytes(image)


def three_partition_image(data_last=900, lba=4096, device_lbas=4096, data_uuid=None):
    return build_gpt([
        ("boot-v1", MS_DATA, "aaaaaaaa-0000-0000-0000-000000000001", 100, 199),
        ("root-v1", LINUX_FS, "aaaaaaaa-0000-0000-0000-000000000002", 200, 799),
        ("data-v1", LINUX_FS, data_uuid or "aaaaaaaa-0000-0000-0000-000000000003", 800, data_last),
    ], lba=lba, device_lbas=device_lbas)


class GptParsingTests(unittest.TestCase):
    def test_parses_4096_byte_lba_layout(self):
        gpt = vp.parse_gpt(three_partition_image())
        self.assertEqual(gpt["lba_size"], 4096)
        self.assertEqual(gpt["device_size"], 4096 * 4096)
        self.assertEqual([p["name"] for p in gpt["partitions"]],
                         ["boot-v1", "root-v1", "data-v1"])
        self.assertEqual(gpt["partitions"][0]["start"], 100 * 4096)
        self.assertEqual(gpt["partitions"][0]["size"], 100 * 4096)
        self.assertEqual(gpt["partitions"][0]["type_name"], "Microsoft basic data")

    def test_parses_512_byte_lba_layout(self):
        gpt = vp.parse_gpt(three_partition_image(lba=512))
        self.assertEqual(gpt["lba_size"], 512)
        self.assertEqual(gpt["partitions"][1]["start"], 200 * 512)

    def test_rejects_missing_signature(self):
        with self.assertRaisesRegex(vp.LayoutError, "no GPT header"):
            vp.parse_gpt(b"\x00" * 65536)

    def test_rejects_corrupt_header(self):
        raw = bytearray(three_partition_image())
        raw[4096 + 40] ^= 0xFF
        with self.assertRaisesRegex(vp.LayoutError, "header CRC"):
            vp.parse_gpt(bytes(raw))

    def test_rejects_corrupt_entries(self):
        raw = bytearray(three_partition_image())
        raw[2 * 4096 + 60] ^= 0xFF
        with self.assertRaisesRegex(vp.LayoutError, "entry CRC"):
            vp.parse_gpt(bytes(raw))

    def test_rejects_truncated_entry_array(self):
        raw = three_partition_image()[:2 * 4096 + 64]
        with self.assertRaisesRegex(vp.LayoutError, "extend past"):
            vp.parse_gpt(raw)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.image = vp.parse_gpt(three_partition_image(data_last=900))
        self.target = vp.parse_gpt(
            three_partition_image(data_last=4000, device_lbas=8192))

    def test_grown_partition_is_blocked(self):
        plan = vp.build_plan(self.image, self.target, has_bmap=True)
        rows = {row["index"]: row for row in plan["rows"]}
        self.assertTrue(rows[1]["selectable"])
        self.assertTrue(rows[2]["selectable"])
        self.assertFalse(rows[3]["selectable"])
        self.assertIn("grown", rows[3]["reason"])
        self.assertTrue(plan["partial_supported"])

    def test_partuuid_change_warns_but_stays_selectable(self):
        target = vp.parse_gpt(three_partition_image(
            data_last=900, data_uuid="bbbbbbbb-0000-0000-0000-000000000009"))
        plan = vp.build_plan(self.image, target, has_bmap=True)
        row = plan["rows"][2]
        self.assertTrue(row["selectable"])
        self.assertIn("PARTUUID", row["warning"])

    def test_partial_requires_a_bmap(self):
        plan = vp.build_plan(self.image, self.target, has_bmap=False)
        self.assertFalse(plan["partial_supported"])
        self.assertIn("bmap", plan["blocker"])

    def test_name_mismatch_blocks_selection(self):
        other = vp.parse_gpt(build_gpt([
            ("boot-v2", MS_DATA, "aaaaaaaa-0000-0000-0000-000000000001", 100, 199),
            ("root-v1", LINUX_FS, "aaaaaaaa-0000-0000-0000-000000000002", 200, 799),
            ("data-v1", LINUX_FS, "aaaaaaaa-0000-0000-0000-000000000003", 800, 900),
        ]))
        plan = vp.build_plan(self.image, other, has_bmap=True)
        self.assertFalse(plan["rows"][0]["selectable"])
        self.assertIn("Name differs", plan["rows"][0]["reason"])

    def test_ranges_use_image_sizes_and_reject_blocked_rows(self):
        plan = vp.build_plan(self.image, self.target, has_bmap=True)
        self.assertEqual(vp.ranges_for(plan["rows"], {1, 2}),
                         f"{100 * 4096}:{100 * 4096},{200 * 4096}:{600 * 4096}")
        with self.assertRaises(vp.LayoutError):
            vp.ranges_for(plan["rows"], {3})
        with self.assertRaises(vp.LayoutError):
            vp.ranges_for(plan["rows"], set())


class ImageHeadTests(unittest.TestCase):
    def test_reads_head_from_plain_gzip_and_zstd(self):
        payload = three_partition_image()
        want = len(payload)
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "image.wic"
            raw.write_bytes(payload)
            subprocess.run(["gzip", "-k", "-f", str(raw)], check=True)
            subprocess.run(["zstd", "-q", "-f", str(raw), "-o", f"{raw}.zst"], check=True)

            for name in ("image.wic", "image.wic.gz", "image.wic.zst"):
                with self.subTest(name=name):
                    head = vp.read_image_head(Path(directory) / name, want)
                    self.assertEqual(head, payload)
                    self.assertEqual(vp.parse_gpt(head)["partitions"][0]["name"], "boot-v1")

    def test_missing_local_image_is_reported(self):
        with self.assertRaisesRegex(vp.LayoutError, "not found"):
            vp.read_image_head("/nonexistent/image.wic")


if __name__ == "__main__":
    unittest.main()
