#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""
x86_replace_application.py — Deploy (or replace) an application rootfs image
onto the NVMe app-a partition.

The ext4 image is expected to be self-contained: rootfs + kernel at /boot/bzImage
(as produced by Yocto's core-image-minimal or similar).

Usage:
    sudo python3 x86_replace_application.py <slot> <ext4_image> [--sha256 SUM]

    slot        : "a" (nvme0n1p4, label app-a)
    ext4_image  : Path to the .rootfs.ext4 file, or "-" to stream the image
                  from standard input (no temporary copy is made)
    --sha256    : Optional expected sha256 of the image; verified while writing

Example:
    sudo python3 x86_replace_application.py a /tmp/core-image-minimal-genericx86-64.rootfs.ext4
    gzip -dc bundle | tar -xO x86/rootfs.ext4 | sudo x86-replace-application a - --sha256 <sum>

The script performs:
    1. Validates arguments and checks that the partition exists
    2. Unmounts the target partition if currently mounted
    3. Writes the ext4 image directly to the partition via dd
    4. Expands the filesystem to fill the partition (resize2fs)
    5. Restores the filesystem label (app-a)
    6. Mounts the partition and verifies the kernel is present
    7. Patches /etc/fstab inside the deployed rootfs
    8. Unmounts and syncs
"""

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile

PARTITION_MAP = {
    "a": {"device": "/dev/nvme0n1p4", "label": "app-a"},
}


def run(cmd, check=True, capture=False):
    """Run a shell command, printing it for visibility."""
    print(f"  + {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
    )


def set_ext4_label(device, label):
    """Set ext4 filesystem label.

    Prefers e2label (from e2fsprogs) which handles metadata checksums
    properly.  Falls back to patching every superblock copy and running
    e2fsck to recalculate checksums.
    """
    import shutil

    if shutil.which("e2label"):
        run(["e2label", device, label])
        return

    import struct

    # --- Fallback: patch all superblock copies, then e2fsck ---
    SUPERBLOCK_OFFSET = 1024
    LABEL_OFFSET = 0x78          # 16-byte field inside the superblock
    encoded = label.encode("utf-8")[:16].ljust(16, b"\x00")

    with open(device, "r+b") as f:
        # Read geometry from the primary superblock
        f.seek(SUPERBLOCK_OFFSET)
        sb = f.read(1024)
        s_blocks_count     = struct.unpack_from("<I", sb, 0x04)[0]
        s_first_data_block = struct.unpack_from("<I", sb, 0x14)[0]
        s_log_block_size   = struct.unpack_from("<I", sb, 0x18)[0]
        s_blocks_per_group = struct.unpack_from("<I", sb, 0x20)[0]
        s_feature_ro_compat = struct.unpack_from("<I", sb, 0x64)[0]

        block_size = 1024 << s_log_block_size
        total_groups = (s_blocks_count + s_blocks_per_group - 1) // s_blocks_per_group
        sparse = bool(s_feature_ro_compat & 0x0001)  # SPARSE_SUPER

        # Build list of block groups that carry a superblock copy
        groups = {0, 1}
        if sparse:
            for base in (3, 5, 7):
                p = base
                while p < total_groups:
                    groups.add(p)
                    p *= base
        else:
            groups = set(range(total_groups))
        groups = sorted(g for g in groups if g < total_groups)

        print(f"  (e2label not found \u2014 patching label in {len(groups)} superblock copies)")

        for g in groups:
            if g == 0:
                offset = SUPERBLOCK_OFFSET + LABEL_OFFSET
            else:
                first_block = s_first_data_block + g * s_blocks_per_group
                offset = first_block * block_size + LABEL_OFFSET
            f.seek(offset)
            f.write(encoded)

        f.flush()
        os.fsync(f.fileno())

    # e2fsck recalculates metadata checksums for the updated superblocks
    if shutil.which("e2fsck"):
        print("  + running e2fsck to repair superblock checksums")
        run(["e2fsck", "-fy", device], check=False)

    print(f"  + label set to \'{label}\'")


def is_mounted(device):
    """Check if a device is currently mounted."""
    result = run(["findmnt", "-rn", "-S", device], check=False, capture=True)
    return bool(result.stdout.strip())


def unmount(device):
    """Unmount a device if mounted."""
    if is_mounted(device):
        print(f"Unmounting {device} ...")
        run(["umount", device])


def stream_stdin_to_device(device, expected_sha=None):
    """Write the ext4 image from standard input straight to the device.

    Computes the sha256 while writing so no temporary copy is needed; aborts if
    an expected sha256 was given and does not match.
    """
    print(f"  + streaming stdin -> {device}")
    h = hashlib.sha256()
    total = 0
    buf = 4 * 1024 * 1024
    src = sys.stdin.buffer
    with open(device, "wb") as dst:
        while True:
            chunk = src.read(buf)
            if not chunk:
                break
            h.update(chunk)
            dst.write(chunk)
            total += len(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    print(f"  wrote {total} bytes from stdin")
    if total == 0:
        print("ERROR: no data received on stdin", file=sys.stderr)
        sys.exit(1)
    if expected_sha:
        got = h.hexdigest()
        if got != expected_sha:
            print(f"ERROR: sha256 mismatch (expected {expected_sha}, got {got})",
                  file=sys.stderr)
            sys.exit(1)
        print("  sha256 OK (stream)")


def main():
    parser = argparse.ArgumentParser(
        description="Deploy an application rootfs ext4 image to an NVMe app partition."
    )
    parser.add_argument(
        "slot",
        choices=["a"],
        help="Target slot: 'a' for app-a (nvme0n1p4)",
    )
    parser.add_argument(
        "ext4_image",
        help="Path to the .rootfs.ext4 file to deploy, or '-' to read from stdin",
    )
    parser.add_argument(
        "--sha256",
        default=None,
        help="Expected sha256 of the image (verified while writing)",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="Target partition, overriding the slot map (a jumbo app's slot is "
             "not p4)",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Filesystem label to set, overriding the slot map",
    )
    args = parser.parse_args()

    slot = PARTITION_MAP[args.slot]
    device = args.device or slot["device"]
    label = args.label or slot["label"]
    from_stdin = args.ext4_image == "-"
    ext4_image = None if from_stdin else os.path.abspath(args.ext4_image)

    # --- Preflight checks ---
    if os.geteuid() != 0:
        print("ERROR: This script must be run as root (sudo).", file=sys.stderr)
        sys.exit(1)

    if not from_stdin and not os.path.isfile(ext4_image):
        print(f"ERROR: Image not found: {ext4_image}", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(device):
        print(f"ERROR: Partition device not found: {device}", file=sys.stderr)
        print("Is the NVMe disk partitioned? (run init-install-efi.sh first)", file=sys.stderr)
        sys.exit(1)

    print(f"=== Deploying to {label} ({device}) ===")
    print(f"Image: {'<stdin>' if from_stdin else ext4_image}")
    print()

    # --- Step 1: Unmount ---
    print("Step 1: Ensure partition is unmounted")
    unmount(device)

    # --- Step 2: Write image ---
    print()
    print("Step 2: Writing ext4 image to partition")
    if from_stdin:
        stream_stdin_to_device(device, expected_sha=args.sha256)
    else:
        if args.sha256:
            print("  + verifying source sha256")
            h = hashlib.sha256()
            with open(ext4_image, "rb") as f:
                for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                    h.update(chunk)
            got = h.hexdigest()
            if got != args.sha256:
                print(f"ERROR: sha256 mismatch (expected {args.sha256}, got {got})",
                      file=sys.stderr)
                sys.exit(1)
            print("  sha256 OK")
        run(["dd", f"if={ext4_image}", f"of={device}", "bs=4M", "conv=fsync",
             ])

    # --- Step 3: Check and resize filesystem ---
    print()
    print("Step 3: Checking and expanding filesystem")
    run(["e2fsck", "-fy", device], check=False)
    run(["resize2fs", device])
    run(["e2fsck", "-fy", device], check=False)

    # --- Step 4: Restore label ---
    print()
    print(f"Step 4: Setting filesystem label to '{label}'")
    set_ext4_label(device, label)

    # --- Step 5: Mount and verify ---
    print()
    print("Step 5: Mounting and verifying deployment")
    mountpoint = tempfile.mkdtemp(prefix="app-deploy-")
    try:
        run(["mount", device, mountpoint])

        # Verify kernel is present
        kernel_path = os.path.join(mountpoint, "boot", "bzImage")
        if os.path.exists(kernel_path):
            size_mb = os.path.getsize(kernel_path) / (1024 * 1024)
            print(f"  Kernel found: /boot/bzImage ({size_mb:.1f} MB)")
        else:
            # Check for versioned kernel names
            boot_dir = os.path.join(mountpoint, "boot")
            if os.path.isdir(boot_dir):
                kernels = [f for f in os.listdir(boot_dir) if f.startswith("bzImage")]
                if kernels:
                    print(f"  Kernel found: /boot/{kernels[0]}")
                else:
                    print("  WARNING: No bzImage found in /boot/. "
                          "GRUB will fail to load this entry.")
            else:
                print("  WARNING: No /boot directory in rootfs. "
                      "GRUB will fail to load this entry.")

        # --- Step 6: Patch fstab ---
        print()
        print("Step 6: Patching /etc/fstab")
        fstab_path = os.path.join(mountpoint, "etc", "fstab")
        if os.path.exists(fstab_path):
            with open(fstab_path, "r") as f:
                original = f.read()
            print(f"  Original fstab:\n{original}")

            # Build new fstab: keep comment and virtual-fs lines,
            # drop any /dev/* or UUID=* root entries, add our label-based root
            new_lines = []
            for line in original.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    new_lines.append(line)
                    continue
                # Keep virtual filesystem mounts (proc, devpts, tmpfs, sysfs)
                fields = stripped.split()
                if len(fields) >= 2 and fields[1] in ("/proc", "/sys", "/dev/pts",
                                                       "/dev/shm", "/tmp", "/run"):
                    new_lines.append(line)
                    continue
                # Drop everything else (old root, boot entries from build)
            new_lines.append(f"LABEL={label}  /  ext4  defaults  1  1")
            new_fstab = "\n".join(new_lines) + "\n"

            with open(fstab_path, "w") as f:
                f.write(new_fstab)
            print(f"  Updated fstab:\n{new_fstab}")
        else:
            # Create minimal fstab
            os.makedirs(os.path.join(mountpoint, "etc"), exist_ok=True)
            with open(fstab_path, "w") as f:
                f.write(f"LABEL={label}  /  ext4  defaults  1  1\n")
            print(f"  Created minimal fstab with LABEL={label}")

        # Sync before unmount
        run(["sync"])

    finally:
        # --- Step 7: Unmount ---
        print()
        print("Step 7: Unmounting")
        run(["umount", mountpoint], check=False)
        os.rmdir(mountpoint)

    run(["sync"])

    print()
    print("=" * 50)
    print(f"Deployment complete: {label} ({device})")
    print()
    print(f"To boot into this image, reboot and select '{label}' from the GRUB menu.")
    print("=" * 50)


if __name__ == "__main__":
    main()
