from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from scripts import check_host_vm_dependencies as dependencies
from scripts.check_host_vm_dependencies import (
    GUEST_LABELS,
    HOST_LABELS,
    _arp_ips_for_mac,
    _read_active_clone_context,
    _resolve_running_vm_ip,
    check_dependencies,
    render_result,
    validate_vm_name,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def configured_guest_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBMISSION_GUEST_PASSWORD", "9072")


def test_render_result_has_only_the_two_allowed_forms() -> None:
    assert render_result("宿主Python", True) == "宿主Python已安装"
    assert render_result("虚拟机Xcode", False) == "虚拟机Xcode未安装"


@pytest.mark.parametrize("value", ("abc", "abcde", "Abcd", "1234", "ab-c", ""))
def test_validate_vm_name_rejects_everything_except_four_lowercase_letters(
    value: str,
) -> None:
    with pytest.raises(ValueError, match="four lowercase letters"):
        validate_vm_name(value)


def test_check_dependencies_keeps_host_results_and_adds_connected_guest_results() -> None:
    host_results = {label: label == "宿主Python" for label in HOST_LABELS}
    guest_results = {label: label == "虚拟机Python" for label in GUEST_LABELS}

    results = check_dependencies(
        "abcd",
        host_checker=lambda: host_results,
        guest_checker=lambda vm_name: guest_results if vm_name == "abcd" else None,
    )

    assert list(results) == [*HOST_LABELS, *GUEST_LABELS]
    assert results["宿主Python"] is True
    assert results["宿主Node.js"] is False
    assert results["虚拟机Python"] is True
    assert results["虚拟机Xcode"] is False


def test_failed_guest_connection_marks_every_guest_item_uninstalled() -> None:
    host_results = {label: True for label in HOST_LABELS}

    results = check_dependencies(
        "abcd",
        host_checker=lambda: host_results,
        guest_checker=lambda _vm_name: None,
    )

    assert results["虚拟机连接"] is False
    assert all(results[label] is False for label in GUEST_LABELS)
    assert all(results[label] is True for label in HOST_LABELS)


def test_arp_match_requires_the_registered_mac() -> None:
    output = "\n".join(
        (
            "? (192.168.64.20) at aa:b:cc:d:ee:f on bridge100 ifscope [bridge]",
            "? (192.168.64.21) at 11:22:33:44:55:66 on bridge100 ifscope [bridge]",
        )
    )

    assert _arp_ips_for_mac(output, "aa:0b:cc:0d:ee:0f") == {"192.168.64.20"}


def test_vm_ip_falls_back_to_unique_registered_mac_when_backend_has_no_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1:] == ["status", "stgf"]:
            return subprocess.CompletedProcess(command, 0, "started\n", "")
        if command[1:] == ["ip-address", "stgf"]:
            return subprocess.CompletedProcess(command, 1, "", "unsupported")
        if command == ["/usr/sbin/arp", "-an"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "? (192.168.64.56) at ce:c3:e8:e0:c1:c on bridge100 ifscope [bridge]\n",
                "",
            )
        raise AssertionError(command)

    monkeypatch.setattr(
        "scripts.check_host_vm_dependencies._quiet_run",
        fake_run,
    )

    assert _resolve_running_vm_ip("stgf", "ce:c3:e8:e0:c1:0c") == "192.168.64.56"


def test_dependency_checker_uses_shared_broker_without_plaintext_child_environment(
    tmp_path: Path,
) -> None:
    dummy_value = "9072"
    environment = dependencies.password_environment(
        {"PATH": os.environ["PATH"], "SUBMISSION_GUEST_PASSWORD": dummy_value},
        runtime_dir=tmp_path,
    )

    assert environment["SSH_ASKPASS_REQUIRE"] == "force"
    assert "DEPENDENCY_CHECK_ASKPASS" not in environment
    assert "SUBMISSION_GUEST_PASSWORD" not in environment
    assert environment["SUBMISSION_SSH_ASKPASS_SOCKET"]
    assert environment["SUBMISSION_SSH_ASKPASS_CAPABILITY"]
    assert Path(environment["SSH_ASKPASS"]).stat().st_mode & 0o111

    completed = subprocess.run(
        [environment["SSH_ASKPASS"]],
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    assert completed.stdout == dummy_value + "\n"


def test_shared_broker_explicit_environment_does_not_fall_back_to_ambient_password(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="missing required host setting"):
        dependencies.password_environment(
            {"PATH": os.environ["PATH"]}, runtime_dir=tmp_path
        )


def test_dependency_checker_source_has_no_embedded_askpass_value() -> None:
    source = (ROOT / "scripts" / "check_host_vm_dependencies.py").read_text(
        encoding="utf-8"
    )

    assert 'print("1234")' not in source
    assert "DEPENDENCY_CHECK_ASKPASS" not in source
    assert "guest_password(environ=os.environ)" not in source


def test_dependency_checker_contains_no_utm_lifecycle_command() -> None:
    source = (ROOT / "scripts" / "check_host_vm_dependencies.py").read_text(
        encoding="utf-8"
    )
    forbidden = (
        "utmctl start",
        "utmctl stop",
        "utmctl suspend",
        "utmctl restart",
        "open -a UTM",
        "open ",
        ".utm",
        ".write_text(",
        ".write_bytes(",
        ".mkdir(",
        "os.replace(",
    )
    for fragment in forbidden:
        assert fragment not in source


def test_active_clone_context_requires_exact_active_record_and_steps_2_through_6(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "stgf.utm"
    bundle.mkdir()
    config_uuid = "764188C2-FC3C-4BC7-BBF1-320827251EB2"
    (bundle / "config.plist").write_bytes(
        __import__("plistlib").dumps(
            {
                "Information": {"UUID": config_uuid},
                "Network": [{"MacAddress": "ce:c3:e8:e0:c1:0c"}],
            }
        )
    )
    active = tmp_path / "active.json"
    active.write_text(
        __import__("json").dumps(
            {
                "schema_version": 1,
                "vm_name": "stgf",
                "config_uuid": config_uuid,
                "bundle_path": str(bundle),
            }
        ),
        encoding="utf-8",
    )
    active.chmod(0o600)
    state = tmp_path / "stgf.json"
    state.write_text(
        __import__("json").dumps(
            {
                "vm_name": "stgf",
                "bundle": str(bundle),
                "config_uuid": config_uuid,
                "steps": [2, 3, 4, 5, 6],
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)
    record = {
        "vm_name": "stgf",
        "bundle_path": str(bundle),
        "config_uuid": config_uuid,
        "mac_address": "ce:c3:e8:e0:c1:0c",
        "status": "complete",
        "directory_present": 0,
        "reusable": 0,
        "available": 0,
        "application_name": None,
    }
    monkeypatch.setattr(
        "scripts.check_host_vm_dependencies.ACTIVE_CLONE_PATH", active
    )
    monkeypatch.setattr(
        "scripts.check_host_vm_dependencies.POST_CLONE_STATE_DIR", tmp_path
    )

    assert _read_active_clone_context("stgf", record) == "ce:c3:e8:e0:c1:0c"

    state.write_text(
        __import__("json").dumps(
            {
                "vm_name": "stgf",
                "bundle": str(bundle),
                "config_uuid": config_uuid,
                "steps": [2, 3, 4, 5],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="steps"):
        _read_active_clone_context("stgf", record)
