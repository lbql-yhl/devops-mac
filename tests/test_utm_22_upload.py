from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import utm_22_upload
from scripts.utm_22_upload import (
    UploadTarget,
    UploadTargetError,
    _parse_args,
    _run_upload,
    choose_archive,
    resolve_manual_target,
    resolve_run_target,
    ssh_recovery_action,
)


def test_upload_node_process_uses_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def runner(command, **kwargs):
        observed["command"] = command
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "BUILD_UPLOAD_FINAL_STATE=COMPLETE\n"
                "BUILD_PROCESSING_STATE=VALID\n"
            ),
        )

    monkeypatch.setattr(utm_22_upload.subprocess, "run", runner)
    monkeypatch.setattr(utm_22_upload, "password_environment", lambda: {})

    _run_upload(
        UploadTarget("SlopeRight", "gvby", "manual:SlopeRight-gvby"),
        "192.168.64.68",
        "/Users/example/Downloads/utm_22_distribute.mjs",
        {
            "path": "/Archives/SlopeRight.xcarchive",
            "version": "1.0",
            "build": "1",
        },
        "/Users/example/Downloads/apple-store-bm/config/prod.yml",
        1800,
    )

    remote_command = observed["command"][-1]
    assert remote_command.startswith(
        "/usr/bin/env NODE_USE_ENV_PROXY=1 /usr/local/bin/node "
    )


def test_resolve_run_target_requires_one_owned_exact_binding() -> None:
    payload = {
        "runs": [
            {
                "id": "submission-1",
                "app_name": "PitchPan",
                "vm_name": "oxnt",
                "host_machine": "host-a",
                "submission_data": {
                    "app_name": "PitchPan",
                    "host_machine": "host-a",
                },
            }
        ]
    }

    target = resolve_run_target(payload, "submission-1", "host-a")

    assert target.app_name == "PitchPan"
    assert target.vm_name == "oxnt"
    assert target.attempt_owner == "submission-1"


def test_resolve_run_target_rejects_host_mismatch() -> None:
    payload = {
        "runs": [
            {
                "id": "submission-1",
                "app_name": "PitchPan",
                "vm_name": "oxnt",
                "submission_data": {
                    "app_name": "PitchPan",
                    "host_machine": "host-b",
                },
            }
        ]
    }

    with pytest.raises(UploadTargetError, match="RUN_HOST_OWNERSHIP_MISMATCH"):
        resolve_run_target(payload, "submission-1", "host-a")


def test_resolve_manual_target_matches_full_app_vm_identity(tmp_path: Path) -> None:
    database = tmp_path / "vm-inventory.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE vm_inventory "
            "(vm_name TEXT, application_name TEXT, status TEXT, "
            "directory_present INTEGER, reusable INTEGER, available INTEGER)"
        )
        connection.execute(
            "INSERT INTO vm_inventory VALUES (?, ?, ?, ?, ?, ?)",
            ("oxnt", "Pitch-Pan", "complete", 1, 0, 0),
        )

    target = resolve_manual_target(database, "Pitch-Pan-oxnt")

    assert target.app_name == "Pitch-Pan"
    assert target.vm_name == "oxnt"
    assert target.attempt_owner == "manual:Pitch-Pan-oxnt"


def test_resolve_manual_target_rejects_unbound_or_non_complete_vm(tmp_path: Path) -> None:
    database = tmp_path / "vm-inventory.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE vm_inventory "
            "(vm_name TEXT, application_name TEXT, status TEXT, "
            "directory_present INTEGER, reusable INTEGER, available INTEGER)"
        )
        connection.execute(
            "INSERT INTO vm_inventory VALUES (?, ?, ?, ?, ?, ?)",
            ("oxnt", "PitchPan", "complete", 1, 0, 1),
        )

    with pytest.raises(UploadTargetError, match="TARGET_MATCH_COUNT=0"):
        resolve_manual_target(database, "PitchPan-oxnt")


def test_choose_archive_requires_one_app_and_bundle_match() -> None:
    candidates = [
        {
            "path": "/Archives/PitchPan.xcarchive",
            "app_name": "PitchPan",
            "bundle_id": "io.pitchpan.example",
            "version": "1.0.0",
            "build": "1",
        },
        {
            "path": "/Archives/Other.xcarchive",
            "app_name": "Other",
            "bundle_id": "io.other.example",
            "version": "1.0.0",
            "build": "1",
        },
    ]

    selected = choose_archive(candidates, "PitchPan", "io.pitchpan.example")

    assert selected["path"] == "/Archives/PitchPan.xcarchive"


def test_choose_archive_never_guesses_latest_when_multiple_match() -> None:
    candidates = [
        {
            "path": f"/Archives/PitchPan-{index}.xcarchive",
            "app_name": "PitchPan",
            "bundle_id": "io.pitchpan.example",
            "version": "1.0.0",
            "build": "1",
        }
        for index in (1, 2)
    ]

    with pytest.raises(UploadTargetError, match="ARCHIVE_MATCH_COUNT=2"):
        choose_archive(candidates, "PitchPan", "io.pitchpan.example")


def test_choose_archive_accepts_one_exact_path_when_multiple_match() -> None:
    candidates = [
        {
            "path": f"/Archives/PitchPan-{index}.xcarchive",
            "app_name": "PitchPan",
            "bundle_id": "io.pitchpan.example",
            "version": "1.0.0",
            "build": "2",
        }
        for index in (1, 2)
    ]

    selected = choose_archive(
        candidates,
        "PitchPan",
        "io.pitchpan.example",
        archive_path="/Archives/PitchPan-2.xcarchive",
    )

    assert selected["path"] == "/Archives/PitchPan-2.xcarchive"


def test_choose_archive_rejects_nonmatching_exact_path() -> None:
    candidates = [
        {
            "path": "/Archives/PitchPan-2.xcarchive",
            "app_name": "PitchPan",
            "bundle_id": "io.pitchpan.example",
            "version": "1.0.0",
            "build": "2",
        }
    ]

    with pytest.raises(UploadTargetError, match="ARCHIVE_MATCH_COUNT=0"):
        choose_archive(
            candidates,
            "PitchPan",
            "io.pitchpan.example",
            archive_path="/Archives/PitchPan-missing.xcarchive",
        )


def test_archive_path_is_manual_target_only() -> None:
    with pytest.raises(SystemExit):
        _parse_args(
            [
                "--run-id",
                "submission-1",
                "--archive-path",
                "/Archives/PitchPan-2.xcarchive",
            ]
        )


def test_ssh_recovery_resumes_same_attempt_after_transport_loss() -> None:
    assert ssh_recovery_action(255, attempt_exists=True, remote_process_running=False) == "resume_attempt"
    assert ssh_recovery_action(255, attempt_exists=False, remote_process_running=True) == "wait_same_process"
    assert ssh_recovery_action(255, attempt_exists=False, remote_process_running=False) == "prove_not_executed"
    assert ssh_recovery_action(1, attempt_exists=True, remote_process_running=False) == "stop"
