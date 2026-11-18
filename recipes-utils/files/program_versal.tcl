# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# XSDB script to reset and program a Versal device with retry logic.
#
# The boot image path may be overridden via the BOOTBIN environment variable
# (used by the JTAG programming flow that lets the user pick an arbitrary
# boot.bin/.pdi). Falls back to the packaged Versal recovery image.

if {[info exists ::env(BOOTBIN)] && $::env(BOOTBIN) ne ""} {
    set bootbin $::env(BOOTBIN)
} else {
    set bootbin "/lib/firmware/versal/boot.bin"
}
if {[info exists ::env(MAX_ATTEMPTS)] && $::env(MAX_ATTEMPTS) ne ""} {
    set max_attempts $::env(MAX_ATTEMPTS)
} else {
    set max_attempts 4
}
set attempt 1
set success 0

proc log_msg {msg} {
    puts "[clock format [clock seconds] -format {%Y-%m-%d %H:%M:%S}] $msg"
    flush stdout
}

if {![file exists $bootbin]} {
    log_msg "ERROR: boot image not found: $bootbin"
    exit 1
}

log_msg "Using boot image: $bootbin"

log_msg "Connecting to hw_server / target"
connect
target 1

while {$attempt <= $max_attempts} {

    log_msg "Programming attempt $attempt of $max_attempts"

    set rc [catch {
        device reset
        after 1000
        device program $bootbin
    } err]

    if {$rc == 0} {
        log_msg "Programming succeeded on attempt $attempt"
        set success 1
        break
    } else {
        log_msg "Programming failed on attempt $attempt"
        log_msg "Error: $err"

        incr attempt

        if {$attempt <= $max_attempts} {
            log_msg "Retrying..."
            after 1000
        }
    }
}

if {!$success} {
    log_msg "ERROR: programming failed after $max_attempts attempts"
    exit 1
}

log_msg "Done"
exit 0