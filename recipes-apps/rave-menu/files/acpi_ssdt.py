# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""ACPI SSDT loading and configuration.

This module provides:
1. Runtime SSDT loading via ConfigFS (for diagnostics/testing)
2. Boot-time SSDT configuration via GRUB environment

SSDTs are loaded at boot by the initramfs ssdt-loader module based on
the kernel cmdline parameter: ssdt=name1,name2,...

The GRUB config reads ssdt_enabled from grubenv and adds the ssdt=
parameter. Enable/disable SSDTs via grub-editenv:
    grub-editenv /boot/efi/EFI/BOOT/grubenv set ssdt_enabled=adau1860

Templates are in /lib/firmware/acpi/ssdt-templates/<name>.aml
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

from helpers import run_cmd, running_from_nvme

# --- Paths ---
SSDT_TEMPLATES_DIR = Path("/lib/firmware/acpi/ssdt-templates")
FIRMWARE_ACPI_DIR = Path("/lib/firmware/acpi")  # Legacy path
CONFIGFS_ACPI_TABLE = Path("/sys/kernel/config/acpi/table")
GRUBENV_PATH_CANDIDATES = (
    Path("/boot/efi/EFI/BOOT/grubenv"),
    Path("/boot/efi/EFI/rave2/grubenv"),
    Path("/boot/EFI/BOOT/grubenv"),
    Path("/boot/EFI/rave2/grubenv"),
    Path("/boot/EFI/EFI/BOOT/grubenv"),
    Path("/boot/EFI/EFI/rave2/grubenv"),
)

NVME_GRUBENV_PATH_CANDIDATES = (
    Path("/boot/efi/EFI/rave2/grubenv"),
    Path("/boot/efi/EFI/BOOT/grubenv"),
)

USB_GRUBENV_PATH_CANDIDATES = (
    Path("/boot/EFI/EFI/rave2/grubenv"),
    Path("/boot/EFI/EFI/BOOT/grubenv"),
    Path("/boot/EFI/rave2/grubenv"),
    Path("/boot/EFI/BOOT/grubenv"),
)


def _is_mounted(path: str) -> bool:
    """Return True if path is currently a mount point in /proc/mounts."""
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as mounts:
            for line in mounts:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == path:
                    return True
    except Exception:
        pass
    return os.path.ismount(path)


def _preferred_grubenv_candidates(for_write: bool = False) -> tuple[Path, ...]:
    """Return grubenv candidates ordered by active boot medium.

    When running from USB and writing, unmount /boot/efi first so NVMe ESP
    does not shadow the USB boot files.
    """
    if running_from_nvme():
        preferred = NVME_GRUBENV_PATH_CANDIDATES
        fallback = USB_GRUBENV_PATH_CANDIDATES
    else:
        preferred = USB_GRUBENV_PATH_CANDIDATES
        fallback = NVME_GRUBENV_PATH_CANDIDATES

    return preferred + fallback + GRUBENV_PATH_CANDIDATES


_BOOT_VIEW_PREPARED = False


def _prepare_boot_view() -> tuple[bool, str | None]:
    """Ensure grubenv access targets the active boot medium's ESP.

    When booted from USB, /etc/fstab mounts the NVMe ESP at /boot/efi,
    which shadows the USB ESP's /boot/EFI/* via case-insensitive FAT. Any
    grubenv read or write then hits the wrong medium and can return stale
    state from a prior install. Unmount /boot/efi once per process so all
    later access goes to the USB ESP directly.
    """
    global _BOOT_VIEW_PREPARED
    if _BOOT_VIEW_PREPARED:
        return True, None
    if not running_from_nvme() and _is_mounted("/boot/efi"):
        umount_result = run_cmd(["/bin/umount", "/boot/efi"], timeout=5)
        if umount_result.returncode not in (0,) and _is_mounted("/boot/efi"):
            err = (umount_result.stderr or umount_result.stdout or "failed to unmount /boot/efi").strip()
            return False, err
    _BOOT_VIEW_PREPARED = True
    return True, None


# Backwards-compatible alias for the previous write-only name.
_prepare_boot_view_for_write = _prepare_boot_view


def _candidate_grubcfg_paths() -> list[Path]:
    """Return candidate grub.cfg paths matching grubenv search order."""
    seen = set()
    out = []
    for grubenv_path in _preferred_grubenv_candidates(for_write=True):
        grub_cfg = grubenv_path.with_name("grub.cfg")
        key = str(grub_cfg).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(grub_cfg)
    return out


def _ensure_grubenv_for_write() -> tuple[list[Path], str | None]:
    """Return grubenv paths for writes, creating one next to grub.cfg if needed."""
    existing = _existing_grubenv_paths(for_write=True)
    if existing:
        return existing, None

    grub_cfg_candidates = [p for p in _candidate_grubcfg_paths() if p.exists()]
    if not grub_cfg_candidates:
        return [], None

    created = []
    for grub_cfg in grub_cfg_candidates:
        grubenv_path = grub_cfg.with_name("grubenv")
        if grubenv_path.exists():
            created.append(grubenv_path)
            continue
        try:
            subprocess.run(
                ["grub-editenv", str(grubenv_path), "create"],
                check=True,
                capture_output=True,
                text=True,
            )
            created.append(grubenv_path)
        except subprocess.CalledProcessError as e:
            return [], f"Failed to create grubenv at {grubenv_path}: {e.stderr.strip()}"
        except FileNotFoundError:
            return [], "grub-editenv not installed"

    # Re-scan with normal path resolver to preserve preferred ordering.
    return _existing_grubenv_paths(for_write=True), None


def _existing_grubenv_paths(for_write: bool = False) -> list[Path]:
    """Return existing grubenv files in preferred search order."""
    # Reads must also see the active boot medium's ESP, not a shadowed mount.
    _prepare_boot_view()
    seen = set()
    paths = []
    for path in _preferred_grubenv_candidates(for_write=for_write):
        # Resolve duplicate spellings on case-insensitive VFAT mounts.
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            paths.append(path)
    return paths


class SsdtInfo(NamedTuple):
    """Information about an available SSDT."""
    name: str           # Template name for cmdline (e.g., "adau1860")
    table_name: str     # ConfigFS table name (e.g., "adau1860")
    filename: str       # Full filename (e.g., "adau1860.aml")
    path: str           # Full path
    size: int           # File size in bytes
    loaded: bool        # Whether currently loaded via ConfigFS
    enabled: bool       # Whether enabled in GRUB for next boot
    description: str    # Human-readable description


# Map of known SSDTs to their descriptions
SSDT_DESCRIPTIONS = {
    "adau1860": "ADAU1860 Audio Codec (SPI on AMDI0063)",
    "adau1860-ssdt": "ADAU1860 Audio Codec (SPI on AMDI0063)",
}


def _table_name_from_stem(stem: str) -> str:
    """Convert SSDT filename stem to ConfigFS table name.
    
    E.g., "adau1860-ssdt" -> "adau1860"
    """
    for suffix in ("-ssdt", "_ssdt", "-SSDT", "_SSDT"):
        if stem.endswith(suffix):
            return stem[:-len(suffix)]
    return stem


def _ensure_configfs_mounted() -> tuple[bool, str]:
    """Ensure configfs is mounted."""
    configfs_path = Path("/sys/kernel/config")
    if configfs_path.is_dir() and any(configfs_path.iterdir()):
        return True, "configfs already mounted"
    
    # Try to mount it
    try:
        subprocess.run(
            ["mount", "-t", "configfs", "none", "/sys/kernel/config"],
            check=True, capture_output=True, text=True
        )
        return True, "configfs mounted"
    except subprocess.CalledProcessError as e:
        return False, f"Failed to mount configfs: {e.stderr}"


def _ensure_acpi_configfs() -> tuple[bool, str]:
    """Ensure acpi_configfs module is loaded and available."""
    # First ensure configfs is mounted
    ok, msg = _ensure_configfs_mounted()
    if not ok:
        return False, msg
    
    # Check if acpi_configfs is already available
    if CONFIGFS_ACPI_TABLE.exists():
        return True, "acpi_configfs ready"
    
    # Try to load the module
    try:
        subprocess.run(
            ["modprobe", "acpi_configfs"],
            check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as e:
        return False, f"Failed to load acpi_configfs: {e.stderr}"
    
    # Verify it's now available
    if CONFIGFS_ACPI_TABLE.exists():
        return True, "acpi_configfs loaded"
    
    return False, "acpi_configfs loaded but table dir not found"


def list_available_ssdts() -> list[SsdtInfo]:
    """List all available SSDT template files."""
    ssdts = []
    enabled_list = get_enabled_ssdts()
    
    # Check templates directory first (new location)
    search_dirs = [SSDT_TEMPLATES_DIR, FIRMWARE_ACPI_DIR]
    
    seen_names = set()
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        
        for aml_file in search_dir.glob("*.aml"):
            name = aml_file.stem  # e.g., "adau1860"
            if name in seen_names:
                continue
            seen_names.add(name)
            
            # For templates, name is the cmdline arg directly
            # For legacy files, strip -ssdt suffix
            table_name = _table_name_from_stem(name)
            loaded = _is_ssdt_loaded(table_name)
            desc = SSDT_DESCRIPTIONS.get(name, SSDT_DESCRIPTIONS.get(f"{name}-ssdt", "Unknown ACPI device"))
            
            ssdts.append(SsdtInfo(
                name=name,
                table_name=table_name,
                filename=aml_file.name,
                path=str(aml_file),
                size=aml_file.stat().st_size,
                loaded=loaded,
                enabled=table_name in enabled_list,
                description=desc,
            ))
    
    return sorted(ssdts, key=lambda s: s.name)


def _is_ssdt_loaded(name: str) -> bool:
    """Check if an SSDT is currently loaded via ConfigFS."""
    table_dir = CONFIGFS_ACPI_TABLE / name
    return table_dir.exists()


def load_ssdt(name: str) -> tuple[bool, str]:
    """Load an SSDT via ACPI ConfigFS.
    
    Args:
        name: The SSDT name (stem of the .aml file, e.g., "adau1860-ssdt")
    
    Returns:
        (success, message) tuple
    """
    aml_path = FIRMWARE_ACPI_DIR / f"{name}.aml"
    
    if not aml_path.exists():
        return False, f"SSDT file not found: {aml_path}"
    
    # Ensure acpi_configfs is available
    ok, msg = _ensure_acpi_configfs()
    if not ok:
        return False, msg
    
    # Use cleaned table name for ConfigFS (strip -ssdt suffix)
    table_name = _table_name_from_stem(name)
    
    # Check if already loaded
    table_dir = CONFIGFS_ACPI_TABLE / table_name
    if table_dir.exists():
        return True, f"SSDT '{table_name}' already loaded"
    
    # Create the table directory
    try:
        table_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        return False, "Permission denied creating table directory (need root)"
    except OSError as e:
        return False, f"Failed to create table directory: {e}"
    
    # Write the AML file - must use dd to write in one shot
    aml_dest = table_dir / "aml"
    file_size = aml_path.stat().st_size
    
    try:
        result = subprocess.run(
            ["dd", f"if={aml_path}", f"of={aml_dest}", f"bs={file_size}"],
            check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as e:
        # Clean up the directory on failure
        try:
            table_dir.rmdir()
        except:
            pass
        return False, f"Failed to load SSDT: {e.stderr}"
    
    return True, f"SSDT '{table_name}' loaded successfully"


def unload_ssdt(name: str) -> tuple[bool, str]:
    """Attempt to unload an SSDT by removing its ConfigFS directory.
    
    NOTE: The Linux kernel does NOT support unloading ACPI tables once loaded.
    The ConfigFS directory cannot be removed while the table is active.
    A reboot is required to remove a loaded SSDT.
    
    Args:
        name: The SSDT name (stem of the .aml file)
    
    Returns:
        (success, message) tuple
    """
    table_name = _table_name_from_stem(name)
    table_dir = CONFIGFS_ACPI_TABLE / table_name
    
    if not table_dir.exists():
        return True, f"SSDT '{table_name}' not loaded"
    
    # ACPI tables cannot be unloaded at runtime - this will fail with EBUSY
    return False, f"SSDT '{table_name}' cannot be unloaded (kernel limitation). Reboot required."


def get_ssdt_status() -> dict:
    """Get comprehensive status of SSDT loading capability and state."""
    existing_grubenv = _existing_grubenv_paths()
    preferred = _preferred_grubenv_candidates()
    primary_grubenv = existing_grubenv[0] if existing_grubenv else preferred[0]

    status = {
        "configfs_available": False,
        "acpi_configfs_available": False,
        "templates_dir_exists": SSDT_TEMPLATES_DIR.exists(),
        "available_ssdts": [],
        "loaded_ssdts": [],
        "enabled_ssdts": get_enabled_ssdts(),
        "adau1860_enabled": is_adau1860_enabled(),
        "grubenv_path": str(primary_grubenv),
        "grubenv_exists": bool(existing_grubenv),
        "grubenv_paths": [str(p) for p in existing_grubenv],
        "boot_source": "nvme" if running_from_nvme() else "usb",
        "error": None,
    }
    
    # Check configfs
    configfs_path = Path("/sys/kernel/config")
    status["configfs_available"] = configfs_path.is_dir()
    
    # Check acpi_configfs
    status["acpi_configfs_available"] = CONFIGFS_ACPI_TABLE.exists()
    
    # List available and loaded SSDTs
    try:
        ssdts = list_available_ssdts()
        status["available_ssdts"] = [s._asdict() for s in ssdts]
        status["loaded_ssdts"] = [s.name for s in ssdts if s.loaded]
    except Exception as e:
        status["error"] = str(e)
    
    return status


# --- ADAU1860 Enable/Disable Configuration via GRUB Environment ---


def _get_grubenv_value(key: str) -> str | None:
    """Read a value from the GRUB environment."""
    for grubenv_path in _existing_grubenv_paths():
        try:
            result = subprocess.run(
                ["grub-editenv", str(grubenv_path), "list"],
                check=True, capture_output=True, text=True
            )
            for line in result.stdout.splitlines():
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1]
        except subprocess.CalledProcessError:
            continue
        except FileNotFoundError:
            # grub-editenv not installed
            return None
    
    return None


def _set_grubenv_value(key: str, value: str) -> tuple[bool, str]:
    """Set a value in the GRUB environment."""
    ok, err = _prepare_boot_view_for_write()
    if not ok:
        return False, err or "failed to prepare boot filesystem view"

    grubenv_paths, ensure_err = _ensure_grubenv_for_write()
    if ensure_err:
        return False, ensure_err
    if not grubenv_paths:
        candidates = ", ".join(str(p) for p in _preferred_grubenv_candidates(for_write=True))
        return False, f"GRUB environment file not found. Checked: {candidates}"

    failures = []
    for grubenv_path in grubenv_paths:
        try:
            subprocess.run(
                ["grub-editenv", str(grubenv_path), "set", f"{key}={value}"],
                check=True, capture_output=True, text=True
            )
        except subprocess.CalledProcessError as e:
            failures.append(f"{grubenv_path}: {e.stderr.strip()}")
        except FileNotFoundError:
            return False, "grub-editenv not installed"

    if failures:
        return False, "Failed to set grubenv: " + "; ".join(failures)

    return True, f"Set {key}={value} in {len(grubenv_paths)} grubenv file(s) on active boot media"


def _unset_grubenv_value(key: str) -> tuple[bool, str]:
    """Remove a value from the GRUB environment."""
    ok, err = _prepare_boot_view_for_write()
    if not ok:
        return False, err or "failed to prepare boot filesystem view"

    grubenv_paths = _existing_grubenv_paths(for_write=True)
    if not grubenv_paths:
        # Missing grubenv means no variables are persisted; treat unset as idempotent.
        return True, f"{key} already unset (no grubenv present on active boot media)"

    failures = []
    for grubenv_path in grubenv_paths:
        try:
            subprocess.run(
                ["grub-editenv", str(grubenv_path), "unset", key],
                check=True, capture_output=True, text=True
            )
        except subprocess.CalledProcessError as e:
            failures.append(f"{grubenv_path}: {e.stderr.strip()}")
        except FileNotFoundError:
            return False, "grub-editenv not installed"

    if failures:
        return False, "Failed to unset grubenv: " + "; ".join(failures)

    return True, f"Unset {key} in {len(grubenv_paths)} grubenv file(s) on active boot media"


def get_enabled_ssdts() -> list[str]:
    """Get SSDTs persisted in grubenv (the value that will apply at next boot)."""
    value = _get_grubenv_value("ssdt_enabled")
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def set_enabled_ssdts(ssdts: list[str]) -> tuple[bool, str]:
    """Set the list of enabled SSDTs in GRUB environment.
    
    Args:
        ssdts: List of SSDT names to enable (empty list disables all)
    
    Returns:
        (success, message) tuple
    """
    if not ssdts:
        return _unset_grubenv_value("ssdt_enabled")
    
    value = ",".join(ssdts)
    return _set_grubenv_value("ssdt_enabled", value)


def is_adau1860_enabled() -> bool:
    """Check if ADAU1860 is enabled in grubenv (will apply at next boot)."""
    return "adau1860" in get_enabled_ssdts()


def set_adau1860_enabled(enabled: bool) -> tuple[bool, str]:
    """Enable or disable ADAU1860 in GRUB environment.
    
    Args:
        enabled: True to enable, False to disable
    
    Returns:
        (success, message) tuple
    """
    current = get_enabled_ssdts()

    if enabled:
        if "adau1860" not in current:
            current.append("adau1860")
    else:
        if "adau1860" in current:
            current.remove("adau1860")

    ok, msg = set_enabled_ssdts(current)
    if not ok:
        return False, msg

    action = "enabled" if enabled else "disabled"
    return True, f"ADAU1860 {action}. Reboot required to apply."
