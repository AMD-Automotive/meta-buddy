# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Backward-compatible shim — NAU8822 mic metering now lives in audio_targets.

The analog mic is registered as the ``nau8822_mic`` target in
``audio_targets``. This module re-exports the generic engine functions bound
to that key so existing imports keep working.
"""

import audio_targets

_KEY = "nau8822_mic"


def start_metering() -> dict:
    return audio_targets.start_metering(_KEY)


def stop_metering() -> dict:
    return audio_targets.stop_metering(_KEY)


def get_levels() -> dict:
    return audio_targets.get_levels(_KEY)


def get_meter_status() -> dict:
    return audio_targets.input_state(_KEY)


def start_loopback(pair=None) -> dict:
    return audio_targets.start_loopback(_KEY, pair)


def get_loopback_status() -> dict:
    return audio_targets.get_loopback_status(_KEY)
