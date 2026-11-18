#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Constants for Versal PCIe payload programming."""

# Device identification
VENDOR_ID = 0x10ee
DEVICE_ID = 0x10ee
REVISION = 0x42

# BAR configuration
BAR_INDEX = 2
BAR_SIZE = 256 * 1024 * 1024  # 256MB

# Memory layout offsets
STATUS_OFFSET = 0x0
SIZE_OFFSET = 0x4
CHUNK_NUMBER_OFFSET = 0x08
TOTAL_CHUNKS_OFFSET = 0x0C
MD5_OFFSET_0 = 0x10
MD5_OFFSET_1 = 0x14
MD5_OFFSET_2 = 0x18
MD5_OFFSET_3 = 0x1C
TOTAL_SIZE_LO_OFFSET = 0x20
TOTAL_SIZE_HI_OFFSET = 0x24
TRANSFER_OFFSET_LO = 0x28
TRANSFER_OFFSET_HI = 0x2C
TRANSFER_FLAGS_OFFSET = 0x30
FAIL_REASON_OFFSET = 0x34
FAIL_DETAIL_OFFSET = 0x38
LOG_MAGIC_OFFSET = 0x3C
EXTENT_DIGEST_OFFSET = 0x40
EXTENT_DIGEST_SIZE = 32
# LUN that phase 3/4 block operations act on. 0 is the rootfs LUN, and is the
# default, so a target that predates LUN addressing behaves exactly as before.
LUN_INDEX_OFFSET = 0x60
PROVISION_FLAGS_OFFSET = 0x64
PROVISION_TOKEN_OFFSET = 0x68
EXEC_EXIT_OFFSET = 0x6C
PCIIMAGE_OFFSET = 0x1000
OSPI_OFFSET = 0x1000
LOG_OFFSET = 0x1000
PAYLOAD_OFFSET = 0x1000

TRANSFER_FLAG_FINAL = 0x1
# Set when EXTENT_DIGEST holds a SHA-256 of this extent, so the target can
# reject a bad transfer before it reaches the device. Old hosts leave it clear.
# SHA-256 rather than MD5 because both ends accelerate it in hardware.
TRANSFER_FLAG_DIGEST = 0x2

# Bad extents are resent rather than written, so a transport glitch cannot
# corrupt the device.
UFS_EXTENT_RETRIES = 3

# Written alongside the log so the host can tell a real log from stale payload.
LOG_MAGIC = 0x106DA7A

# Maximum chunk size for SDA transfers
SDA_CHUNK_MAX = 128 * 1024 * 1024  # 128MB

# Magic values - Phase 1: pciImage loading
HOST_READY = 0xFEEDB007
PAYLOAD_READY = 0xB007AB1E

# Magic values - Phase 2: OSPI programming
FLASH_READY = 0xF1A54000
FLASH_GO = 0x60F1A540
FLASH_STATUS_001 = 0xF1A5A001
FLASH_STATUS_002 = 0xF1A5A002
FLASH_STATUS_003 = 0xF1A5A003
FLASH_BAD = 0xF1A54BAD
FLASH_DEAD = 0xF1A54DED
FLASH_SUCCESS = 0xF1A5A999

# Magic values - Phase 3: SDA programming
SDA_GO_SIGNAL = 0x605DA000
SDA_CHUNK_ACK = 0x005DA001
SDA_BAD = 0x005DABAD
SDA_DEAD = 0x05DADEAD
SDA_SUCCESS = 0x005DA999

# Magic values - variable-offset UFS transfer
UFS_WRITE_GO = 0x605DA100
UFS_WRITE_ACK = 0x005DA101
UFS_WRITE_DATA_DONE = 0x005DA102
UFS_VERIFY_GO = 0x605DA102
UFS_WRITE_RETRY = 0x005DA103
UFS_WRITE_SUCCESS = 0x005DA199
UFS_READ_GO = 0x605DA200
UFS_READ_READY = 0x005DA201
UFS_READ_ACK = 0x605DA201
UFS_READ_DONE = 0x605DA2FF
UFS_READ_SUCCESS = 0x005DA299

# Magic values - zero a UFS span before a sparse write
UFS_ZERO_GO = 0x605DA300
UFS_ZERO_SUCCESS = 0x005DA399

# Magic values - run a diagnostic command on the target.
# Anyone who can write this BAR can already rewrite the boot flash and the
# entire rootfs, so this grants no privilege that was not already available.
EXEC_GO = 0x605DA600
EXEC_DONE = 0x005DA699
EXEC_TIMEOUT = 330.0

# Magic values - Phase 5: UFS LUN query and provisioning
UFS_LUNS_GO = 0x605DA400
UFS_LUNS_READY = 0x005DA401
UFS_PROV_GO = 0x605DA500
UFS_PROV_CHECKED = 0x005DA501
UFS_PROV_APPLIED = 0x005DA502
UFS_PROV_RESET_GO = 0x605DA501
UFS_PROV_SUCCESS = 0x005DA599
UFS_PROV_BAD = 0x005DA5BD

PROV_FLAG_COMMIT = 0x1
PROV_FLAG_ALLOW_WIPE = 0x2

UFS_LUNS_TIMEOUT = 120.0
UFS_PROV_TIMEOUT = 300.0

# Structured JSON responses sit well clear of the payload/log window at
# 0x1000, which the target overwrites with logs at the end of every cycle.
RESPONSE_OFFSET = 0x0C000000
RESPONSE_SIZE_OFFSET = 0x70
RESPONSE_MAX = 16 * 1024 * 1024

# Captured once per device, keyed by UFS serial: the layout a board was first
# seen with is what "restore default" puts back.
UFS_BASELINE_DIR = "/var/lib/versal/ufs-baseline"

UFS_MANIFEST_MAGIC = b"UFSC"
UFS_MANIFEST_VERSION = 1
UFS_MANIFEST_SHA256 = 1

# Reason codes published at FAIL_REASON_OFFSET alongside SDA_BAD / SDA_DEAD.
# FAIL_DETAIL_OFFSET carries the extent or range index where it is meaningful.
FAIL_NONE = 0
FAIL_DEVICE_MISSING = 1
FAIL_CAPACITY_READ = 2
FAIL_IMAGE_TOO_LARGE = 3
FAIL_DEVICE_OPEN = 4
FAIL_CHUNK_OUT_OF_RANGE = 5
FAIL_CHUNK_OUT_OF_ORDER = 6
FAIL_CHUNK_CHECKSUM = 7
FAIL_CHUNK_WRITE = 8
FAIL_EXTENT_OVERLAP = 9
FAIL_EXTENT_INVALID = 10
FAIL_EXTENT_WRITE = 11
FAIL_MANIFEST_TOO_LARGE = 12
FAIL_MANIFEST_PARSE = 13
FAIL_MANIFEST_COVERAGE = 14
FAIL_VERIFY_MISMATCH = 15
FAIL_VERIFY_READ = 16
FAIL_READ_INVALID = 17
FAIL_READ_IO = 18
FAIL_ZERO_INVALID = 19
FAIL_ZERO_IO = 20
FAIL_EXCEPTION = 21
FAIL_EXTENT_CHECKSUM = 22
FAIL_NO_BSG = 23
FAIL_GEOMETRY_READ = 24
FAIL_CONFIG_READ = 25
FAIL_CONFIG_LOCKED = 26
FAIL_PLAN_PARSE = 27
FAIL_PLAN_TOO_MANY_LUNS = 28
FAIL_PLAN_OVER_CAPACITY = 29
FAIL_PLAN_LUN_TOO_SMALL = 30
FAIL_PLAN_WOULD_LOCK = 31
FAIL_PLAN_NO_WIPE_CONSENT = 32
FAIL_PLAN_STALE_TOKEN = 33
FAIL_CONFIG_WRITE = 34
FAIL_VERIFY_LAYOUT = 35
FAIL_DEVICE_RESET = 36
FAIL_LUN_MISSING = 37
FAIL_EXEC_SPAWN = 38
FAIL_EXEC_TIMEOUT = 39
FAIL_PLAN_BAD_BOOT_LUN = 40

FAIL_REASONS = {
    FAIL_NONE: "no reason recorded",
    FAIL_DEVICE_MISSING: "/dev/sda not present",
    FAIL_CAPACITY_READ: "could not read /dev/sda capacity",
    FAIL_IMAGE_TOO_LARGE: "image larger than /dev/sda",
    FAIL_DEVICE_OPEN: "could not open /dev/sda",
    FAIL_CHUNK_OUT_OF_RANGE: "chunk number outside the declared total",
    FAIL_CHUNK_OUT_OF_ORDER: "chunk number did not advance",
    FAIL_CHUNK_CHECKSUM: "chunk MD5 mismatch in the BAR",
    FAIL_CHUNK_WRITE: "chunk write to /dev/sda failed",
    FAIL_EXTENT_OVERLAP: "extent overlapped the previous extent",
    FAIL_EXTENT_INVALID: "extent size or offset outside the device",
    FAIL_EXTENT_WRITE: "extent write to /dev/sda failed",
    FAIL_MANIFEST_TOO_LARGE: "checksum manifest exceeded the BAR payload",
    FAIL_MANIFEST_PARSE: "checksum manifest was malformed",
    FAIL_MANIFEST_COVERAGE: "manifest did not match the written extents",
    FAIL_VERIFY_MISMATCH: "SHA-256 mismatch reading back /dev/sda",
    FAIL_VERIFY_READ: "read error while verifying /dev/sda",
    FAIL_READ_INVALID: "readback offset or size outside the device",
    FAIL_READ_IO: "read error serving a readback request",
    FAIL_ZERO_INVALID: "zero span outside the device",
    FAIL_ZERO_IO: "error zeroing /dev/sda",
    FAIL_EXCEPTION: "unhandled exception on the target",
    FAIL_EXTENT_CHECKSUM: "extent arrived in the BAR corrupted, after retries",
    FAIL_NO_BSG: "no UFS BSG device; kernel lacks CONFIG_SCSI_UFS_BSG",
    FAIL_GEOMETRY_READ: "could not read the UFS geometry descriptor",
    FAIL_CONFIG_READ: "could not read the UFS configuration descriptor",
    FAIL_CONFIG_LOCKED: "bConfigDescrLock is set; this device can never be re-provisioned",
    FAIL_PLAN_PARSE: "provisioning plan was malformed",
    FAIL_PLAN_TOO_MANY_LUNS: "plan requests more LUNs than the device supports",
    FAIL_PLAN_OVER_CAPACITY: "plan exceeds the allocatable capacity",
    FAIL_PLAN_LUN_TOO_SMALL: "plan LUN size rounds down to zero allocation units",
    FAIL_PLAN_WOULD_LOCK: "plan would set bConfigDescrLock, which is always refused",
    FAIL_PLAN_NO_WIPE_CONSENT: "commit requested without explicit wipe consent",
    FAIL_PLAN_STALE_TOKEN: "plan token does not match the last layout report",
    FAIL_CONFIG_WRITE: "the device rejected the configuration descriptor write",
    FAIL_VERIFY_LAYOUT: "layout after reset does not match the plan",
    FAIL_DEVICE_RESET: "UFS device reset or re-enumeration failed",
    FAIL_LUN_MISSING: "the requested UFS LUN does not exist",
    FAIL_EXEC_SPAWN: "could not run the diagnostic command",
    FAIL_EXEC_TIMEOUT: "the diagnostic command timed out",
    FAIL_PLAN_BAD_BOOT_LUN: "plan assigns bBootLunID the device or spec does not allow",
}

STATUS_NAMES = {
    HOST_READY: "HOST_READY",
    PAYLOAD_READY: "PAYLOAD_READY",
    FLASH_READY: "FLASH_READY",
    FLASH_GO: "FLASH_GO",
    FLASH_STATUS_001: "FLASH_STATUS_001",
    FLASH_STATUS_002: "FLASH_STATUS_002",
    FLASH_STATUS_003: "FLASH_STATUS_003",
    FLASH_BAD: "FLASH_BAD",
    FLASH_DEAD: "FLASH_DEAD",
    FLASH_SUCCESS: "FLASH_SUCCESS",
    SDA_GO_SIGNAL: "SDA_GO",
    SDA_CHUNK_ACK: "SDA_CHUNK_ACK",
    SDA_BAD: "SDA_BAD",
    SDA_DEAD: "SDA_DEAD",
    SDA_SUCCESS: "SDA_SUCCESS",
    UFS_WRITE_GO: "UFS_WRITE_GO",
    UFS_WRITE_ACK: "UFS_WRITE_ACK",
    UFS_WRITE_DATA_DONE: "UFS_WRITE_DATA_DONE",
    UFS_VERIFY_GO: "UFS_VERIFY_GO",
    UFS_WRITE_RETRY: "UFS_WRITE_RETRY",
    UFS_WRITE_SUCCESS: "UFS_WRITE_SUCCESS",
    UFS_READ_GO: "UFS_READ_GO",
    UFS_READ_READY: "UFS_READ_READY",
    UFS_READ_DONE: "UFS_READ_DONE",
    UFS_READ_SUCCESS: "UFS_READ_SUCCESS",
    UFS_ZERO_GO: "UFS_ZERO_GO",
    UFS_ZERO_SUCCESS: "UFS_ZERO_SUCCESS",
    EXEC_GO: "EXEC_GO",
    EXEC_DONE: "EXEC_DONE",
    UFS_LUNS_GO: "UFS_LUNS_GO",
    UFS_LUNS_READY: "UFS_LUNS_READY",
    UFS_PROV_GO: "UFS_PROV_GO",
    UFS_PROV_CHECKED: "UFS_PROV_CHECKED",
    UFS_PROV_APPLIED: "UFS_PROV_APPLIED",
    UFS_PROV_RESET_GO: "UFS_PROV_RESET_GO",
    UFS_PROV_SUCCESS: "UFS_PROV_SUCCESS",
    UFS_PROV_BAD: "UFS_PROV_BAD",
}

# Timeouts
READY_TIMEOUT = 30.0
FLASH_READY_TIMEOUT = 120.0
STATUS_POLL_INTERVAL = 0.5
PROGRAMMING_TIMEOUT = 500.0
SDA_CHUNK_TIMEOUT = 300.0
# Discard/zero of a whole image span, which may fall back to buffered writes.
UFS_ZERO_TIMEOUT = 1800.0
# A full-image read-back and SHA-256 pass is far slower than a single extent.
UFS_VERIFY_TIMEOUT = 1800.0

# Inputs
DEFAULT_PCIIMAGE_FILE = "/lib/firmware/versal/pcieImage"
