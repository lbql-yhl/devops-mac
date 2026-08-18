#!/usr/bin/env python3
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.vm_inventory import (  # noqa: E402
    InventoryError,
    claim_available_vm,
    claim_exact_available_vm,
    get_record,
    mark_used,
    register_clone,
    register_existing,
    record_guest_identity,
    rebind_application_name,
    reserve_vm_name,
    scan_inventory,
    set_available,
    update_guest_mac,
)


def test_reservation_uses_sqlite_and_excludes_existing_bundle(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    (images / "abcd.utm").mkdir()
    database = tmp_path / "runtime" / "vm-inventory.sqlite3"
    candidates = iter(("abcd", "abcd", "efgh"))
    name = reserve_vm_name(database, images, candidate_factory=lambda: next(candidates))
    assert name == "efgh"
    assert database.stat().st_mode & 0o777 == 0o600
    assert get_record(database, "efgh")["status"] == "reserved"


def test_reservation_persists_uniqueness_and_registers_identity(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    database = tmp_path / "inventory.sqlite3"
    candidates = iter(("abcd", "efgh"))
    first = reserve_vm_name(database, images, candidate_factory=lambda: next(candidates))
    assert first == "abcd"
    try:
        reserve_vm_name(database, images, candidate_factory=lambda: "abcd")
    except InventoryError as error:
        assert "unique" in str(error).lower() or "exhausted" in str(error).lower()
    else:
        raise AssertionError("duplicate name reservation was accepted")
    register_clone(
        database,
        "abcd",
        "11111111-1111-1111-1111-111111111111",
        images / "abcd.utm",
        "02:00:00:00:00:01",
        "a" * 64,
    )
    record = get_record(database, "abcd")
    assert record["status"] == "complete"
    assert record["config_uuid"] == "11111111-1111-1111-1111-111111111111"


def test_scan_marks_cleaned_name_reusable_and_reports_unregistered(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    database = tmp_path / "inventory.sqlite3"
    reserve_vm_name(database, images, candidate_factory=lambda: "abcd")
    (images / "abcd.utm").mkdir()
    register_clone(
        database,
        "abcd",
        "11111111-1111-1111-1111-111111111111",
        images / "abcd.utm",
        "02:00:00:00:00:01",
        "a" * 64,
    )
    (images / "efgh.utm").mkdir()
    (images / "macOS.utm").mkdir()
    result = scan_inventory(database, images, now="2026-07-29T00:00:00+00:00")
    assert result["unregistered"] == [{"vm_name": "efgh", "bundle_path": str(images / "efgh.utm")}]
    assert result["unregistered_available"] == 0
    assert result["master_paths"] == [str(images / "macOS.utm")]
    assert result["newly_reusable"] == []
    (images / "abcd.utm").rmdir()
    result = scan_inventory(database, images, now="2026-07-30T00:00:00+00:00")
    assert result["newly_reusable"] == ["abcd"]
    assert get_record(database, "abcd")["reusable"] == 1
    assert reserve_vm_name(database, images, candidate_factory=lambda: "abcd") == "abcd"


def test_usage_application_and_available_flags(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    database = tmp_path / "inventory.sqlite3"
    reserve_vm_name(database, images, candidate_factory=lambda: "abcd")
    set_available(database, "abcd", True)
    mark_used(database, "abcd", "示例应用", used_at="2026-07-29T01:00:00+00:00")
    record = get_record(database, "abcd")
    assert record["application_name"] == "示例应用"
    assert record["last_used_at"] == "2026-07-29T01:00:00+00:00"
    assert record["available"] == 0


def test_claim_available_vm_atomically_binds_first_complete_present_vm(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    database = tmp_path / "runtime" / "vm-inventory.sqlite3"
    for name, suffix in (("abcd", "1"), ("efgh", "2")):
        bundle = images / f"{name}.utm"
        reserve_vm_name(database, images, candidate_factory=lambda value=name: value)
        bundle.mkdir()
        register_clone(
            database,
            name,
            f"{suffix * 8}-{suffix * 4}-{suffix * 4}-{suffix * 4}-{suffix * 12}",
            bundle,
            f"02:00:00:00:00:0{suffix}",
            suffix * 64,
        )
        set_available(database, name, True)
    scan_inventory(database, images)

    claimed = claim_available_vm(database, "StringBed", used_at="2026-07-31T00:00:00+00:00")

    assert claimed == "abcd"
    first = get_record(database, "abcd")
    second = get_record(database, "efgh")
    assert first["application_name"] == "StringBed"
    assert first["available"] == 0
    assert first["last_used_at"] == "2026-07-31T00:00:00+00:00"
    assert second["application_name"] is None
    assert second["available"] == 1


def test_claim_available_vm_reuses_same_application_after_feishu_retry(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    database = tmp_path / "runtime" / "vm-inventory.sqlite3"
    bundle = images / "abcd.utm"
    reserve_vm_name(database, images, candidate_factory=lambda: "abcd")
    bundle.mkdir()
    register_clone(
        database,
        "abcd",
        "11111111-1111-1111-1111-111111111111",
        bundle,
        "02:00:00:00:00:01",
        "a" * 64,
    )
    set_available(database, "abcd", True)
    scan_inventory(database, images)

    assert claim_available_vm(database, "StringBed") == "abcd"
    assert claim_available_vm(database, "StringBed") == "abcd"


def test_claim_exact_available_vm_binds_only_the_named_complete_vm(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    database = tmp_path / "runtime" / "vm-inventory.sqlite3"
    for name, suffix in (("abcd", "1"), ("efgh", "2")):
        bundle = images / f"{name}.utm"
        reserve_vm_name(database, images, candidate_factory=lambda value=name: value)
        bundle.mkdir()
        register_clone(
            database,
            name,
            f"{suffix * 8}-{suffix * 4}-{suffix * 4}-{suffix * 4}-{suffix * 12}",
            bundle,
            f"02:00:00:00:00:0{suffix}",
            suffix * 64,
        )
        set_available(database, name, True)
    scan_inventory(database, images)

    claimed = claim_exact_available_vm(
        database, "efgh", "test", used_at="2026-08-04T00:00:00+00:00"
    )

    assert claimed == "efgh"
    assert get_record(database, "abcd")["available"] == 1
    record = get_record(database, "efgh")
    assert record["application_name"] == "test"
    assert record["available"] == 0
    assert record["last_used_at"] == "2026-08-04T00:00:00+00:00"
    assert claim_exact_available_vm(database, "efgh", "test") == "efgh"


def test_claim_exact_available_vm_rejects_unavailable_or_other_application(
    tmp_path: Path,
) -> None:
    images = tmp_path / "images"
    images.mkdir()
    database = tmp_path / "inventory.sqlite3"
    bundle = images / "abcd.utm"
    reserve_vm_name(database, images, candidate_factory=lambda: "abcd")
    bundle.mkdir()
    register_clone(
        database,
        "abcd",
        "11111111-1111-1111-1111-111111111111",
        bundle,
        "02:00:00:00:00:01",
        "a" * 64,
    )
    scan_inventory(database, images)

    try:
        claim_exact_available_vm(database, "abcd", "test")
    except InventoryError as error:
        assert "available" in str(error).lower()
    else:
        raise AssertionError("unavailable VM was claimed")

    set_available(database, "abcd", True)
    assert claim_exact_available_vm(database, "abcd", "应用甲") == "abcd"
    try:
        claim_exact_available_vm(database, "abcd", "test")
    except InventoryError as error:
        assert "application" in str(error).lower()
    else:
        raise AssertionError("VM was rebound to another application")


def test_used_vm_cannot_be_rebound_to_a_different_application(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    database = tmp_path / "inventory.sqlite3"
    reserve_vm_name(database, images, candidate_factory=lambda: "abcd")
    mark_used(database, "abcd", "应用甲", used_at="2026-07-29T01:00:00+00:00")
    try:
        mark_used(database, "abcd", "应用乙", used_at="2026-07-29T02:00:00+00:00")
    except InventoryError as error:
        assert "application" in str(error).lower()
    else:
        raise AssertionError("used VM was rebound to a different application")


def test_explicit_application_rename_rebinds_only_the_exact_old_name(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    database = tmp_path / "inventory.sqlite3"
    bundle = images / "abcd.utm"
    reserve_vm_name(database, images, candidate_factory=lambda: "abcd")
    bundle.mkdir()
    register_clone(
        database,
        "abcd",
        "11111111-1111-1111-1111-111111111111",
        bundle,
        "02:00:00:00:00:01",
        "a" * 64,
    )
    scan_inventory(database, images)
    set_available(database, "abcd", True)
    mark_used(database, "abcd", "RainyDay")

    rebind_application_name(database, "abcd", "RainyDay", "RainyDay-Kids")

    record = get_record(database, "abcd")
    assert record["application_name"] == "RainyDay-Kids"
    assert record["available"] == 0
    try:
        rebind_application_name(database, "abcd", "WrongOldName", "AnotherName")
    except InventoryError as error:
        assert "old application" in str(error).lower()
    else:
        raise AssertionError("application rename accepted a mismatched old name")


def test_register_existing_is_idempotent_and_non_test_app_names_are_unique(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    database = tmp_path / "inventory.sqlite3"
    for name in ("abcd", "efgh"):
        (images / f"{name}.utm").mkdir()
    register_existing(
        database,
        "abcd",
        "11111111-1111-1111-1111-111111111111",
        images / "abcd.utm",
        "02:00:00:00:00:01",
        "a" * 64,
        "test",
        vm_created_at="2026-07-29T00:00:00+00:00",
    )
    register_existing(
        database,
        "abcd",
        "11111111-1111-1111-1111-111111111111",
        images / "abcd.utm",
        "02:00:00:00:00:01",
        "a" * 64,
        "test",
        vm_created_at="2026-07-29T00:00:00+00:00",
    )
    register_existing(
        database,
        "efgh",
        "22222222-2222-2222-2222-222222222222",
        images / "efgh.utm",
        "02:00:00:00:00:02",
        "b" * 64,
        "唯一应用",
    )
    try:
        register_existing(
            database,
            "efgh",
            "22222222-2222-2222-2222-222222222222",
            images / "efgh.utm",
            "02:00:00:00:00:02",
            "b" * 64,
            "另一个应用",
        )
    except InventoryError as error:
        assert "application" in str(error).lower()
    else:
        raise AssertionError("existing VM application conflict was accepted")


def test_guest_identity_is_recorded_and_each_code_is_unique(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    database = tmp_path / "inventory.sqlite3"
    for name in ("abcd", "efgh"):
        reserve_vm_name(database, images, candidate_factory=lambda name=name: name)
        (images / f"{name}.utm").mkdir()
        register_clone(
            database,
            name,
            "11111111-1111-1111-1111-111111111111" if name == "abcd" else "22222222-2222-2222-2222-222222222222",
            images / f"{name}.utm",
            "02:00:00:00:00:01" if name == "abcd" else "02:00:00:00:00:02",
            ("a" if name == "abcd" else "b") * 64,
        )
    record_guest_identity(
        database,
        "abcd",
        "SERIAL-ABCD",
        "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        "02:00:00:00:10:01",
    )
    record = get_record(database, "abcd")
    assert record["guest_serial_number"] == "SERIAL-ABCD"
    assert record["guest_platform_uuid"] == "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
    assert record["guest_mac_address"] == "02:00:00:00:10:01"
    assert len(record["guest_identity_sha256"]) == 64
    try:
        record_guest_identity(
            database,
            "efgh",
            "SERIAL-ABCD",
            "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB",
            "02:00:00:00:10:02",
        )
    except InventoryError as error:
        assert "guest identity" in str(error).lower()
    else:
        raise AssertionError("duplicate guest serial was accepted")


def test_guest_mac_can_be_updated_after_utm_network_randomization(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    database = tmp_path / "inventory.sqlite3"
    reserve_vm_name(database, images, candidate_factory=lambda: "abcd")
    (images / "abcd.utm").mkdir()
    register_clone(
        database,
        "abcd",
        "11111111-1111-1111-1111-111111111111",
        images / "abcd.utm",
        "02:00:00:00:00:01",
        "a" * 64,
    )
    record_guest_identity(database, "abcd", "SERIAL-ABCD", "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", "02:00:00:00:10:01")
    update_guest_mac(database, "abcd", "02:00:00:00:10:02")
    assert get_record(database, "abcd")["guest_mac_address"] == "02:00:00:00:10:02"


if __name__ == "__main__":
    raise SystemExit(0)
