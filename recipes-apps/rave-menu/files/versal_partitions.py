# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Partition-table inspection for Versal UFS flash programming.

Parses GPTs read back from the target over PCIe and from the head of a WIC
image, and decides which partitions may be written on their own. The UFS
device uses 4096-byte LBAs, so the LBA size is detected rather than assumed.
"""

import os
import struct
import subprocess
import tempfile
import urllib.request
import uuid
import zlib
from urllib.parse import urlsplit

GPT_SIGNATURE = b"EFI PART"
LBA_CANDIDATES = (512, 4096)
HEAD_BYTES = 128 * 1024
MAX_ENTRIES = 1024

# Compressed prefix sizes tried in turn until enough of the image decompresses.
PROBE_STEPS = (1 << 20, 4 << 20, 16 << 20, 64 << 20)

TYPE_NAMES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI system",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Microsoft basic data",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot",
}


class LayoutError(Exception):
    """A partition table or image head could not be read or trusted."""


def _guid(raw):
    return str(uuid.UUID(bytes_le=raw))


def parse_gpt(data):
    """Parse a GPT from a device or image head, detecting the LBA size."""
    for lba_size in LBA_CANDIDATES:
        if data[lba_size:lba_size + len(GPT_SIGNATURE)] == GPT_SIGNATURE:
            return _parse_gpt_at(data, lba_size)
    raise LayoutError("no GPT header found at a 512 or 4096 byte LBA")


def _parse_gpt_at(data, lba_size):
    base = lba_size
    header_size = struct.unpack_from("<I", data, base + 12)[0]
    if not 92 <= header_size <= lba_size:
        raise LayoutError(f"implausible GPT header size {header_size}")

    header = bytearray(data[base:base + header_size])
    if len(header) != header_size:
        raise LayoutError("truncated GPT header")
    stored_crc = struct.unpack_from("<I", header, 16)[0]
    header[16:20] = b"\x00\x00\x00\x00"
    if zlib.crc32(bytes(header)) != stored_crc:
        raise LayoutError("GPT header CRC mismatch")

    alternate_lba = struct.unpack_from("<Q", data, base + 32)[0]
    first_usable, last_usable = struct.unpack_from("<QQ", data, base + 40)
    disk_guid = _guid(data[base + 56:base + 72])
    entry_lba, entry_count, entry_size, entries_crc = struct.unpack_from(
        "<QIII", data, base + 72)

    if entry_size < 128 or entry_size % 8 or not 0 < entry_count <= MAX_ENTRIES:
        raise LayoutError(
            f"implausible GPT entry array: {entry_count} x {entry_size} bytes")

    start = entry_lba * lba_size
    end = start + entry_count * entry_size
    if end > len(data):
        raise LayoutError("GPT entries extend past the data that was read")
    entries = data[start:end]
    if zlib.crc32(entries) != entries_crc:
        raise LayoutError("GPT partition entry CRC mismatch")

    partitions = []
    for index in range(entry_count):
        raw = entries[index * entry_size:(index + 1) * entry_size]
        if raw[:16] == b"\x00" * 16:
            continue
        first_lba, last_lba = struct.unpack_from("<QQ", raw, 32)
        if last_lba < first_lba:
            raise LayoutError(f"partition {index + 1} ends before it starts")
        type_guid = _guid(raw[:16])
        partitions.append({
            "index": index + 1,
            "name": raw[56:128].decode("utf-16-le", "replace").split("\x00")[0],
            "type_guid": type_guid,
            "type_name": TYPE_NAMES.get(type_guid, type_guid),
            "part_uuid": _guid(raw[16:32]),
            "start": first_lba * lba_size,
            "size": (last_lba - first_lba + 1) * lba_size,
        })

    return {
        "lba_size": lba_size,
        "disk_guid": disk_guid,
        "device_size": (alternate_lba + 1) * lba_size,
        "first_usable": first_usable * lba_size,
        "last_usable": (last_usable + 1) * lba_size,
        "partitions": partitions,
    }


def _compression_suffix(name):
    for suffix in (".zst", ".gz"):
        if name.endswith(suffix):
            return suffix
    return ""


def _decompress_prefix(path, want):
    suffix = _compression_suffix(str(path))
    if not suffix:
        with open(path, "rb") as handle:
            return handle.read(want)

    cmd = ["zstd", "-dc", str(path)] if suffix == ".zst" else ["gzip", "-dc", str(path)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        return proc.stdout.read(want)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def _fetch_prefix(url, nbytes, dest):
    """Save up to nbytes of a URL, tolerating servers that ignore Range."""
    request = urllib.request.Request(url, headers={
        "Range": f"bytes=0-{nbytes - 1}",
        "User-Agent": "RAVE2-Layout/1",
    })
    written = 0
    with urllib.request.urlopen(request, timeout=30) as response, open(dest, "wb") as out:
        while written < nbytes:
            chunk = response.read(min(1 << 20, nbytes - written))
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)
    return written


def read_image_head(source, want=HEAD_BYTES):
    """Return the first `want` uncompressed bytes of a WIC image."""
    source = str(source)
    if not source.startswith(("http://", "https://")):
        if not os.path.exists(source):
            raise LayoutError(f"image not found: {source}")
        head = _decompress_prefix(source, want)
    else:
        head = _fetch_url_head(source, want)

    if len(head) < want:
        raise LayoutError("image is too small to contain a partition table")
    return head


def _fetch_url_head(url, want):
    suffix = _compression_suffix(urlsplit(url).path)
    with tempfile.TemporaryDirectory() as directory:
        probe = os.path.join(directory, "head" + suffix)
        for nbytes in PROBE_STEPS:
            fetched = _fetch_prefix(url, nbytes, probe)
            head = _decompress_prefix(probe, want)
            if len(head) >= want or fetched < nbytes:
                return head
    raise LayoutError("could not read enough of the image to find a partition table")


def build_plan(image, target, has_bmap):
    """Pair image and target partitions and decide what may be written alone."""
    image_parts = image["partitions"]
    target_parts = target["partitions"]
    rows = []

    for position in range(max(len(image_parts), len(target_parts))):
        img = image_parts[position] if position < len(image_parts) else None
        tgt = target_parts[position] if position < len(target_parts) else None
        selectable, reason, warning = _classify(img, tgt)
        rows.append({
            "index": (img or tgt)["index"],
            "name": (img or tgt)["name"],
            "type_name": (img or tgt)["type_name"],
            "start": (img or tgt)["start"],
            "image_size": img["size"] if img else None,
            "target_size": tgt["size"] if tgt else None,
            "selectable": selectable,
            "reason": reason,
            "warning": warning,
        })

    selectable_rows = [row for row in rows if row["selectable"]]
    if not has_bmap:
        blocker = "Supply a bmap to enable partial updates."
    elif not selectable_rows:
        blocker = "No partition on the target matches the image closely enough to write on its own."
    else:
        blocker = None

    return {
        "rows": rows,
        "partial_supported": blocker is None,
        "blocker": blocker,
    }


def _classify(img, tgt):
    if img is None:
        return False, "Present on the target but not in the image.", None
    if tgt is None:
        return False, "Present in the image but not on the target.", None
    if img["name"] != tgt["name"]:
        return False, f"Name differs: image {img['name']!r}, target {tgt['name']!r}.", None
    if img["type_guid"] != tgt["type_guid"]:
        return False, "Partition type differs between image and target.", None
    if img["start"] != tgt["start"]:
        return False, "Start offset differs between image and target.", None
    if tgt["size"] > img["size"]:
        return False, "Target partition has been grown; only a full image write can rewrite it.", None
    if tgt["size"] < img["size"]:
        return False, "Target partition is smaller than the image partition.", None

    warning = None
    if img["part_uuid"] != tgt["part_uuid"]:
        warning = "PARTUUID differs; the target was flashed from a different build."
    return True, None, warning


def ranges_for(rows, selected_indexes):
    """Build the offset:size range spec for a partial update."""
    spec = []
    for row in rows:
        if row["index"] in selected_indexes:
            if not row["selectable"]:
                raise LayoutError(f"partition {row['index']} cannot be written on its own")
            spec.append(f"{row['start']}:{row['image_size']}")
    if not spec:
        raise LayoutError("no partitions selected")
    return ",".join(spec)
