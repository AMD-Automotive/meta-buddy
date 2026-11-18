#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

"""Tests for host-side UFS LUN planning and the baseline store.

The geometry fixture is the real RAVE2 part as read from hardware: 4 MiB
allocation units, 61,022 allocatable units, and two reserved boot LUNs at
indices 1 and 2.
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "files"))

import pytest  # noqa: E402

import versal_ufs_luns as ufs  # noqa: E402

AU = 4 * 1024 * 1024


def rave2_report():
    return {
        "serial": "66e98d8df628",
        "product": "KLUEG8UHGC-B0E1",
        "spec_version": "3.1",
        "token": 1234,
        "config_descriptor_hex": "e601",
        "geometry": {
            "au_bytes": AU,
            "max_number_lu": 8,
            "budget_alloc_units": 61022,
            "budget_bytes": 61022 * AU,
            "cap_adj_fac": {"0": 256, "3": 768},
            "total_raw_bytes": 61022 * AU,
        },
        "luns": [
            {"lun": 0, "enabled": True, "boot_lun_id": 0, "memory_type": 0,
             "data_reliability": 0, "write_protect": 0,
             "logical_block_size_exp": 0x0C, "provisioning_type": 2,
             "num_alloc_units": 61016, "capacity_bytes": 61016 * AU},
            {"lun": 1, "enabled": True, "boot_lun_id": 1, "memory_type": 3,
             "data_reliability": 1, "write_protect": 0,
             "logical_block_size_exp": 0x0C, "provisioning_type": 2,
             "num_alloc_units": 3, "capacity_bytes": 4 * 1024 * 1024},
            {"lun": 2, "enabled": True, "boot_lun_id": 2, "memory_type": 3,
             "data_reliability": 1, "write_protect": 0,
             "logical_block_size_exp": 0x0C, "provisioning_type": 2,
             "num_alloc_units": 3, "capacity_bytes": 4 * 1024 * 1024},
        ],
    }


@pytest.mark.parametrize("text,expected", [
    ("32G", 32 * 1024 ** 3),
    ("512M", 512 * 1024 ** 2),
    ("0x100000", 0x100000),
    ("1024", 1024),
    ("rest", "rest"),
    ("REST", "rest"),
])
def test_parse_size(text, expected):
    assert ufs.parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "banana", "-5", "0", "12Q"])
def test_parse_size_rejects_nonsense(text):
    with pytest.raises(ufs.PlanError):
        ufs.parse_size(text)


def test_boot_luns_are_reserved():
    report = rave2_report()
    assert sorted(ufs.boot_luns(report)) == [1, 2]
    # The boot LUNs' 6 allocation units are not offered to a plan.
    assert ufs.available_alloc_units(report) == 61016
    assert ufs.assignable_luns(report) == [0, 3, 4, 5, 6, 7]


def test_plan_assigns_around_boot_luns():
    plan = ufs.plan_from_sizes(rave2_report(), [ufs.parse_size("32G"), ufs.parse_size("64G")])
    assert [entry["lun"] for entry in plan["luns"]] == [0, 3]
    assert plan["token"] == 1234


def test_rest_absorbs_the_remainder_exactly():
    plan = ufs.plan_from_sizes(rave2_report(), [ufs.parse_size("32G"), "rest"])
    assert plan["luns"][1]["size_bytes"] == "rest"


def test_sizes_round_down_to_whole_allocation_units():
    # 32 GiB + 1 byte must not silently become 32 GiB + one unit.
    plan = ufs.plan_from_sizes(rave2_report(), [32 * 1024 ** 3 + 1])
    assert plan["luns"][0]["size_bytes"] == 32 * 1024 ** 3


def test_rejects_size_below_one_allocation_unit():
    with pytest.raises(ufs.PlanError, match="rounds down to zero"):
        ufs.plan_from_sizes(rave2_report(), [1024])


def test_rejects_over_capacity():
    with pytest.raises(ufs.PlanError, match="available"):
        ufs.plan_from_sizes(rave2_report(), [500 * 1024 ** 3])


def test_rejects_more_luns_than_assignable():
    with pytest.raises(ufs.PlanError, match="assignable"):
        ufs.plan_from_sizes(rave2_report(), [1024 ** 3] * 7)


def test_rejects_two_rest_entries():
    with pytest.raises(ufs.PlanError, match="at most one"):
        ufs.plan_from_sizes(rave2_report(), ["rest", "rest"])


def test_baseline_is_captured_once_and_never_overwritten(tmp_path):
    report = rave2_report()
    path, created = ufs.capture_baseline(report, tmp_path)
    assert created and path.exists()

    # A second capture must not replace it: a baseline taken after someone has
    # re-provisioned a board would record the wrong layout as its default.
    report["config_descriptor_hex"] = "ffff"
    _, created_again = ufs.capture_baseline(report, tmp_path)
    assert not created_again
    assert ufs.load_baseline("66e98d8df628", tmp_path) == bytes.fromhex("e601")


def test_baseline_missing_serial_is_not_stored(tmp_path):
    report = rave2_report()
    report["serial"] = ""
    path, created = ufs.capture_baseline(report, tmp_path)
    assert path is None and not created


def test_baseline_serial_cannot_escape_the_store(tmp_path):
    report = rave2_report()
    report["serial"] = "../../etc/passwd"
    path, _ = ufs.capture_baseline(report, tmp_path)
    assert path is None or tmp_path in path.parents


# ---------------------------------------------------------------------------
#  Boot LUN assignment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    (None, None),
    ("none", []),
    ("NONE", []),
    ("", []),
    ("1", [0]),
    ("1,2", [0, 1]),
    ("2,1", [1, 0]),
])
def test_parse_boot_slots(text, expected):
    assert ufs.parse_boot_slots(text) == expected


@pytest.mark.parametrize("text", ["0", "-1", "abc"])
def test_parse_boot_slots_rejects_bad_positions(text):
    with pytest.raises(ufs.PlanError):
        ufs.parse_boot_slots(text)


def test_plan_without_boot_slots_leaves_boot_luns_alone():
    plan = ufs.plan_from_sizes(rave2_report(), [32 * 1024 ** 3, "rest"])
    assert "manage_boot_luns" not in plan
    # LUNs 1 and 2 are the boot LUNs, so the plan skips over them.
    assert [entry["lun"] for entry in plan["luns"]] == [0, 3]
    assert not any("boot_lun_id" in entry for entry in plan["luns"])


def test_plan_may_drop_every_boot_lun():
    plan = ufs.plan_from_sizes(rave2_report(), [32 * 1024 ** 3, "rest"], [])
    assert plan["manage_boot_luns"] is True
    # With nothing reserved the plan gets the consecutive LUNs back.
    assert [entry["lun"] for entry in plan["luns"]] == [0, 1]
    assert not any(entry.get("boot_lun_id") for entry in plan["luns"])


def test_plan_assigns_boot_lun_a_then_b():
    plan = ufs.plan_from_sizes(
        rave2_report(), [32 * 1024 ** 3, 4 * 1024 ** 2, 4 * 1024 ** 2], [1, 2])
    assert [entry.get("boot_lun_id", 0) for entry in plan["luns"]] == [0, 1, 2]


def test_plan_honours_the_order_boot_luns_are_named():
    plan = ufs.plan_from_sizes(
        rave2_report(), [32 * 1024 ** 3, 4 * 1024 ** 2, 4 * 1024 ** 2], [2, 1])
    assert [entry.get("boot_lun_id", 0) for entry in plan["luns"]] == [0, 2, 1]


def test_plan_rejects_more_boot_luns_than_the_spec_allows():
    with pytest.raises(ufs.PlanError, match="boot LUNs requested"):
        ufs.plan_from_sizes(rave2_report(), [AU, AU, AU, AU], [0, 1, 2])


def test_plan_rejects_a_repeated_boot_lun():
    with pytest.raises(ufs.PlanError, match="only be named once"):
        ufs.plan_from_sizes(rave2_report(), [AU, AU], [1, 1])


def test_plan_rejects_a_boot_slot_outside_the_plan():
    with pytest.raises(ufs.PlanError, match="outside the plan"):
        ufs.plan_from_sizes(rave2_report(), [AU, AU], [5])


def test_managed_plan_reclaims_the_boot_lun_budget():
    report = rave2_report()
    assert ufs.available_alloc_units(report) == 61022 - 6
    assert ufs.available_alloc_units(report, manage_boot=True) == 61022
    # The whole budget is spendable once the boot LUNs are the plan's to place.
    plan = ufs.plan_from_sizes(report, [61022 * AU], [])
    assert plan["luns"][0]["size_bytes"] == 61022 * AU


def test_device_reported_boot_lun_limit_is_used():
    report = rave2_report()
    report["max_boot_luns"] = 1
    with pytest.raises(ufs.PlanError, match="the device allows 1"):
        ufs.plan_from_sizes(report, [AU, AU], [0, 1])


# ---------------------------------------------------------------------------
#  Per-LUN entries: identity and descriptor fields survive
# ---------------------------------------------------------------------------

def ui_entries(report):
    """What the editor sends when the user changes nothing."""
    return [
        {"lun": e["lun"], "size": e["num_alloc_units"] * AU,
         "boot_lun_id": e["boot_lun_id"], "memory_type": e["memory_type"],
         "data_reliability": e["data_reliability"],
         "write_protect": e["write_protect"],
         "logical_block_size_exp": e["logical_block_size_exp"],
         "provisioning_type": e["provisioning_type"]}
        for e in report["luns"] if e["enabled"]]


def test_no_edit_plan_restates_every_lun_exactly():
    report = rave2_report()
    plan = ufs.build_plan(report, ui_entries(report))
    assert plan["manage_boot_luns"] is True
    by_lun = {e["lun"]: e for e in plan["luns"]}
    assert sorted(by_lun) == [0, 1, 2]
    for original in report["luns"]:
        rebuilt = by_lun[original["lun"]]
        assert rebuilt["boot_lun_id"] == original["boot_lun_id"]
        assert rebuilt["memory_type"] == original["memory_type"]
        assert rebuilt["data_reliability"] == original["data_reliability"]
        assert rebuilt["write_protect"] == original["write_protect"]
        assert rebuilt["logical_block_size_exp"] == original["logical_block_size_exp"]
        assert rebuilt["provisioning_type"] == original["provisioning_type"]
        assert rebuilt["size_bytes"] == original["num_alloc_units"] * AU


def test_unstated_fields_come_from_the_device():
    # Sizes only: the enhanced boot LUNs must not be flattened to plain memory.
    report = rave2_report()
    plan = ufs.build_plan(report, [
        {"lun": e["lun"], "size": e["num_alloc_units"] * AU}
        for e in report["luns"] if e["enabled"]])
    by_lun = {e["lun"]: e for e in plan["luns"]}
    assert by_lun[1]["memory_type"] == 3
    assert by_lun[1]["data_reliability"] == 1
    assert by_lun[1]["boot_lun_id"] == 1
    assert by_lun[2]["boot_lun_id"] == 2


def test_lun_numbers_are_kept_including_gaps():
    plan = ufs.build_plan(rave2_report(), [
        {"lun": 0, "size": 32 * 1024 ** 3},
        {"lun": 5, "size": 8 * 1024 ** 3},
    ])
    assert [e["lun"] for e in plan["luns"]] == [0, 5]


def test_a_lun_may_not_be_named_twice():
    with pytest.raises(ufs.PlanError, match="named twice"):
        ufs.build_plan(rave2_report(), [{"lun": 0, "size": AU}, {"lun": 0, "size": AU}])


def test_lun_index_must_exist_on_the_device():
    with pytest.raises(ufs.PlanError, match="invalid LUN index"):
        ufs.build_plan(rave2_report(), [{"lun": 9, "size": AU}])


def test_boot_ids_come_from_the_entry_not_the_order():
    plan = ufs.build_plan(rave2_report(), [
        {"lun": 0, "size": 32 * 1024 ** 3},
        {"lun": 1, "size": AU, "boot_lun_id": 2},
        {"lun": 2, "size": AU, "boot_lun_id": 1},
    ])
    by_lun = {e["lun"]: e for e in plan["luns"]}
    assert by_lun[1]["boot_lun_id"] == 2
    assert by_lun[2]["boot_lun_id"] == 1


def test_rejects_a_repeated_boot_id():
    with pytest.raises(ufs.PlanError, match="each be assigned once"):
        ufs.build_plan(rave2_report(), [
            {"lun": 0, "size": AU, "boot_lun_id": 1},
            {"lun": 1, "size": AU, "boot_lun_id": 1},
        ])


def test_rejects_an_out_of_range_boot_id():
    with pytest.raises(ufs.PlanError, match="invalid bBootLunID"):
        ufs.build_plan(rave2_report(), [{"lun": 0, "size": AU, "boot_lun_id": 3}])


def test_legacy_plan_carries_no_descriptor_fields():
    # The target refuses them without manage_boot_luns, and preserves them itself.
    plan = ufs.plan_from_sizes(rave2_report(), [32 * 1024 ** 3])
    assert "manage_boot_luns" not in plan
    for entry in plan["luns"]:
        assert set(entry) == {"lun", "enabled", "size_bytes"}


def test_plan_from_file_parses_human_sizes(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"luns": [
        {"lun": 0, "size": "32G"},
        {"lun": 3, "size": "rest"},
    ]}))
    plan = ufs.plan_from_file(rave2_report(), str(path))
    assert plan["luns"][0]["size_bytes"] == 32 * 1024 ** 3
    assert plan["luns"][1]["size_bytes"] == "rest"
    assert [e["lun"] for e in plan["luns"]] == [0, 3]


def test_plan_from_file_reports_a_bad_document(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("not json")
    with pytest.raises(ufs.PlanError, match="cannot read plan"):
        ufs.plan_from_file(rave2_report(), str(path))


# ---------------------------------------------------------------------------
#  Restore factory default
# ---------------------------------------------------------------------------

def factory_report():
    """A RAVE2 device still in its shipped layout."""
    report = rave2_report()
    report["manufacturer_id"] = 0x01CE
    report["config_descriptor_hex"] = ufs.FACTORY_LAYOUTS[0]["hex"]
    return report


def test_builtin_factory_layout_matches_its_recorded_hash():
    # A corrupt table would silently re-provision every board to rubbish.
    for entry in ufs.FACTORY_LAYOUTS:
        raw = bytes.fromhex(entry["hex"])
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        assert len(raw) == 230


def test_known_part_restores_the_builtin_layout(tmp_path):
    report = factory_report()
    descriptor, source = ufs.restore_descriptor(report, tmp_path)
    assert source == "factory"
    assert descriptor == bytes.fromhex(ufs.FACTORY_LAYOUTS[0]["hex"])


def test_builtin_layout_wins_over_a_captured_baseline(tmp_path):
    # The baseline may have been captured after someone re-provisioned the
    # board, and nothing afterwards can tell. The known part always wins.
    report = factory_report()
    ufs.capture_baseline(report, tmp_path)
    binary, _meta = ufs.baseline_paths(report["serial"], tmp_path)
    binary.write_bytes(b"\xff" * 230)
    descriptor, source = ufs.restore_descriptor(report, tmp_path)
    assert source == "factory"
    assert descriptor != b"\xff" * 230


def test_product_trailing_space_still_matches(tmp_path):
    report = factory_report()
    report["product"] = "KLUEG8UHGC-B0E1 "
    assert ufs.restore_descriptor(report, tmp_path)[1] == "factory"


@pytest.mark.parametrize("field,value", [
    ("product", "SOMETHING-ELSE"),
    ("manufacturer_id", 0x1234),
])
def test_a_different_part_does_not_match(tmp_path, field, value):
    report = factory_report()
    report[field] = value
    assert ufs.restore_descriptor(report, tmp_path)[1] is None


@pytest.mark.parametrize("field", ["au_bytes", "budget_alloc_units", "max_number_lu"])
def test_different_geometry_does_not_match(tmp_path, field):
    # Same part string, different capacity or allocation unit: writing this
    # descriptor would describe a device that is not there.
    report = factory_report()
    report["geometry"][field] = report["geometry"][field] * 2
    assert ufs.restore_descriptor(report, tmp_path)[1] is None


def test_unknown_part_falls_back_to_the_captured_baseline(tmp_path):
    report = factory_report()
    report["product"] = "SOMETHING-ELSE"
    ufs.capture_baseline(report, tmp_path)
    descriptor, source = ufs.restore_descriptor(report, tmp_path)
    assert source == "baseline"
    assert descriptor == bytes.fromhex(ufs.FACTORY_LAYOUTS[0]["hex"])


def test_unknown_part_with_no_baseline_has_nothing_to_restore(tmp_path):
    report = factory_report()
    report["product"] = "SOMETHING-ELSE"
    assert ufs.restore_descriptor(report, tmp_path) == (None, None)
