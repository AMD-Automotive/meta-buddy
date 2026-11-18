#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""PCIe device discovery and BAR access helpers."""

import mmap
import os
import struct
from pathlib import Path

from versal_payload_constants import DEVICE_ID, REVISION, VENDOR_ID


class PCIeDevice:
    """Represents a PCIe device."""

    def __init__(self, bus, dev, func, vendor_id, device_id, revision):
        self.bus = bus
        self.dev = dev
        self.func = func
        self.vendor_id = vendor_id
        self.device_id = device_id
        self.revision = revision
        self.bdf = f"{bus:02x}:{dev:02x}.{func}"

    def __str__(self):
        return (
            f"{self.bdf} - VID: 0x{self.vendor_id:04x}, "
            f"DID: 0x{self.device_id:04x}, Rev: 0x{self.revision:02x}"
        )


def find_pcie_device():
    """Find the target PCIe device by VID/DID/Revision."""
    print("=" * 70)
    print("Searching for PCIe device...")
    print(f"  Vendor ID: 0x{VENDOR_ID:04x}")
    print(f"  Device ID: 0x{DEVICE_ID:04x}")
    print(f"  Revision:  0x{REVISION:02x}")
    print("=" * 70)

    sysfs_pci = Path("/sys/bus/pci/devices")

    if not sysfs_pci.exists():
        print("ERROR: /sys/bus/pci/devices not found")
        return None

    for device_path in sysfs_pci.iterdir():
        try:
            vendor_file = device_path / "vendor"
            if not vendor_file.exists():
                continue
            vendor_id = int(vendor_file.read_text().strip(), 16)

            device_file = device_path / "device"
            if not device_file.exists():
                continue
            device_id = int(device_file.read_text().strip(), 16)

            revision_file = device_path / "revision"
            if not revision_file.exists():
                continue
            revision = int(revision_file.read_text().strip(), 16)

            if (
                vendor_id == VENDOR_ID
                and device_id == DEVICE_ID
                and revision == REVISION
            ):
                bdf = device_path.name
                parts = bdf.split(":")
                if len(parts) == 3:
                    bus = int(parts[1], 16)
                    dev_func = parts[2].split(".")
                    dev = int(dev_func[0], 16)
                    func = int(dev_func[1], 16)

                    device = PCIeDevice(bus, dev, func, vendor_id, device_id, revision)
                    print(f"Found device: {device}")
                    return device

        except (ValueError, FileNotFoundError):
            continue

    print("ERROR: Device not found")
    return None


def enable_bar(device, bar_index):
    """Enable BAR and return its resource path and size."""
    print("")
    print("=" * 70)
    print(f"Enabling BAR{bar_index}...")
    print("=" * 70)

    resource_file = f"/sys/bus/pci/devices/0000:{device.bdf}/resource{bar_index}"

    if not os.path.exists(resource_file):
        print(f"ERROR: {resource_file} not found")
        return None, None

    with open(f"/sys/bus/pci/devices/0000:{device.bdf}/resource", "r", encoding="ascii") as f:
        lines = f.readlines()
        if bar_index >= len(lines):
            print(f"ERROR: BAR{bar_index} not found in resource file")
            return None, None

        parts = lines[bar_index].split()
        if len(parts) < 2:
            print("ERROR: Invalid resource file format")
            return None, None

        bar_start = int(parts[0], 16)
        bar_end = int(parts[1], 16)
        bar_size = bar_end - bar_start + 1 if bar_end > bar_start else 0

    if bar_start == 0 or bar_size == 0:
        print(f"ERROR: BAR{bar_index} not assigned")
        return None, None

    print(f"BAR{bar_index} address: 0x{bar_start:08x}")
    print(f"BAR{bar_index} size: {bar_size / (1024 * 1024):.0f} MB")

    try:
        config_file = f"/sys/bus/pci/devices/0000:{device.bdf}/config"
        with open(config_file, "r+b") as f:
            f.seek(0x04)
            cmd = struct.unpack("<H", f.read(2))[0]
            print(f"Current PCI command: 0x{cmd:04x}")

            cmd |= 0x0006

            f.seek(0x04)
            f.write(struct.pack("<H", cmd))
            f.flush()

            f.seek(0x04)
            new_cmd = struct.unpack("<H", f.read(2))[0]
            print(f"Updated PCI command: 0x{new_cmd:04x}")
    except Exception as e:
        print(f"WARNING: Could not enable device: {e}")

    return resource_file, bar_size


def map_bar(resource_path):
    """Memory map the BAR region."""
    print("")
    print("Mapping BAR to process memory...")

    try:
        with open(resource_path, "r+b") as f:
            mem = mmap.mmap(f.fileno(), 0, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
            print("BAR mapped successfully")
            return mem
    except Exception as e:
        print(f"ERROR: Failed to map BAR: {e}")
        return None


def read_u32(mem, offset):
    """Read 32-bit value from BAR offset."""
    return struct.unpack("<I", mem[offset : offset + 4])[0]


def write_u32(mem, offset, value):
    """Write 32-bit value to BAR offset."""
    mem[offset : offset + 4] = struct.pack("<I", value)
