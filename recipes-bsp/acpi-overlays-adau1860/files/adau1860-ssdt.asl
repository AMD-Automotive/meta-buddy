/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

/*
 * ADAU1860 SSDT overlay
 *
 * Declares an ACPI child device on the AMD designware SPI controller
 * (AMDI0063, UID=2 -> \_SB.HID2 on this platform) so the in-kernel
 * adau1860-spi driver binds via its acpi_match_table (_HID "PMOD1860").
 *
 * Delivered to the kernel via the early-initrd ACPI table-upgrade
 * mechanism (Documentation/admin-guide/acpi/initrd_table_override.rst):
 * compiled .aml is packaged inside a cpio archive at
 *   kernel/firmware/acpi/adau1860-ssdt.aml
 * and loaded as a separate boot module alongside the main initramfs.
 *
 * SPI parameters match the ADAU1860 datasheet: 4-wire, mode 0
 * (CPOL=0/CPHA=0), 8 data bits, chip select 0, conservative 1 MHz
 * initial clock (driver may raise after register init).
 */

DefinitionBlock ("adau1860-ssdt.aml", "SSDT", 2, "AMDADI", "ADAU1860", 0x00000001)
{
    External (\_SB_.HID2, DeviceObj)

    Scope (\_SB.HID2)
    {
        Device (ADAU)
        {
            Name (_HID, "PMOD1860")
            Name (_DDN, "ADAU1860 reference codec")
            Name (_UID, Zero)

            Method (_STA, 0, NotSerialized)
            {
                Return (0x0F)
            }

            Name (RBUF, ResourceTemplate ()
            {
                SpiSerialBusV2 (
                    0x0000,              // DeviceSelection (CS#0)
                    PolarityLow,         // DevicePolarity
                    FourWireMode,        // WireMode
                    0x08,                // DataBitLength
                    ControllerInitiated, // SlaveMode
                    0x000F4240,          // ConnectionSpeed (1 MHz)
                    ClockPolarityLow,    // ClockPolarity (CPOL=0)
                    ClockPhaseFirst,     // ClockPhase    (CPHA=0)
                    "\\_SB.HID2",        // ResourceSource
                    0x00,                // ResourceSourceIndex
                    ResourceConsumer,    ,
                    )
            })

            Method (_CRS, 0, Serialized)
            {
                Return (RBUF)
            }
        }
    }
}
