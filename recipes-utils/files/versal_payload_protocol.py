#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Protocol helpers for payload transfer and status handling."""

import hashlib
import gzip
import json
import math
import os
import re
import struct
import subprocess
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager

from versal_payload_constants import *  # noqa: F403
from versal_payload_pcie import *  # noqa: F403


def describe_status(value):
    """Render a status word as name and hex, never as a bare decimal."""
    if value is None:
        return "no response (timeout)"
    return f"{STATUS_NAMES.get(value, 'UNKNOWN')} (0x{value:08x})"


def describe_failure(mem):
    """Read the target's failure reason, if it recorded one."""
    reason = read_u32(mem, FAIL_REASON_OFFSET)
    detail = read_u32(mem, FAIL_DETAIL_OFFSET)
    if reason == FAIL_NONE:
        return None
    text = FAIL_REASONS.get(reason, f"unrecognised reason {reason}")
    if detail:
        return f"{text} (reason {reason}, detail {detail})"
    return f"{text} (reason {reason})"


def report_failure(mem, context, status):
    """Print the target's status and reason for a failed operation."""
    print(f"ERROR: {context} failed with status {describe_status(status)}")
    reason = describe_failure(mem)
    if reason:
        print(f"       target reported: {reason}")
    else:
        print("       target did not record a reason; see the device log below")


def calculate_md5(data):
    """Calculate MD5 hash and return as 4 x 32-bit words."""
    md5_hash = hashlib.md5(data).digest()
    words = struct.unpack("<4I", md5_hash)
    return words


def wait_for_signal(mem, offset, expected, timeout, name):
    """Poll offset waiting for expected signal."""
    print("")
    print("=" * 70)
    print(f"Waiting for {name} signal...")
    print(f"Expecting: 0x{expected:08x} at offset 0x{offset:x}")
    print("=" * 70)

    start_time = time.time()
    last_value = None

    while True:
        value = read_u32(mem, offset)

        if value != last_value:
            elapsed = time.time() - start_time
            print(f"[{elapsed:6.1f}s] Status: 0x{value:08x}")
            last_value = value

        if value == expected:
            elapsed = time.time() - start_time
            print(f"{name} signal received! (after {elapsed:.1f}s)")
            return True

        elapsed = time.time() - start_time
        if elapsed > timeout:
            print(f"ERROR: Timeout waiting for {name} signal")
            print(f"Last value: 0x{value:08x}")
            return False

        time.sleep(STATUS_POLL_INTERVAL)


def wait_for_any_signal(mem, offset, expected_set, timeout, name, verbose=True):
    """Poll offset waiting for any expected signal.

    Returns matched signal value, or None on timeout.
    """
    if verbose:
        print("")
        print("=" * 70)
        print(f"Waiting for {name}...")
        signal_strs = ", ".join(f"0x{s:08x}" for s in expected_set)
        print(f"Expecting one of: {signal_strs} at offset 0x{offset:x}")
        print("=" * 70)

    start_time = time.time()
    last_value = None

    while True:
        value = read_u32(mem, offset)

        if verbose and value != last_value:
            elapsed = time.time() - start_time
            print(f"[{elapsed:6.1f}s] Status: 0x{value:08x}")
            last_value = value

        if value in expected_set:
            if verbose:
                elapsed = time.time() - start_time
                print(f"{name} signal 0x{value:08x} received! (after {elapsed:.1f}s)")
            return value

        elapsed = time.time() - start_time
        if elapsed > timeout:
            print(f"ERROR: Timeout waiting for {name}")
            print(f"Last value: 0x{value:08x}")
            return None

        time.sleep(STATUS_POLL_INTERVAL)

def load_and_verify_file(mem, offset, filepath, bar_size, name):
    """Load file to BAR, verify, and return size and MD5."""
    print("")
    print("=" * 70)
    print(f"Loading {name}...")
    print("=" * 70)

    if not os.path.exists(filepath):
        print(f"ERROR: File not found: {filepath}")
        return None, None

    file_size = os.path.getsize(filepath)
    available = bar_size - offset

    print(f"File: {filepath}")
    print(f"Size: {file_size} bytes ({file_size / (1024 * 1024):.2f} MB)")
    print(f"Target offset: 0x{offset:x}")
    print(f"Available space: {available / (1024 * 1024):.2f} MB")

    if file_size > available:
        print("ERROR: File too large")
        return None, None

    with open(filepath, "rb") as f:
        data = f.read()

    md5_words = calculate_md5(data)
    md5_bytes = struct.pack("<4I", *md5_words)
    print(f"MD5: {md5_bytes.hex()}")

    print("Writing...")
    start_time = time.time()

    chunk_size = 1024 * 1024
    written = 0

    while written < file_size:
        chunk = min(chunk_size, file_size - written)
        mem[offset + written : offset + written + chunk] = data[written : written + chunk]
        written += chunk

        if written % (10 * 1024 * 1024) == 0 or written == file_size:
            elapsed = time.time() - start_time
            speed = (written / (1024 * 1024)) / elapsed if elapsed > 0 else 0
            print(
                f"  Written: {written / (1024 * 1024):.2f} / "
                f"{file_size / (1024 * 1024):.2f} MB ({speed:.1f} MB/s)"
            )

    mem.flush()

    elapsed = time.time() - start_time
    speed = (file_size / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    print(f"Written in {elapsed:.2f}s ({speed:.1f} MB/s)")

    print("Verifying...")
    read_back = bytes(mem[offset : offset + file_size])

    if read_back == data:
        print("Verification: PASSED")
        return file_size, md5_words

    print("Verification: FAILED")
    for i in range(min(len(read_back), len(data))):
        if read_back[i] != data[i]:
            print(f"First mismatch at offset 0x{i:x}")
            print(f"  Expected: 0x{data[i]:02x}")
            print(f"  Got:      0x{read_back[i]:02x}")
            break
    return None, None


def monitor_programming_status(mem):
    """Monitor OSPI programming status until completion or error."""
    print("")
    print("=" * 70)
    print("Monitoring OSPI Programming Status...")
    print("=" * 70)

    start_time = time.time()
    last_status = None

    status_descriptions = {
        FLASH_STATUS_001: "Validation passed",
        FLASH_STATUS_002: "Erasing flash",
        FLASH_STATUS_003: "Erase complete - programming flash",
        FLASH_BAD: "VALIDATION FAILED",
        FLASH_DEAD: "PROGRAMMING FAILED",
        FLASH_SUCCESS: "SUCCESS",
    }

    while True:
        status = read_u32(mem, STATUS_OFFSET)

        if status != last_status:
            elapsed = time.time() - start_time
            desc = status_descriptions.get(status, "Unknown status")
            print(f"[{elapsed:6.1f}s] Status: 0x{status:08x} - {desc}")
            last_status = status

        if status == FLASH_SUCCESS:
            elapsed = time.time() - start_time
            print(f"\nProgramming completed successfully! (Total time: {elapsed:.1f}s)")
            return True

        if status == FLASH_BAD:
            print("\nERROR: OSPI image validation failed")
            return False

        if status == FLASH_DEAD:
            print("\nERROR: Flash programming failed")
            return False

        elapsed = time.time() - start_time
        if elapsed > PROGRAMMING_TIMEOUT:
            print(f"\nERROR: Programming timeout after {elapsed:.1f}s")
            return False

        time.sleep(STATUS_POLL_INTERVAL)


def collect_logs(mem):
    """Retrieve and display logs from device using SIZE_OFFSET for length."""
    print("")
    print("=" * 70)
    print("Collecting Device Logs...")
    print("=" * 70)

    # The log region doubles as the payload window, so only trust it once the
    # target has stamped the magic that says it just wrote a log there.
    if read_u32(mem, LOG_MAGIC_OFFSET) != LOG_MAGIC:
        print("No device log available: the target did not publish one for this")
        print("operation (it may still be running, or it never reached the end of")
        print("a cycle). The BAR payload region holds transfer data, not a log.")
        return ""

    log_size = read_u32(mem, SIZE_OFFSET)
    if log_size == 0 or log_size > (BAR_SIZE - LOG_OFFSET):
        log_size = 4 * 1024

    log_data = bytes(mem[LOG_OFFSET : LOG_OFFSET + log_size])

    log_end = log_size
    for i, byte in enumerate(log_data):
        if byte == 0:
            log_end = i
            break

    log_text = log_data[:log_end].decode("ascii", errors="replace")

    if log_text.strip():
        print("Device logs:")
        print("-" * 70)
        print(log_text)
        print("-" * 70)
    else:
        print("No logs available")

    return log_text


def _xml_name(element):
    return element.tag.rsplit("}", 1)[-1]


def _only_child(parent, name, require_text=True):
    matches = [element for element in parent if _xml_name(element) == name]
    if len(matches) != 1 or (require_text and not matches[0].text):
        raise ValueError(f"bmap must contain exactly one {name}")
    return matches[0]


def load_bmap_v2(bmap_file):
    """Load and fully validate a Yocto/bmaptool format 2.0 block map."""
    raw = Path(bmap_file).read_bytes()
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError(f"bmap file is too large: {len(raw)} bytes")
    root = ET.fromstring(raw)
    if _xml_name(root) != "bmap" or root.get("version") != "2.0":
        raise ValueError(f"unsupported bmap version: {root.get('version')!r}")

    def integer(name):
        text = _only_child(root, name).text.strip()
        if not re.fullmatch(r"[0-9]+", text):
            raise ValueError(f"invalid bmap {name}: {text!r}")
        return int(text)

    image_size = integer("ImageSize")
    block_size = integer("BlockSize")
    blocks_count = integer("BlocksCount")
    mapped_count = integer("MappedBlocksCount")
    if image_size <= 0 or block_size <= 0:
        raise ValueError("bmap ImageSize and BlockSize must be positive")
    expected_blocks = (image_size + block_size - 1) // block_size
    if blocks_count != expected_blocks:
        raise ValueError(
            f"bmap BlocksCount {blocks_count} does not match image size {image_size}"
        )

    checksum_type = _only_child(root, "ChecksumType").text.strip().lower()
    if checksum_type != "sha256":
        raise ValueError(f"unsupported bmap checksum type: {checksum_type}")
    file_checksum = _only_child(root, "BmapFileChecksum").text.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", file_checksum):
        raise ValueError("invalid BmapFileChecksum")
    checksum_bytes = file_checksum.encode()
    if raw.count(checksum_bytes) != 1:
        raise ValueError("BmapFileChecksum value is not unique in bmap")
    calculated = hashlib.sha256(raw.replace(checksum_bytes, b"0" * 64, 1)).hexdigest()
    if calculated != file_checksum:
        raise ValueError(
            f"bmap file checksum mismatch: calculated {calculated}, expected {file_checksum}"
        )

    block_map = _only_child(root, "BlockMap", require_text=False)
    if len(block_map) > (BAR_SIZE - PAYLOAD_OFFSET - 12) // 48:
        raise ValueError("bmap has too many ranges for the checksum manifest")
    ranges = []
    previous_last = -1
    actual_mapped_count = 0
    for element in block_map:
        if _xml_name(element) != "Range" or not element.text:
            raise ValueError("bmap BlockMap contains an invalid element")
        match = re.fullmatch(r"\s*([0-9]+)(?:\s*-\s*([0-9]+))?\s*", element.text)
        if not match:
            raise ValueError(f"invalid bmap range: {(element.text or '').strip()}")
        first = int(match.group(1))
        last = int(match.group(2) or match.group(1))
        if first > last or first <= previous_last or last >= blocks_count:
            raise ValueError(f"invalid or unordered bmap range: {element.text.strip()}")
        digest_text = element.get("chksum", "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest_text):
            raise ValueError(f"invalid SHA-256 for bmap range {element.text.strip()}")
        ranges.append((first, last, bytes.fromhex(digest_text)))
        actual_mapped_count += last - first + 1
        previous_last = last
    if not ranges:
        raise ValueError("bmap contains no mapped ranges")
    if actual_mapped_count != mapped_count:
        raise ValueError(
            f"bmap MappedBlocksCount {mapped_count} does not match ranges {actual_mapped_count}"
        )
    return image_size, block_size, ranges


def parse_bmap(bmap_file, image_size=None):
    """Return the image size and absolute SDA chunks from a validated bmap."""
    bmap_image_size, block_size, ranges = load_bmap_v2(bmap_file)
    if image_size is not None and bmap_image_size != image_size:
        raise ValueError(
            f"bmap image size {bmap_image_size} does not match WIC size {image_size}"
        )
    indexes = set()
    for first_block, last_block, _digest in ranges:
        first_byte = first_block * block_size
        last_byte = min((last_block + 1) * block_size, bmap_image_size) - 1
        indexes.update(range(first_byte // SDA_CHUNK_MAX, last_byte // SDA_CHUNK_MAX + 1))
    return bmap_image_size, sorted(indexes)


def parse_bmap_extents(bmap_file):
    """Return image size and coalesced mapped byte extents capped at 128 MiB."""
    image_size, block_size, ranges = load_bmap_v2(bmap_file)
    mapped = []
    for first_block, last_block, _digest in ranges:
        first = first_block * block_size
        end = min((last_block + 1) * block_size, image_size)
        if mapped and first <= mapped[-1][1]:
            mapped[-1] = (mapped[-1][0], max(mapped[-1][1], end))
        else:
            mapped.append((first, end))

    extents = []
    for first, end in mapped:
        offset = first
        while offset < end:
            size = min(SDA_CHUNK_MAX, end - offset)
            extents.append((offset, size))
            offset += size
    if not extents:
        raise ValueError("bmap contains no mapped ranges")
    return image_size, extents


def merge_extents(extents):
    """Normalize offset/size pairs into sorted non-overlapping byte ranges."""
    merged = []
    for offset, size in sorted(extents):
        if offset < 0 or size <= 0:
            raise ValueError(f"invalid extent offset={offset} size={size}")
        end = offset + size
        if merged and offset <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((offset, end))
    return merged


def parse_bmap_checksum_ranges(bmap_file, selections=None):
    """Return selected bmap ranges as offset, size, and optional SHA-256."""
    image_size, block_size, ranges = load_bmap_v2(bmap_file)
    if selections is None:
        selections = [(0, image_size)]
    selections = merge_extents(selections)
    selections = [(start, end - start) for start, end in selections]

    records = []
    previous_end = 0
    for first_block, last_block, range_digest in ranges:
        range_start = first_block * block_size
        range_end = min((last_block + 1) * block_size, image_size)
        for selection_start, selection_size in selections:
            selection_end = selection_start + selection_size
            start = max(range_start, selection_start)
            end = min(range_end, selection_end)
            if start >= end:
                continue
            if start < previous_end:
                raise ValueError("selected bmap ranges overlap or are out of order")
            digest = range_digest if (start, end) == (range_start, range_end) else None
            records.append((start, end - start, digest))
            previous_end = end
    if not records:
        raise ValueError("selection contains no mapped bmap ranges")
    return image_size, records


def build_ufs_manifest(records):
    """Serialize offset, length, SHA-256 records for target verification."""
    header = struct.pack(
        "<4sHHI", UFS_MANIFEST_MAGIC, UFS_MANIFEST_VERSION,
        UFS_MANIFEST_SHA256, len(records),
    )
    return header + b"".join(
        struct.pack("<QQ32s", offset, size, digest)
        for offset, size, digest in records
    )


def bmap_chunk_indexes(bmap_file, image_size):
    """Return absolute SDA chunk indexes touched by a standard bmap file."""
    return parse_bmap(bmap_file, image_size)[1]


@contextmanager
def open_wic_stream(wic_file):
    """Open a plain, gzip, or zstd WIC as an uncompressed binary stream."""
    path = os.fspath(wic_file)
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as stream:
            yield stream
        return
    if path.endswith(".zst"):
        process = subprocess.Popen(
            ["zstd", "-dc", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            try:
                yield process.stdout
            except BaseException:
                process.kill()
                process.wait()
                raise
            process.stdout.close()
            stderr = process.stderr.read().decode(errors="replace")
            if process.wait() != 0:
                raise OSError(f"zstd decompression failed: {stderr.strip()}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stderr.close()
        return
    with open(path, "rb") as stream:
        yield stream


def read_exact(stream, size):
    """Read exactly size bytes unless the stream ends early."""
    data = bytearray()
    while len(data) < size:
        block = stream.read(size - len(data))
        if not block:
            raise OSError(f"WIC stream ended {size - len(data)} bytes early")
        data.extend(block)
    return bytes(data)


def discard_exact(stream, size):
    """Consume exactly size bytes without retaining an unmapped chunk."""
    remaining = size
    while remaining:
        block = stream.read(min(1024 * 1024, remaining))
        if not block:
            raise OSError(f"WIC stream ended {remaining} bytes early")
        remaining -= len(block)


def write_u64(mem, low_offset, high_offset, value):
    write_u32(mem, low_offset, value & 0xFFFFFFFF)
    write_u32(mem, high_offset, (value >> 32) & 0xFFFFFFFF)


def select_lun(mem, lun):
    """Point the target's block operations at a UFS LUN.

    Written before every phase 3/4 command rather than once per session,
    because the target reads it at the start of each handler and a stale value
    would silently send a write to the wrong LUN.
    """
    write_u32(mem, LUN_INDEX_OFFSET, lun)
    mem.flush()


def exec_command(mem, command, timeout=EXEC_TIMEOUT):
    """Run a diagnostic command on the target and print its output.

    The recovery payload has no network and no guaranteed console, so without
    this the only way to inspect it is to rebuild and re-push the whole image.
    The target returns the output through the normal log channel, so there is
    nothing to read back here beyond the exit code.
    """
    payload = command.encode("utf-8")
    if len(payload) > 64 * 1024:
        print("ERROR: command is too long")
        return None

    print("")
    print("=" * 70)
    print(f"Target exec: {command}")
    print("=" * 70)

    mem[PAYLOAD_OFFSET:PAYLOAD_OFFSET + len(payload)] = payload
    write_u32(mem, SIZE_OFFSET, len(payload))
    mem.flush()
    write_u32(mem, STATUS_OFFSET, EXEC_GO)
    mem.flush()

    result = wait_for_any_signal(
        mem, STATUS_OFFSET, {EXEC_DONE, SDA_BAD, SDA_DEAD},
        timeout, "target exec", verbose=False,
    )
    if result != EXEC_DONE:
        report_failure(mem, "target exec", result)
        collect_logs(mem)
        return None

    exit_code = read_u32(mem, EXEC_EXIT_OFFSET)
    collect_logs(mem)
    print(f"Target exec finished with exit code {exit_code}")
    return exit_code


def read_response(mem, name):
    """Read and verify a JSON response from the target's response region."""
    size = read_u32(mem, RESPONSE_SIZE_OFFSET)
    if size == 0 or size > RESPONSE_MAX:
        print(f"ERROR: {name} response has an implausible length {size}")
        return None
    data = bytes(mem[RESPONSE_OFFSET:RESPONSE_OFFSET + size])
    expected = tuple(read_u32(mem, offset) for offset in (
        MD5_OFFSET_0, MD5_OFFSET_1, MD5_OFFSET_2, MD5_OFFSET_3
    ))
    if calculate_md5(data) != expected:
        print(f"ERROR: {name} response failed its MD5 check")
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        print(f"ERROR: {name} response is not valid JSON: {error}")
        return None


def query_ufs_luns(mem):
    """Ask the target to describe its current LUN layout. Read-only."""
    write_u32(mem, STATUS_OFFSET, UFS_LUNS_GO)
    mem.flush()
    result = wait_for_any_signal(
        mem, STATUS_OFFSET, {UFS_LUNS_READY, UFS_PROV_BAD, SDA_BAD, SDA_DEAD},
        UFS_LUNS_TIMEOUT, "UFS LUN query", verbose=False,
    )
    if result != UFS_LUNS_READY:
        report_failure(mem, "UFS LUN query", result)
        collect_logs(mem)
        return None
    return read_response(mem, "UFS LUN query")


def check_ufs_plan(mem, plan, commit=False, allow_wipe=False):
    """Send a provisioning plan for validation, and optionally for commit.

    With commit clear this is entirely non-destructive: the target validates
    the plan against the live device and reports the layout it would produce
    without writing anything.
    """
    payload = json.dumps(plan, separators=(",", ":")).encode("utf-8")
    if len(payload) > RESPONSE_MAX:
        print("ERROR: provisioning plan is too large")
        return None

    flags = (PROV_FLAG_COMMIT if commit else 0) | (PROV_FLAG_ALLOW_WIPE if allow_wipe else 0)
    mem[PAYLOAD_OFFSET:PAYLOAD_OFFSET + len(payload)] = payload
    write_u32(mem, SIZE_OFFSET, len(payload))
    write_u32(mem, PROVISION_FLAGS_OFFSET, flags)
    mem.flush()
    write_u32(mem, STATUS_OFFSET, UFS_PROV_GO)
    mem.flush()

    expected = {UFS_PROV_CHECKED, UFS_PROV_APPLIED, UFS_PROV_BAD, SDA_BAD, SDA_DEAD}
    result = wait_for_any_signal(
        mem, STATUS_OFFSET, expected, UFS_PROV_TIMEOUT, "UFS provisioning", verbose=False,
    )
    if result not in (UFS_PROV_CHECKED, UFS_PROV_APPLIED):
        report_failure(mem, "UFS provisioning", result)
        collect_logs(mem)
        return None

    if result == UFS_PROV_CHECKED:
        return read_response(mem, "UFS provisioning")

    # The descriptor is written but a new layout only becomes live after a
    # device reset, so drive that and make the target prove the result.
    print("Configuration descriptor written; resetting the UFS device...")
    write_u32(mem, STATUS_OFFSET, UFS_PROV_RESET_GO)
    mem.flush()
    result = wait_for_any_signal(
        mem, STATUS_OFFSET, {UFS_PROV_SUCCESS, UFS_PROV_BAD, SDA_BAD, SDA_DEAD},
        UFS_PROV_TIMEOUT, "UFS reset and verify", verbose=False,
    )
    if result != UFS_PROV_SUCCESS:
        report_failure(mem, "UFS reset and verify", result)
        collect_logs(mem)
        return None
    response = read_response(mem, "UFS provisioning")
    collect_logs(mem)
    return response


def zero_ufs(mem, offset, length, lun=0):
    """Ask the target to zero a UFS span before a sparse write.

    bmap programming only writes mapped ranges, so blocks the image leaves as
    holes keep the previous image's content. Those holes include in-use ext4
    metadata, so without this the destination must already be zeroed.
    """
    print("")
    print("=" * 70)
    print(f"Zeroing UFS span on LUN {lun}: offset {offset}, {length / (1024 ** 3):.2f} GiB")
    print("=" * 70)

    select_lun(mem, lun)
    write_u64(mem, TRANSFER_OFFSET_LO, TRANSFER_OFFSET_HI, offset)
    write_u64(mem, TOTAL_SIZE_LO_OFFSET, TOTAL_SIZE_HI_OFFSET, length)
    mem.flush()
    write_u32(mem, STATUS_OFFSET, UFS_ZERO_GO)
    mem.flush()

    start = time.time()
    result = wait_for_any_signal(
        mem, STATUS_OFFSET, {UFS_ZERO_SUCCESS, SDA_BAD, SDA_DEAD},
        UFS_ZERO_TIMEOUT, "UFS zeroing", verbose=False,
    )
    if result != UFS_ZERO_SUCCESS:
        report_failure(mem, "UFS zeroing", result)
        collect_logs(mem)
        return False
    print(f"UFS span zeroed in {time.time() - start:.1f}s")
    return True


def send_ufs_extents(mem, wic_file, bmap_file, selections=None, zero_first=False, lun=0):
    """Program selected bmap ranges and verify them from UFS using SHA-256."""
    try:
        image_size, checksum_ranges = parse_bmap_checksum_ranges(bmap_file, selections)
    except (ET.ParseError, OSError, ValueError) as error:
        print(f"ERROR: Invalid bmap file {bmap_file}: {error}")
        return False

    if not str(wic_file).endswith((".gz", ".zst")):
        actual_size = os.path.getsize(wic_file)
        if actual_size != image_size:
            print(f"ERROR: bmap image size {image_size} does not match WIC size {actual_size}")
            return False

    if zero_first:
        # Zero only what this run will cover, so a partition-scoped update
        # cannot erase partitions it was not asked to touch.
        if selections:
            spans = merge_extents(selections)
        else:
            spans = [(0, image_size)]
        for start, end in spans:
            if not zero_ufs(mem, start, end - start, lun):
                return False

    select_lun(mem, lun)
    transport_extents = []
    for range_index, (offset, size, digest) in enumerate(checksum_ranges):
        sent = 0
        while sent < size:
            chunk_size = min(SDA_CHUNK_MAX, size - sent)
            # An extent covering a whole bmap range already has a SHA-256 in the
            # bmap, so most extents cost the host no hashing at all.
            whole_range = sent == 0 and chunk_size == size
            transport_extents.append(
                (offset + sent, chunk_size, range_index,
                 digest if (whole_range and digest is not None) else None)
            )
            sent += chunk_size

    total_bytes = sum(size for _, size, _, _ in transport_extents)
    reused = sum(1 for _, _, _, digest in transport_extents if digest is not None)
    print(
        f"Mapped ranges: {len(checksum_ranges)}, transport extents: "
        f"{len(transport_extents)}, {total_bytes / (1024 ** 3):.2f} GiB"
    )
    print(f"Per-extent SHA-256: {reused} reused from the bmap, "
          f"{len(transport_extents) - reused} calculated")
    stream_offset = 0
    transferred = 0
    retransmits = 0
    calculated_hashes = {
        index: hashlib.sha256()
        for index, (_offset, _size, digest) in enumerate(checksum_ranges)
        if digest is None
    }
    try:
        with open_wic_stream(wic_file) as stream:
            for index, (offset, size, range_index, known) in enumerate(transport_extents):
                discard_exact(stream, offset - stream_offset)
                data = read_exact(stream, size)
                stream_offset = offset + size
                if range_index in calculated_hashes:
                    calculated_hashes[range_index].update(data)

                digest = known if known is not None else hashlib.sha256(data).digest()
                final = index == len(transport_extents) - 1
                flags = TRANSFER_FLAG_DIGEST | (TRANSFER_FLAG_FINAL if final else 0)

                # A corrupted extent is resent rather than written, so the
                # device never holds data that failed its digest.
                for attempt in range(UFS_EXTENT_RETRIES + 1):
                    mem[PAYLOAD_OFFSET:PAYLOAD_OFFSET + size] = data
                    write_u32(mem, SIZE_OFFSET, size)
                    write_u64(mem, TRANSFER_OFFSET_LO, TRANSFER_OFFSET_HI, offset)
                    mem[EXTENT_DIGEST_OFFSET:EXTENT_DIGEST_OFFSET + EXTENT_DIGEST_SIZE] = digest
                    write_u32(mem, TRANSFER_FLAGS_OFFSET, flags)
                    mem.flush()
                    write_u32(mem, STATUS_OFFSET, UFS_WRITE_GO)
                    mem.flush()

                    expected = {UFS_WRITE_DATA_DONE} if final else {UFS_WRITE_ACK}
                    expected |= {UFS_WRITE_RETRY, SDA_BAD, SDA_DEAD}
                    result = wait_for_any_signal(
                        mem, STATUS_OFFSET, expected, SDA_CHUNK_TIMEOUT,
                        f"UFS extent {index + 1}/{len(transport_extents)}", verbose=False,
                    )
                    if result != UFS_WRITE_RETRY:
                        break
                    retransmits += 1
                    print(f"  Extent {index + 1} failed its checksum on the target; "
                          f"resending (attempt {attempt + 2})")

                if result not in {UFS_WRITE_ACK, UFS_WRITE_DATA_DONE}:
                    report_failure(mem, f"UFS extent {index + 1} at offset {offset}", result)
                    return False
                transferred += size
                print(
                    f"  Extent {index + 1}/{len(transport_extents)}: offset {offset}, "
                    f"{size / (1024 * 1024):.1f} MiB"
                )
            discard_exact(stream, image_size - stream_offset)
            if stream.read(1):
                raise OSError("WIC stream is larger than the bmap image size")
    except OSError as error:
        print(f"ERROR: Failed to read WIC image: {error}")
        return False

    manifest_records = []
    for index, (offset, size, digest) in enumerate(checksum_ranges):
        manifest_records.append(
            (offset, size, digest if digest is not None else calculated_hashes[index].digest())
        )
    manifest = build_ufs_manifest(manifest_records)
    mem[PAYLOAD_OFFSET:PAYLOAD_OFFSET + len(manifest)] = manifest
    write_u32(mem, SIZE_OFFSET, len(manifest))
    mem.flush()
    write_u32(mem, STATUS_OFFSET, UFS_VERIFY_GO)
    mem.flush()
    result = wait_for_any_signal(
        mem, STATUS_OFFSET, {UFS_WRITE_SUCCESS, SDA_BAD, SDA_DEAD},
        UFS_VERIFY_TIMEOUT, "UFS checksum verification", verbose=False,
    )
    if result != UFS_WRITE_SUCCESS:
        report_failure(mem, "UFS checksum verification", result)
        return False

    print(
        f"UFS sparse transfer complete: {transferred / (1024 ** 3):.2f} GiB, "
        f"{len(manifest_records)} SHA-256 ranges verified"
    )
    if retransmits:
        print(f"WARNING: {retransmits} extent(s) had to be resent after arriving "
              f"corrupted in the BAR; the transport is not clean")
    return True


def read_ufs(mem, offset, length, output_file, lun=0):
    """Read a UFS byte range from the target into a local file."""
    if offset < 0 or length <= 0:
        raise ValueError("UFS read offset must be non-negative and length positive")
    select_lun(mem, lun)
    remaining = length
    current = offset
    with open(output_file, "wb") as output:
        while remaining:
            size = min(SDA_CHUNK_MAX, remaining)
            write_u64(mem, TRANSFER_OFFSET_LO, TRANSFER_OFFSET_HI, current)
            write_u32(mem, SIZE_OFFSET, size)
            write_u32(mem, STATUS_OFFSET, UFS_READ_GO if current == offset else UFS_READ_ACK)
            mem.flush()
            result = wait_for_any_signal(
                mem, STATUS_OFFSET, {UFS_READ_READY, SDA_BAD, SDA_DEAD},
                SDA_CHUNK_TIMEOUT, "UFS read", verbose=False,
            )
            if result != UFS_READ_READY:
                report_failure(mem, f"UFS read at offset {current}", result)
                return False
            data = bytes(mem[PAYLOAD_OFFSET:PAYLOAD_OFFSET + size])
            expected = tuple(read_u32(mem, md5_offset) for md5_offset in (
                MD5_OFFSET_0, MD5_OFFSET_1, MD5_OFFSET_2, MD5_OFFSET_3
            ))
            if calculate_md5(data) != expected:
                print(f"ERROR: UFS read MD5 mismatch at offset {current}")
                return False
            output.write(data)
            current += size
            remaining -= size
        write_u32(mem, STATUS_OFFSET, UFS_READ_DONE)
        mem.flush()
        result = wait_for_any_signal(
            mem, STATUS_OFFSET, {UFS_READ_SUCCESS, SDA_BAD, SDA_DEAD},
            SDA_CHUNK_TIMEOUT, "UFS read completion", verbose=False,
        )
    return result == UFS_READ_SUCCESS


def send_sda_chunks(mem, bar_size, wic_file, bmap_file=None, selections=None,
                    zero_first=False, lun=0):
    """Send a WIC file to the target, optionally skipping unmapped bmap chunks."""
    print("\n" + "=" * 70)
    print("PHASE: SDA PROGRAMMING (Chunked WIC Transfer)")
    print("=" * 70)

    if not os.path.exists(wic_file):
        print(f"ERROR: WIC file not found: {wic_file}")
        return False

    if bmap_file:
        return send_ufs_extents(mem, wic_file, bmap_file, selections, zero_first, lun)

    select_lun(mem, lun)
    file_size = os.path.getsize(wic_file)
    total_chunks = math.ceil(file_size / SDA_CHUNK_MAX)
    chunk_indexes = list(range(total_chunks))
    if total_chunks == 0:
        print("ERROR: WIC file is empty")
        return False

    print(f"WIC file: {wic_file}")
    if bmap_file:
        print(f"Bmap file: {bmap_file}")
    print(f"Size: {file_size} bytes ({file_size / (1024 * 1024 * 1024):.2f} GB)")
    print(f"Chunk size: {SDA_CHUNK_MAX / (1024 * 1024):.0f} MB")
    print(f"Chunks to transfer: {len(chunk_indexes)}/{total_chunks}")

    overall_start = time.time()
    last_percent_logged = -1
    transferred = 0

    selected_chunks = set(chunk_indexes)
    transfer_idx = 0
    try:
        with open_wic_stream(wic_file) as stream:
            for chunk_idx in range(total_chunks):
                source_size = min(SDA_CHUNK_MAX, file_size - chunk_idx * SDA_CHUNK_MAX)
                if chunk_idx not in selected_chunks:
                    discard_exact(stream, source_size)
                    continue
                chunk_data = read_exact(stream, source_size)
                chunk_size = source_size

                write_start = time.time()
                written = 0
                copy_block = 1024 * 1024
                while written < chunk_size:
                    block = min(copy_block, chunk_size - written)
                    mem[PAYLOAD_OFFSET + written : PAYLOAD_OFFSET + written + block] = chunk_data[
                        written : written + block
                    ]
                    written += block
                mem.flush()
                write_elapsed = time.time() - write_start

                chunk_md5 = calculate_md5(chunk_data)

                write_u32(mem, SIZE_OFFSET, chunk_size)
                write_u32(mem, CHUNK_NUMBER_OFFSET, chunk_idx)
                write_u32(mem, TOTAL_CHUNKS_OFFSET, total_chunks)
                write_u32(mem, MD5_OFFSET_0, chunk_md5[0])
                write_u32(mem, MD5_OFFSET_1, chunk_md5[1])
                write_u32(mem, MD5_OFFSET_2, chunk_md5[2])
                write_u32(mem, MD5_OFFSET_3, chunk_md5[3])

                if transfer_idx == 0:
                    write_u32(mem, TOTAL_SIZE_LO_OFFSET, file_size & 0xFFFFFFFF)
                    write_u32(mem, TOTAL_SIZE_HI_OFFSET, (file_size >> 32) & 0xFFFFFFFF)

                mem.flush()

                write_u32(mem, STATUS_OFFSET, SDA_GO_SIGNAL)
                mem.flush()

                transferred += chunk_size
                percent = int(((transfer_idx + 1) / len(chunk_indexes)) * 100)
                nearest_5 = (percent // 5) * 5
                if nearest_5 > last_percent_logged or transfer_idx in (0, len(chunk_indexes) - 1):
                    elapsed = time.time() - overall_start
                    speed = transferred / (1024 * 1024) / elapsed if elapsed > 0 else 0
                    print(
                        f"  Chunk {chunk_idx + 1}/{total_chunks} sent "
                        f"({chunk_size / (1024 * 1024):.1f} MB, "
                        f"BAR write {write_elapsed:.1f}s) - "
                        f"{percent}% of selected chunks complete "
                        f"({speed:.1f} MB/s avg)"
                    )
                    last_percent_logged = nearest_5

                if chunk_idx < total_chunks - 1:
                    result = wait_for_any_signal(
                        mem,
                        STATUS_OFFSET,
                        {SDA_CHUNK_ACK, SDA_BAD, SDA_DEAD},
                        SDA_CHUNK_TIMEOUT,
                        f"SDA chunk {chunk_idx + 1} ACK",
                        verbose=False,
                    )

                    if result is None:
                        print(f"ERROR: Timeout waiting for chunk {chunk_idx + 1} ACK")
                        return False
                    if result in (SDA_BAD, SDA_DEAD):
                        report_failure(mem, f"SDA chunk {chunk_idx + 1}", result)
                        return False
                else:
                    result = wait_for_any_signal(
                        mem,
                        STATUS_OFFSET,
                        {SDA_SUCCESS, SDA_BAD, SDA_DEAD},
                        SDA_CHUNK_TIMEOUT,
                        "SDA final status",
                        verbose=False,
                    )

                    if result is None:
                        print("ERROR: Timeout waiting for SDA final status")
                        return False
                    if result in (SDA_BAD, SDA_DEAD):
                        report_failure(mem, "SDA final chunk", result)
                        return False
                transfer_idx += 1
            if stream.read(1):
                raise OSError("WIC stream is larger than the bmap image size")
    except OSError as error:
        print(f"ERROR: Failed to read WIC image: {error}")
        return False

    overall_elapsed = time.time() - overall_start
    avg_speed = transferred / (1024 * 1024) / overall_elapsed if overall_elapsed > 0 else 0
    print(
        f"\nSDA transfer complete: {transferred / (1024 * 1024 * 1024):.2f} GB transferred "
        f"in {overall_elapsed:.1f}s ({avg_speed:.1f} MB/s avg)"
    )
    return True
