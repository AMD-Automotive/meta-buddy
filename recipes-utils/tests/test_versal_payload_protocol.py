# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

import tempfile
import unittest
import gzip
import hashlib
import struct
from pathlib import Path
from unittest import mock

import sys

FILES_DIR = Path(__file__).resolve().parents[1] / "files"
sys.path.insert(0, str(FILES_DIR))

import versal_payload_protocol as protocol


class Memory(bytearray):
    def flush(self):
        pass


class BmapChunkIndexesTests(unittest.TestCase):
    def write_bmap(self, directory, image_size, ranges, namespace="", checksums=None):
        prefix = f' xmlns="{namespace}"' if namespace else ""
        if checksums is None:
            checksums = ["ab" * 32] * len(ranges)
        range_xml = "".join(
            f'<Range chksum="{checksum}">{value}</Range>'
            for value, checksum in zip(ranges, checksums)
        )
        mapped_count = 0
        for value in ranges:
            bounds = value.split("-", 1)
            mapped_count += int(bounds[-1]) - int(bounds[0]) + 1
        blocks_count = (image_size + 4095) // 4096
        path = Path(directory) / "image.bmap"
        xml = (
            f'<bmap version="2.0"{prefix}>'
            f"<ImageSize>{image_size}</ImageSize>"
            "<BlockSize>4096</BlockSize>"
            f"<BlocksCount>{blocks_count}</BlocksCount>"
            f"<MappedBlocksCount>{mapped_count}</MappedBlocksCount>"
            "<ChecksumType>sha256</ChecksumType>"
            f"<BmapFileChecksum>{'0' * 64}</BmapFileChecksum>"
            f"<BlockMap>{range_xml}</BlockMap>"
            "</bmap>"
        )
        checksum = hashlib.sha256(xml.encode()).hexdigest()
        path.write_text(xml.replace("0" * 64, checksum, 1), encoding="utf-8")
        return path

    def test_maps_namespaced_ranges_to_absolute_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_bmap(
                directory,
                512 * 1024 * 1024,
                ["0-1", "65536-65537", "98304"],
                "http://www.example.com/bmap/2.0",
            )
            self.assertEqual(
                protocol.bmap_chunk_indexes(path, 512 * 1024 * 1024),
                [0, 2, 3],
            )

    def test_rejects_image_size_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_bmap(directory, 4096, ["0"])
            with self.assertRaisesRegex(ValueError, "does not match"):
                protocol.bmap_chunk_indexes(path, 8192)

    def test_rejects_corrupt_bmap_self_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_bmap(directory, 4096, ["0"])
            path.write_text(path.read_text().replace("</bmap>", "\n</bmap>"))
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                protocol.load_bmap_v2(path)

    def test_rejects_inconsistent_counts_and_unordered_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_bmap(directory, 12288, ["1", "0"])
            with self.assertRaisesRegex(ValueError, "unordered"):
                protocol.load_bmap_v2(path)

    def test_rejects_unsupported_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_bmap(directory, 4096, ["0"])
            raw = path.read_text()
            raw = raw.replace('version="2.0"', 'version="3.0"')
            path.write_text(raw)
            with self.assertRaisesRegex(ValueError, "unsupported bmap version"):
                protocol.load_bmap_v2(path)

    def test_coalesces_adjacent_ranges_and_splits_large_extents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_bmap(directory, 32768, ["0-1", "2-5", "7"])
            with mock.patch.object(protocol, "SDA_CHUNK_MAX", 8192):
                size, extents = protocol.parse_bmap_extents(path)
            self.assertEqual(size, 32768)
            self.assertEqual(extents, [(0, 8192), (8192, 8192), (16384, 8192), (28672, 4096)])

    def test_checksum_plan_reuses_whole_range_and_marks_clipped_range(self):
        with tempfile.TemporaryDirectory() as directory:
            digest = "ab" * 32
            path = self.write_bmap(directory, 16384, ["0-1"], checksums=[digest])
            _size, whole = protocol.parse_bmap_checksum_ranges(path)
            _size, clipped = protocol.parse_bmap_checksum_ranges(path, [(4096, 4096)])
            self.assertEqual(whole, [(0, 8192, bytes.fromhex(digest))])
            self.assertEqual(clipped, [(4096, 4096, None)])

    def test_partial_transfer_manifest_hashes_selected_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.wic"
            image.write_bytes(b"AAAABBBBCCCCDDDD")
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            bmap = self.write_bmap(directory, 16, ["0"], checksums=[digest])
            memory = Memory(4096)
            writes = []
            original_write_u32 = protocol.write_u32

            def capture_write(mem, offset, value):
                original_write_u32(mem, offset, value)
                if offset == protocol.STATUS_OFFSET:
                    writes.append(value)

            with mock.patch.object(protocol, "SDA_CHUNK_MAX", 4), \
                    mock.patch.object(protocol, "PAYLOAD_OFFSET", 256), \
                    mock.patch.object(protocol, "write_u32", side_effect=capture_write), \
                    mock.patch.object(protocol, "wait_for_any_signal", side_effect=[
                        protocol.UFS_WRITE_DATA_DONE, protocol.UFS_WRITE_SUCCESS,
                    ]):
                self.assertTrue(
                    protocol.send_ufs_extents(memory, image, bmap, [(4, 4)])
                )

            manifest_size = protocol.read_u32(memory, protocol.SIZE_OFFSET)
            manifest = bytes(memory[256:256 + manifest_size])
            magic, version, algorithm, count = struct.unpack_from("<4sHHI", manifest)
            offset, size, selected_digest = struct.unpack_from("<QQ32s", manifest, 12)
            self.assertEqual(
                (magic, version, algorithm, count),
                (protocol.UFS_MANIFEST_MAGIC, 1, 1, 1),
            )
            self.assertEqual((offset, size), (4, 4))
            self.assertEqual(selected_digest, hashlib.sha256(b"BBBB").digest())
            self.assertEqual(writes, [protocol.UFS_WRITE_GO, protocol.UFS_VERIFY_GO])

    def test_gzip_stream_is_read_as_uncompressed_wic(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.wic.gz"
            with gzip.open(image, "wb") as stream:
                stream.write(b"AAAABBBB")
            with protocol.open_wic_stream(image) as stream:
                self.assertEqual(protocol.read_exact(stream, 4), b"AAAA")
                protocol.discard_exact(stream, 4)
                self.assertEqual(stream.read(1), b"")

    def test_readback_requests_multiple_extents(self):
        memory = Memory(4096)
        responses = [b"ABCD", b"EFGH"]
        signals = []

        def respond(*_args, **_kwargs):
            if not responses:
                return protocol.UFS_READ_SUCCESS
            data = responses.pop(0)
            memory[256:256 + len(data)] = data
            for offset, word in zip(
                [protocol.MD5_OFFSET_0, protocol.MD5_OFFSET_1,
                 protocol.MD5_OFFSET_2, protocol.MD5_OFFSET_3],
                protocol.calculate_md5(data),
            ):
                memory[offset:offset + 4] = struct.pack("<I", word)
            return protocol.UFS_READ_READY

        original_write_u32 = protocol.write_u32

        def capture_write(mem, offset, value):
            original_write_u32(mem, offset, value)
            if offset == protocol.STATUS_OFFSET:
                signals.append(value)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "ufs.bin"
            with mock.patch.object(protocol, "SDA_CHUNK_MAX", 4), \
                    mock.patch.object(protocol, "PAYLOAD_OFFSET", 256), \
                    mock.patch.object(protocol, "write_u32", side_effect=capture_write), \
                    mock.patch.object(protocol, "wait_for_any_signal", side_effect=respond):
                self.assertTrue(protocol.read_ufs(memory, 8, 8, output))
            self.assertEqual(output.read_bytes(), b"ABCDEFGH")
            self.assertEqual(signals, [protocol.UFS_READ_GO, protocol.UFS_READ_ACK, protocol.UFS_READ_DONE])


if __name__ == "__main__":
    unittest.main()
