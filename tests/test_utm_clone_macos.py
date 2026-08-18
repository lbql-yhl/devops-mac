#!/usr/bin/env python3
import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.utm_clone_macos import (  # noqa: E402
    compare_manifests,
    generate_local_mac,
    new_machine_identifier,
    parse_utmctl_list,
    rewrite_config_bytes,
)
from scripts import utm_clone_macos  # noqa: E402


def test_identity_rewrite_changes_only_allowed_fields() -> None:
    hardware = b"hardware-model"
    source = {
        "Information": {"Name": "macOS", "UUID": "11111111-1111-1111-1111-111111111111"},
        "Network": [{"MacAddress": "02:00:00:00:00:01", "Mode": "Shared"}],
        "System": {"MacPlatform": {"HardwareModel": hardware, "MachineIdentifier": new_machine_identifier()}},
    }
    changed = rewrite_config_bytes(
        plistlib.dumps(source),
        "abcd",
        "22222222-2222-2222-2222-222222222222",
        "02:00:00:00:00:02",
        new_machine_identifier(),
    )
    result = plistlib.loads(changed)
    assert result["Information"]["Name"] == "abcd"
    assert result["Information"]["UUID"] == "22222222-2222-2222-2222-222222222222"
    assert result["Network"][0]["MacAddress"] == "02:00:00:00:00:02"
    assert result["System"]["MacPlatform"]["HardwareModel"] == hardware


def test_manifest_compare_and_utmctl_parser(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "file.txt").write_text("same", encoding="utf-8")
    (destination / "file.txt").write_text("same", encoding="utf-8")
    assert compare_manifests(source, destination, {"config.plist", ".submission-clone.json"}) == []
    rows = parse_utmctl_list(
        "UUID                                 Status   Name\n"
        "11111111-1111-1111-1111-111111111111 stopped  abcd\n"
    )
    assert rows == [
        {
            "uuid": "11111111-1111-1111-1111-111111111111",
            "status": "stopped",
            "name": "abcd",
        }
    ]
    assert len(generate_local_mac({"02:00:00:00:00:01"})) == 17


def test_manifest_ignores_appledouble_sidecars(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "Data").mkdir()
    (destination / "Data").mkdir()
    (source / "Data" / "disk.img").write_bytes(b"same")
    (destination / "Data" / "disk.img").write_bytes(b"same")
    # exFAT creates AppleDouble metadata beside files written in the bundle.
    (source / "._config.plist").write_bytes(b"metadata")
    (destination / "._config.plist").write_bytes(b"metadata")
    (destination / "._.submission-clone.json").write_bytes(b"metadata")
    (destination / "Data" / "._disk.img").write_bytes(b"metadata")

    assert compare_manifests(source, destination, {"config.plist", ".submission-clone.json"}) == []


def test_registration_waits_for_delayed_registry(monkeypatch, tmp_path: Path) -> None:
    bundle = tmp_path / "abcd.utm"
    rows = [{"uuid": "UUID", "status": "stopped", "name": "abcd"}]
    registries = [{"other": True}, {"ready": True}]
    sleeps: list[float] = []

    monkeypatch.setattr(
        utm_clone_macos, "target_status_rows", lambda _uuid, _name: rows
    )
    monkeypatch.setattr(
        utm_clone_macos, "read_registry", lambda: registries.pop(0)
    )
    monkeypatch.setattr(
        utm_clone_macos,
        "registry_matches",
        lambda registry, _uuid, _name, _bundle: (
            [{"match": "yes"}] if registry.get("ready") else []
        ),
    )
    monkeypatch.setattr(utm_clone_macos.time, "sleep", sleeps.append)

    actual_rows, actual_matches, registry_available = utm_clone_macos.wait_for_registration(
        "UUID", "abcd", bundle
    )

    assert actual_rows == rows
    assert actual_matches == [{"match": "yes"}]
    assert registry_available is True
    assert sleeps == [utm_clone_macos.WAIT_SECONDS]


def test_registration_accepts_cli_when_registry_domain_is_unavailable(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "abcd.utm"
    rows = [{"uuid": "UUID", "status": "stopped", "name": "abcd"}]
    sleeps: list[float] = []

    monkeypatch.setattr(
        utm_clone_macos, "target_status_rows", lambda _uuid, _name: rows
    )
    monkeypatch.setattr(utm_clone_macos, "read_registry", lambda: {})
    monkeypatch.setattr(
        utm_clone_macos,
        "registry_matches",
        lambda _registry, _uuid, _name, _bundle: [],
    )
    monkeypatch.setattr(utm_clone_macos.time, "sleep", sleeps.append)

    actual_rows, actual_matches, registry_available = (
        utm_clone_macos.wait_for_registration("UUID", "abcd", bundle)
    )

    assert actual_rows == rows
    assert actual_matches == []
    assert registry_available is False
    assert sleeps == []


def test_clone_capacity_is_checked_before_copy(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.utm"
    images = tmp_path / "images"
    source.mkdir()
    images.mkdir()
    monkeypatch.setattr(
        utm_clone_macos, "package_allocated_bytes", lambda _source: 100
    )
    monkeypatch.setattr(
        utm_clone_macos.shutil,
        "disk_usage",
        lambda _images: SimpleNamespace(
            free=100 + utm_clone_macos.STORAGE_RESERVE_BYTES - 1
        ),
    )

    with pytest.raises(utm_clone_macos.CloneError, match="insufficient clone storage"):
        utm_clone_macos.require_clone_capacity(source, images)


if __name__ == "__main__":
    raise SystemExit(0)
