from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest
import scripts.utm_clash_ip_target as target_module

from scripts.utm_clash_ip_target import (
    BoundVM,
    TargetVMError,
    _arp_ips_for_mac,
    ensure_exact_vm_started,
    require_exact_bound_vm,
    resolve_exact_vm_ip,
)
from scripts.vm_inventory import (
    claim_exact_available_vm,
    register_clone,
    reserve_vm_name,
    scan_inventory,
    set_available,
)


def _registered_vm(tmp_path: Path) -> tuple[Path, Path]:
    images = tmp_path / "images"
    images.mkdir()
    database = tmp_path / "runtime" / "vm-inventory.sqlite3"
    bundle = images / "gdpd.utm"
    reserve_vm_name(database, images, candidate_factory=lambda: "gdpd")
    bundle.mkdir()
    config_uuid = "11111111-1111-1111-1111-111111111111"
    mac = "02:00:00:00:00:01"
    (bundle / "config.plist").write_bytes(
        plistlib.dumps(
            {
                "Information": {"UUID": config_uuid},
                "Network": [{"MacAddress": mac}],
            }
        )
    )
    register_clone(database, "gdpd", config_uuid, bundle, mac, "a" * 64)
    set_available(database, "gdpd", True)
    scan_inventory(database, images)
    claim_exact_available_vm(database, "gdpd", "test")
    return database, images


def test_require_exact_bound_vm_checks_inventory_bundle_and_config(tmp_path: Path) -> None:
    database, images = _registered_vm(tmp_path)

    target = require_exact_bound_vm(database, images, "gdpd", "test")

    assert target.vm_name == "gdpd"
    assert target.bundle_path == (images / "gdpd.utm").resolve()
    assert target.mac_address == "02:00:00:00:00:01"


def test_require_exact_bound_vm_rejects_application_mismatch(tmp_path: Path) -> None:
    database, images = _registered_vm(tmp_path)

    with pytest.raises(TargetVMError, match="application"):
        require_exact_bound_vm(database, images, "gdpd", "other")


def test_ensure_exact_vm_started_starts_only_explicit_stopped_target() -> None:
    responses = iter(("stopped\n", "", "started\n", "running\n", "started\n"))
    commands: list[list[str]] = []

    def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, next(responses), "")

    started = ensure_exact_vm_started(
        "gdpd", run_command=run_command, sleep_fn=lambda _: None
    )

    assert started is True
    assert [command[-2:] for command in commands] == [
        ["status", "gdpd"],
        ["start", "gdpd"],
        ["status", "gdpd"],
        ["status", "gdpd"],
        ["status", "gdpd"],
    ]


def test_ensure_exact_vm_started_does_not_restart_running_target() -> None:
    responses = iter(("running\n", "started\n", "running\n", "started\n"))
    commands: list[list[str]] = []

    def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, next(responses), "")

    started = ensure_exact_vm_started(
        "gdpd", run_command=run_command, sleep_fn=lambda _: None
    )

    assert started is False
    assert all(command[-2:] == ["status", "gdpd"] for command in commands)


def test_ensure_exact_vm_started_rejects_suspended_target() -> None:
    def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "suspended\n", "")

    with pytest.raises(TargetVMError, match="suspended"):
        ensure_exact_vm_started(
            "gdpd", run_command=run_command, sleep_fn=lambda _: None
        )


def test_arp_matching_ignores_unrelated_global_macs() -> None:
    output = "\n".join(
        (
            "? (192.0.2.10) at 0:11:22:33:44:55 on bridge100 ifscope [bridge]",
            "? (192.0.2.11) at 2:0:0:0:0:1 on bridge100 ifscope [bridge]",
        )
    )

    assert _arp_ips_for_mac(output, "02:00:00:00:00:01") == {"192.0.2.11"}


def test_resolve_exact_vm_ip_uses_unique_mac_arp_when_backend_has_no_ip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = BoundVM(
        vm_name="gdpd",
        bundle_path=tmp_path / "gdpd.utm",
        config_uuid="11111111-1111-1111-1111-111111111111",
        mac_address="02:00:00:00:00:01",
    )

    def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[-2:] == ["ip-address", "gdpd"]:
            return subprocess.CompletedProcess(command, 1, "", "unsupported backend")
        if command == ["/usr/sbin/arp", "-an"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "? (192.0.2.11) at 2:0:0:0:0:1 on bridge100 ifscope [bridge]\n",
                "",
            )
        raise AssertionError(command)

    monkeypatch.setattr(
        target_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    assert resolve_exact_vm_ip(
        target, run_command=run_command, sleep_fn=lambda _: None
    ) == "192.0.2.11"
