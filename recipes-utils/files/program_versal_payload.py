#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""PCIe OSPI/SDA flash programming host script entrypoint."""

import os
import argparse
import json
import struct
import sys
import time
from pathlib import Path

# Support both in-tree execution and installed target execution.
MODULE_DIRS = [Path(__file__).resolve().parent, Path("/usr/lib/versal")]
for module_dir in MODULE_DIRS:
    module_dir_str = str(module_dir)
    if module_dir.exists() and module_dir_str not in sys.path:
        sys.path.insert(0, module_dir_str)

from versal_payload_constants import *  # noqa: F403,E402
from versal_payload_pcie import *  # noqa: F403,E402
from versal_payload_protocol import *  # noqa: F403,E402
import versal_ufs_luns as ufs_luns  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ospi_file", nargs="?", help="OSPI image, or '-' to skip")
    parser.add_argument("wic_file", nargs="?", help="WIC image, or '-' to skip")
    parser.add_argument("bmap_file", nargs="?", help="optional bmap for sparse WIC programming")
    parser.add_argument(
        "--read-ufs", nargs=3, metavar=("OFFSET", "LENGTH", "OUTPUT"),
        help="read a UFS byte range; OFFSET and LENGTH accept decimal or 0x-prefixed values",
    )
    parser.add_argument(
        "--ufs-range", nargs=2, action="append", default=[], metavar=("OFFSET", "LENGTH"),
        help="program only this UFS byte range; repeatable and requires a bmap",
    )
    parser.add_argument(
        "--zero-before-write", action="store_true",
        help="zero the target span before a sparse write, so blocks the image "
             "leaves unmapped do not keep the previous image's content",
    )
    parser.add_argument(
        "--ufs-lun", type=lambda v: int(v, 0), default=0, metavar="N",
        help="UFS LUN to program, read or zero (default: 0, the rootfs LUN)",
    )
    parser.add_argument(
        "--exec", dest="exec_command", metavar="COMMAND",
        help="run a shell command on the recovery target and print its output",
    )
    parser.add_argument(
        "--attach", action="store_true",
        help="skip phase 1; talk to a recovery payload that is already running. "
             "Saves re-pushing ~100 MB over the BAR for every command",
    )
    parser.add_argument(
        "--ufs-luns", action="store_true",
        help="report the target's current UFS LUN layout and provisioning limits",
    )
    parser.add_argument(
        "--ufs-sizes", metavar="SPEC",
        help="validate a LUN layout, e.g. '32G,64G,rest'. One entry per LUN; "
             "at most one may be 'rest'. Boot LUNs are preserved unless "
             "--ufs-boot-luns says otherwise",
    )
    parser.add_argument(
        "--ufs-boot-luns", metavar="LIST",
        help="take charge of the boot LUNs: a comma-separated list of 1-based "
             "positions in --ufs-sizes to mark as Boot LUN A and B, or 'none' "
             "to leave the device with no boot LUN at all",
    )
    parser.add_argument(
        "--ufs-plan", metavar="PATH",
        help="a JSON file naming each LUN explicitly: {\"luns\": [{\"lun\": 0, "
             "\"size\": \"rest\"}, ...]}. Unlike --ufs-sizes this keeps LUN "
             "indices exactly as given, gaps included, and may carry per-LUN "
             "descriptor fields such as memory_type",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="emit machine-readable JSON for --ufs-luns and --ufs-sizes",
    )
    parser.add_argument(
        "--json-out", metavar="PATH",
        help="write the LUN report and any plan preview to PATH as a single "
             "JSON document, leaving stdout as the human-readable log",
    )
    parser.add_argument(
        "--ufs-restore-baseline", action="store_true",
        help="restore the layout this device was first seen with",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="confirm an irreversible UFS re-provision. DESTROYS ALL DATA on "
             "every LUN and leaves the board unbootable until it is reimaged",
    )
    args = parser.parse_args()

    pciimage_file = DEFAULT_PCIIMAGE_FILE
    ospi_file = args.ospi_file
    wic_file = args.wic_file
    bmap_file = args.bmap_file
    read_request = None
    if args.read_ufs:
        try:
            read_request = (int(args.read_ufs[0], 0), int(args.read_ufs[1], 0), args.read_ufs[2])
        except ValueError:
            parser.error("--read-ufs OFFSET and LENGTH must be integers")
    try:
        ufs_ranges = [(int(offset, 0), int(length, 0)) for offset, length in args.ufs_range]
    except ValueError:
        parser.error("--ufs-range OFFSET and LENGTH must be integers")
    if any(offset < 0 or length <= 0 for offset, length in ufs_ranges):
        parser.error("--ufs-range OFFSET must be non-negative and LENGTH positive")
    if ufs_ranges and not bmap_file:
        parser.error("--ufs-range requires a bmap_file")
    if args.zero_before_write and not bmap_file:
        parser.error("--zero-before-write requires a bmap_file")
    if args.ufs_lun < 0:
        parser.error("--ufs-lun must be non-negative")

    want_lun_report = bool(args.ufs_luns or args.ufs_sizes or args.ufs_plan
                           or args.ufs_restore_baseline)
    want_provisioning = bool(args.ufs_sizes or args.ufs_plan or args.ufs_restore_baseline)

    # Treat "" or "-" as "skip this phase". This lets shell wrappers pass a
    # positional placeholder without falling back to packaged defaults.
    if ospi_file in ("", "-"):
        ospi_file = None
    if wic_file in ("", "-"):
        wic_file = None
    if bmap_file in ("", "-"):
        bmap_file = None

    if ospi_file is None and wic_file is None and read_request is None \
            and args.exec_command is None and not want_lun_report:
        print("ERROR: At least one of ospi_file or wic_file must be provided")
        print("Use '-' as a placeholder to skip a phase, e.g.:")
        print(f"  sudo {sys.argv[0]} - rootfs.wic   # WIC only")
        print(f"  sudo {sys.argv[0]} ospi.bin -     # OSPI only")
        sys.exit(1)
    if bmap_file and not wic_file:
        print("ERROR: A bmap file requires a WIC image")
        sys.exit(1)

    if os.geteuid() != 0:
        print("ERROR: This script must be run as root")
        print("Try: sudo {}".format(" ".join(sys.argv)))
        sys.exit(1)

    if not os.path.exists(pciimage_file):
        print(f"ERROR: pciImage file not found: {pciimage_file}")
        sys.exit(1)

    for label, path in [("OSPI", ospi_file), ("WIC", wic_file), ("bmap", bmap_file)]:
        if path is not None and not os.path.exists(path):
            print(f"ERROR: {label} file not found: {path}")
            sys.exit(1)

    print("=" * 70)
    print("PCIe Programming - AMD Versal Gen2")
    print("=" * 70)
    print(f"pciImage file: {pciimage_file}")
    print(f"OSPI file:     {ospi_file or '(not provided - skipping OSPI)'}")
    print(f"WIC file:      {wic_file or '(not provided - skipping SDA)'}")
    print(f"Bmap file:     {bmap_file or '(not provided - full WIC transfer)'}")
    print(f"UFS LUN:       {args.ufs_lun}")
    if bmap_file:
        print(f"Zero first:    {'yes' if args.zero_before_write else 'no'}")
    if read_request:
        print(f"UFS read:      offset={read_request[0]} length={read_request[1]} output={read_request[2]}")

    device = find_pcie_device()
    if not device:
        print("\nERROR: Device not found")
        print(f"Looking for: VID=0x{VENDOR_ID:04x}, DID=0x{DEVICE_ID:04x}, REV=0x{REVISION:02x}")
        sys.exit(1)

    resource_path, bar_size = enable_bar(device, BAR_INDEX)
    if not resource_path:
        print("\nERROR: Failed to enable BAR")
        sys.exit(1)

    mem = map_bar(resource_path)
    if not mem:
        print("\nERROR: Failed to map BAR")
        sys.exit(1)

    ospi_success = None
    sda_success = None
    read_success = None
    exec_success = None
    exec_exit = None
    luns_success = None
    plan_success = None
    json_document = {}

    try:
        print("\n" + "=" * 70)
        print("PHASE 1: pciImage LOADING")
        print("=" * 70)

        if args.attach:
            current = read_u32(mem, STATUS_OFFSET)
            print(f"Attaching to a running payload; target status is {describe_status(current)}")
            pciimage_size = 0
        else:
            if not wait_for_signal(mem, STATUS_OFFSET, HOST_READY, READY_TIMEOUT, "Host Ready"):
                print("\nERROR: Device did not become ready")
                sys.exit(1)

            pciimage_size, _ = load_and_verify_file(mem, PCIIMAGE_OFFSET, pciimage_file, bar_size, "pciImage")
            if pciimage_size is None:
                print("\nERROR: pciImage load/verify failed")
                sys.exit(1)

            print("")
            print("Signaling pciImage completion...")
            write_u32(mem, STATUS_OFFSET, PAYLOAD_READY)
            mem.flush()

            readback = read_u32(mem, STATUS_OFFSET)
            print(f"Completion signal verified: 0x{readback:08x}")

        if ospi_file:
            print("=-=-=-=")
            print("Program OSPI")
            print("=-=-=-=")

            if not wait_for_signal(mem, STATUS_OFFSET, FLASH_READY, FLASH_READY_TIMEOUT, "Flash Ready"):
                print("\nERROR: Device did not signal flash ready")
                sys.exit(1)

            ospi_size, ospi_md5 = load_and_verify_file(mem, OSPI_OFFSET, ospi_file, bar_size, "OSPI Image")
            if ospi_size is None:
                print("\nERROR: OSPI image load/verify failed")
                sys.exit(1)

            print("")
            print("=" * 70)
            print("Writing OSPI Metadata...")
            print("=" * 70)
            print(f"Size: {ospi_size} bytes (0x{ospi_size:08x})")
            print(f"MD5:  {struct.pack('<4I', *ospi_md5).hex()}")

            write_u32(mem, SIZE_OFFSET, ospi_size)
            write_u32(mem, MD5_OFFSET_0, ospi_md5[0])
            write_u32(mem, MD5_OFFSET_1, ospi_md5[1])
            write_u32(mem, MD5_OFFSET_2, ospi_md5[2])
            write_u32(mem, MD5_OFFSET_3, ospi_md5[3])
            mem.flush()

            print("Verifying metadata...")
            size_readback = read_u32(mem, SIZE_OFFSET)
            md5_readback = (
                read_u32(mem, MD5_OFFSET_0),
                read_u32(mem, MD5_OFFSET_1),
                read_u32(mem, MD5_OFFSET_2),
                read_u32(mem, MD5_OFFSET_3),
            )

            if size_readback != ospi_size:
                print(f"ERROR: Size mismatch - wrote 0x{ospi_size:08x}, read 0x{size_readback:08x}")
                sys.exit(1)

            if md5_readback != ospi_md5:
                print("ERROR: MD5 mismatch")
                sys.exit(1)

            print("Metadata verified successfully")

            print("")
            print("Starting OSPI programming...")
            write_u32(mem, STATUS_OFFSET, FLASH_GO)
            mem.flush()

            readback = read_u32(mem, STATUS_OFFSET)
            print(f"Flash go signal sent: 0x{readback:08x}")

            print("\n" + "=" * 70)
            print("MONITORING OSPI PROGRAMMING")
            print("=" * 70)

            ospi_success = monitor_programming_status(mem)

            print("=-=-=-=")
            print("OSPI Log")
            print("=-=-=-=")
            print("\n" + "=" * 70)
            print("OSPI LOG COLLECTION")
            print("=" * 70)


            collect_logs(mem)

            if not ospi_success:
                print("\nERROR: OSPI programming failed")
                if wic_file:
                    print("Skipping SDA programming due to OSPI failure")
                    wic_file = None

        if wic_file:
            print("=-=-=-=")
            print("Program filesystem")
            print("=-=-=-=")

            if ospi_file and ospi_success:
                time.sleep(0.5)

            if not ospi_file:
                if not wait_for_signal(mem, STATUS_OFFSET, FLASH_READY, FLASH_READY_TIMEOUT, "Flash Ready (for SDA)"):
                    print("\nERROR: Device did not signal ready for SDA")
                    sys.exit(1)

            sda_success = send_sda_chunks(
                mem, bar_size, wic_file, bmap_file, ufs_ranges or None,
                args.zero_before_write, args.ufs_lun
            )

            print("=-=-=-=")
            print("FS Log")
            print("=-=-=-=")
            print("\n" + "=" * 70)
            print("SDA LOG COLLECTION")
            print("=" * 70)

            collect_logs(mem)

        if read_request:
            print("=-=-=-=")
            print("Read UFS")
            print("=-=-=-=")

            if not ospi_file and not wic_file and not args.attach:
                if not wait_for_signal(mem, STATUS_OFFSET, FLASH_READY, FLASH_READY_TIMEOUT, "Flash Ready (for UFS read)"):
                    print("\nERROR: Device did not signal ready for UFS read")
                    sys.exit(1)

            read_success = read_ufs(mem, *read_request, lun=args.ufs_lun)
            collect_logs(mem)

        if args.exec_command:
            print("=-=-=-=")
            print("Target exec")
            print("=-=-=-=")

            if not ospi_file and not wic_file and not read_request and not args.attach:
                if not wait_for_signal(mem, STATUS_OFFSET, FLASH_READY, FLASH_READY_TIMEOUT, "Flash Ready (for exec)"):
                    print("\nERROR: Device did not signal ready for exec")
                    sys.exit(1)

            exec_exit = exec_command(mem, args.exec_command)
            exec_success = exec_exit == 0

        if want_lun_report:
            print("=-=-=-=")
            print("UFS LUN layout")
            print("=-=-=-=")

            if not ospi_file and not wic_file and not read_request \
                    and not args.exec_command and not args.attach:
                if not wait_for_signal(mem, STATUS_OFFSET, FLASH_READY, FLASH_READY_TIMEOUT,
                                       "Flash Ready (for LUN query)"):
                    print("\nERROR: Device did not signal ready for the LUN query")
                    sys.exit(1)

            report = query_ufs_luns(mem)
            luns_success = report is not None
            if report:
                json_document["report"] = report
                if args.json:
                    print(json.dumps(report, indent=2))
                else:
                    print(ufs_luns.format_report(report))
                path, created = ufs_luns.capture_baseline(report)
                if created:
                    print(f"\nCaptured this device's baseline layout at {path}")
                elif path:
                    print(f"\nBaseline layout already recorded at {path}")

            if report and (args.ufs_sizes or args.ufs_plan):
                print("")
                print("=-=-=-=")
                print("Proposed layout (dry run, nothing is written)")
                print("=-=-=-=")
                try:
                    if args.ufs_plan:
                        plan = ufs_luns.plan_from_file(report, args.ufs_plan)
                    else:
                        sizes = [ufs_luns.parse_size(item)
                                 for item in args.ufs_sizes.split(",") if item.strip()]
                        plan = ufs_luns.plan_from_sizes(
                            report, sizes, ufs_luns.parse_boot_slots(args.ufs_boot_luns))
                except ufs_luns.PlanError as error:
                    print(f"ERROR: {error}")
                    plan_success = False
                else:
                    preview = check_ufs_plan(mem, plan, commit=args.yes, allow_wipe=args.yes)
                    plan_success = preview is not None
                    if preview:
                        json_document["preview"] = preview
                        if args.json:
                            print(json.dumps(preview, indent=2))
                        else:
                            print(ufs_luns.format_preview(preview)
                                  if not preview.get("committed")
                                  else ufs_luns.format_report(preview))
                        if preview.get("committed"):
                            print("\nUFS re-provisioned. All previous contents are gone and "
                                  "the board will not boot until an image is programmed.")
                        else:
                            print("\nThis was a dry run. Nothing on the device has changed.")
                            print("Add --yes to apply it, which destroys all UFS contents.")

            if report and args.ufs_restore_baseline:
                print("")
                print("=-=-=-=")
                print("Restore baseline layout")
                print("=-=-=-=")
                baseline, source = ufs_luns.restore_descriptor(report)
                if baseline is None:
                    print("ERROR: no factory layout is known for this part and no "
                          "baseline was recorded for this device; run --ufs-luns "
                          "on a board before re-provisioning it")
                    plan_success = False
                else:
                    if source == "factory":
                        print(f"Using the built-in factory layout for "
                              f"{(report.get('product') or '').strip()}")
                    else:
                        print("No factory layout is known for this part; using the "
                              "layout this board was first seen with")
                    if not args.yes:
                        print("Add --yes to write it back, which destroys all "
                              "current UFS contents.")
                        plan_success = True
                    else:
                        restore = {"schema": 1, "token": report.get("token", 0),
                                   "luns": [], "restore_descriptor_hex": baseline.hex()}
                        result = check_ufs_plan(mem, restore, commit=True, allow_wipe=True)
                        plan_success = result is not None
                        if result:
                            json_document["preview"] = result
                            json_document["restore_source"] = source
                            print(ufs_luns.format_report(result))
                            print(f"\nLayout restored from the {source} descriptor.")

        print("=-=-=-=")
        print("Summary")
        print("=-=-=-=")
        print("\n" + "=" * 70)
        print("OPERATION SUMMARY")
        print("=" * 70)
        

        print(f"  pciImage loaded:  {pciimage_size} bytes ({pciimage_size / (1024 * 1024):.2f} MB)"
              if pciimage_size else "  pciImage:         (attached to a running payload)")

        if ospi_file:
            status_str = "SUCCESS" if ospi_success else "FAILED"
            print(f"  OSPI:             {status_str}")

        if wic_file:
            status_str = "SUCCESS" if sda_success else "FAILED"
            wic_size = os.path.getsize(wic_file)
            print(f"  SDA (WIC):        {status_str} ({wic_size / (1024 * 1024 * 1024):.2f} GB)")

        if read_request:
            status_str = "SUCCESS" if read_success else "FAILED"
            print(f"  UFS read:         {status_str} ({read_request[1]} bytes)")

        if args.exec_command:
            status_str = "SUCCESS" if exec_success else "FAILED"
            print(f"  Target exec:      {status_str} (exit {exec_exit})")

        if want_lun_report:
            status_str = "SUCCESS" if luns_success else "FAILED"
            print(f"  UFS LUN query:    {status_str}")
        if want_provisioning:
            status_str = "SUCCESS" if plan_success else "FAILED"
            print(f"  Provisioning:     {status_str}")

        all_ok = True
        if ospi_file and not ospi_success:
            all_ok = False
        if wic_file and not sda_success:
            all_ok = False
        if read_request and not read_success:
            all_ok = False
        if args.exec_command and not exec_success:
            all_ok = False
        if want_lun_report and not luns_success:
            all_ok = False
        if want_provisioning and not plan_success:
            all_ok = False

        print("")
        if all_ok:
            print("ALL OPERATIONS COMPLETED SUCCESSFULLY!")
        else:
            print("ONE OR MORE OPERATIONS FAILED!")
            reason = describe_failure(mem)
            if reason:
                print(f"Target failure reason: {reason}")
            print(f"Final target status:   {describe_status(read_u32(mem, STATUS_OFFSET))}")
        print("=" * 70)

        sys.exit(0 if all_ok else 1)

    except KeyboardInterrupt:
        print("\n\nOperation interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nERROR: Unexpected exception: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        # Written even on failure so a caller can tell a device that answered
        # badly from one that never answered at all.
        if args.json_out:
            try:
                with open(args.json_out, "w", encoding="utf-8") as handle:
                    json.dump(json_document, handle, indent=2)
            except OSError as error:
                print(f"WARNING: could not write {args.json_out}: {error}")
        if mem:
            mem.close()


if __name__ == "__main__":
    main()
