from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "utm_clone_and_initialize.py"


def load_module():
    assert SCRIPT.is_file(), f"missing combined workflow entry: {SCRIPT}"
    spec = importlib.util.spec_from_file_location("utm_clone_and_initialize", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verified_clone_output(vm_name: str = "abcd") -> str:
    return (
        "开始执行：utm-vm-clone-step-01\n"
        "步骤1已操作\n"
        "执行成功：utm-vm-clone-step-01；"
        f"STEP_01_CLONE=verified;VM_NAME={vm_name}"
    )


def test_parse_verified_clone_vm_name_requires_unique_verified_output() -> None:
    module = load_module()
    assert module.parse_verified_clone_vm_name(verified_clone_output()) == "abcd"

    invalid_outputs = (
        verified_clone_output().replace(";VM_NAME=abcd", ""),
        verified_clone_output() + ";VM_NAME=wxyz",
        verified_clone_output("ABC1"),
        verified_clone_output().replace("STEP_01_CLONE=verified;", ""),
    )
    for output in invalid_outputs:
        try:
            module.parse_verified_clone_vm_name(output)
        except module.CloneInitializeError:
            pass
        else:
            raise AssertionError("invalid clone output was accepted")


def test_combined_workflow_preserves_invoked_virtualenv_interpreter() -> None:
    module = load_module()
    assert module.PYTHON == Path(sys.executable).absolute()


def test_combined_workflow_overwrites_vm_name_for_initialization(monkeypatch, tmp_path) -> None:
    module = load_module()
    monkeypatch.setattr(module, "ACTIVE_WORKFLOW_PATH", tmp_path / "active.json")
    monkeypatch.setattr(module, "write_active_workflow", lambda record: None)
    monkeypatch.setattr(module, "clear_active_workflow", lambda: None)
    monkeypatch.setattr(module, "verify_completed_inventory", lambda vm_name: None)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_subprocess_run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=verified_clone_output("wxyz") + "\n",
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("VM_NAME", "oldx")
    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    assert module.run_workflow() == 0
    assert os.environ["VM_NAME"] == "wxyz"
    assert len(calls) == 2
    assert calls[1][1]["env"]["VM_NAME"] == "wxyz"
    assert "--vm-name" not in calls[1][0]
    assert calls[1][0] == [str(module.PYTHON), str(module.POST_CLONE_SCRIPT)]


def test_combined_workflow_reports_step_1_completion(monkeypatch, tmp_path, capsys) -> None:
    module = load_module()
    monkeypatch.setattr(module, "ACTIVE_WORKFLOW_PATH", tmp_path / "active.json")
    monkeypatch.setattr(module, "write_active_workflow", lambda record: None)
    monkeypatch.setattr(module, "clear_active_workflow", lambda: None)
    monkeypatch.setattr(module, "verify_completed_inventory", lambda vm_name: None)
    calls = 0

    def fake_subprocess_run(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                command, 0, stdout=verified_clone_output("wxyz") + "\n", stderr=""
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    assert module.run_workflow() == 0
    assert capsys.readouterr().out.splitlines() == ["步骤1已操作"]


def test_combined_workflow_delegates_retry_to_the_step_handoff(monkeypatch, tmp_path) -> None:
    module = load_module()
    monkeypatch.setattr(module, "ACTIVE_WORKFLOW_PATH", tmp_path / "active.json")
    monkeypatch.setattr(module, "write_active_workflow", lambda record: None)
    monkeypatch.setattr(module, "clear_active_workflow", lambda: None)
    monkeypatch.setattr(module, "verify_completed_inventory", lambda vm_name: None)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_subprocess_run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=verified_clone_output("wxyz") + "\n",
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    assert module.run_workflow() == 0
    assert len(calls) == 2
    assert all(call[1]["env"]["VM_NAME"] == "wxyz" for call in calls[1:])
    assert all(
        call[0] == [str(module.PYTHON), str(module.POST_CLONE_SCRIPT)]
        for call in calls[1:]
    )


def test_combined_workflow_reports_single_handoff_failure(monkeypatch, tmp_path, capsys) -> None:
    module = load_module()
    monkeypatch.setattr(module, "ACTIVE_WORKFLOW_PATH", tmp_path / "active.json")
    monkeypatch.setattr(module, "write_active_workflow", lambda record: None)
    monkeypatch.setattr(module, "clear_active_workflow", lambda: None)
    calls: list[list[str]] = []

    def fake_subprocess_run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=verified_clone_output("wxyz") + "\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="UTM_POST_CLONE=blocked: DESKTOP_STATE_NOT_READY\n",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    assert module.run_workflow() == 1
    assert len(calls) == 2
    assert "INITIALIZE_FAILED:DESKTOP_STATE_NOT_READY" in capsys.readouterr().err


def test_combined_workflow_captures_handoff_error_detail(monkeypatch, tmp_path) -> None:
    module = load_module()
    monkeypatch.setattr(module, "ACTIVE_WORKFLOW_PATH", tmp_path / "active.json")
    monkeypatch.setattr(module, "write_active_workflow", lambda record: None)
    calls: list[dict[str, object]] = []

    def fake_subprocess_run(command, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                command, 0, stdout=verified_clone_output("wxyz") + "\n", stderr=""
            )
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="开始执行：utm-vm-clone-step-02\n",
            stderr="执行报错：utm-vm-clone-step-02；connection refused\n",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)
    assert module.run_workflow() == 1
    assert calls[1]["capture_output"] is True


def test_combined_workflow_resumes_exact_active_vm_without_cloning(monkeypatch, tmp_path) -> None:
    module = load_module()
    active_path = tmp_path / "active.json"
    active_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "vm_name": "stgf",
                "config_uuid": "UUID",
                "bundle_path": "/exact/stgf.utm",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ACTIVE_WORKFLOW_PATH", active_path)
    monkeypatch.setattr(
        module,
        "_inventory_record",
        lambda vm_name: {
            "vm_name": vm_name,
            "status": "complete",
            "available": 0,
            "reusable": 0,
            "application_name": None,
            "config_uuid": "UUID",
            "bundle_path": "/exact/stgf.utm",
        },
    )
    monkeypatch.setattr(
        module,
        "_active_inventory_record",
        lambda vm_name: {
            "vm_name": vm_name,
            "config_uuid": "UUID",
            "bundle_path": "/exact/stgf.utm",
        },
    )
    monkeypatch.setattr(module, "verify_completed_inventory", lambda vm_name: None)
    monkeypatch.setattr(module, "clear_active_workflow", lambda: None)
    calls: list[list[str]] = []

    def fake_subprocess_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    assert module.run_workflow() == 0
    assert calls == [[str(module.PYTHON), str(module.POST_CLONE_SCRIPT)]]
    assert os.environ["VM_NAME"] == "stgf"


def test_finished_active_record_is_cleared_as_stale(monkeypatch, tmp_path) -> None:
    module = load_module()
    active_path = tmp_path / "active.json"
    active_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "vm_name": "stgf",
                "config_uuid": "UUID",
                "bundle_path": "/exact/stgf.utm",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ACTIVE_WORKFLOW_PATH", active_path)
    monkeypatch.setattr(
        module,
        "_inventory_record",
        lambda vm_name: {
            "vm_name": vm_name,
            "status": "complete",
            "available": 1,
            "reusable": 0,
            "application_name": None,
            "config_uuid": "UUID",
            "bundle_path": "/exact/stgf.utm",
            "directory_present": 1,
        },
    )

    assert module.load_active_workflow() is None
    assert not active_path.exists()


def test_assigned_active_record_is_cleared_as_stale(monkeypatch, tmp_path) -> None:
    module = load_module()
    active_path = tmp_path / "active.json"
    active_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "vm_name": "stgf",
                "config_uuid": "UUID",
                "bundle_path": "/exact/stgf.utm",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ACTIVE_WORKFLOW_PATH", active_path)
    monkeypatch.setattr(
        module,
        "_inventory_record",
        lambda vm_name: {
            "vm_name": vm_name,
            "status": "complete",
            "available": 0,
            "reusable": 0,
            "application_name": "TrailRation",
            "config_uuid": "UUID",
            "bundle_path": "/exact/stgf.utm",
            "directory_present": 0,
        },
    )

    assert module.load_active_workflow() is None
    assert not active_path.exists()


def test_combined_workflow_does_not_initialize_after_clone_failure(
    monkeypatch, tmp_path, capsys
) -> None:
    module = load_module()
    monkeypatch.setattr(module, "ACTIVE_WORKFLOW_PATH", tmp_path / "active.json")
    calls: list[list[str]] = []

    def fake_subprocess_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="clone failed\n")

    monkeypatch.setenv("VM_NAME", "oldx")
    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    assert module.run_workflow() == 1
    assert "VM_NAME" not in os.environ
    assert len(calls) == 1
    assert "CLONE_FAILED:clone failed" in capsys.readouterr().err


def test_combined_workflow_help_is_side_effect_free(monkeypatch, capsys) -> None:
    module = load_module()

    def forbidden_subprocess_run(*args, **kwargs):
        raise AssertionError("--help must not start cloning")

    monkeypatch.setattr(module.subprocess, "run", forbidden_subprocess_run)

    with pytest.raises(SystemExit) as exit_info:
        module.main(["--help"])

    assert exit_info.value.code == 0
    assert "usage:" in capsys.readouterr().out
