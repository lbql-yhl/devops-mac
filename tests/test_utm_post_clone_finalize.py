from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.utm_post_clone_finalize as finalizer


def _install_success_path(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    record = {
        "bundle_path": str(Path("/tmp/abcd.utm").resolve()),
        "config_uuid": "11111111-1111-1111-1111-111111111111",
        "guest_serial_number": "SERIAL-ABCD",
        "guest_platform_uuid": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        "available": 1,
        "directory_present": 1,
        "status": "complete",
    }
    monkeypatch.setattr(finalizer, "config_identity", lambda bundle: (record["config_uuid"], "02:00:00:00:00:01"))
    monkeypatch.setattr(finalizer, "get_record", lambda database, vm_name: dict(record))
    monkeypatch.setattr(finalizer, "ensure_started", lambda vm_name: events.append("started"))
    monkeypatch.setattr(finalizer, "resolve_ip", lambda vm_name, mac: "192.0.2.10")
    monkeypatch.setattr(finalizer, "ensure_ssh", lambda user, ip: events.append("ssh"))
    monkeypatch.setattr(
        finalizer,
        "enter_guest_desktop",
        lambda user, ip, vm_name, bundle: {"FINDER": "ready"},
    )
    monkeypatch.setattr(finalizer, "verify_admin", lambda user, ip: events.append("admin"))
    monkeypatch.setattr(
        finalizer,
        "read_guest_identity",
        lambda user, ip, mac: SimpleNamespace(
            serial_number="SERIAL-ABCD",
            platform_uuid="AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
            mac="02:00:00:00:00:01",
        ),
    )
    monkeypatch.setattr(
        finalizer,
        "ssh_sudo_script",
        lambda user, ip, script: (
            "DEMO_ID=absent\nDEMO_DSCL=absent\nDEMO_STATE=absent\nDEMO_HOME=absent\n"
            "CLASH_STALE_STATE=absent\nSTALE_DEMO_PATH_PROCESS=absent\n"
            if "DEMO_STATE" in script
            else (
                "AutomaticCheckEnabled=0\nAutomaticDownload=0\n"
                "AutomaticallyInstallMacOSUpdates=0\nAutomaticallyInstallAppUpdates=0\n"
                "CriticalUpdateInstall=0\nConfigDataInstall=0\nAutoUpdate=0\n"
                "sleep=0\ndisplaysleep=0\ndisksleep=0\nscreensaver_idle=0\n"
                "askForPassword=0\nscreenLock=off\nLOCK_SCREEN_SHOW_CLOCK=0\n"
                "LOCK_SCREEN_SHOW_24_HOUR=0\nLOCK_SCREEN_SHOW_USER_PHOTO=0\n"
                "LOCK_SCREEN_SHOW_PASSWORD_HINTS=0\nLOCK_SCREEN_SHOW_MESSAGE=0\n"
                "LOCK_SCREEN_SHOW_POWER_BUTTONS=0\n"
                if "AutomaticCheckEnabled" in script
                else events.append("shutdown") or ""
            )
        ),
    )
    monkeypatch.setattr(finalizer, "ssh_script", lambda user, ip, script: "DEMO_ABSENT\n")
    monkeypatch.setattr(finalizer, "verify_copy", lambda user, ip: {"verification": "passed"})
    monkeypatch.setattr(finalizer, "ensure_stopped", lambda vm_name: events.append("stopped"))
    monkeypatch.setattr(finalizer, "update_guest_mac", lambda *args: events.append("database_mac"))
    monkeypatch.setattr(
        finalizer,
        "confirm_bundle_present",
        lambda *args: events.append("database_directory"),
        raising=False,
    )
    monkeypatch.setattr(finalizer, "set_available", lambda *args: events.append("database_available"))


def test_database_writes_happen_only_after_shutdown_is_independently_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_success_path(monkeypatch, events)

    result = finalizer.finalize(
        vm_name="abcd",
        bundle=Path("/tmp/abcd.utm"),
        expected_config_uuid="11111111-1111-1111-1111-111111111111",
        database=Path("/tmp/inventory.sqlite3"),
    )

    assert result["available"] == "1"
    assert events.index("shutdown") < events.index("stopped")
    assert events.index("stopped") < events.index("database_mac")
    assert events.index("database_mac") < events.index("database_directory")
    assert events.index("database_directory") < events.index("database_available")


def test_failed_stopped_readback_never_writes_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_success_path(monkeypatch, events)

    def fail_stopped(vm_name: str) -> None:
        events.append("stopped_failed")
        raise finalizer.FinalizationError("FINAL_STOPPED_READBACK_FAILED")

    monkeypatch.setattr(finalizer, "ensure_stopped", fail_stopped)

    with pytest.raises(finalizer.FinalizationError, match="FINAL_STOPPED_READBACK_FAILED"):
        finalizer.finalize(
            vm_name="abcd",
            bundle=Path("/tmp/abcd.utm"),
            expected_config_uuid="11111111-1111-1111-1111-111111111111",
            database=Path("/tmp/inventory.sqlite3"),
        )

    assert "database_mac" not in events
    assert "database_available" not in events


def test_finalizer_rejects_cleanup_without_dscl_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_success_path(monkeypatch, events)
    successful_sudo = finalizer.ssh_sudo_script

    def cleanup_missing_dscl(user: str, ip: str, script: str) -> str:
        if "DEMO_STATE" in script:
            return "DEMO_ID=absent\nDEMO_STATE=absent\nDEMO_HOME=absent\n"
        return successful_sudo(user, ip, script)

    monkeypatch.setattr(finalizer, "ssh_sudo_script", cleanup_missing_dscl)

    with pytest.raises(finalizer.FinalizationError, match="FINAL_DEMO_CLEANUP_NOT_VERIFIED"):
        finalizer.finalize(
            vm_name="abcd",
            bundle=Path("/tmp/abcd.utm"),
            expected_config_uuid="11111111-1111-1111-1111-111111111111",
            database=Path("/tmp/inventory.sqlite3"),
        )

    assert "shutdown" not in events
    assert "database_mac" not in events
    assert "database_available" not in events


def test_ping_readiness_retries_the_same_unique_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    return_codes = iter((1, 1, 0))
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=next(return_codes), stdout="", stderr="")

    monkeypatch.setattr(finalizer.subprocess, "run", fake_run)
    monkeypatch.setattr(finalizer.time, "sleep", lambda seconds: None)

    finalizer.ping_until_ready("192.0.2.10")

    assert len(calls) == 3
    assert all(command[-1] == "192.0.2.10" for command in calls)
