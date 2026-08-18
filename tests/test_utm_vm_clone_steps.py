from __future__ import annotations

import importlib.util
from pathlib import Path
import plistlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

import scripts.utm_vm_clone_steps as clone_steps
import scripts.utm_post_clone_guest as post_clone_guest


ROOT = Path(__file__).resolve().parents[1]
POST_CLONE = ROOT / "scripts" / "utm_post_clone.py"
STEP_ONE = ROOT / "scripts" / "utm_vm_clone_step_01_clone.py"
STEP_SCRIPTS = (
    "utm_vm_clone_step_01_clone.py",
    "utm_vm_clone_step_02_account.py",
    "utm_vm_clone_step_03_hardware.py",
    "utm_vm_clone_step_04_desktop.py",
    "utm_vm_clone_step_05_settings.py",
    "utm_vm_clone_step_06_delivery.py",
    "utm_vm_clone_step_07_dependencies.py",
    "utm_vm_clone_step_08_accessibility.py",
    "utm_vm_clone_step_09_app_management.py",
    "utm_vm_clone_step_10_finalize.py",
)


def load_post_clone():
    spec = importlib.util.spec_from_file_location("utm_post_clone_steps", POST_CLONE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_step_one():
    spec = importlib.util.spec_from_file_location("utm_vm_clone_step_one", STEP_ONE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_step_one_preserves_actual_clone_error(monkeypatch, capsys) -> None:
    module = load_step_one()

    def fail_clone(_database):
        raise module.CloneError("copy failed with lowercase detail (!)")

    monkeypatch.setattr(module, "clone_once", fail_clone)

    assert module.main() == 1
    assert (
        "执行报错：utm-vm-clone-step-01；copy failed with lowercase detail (!)"
        in capsys.readouterr().err
    )


def test_clone_skill_has_ten_independent_step_scripts() -> None:
    for name in STEP_SCRIPTS:
        path = ROOT / "scripts" / name
        assert path.is_file(), name
        text = path.read_text(encoding="utf-8")
        assert "--vm-name" in text or name.endswith("step_01_clone.py")


def test_clone_state_and_steps_have_no_legacy_phase_compatibility() -> None:
    sources = (
        (ROOT / "scripts" / "utm_post_clone.py").read_text(encoding="utf-8"),
        (ROOT / "scripts" / "utm_vm_clone_steps.py").read_text(encoding="utf-8"),
    )
    for source in sources:
        assert 'phase="all"' not in source
        assert '"phases"' not in source
        assert 'state.get("evidence"' not in source


def test_post_clone_handoff_order_is_exact() -> None:
    module = load_post_clone()
    assert tuple(Path(path).name for path in module.STEP_SCRIPTS) == STEP_SCRIPTS[1:]
    assert tuple(module.STEP_MARKERS) == (
        "STEP_02_ACCOUNT=verified",
        "STEP_03_HARDWARE=verified",
        "STEP_04_DESKTOP=verified",
        "STEP_05_SETTINGS=verified",
        "STEP_06_DELIVERY=verified",
        "STEP_07_DEPENDENCIES=verified",
        "STEP_08_ACCESSIBILITY=verified",
        "STEP_09_APP_MANAGEMENT=verified",
        "STEP_10_FINALIZE=verified",
    )


def test_post_clone_retries_only_the_failed_dependency_step(monkeypatch) -> None:
    module = load_post_clone()
    monkeypatch.setattr(module, "STEP_RETRY_DELAYS_SECONDS", (0, 0))
    calls: list[str] = []
    attempts = {name: 0 for name in STEP_SCRIPTS[1:]}

    def fake_run(command, **kwargs):
        name = Path(command[1]).name
        calls.append(name)
        attempts[name] += 1
        step_index = module.STEP_SCRIPTS.index(command[1])
        marker = module.STEP_MARKERS[step_index]
        if name == "utm_vm_clone_step_07_dependencies.py" and attempts[name] < 3:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"步骤{step_index + 2}已操作\n执行成功：step；{marker}\n",
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module.run_step_handoff("stgf")

    assert attempts["utm_vm_clone_step_07_dependencies.py"] == 3
    assert all(
        attempts[name] == 1
        for name in STEP_SCRIPTS[1:]
        if name != "utm_vm_clone_step_07_dependencies.py"
    )
    assert calls == list(STEP_SCRIPTS[1:6]) + [STEP_SCRIPTS[6]] * 3 + list(STEP_SCRIPTS[7:])


def test_post_clone_prints_only_step_completion_skip_and_fault_events(
    monkeypatch, capsys
) -> None:
    module = load_post_clone()
    monkeypatch.setattr(module, "STEP_RETRY_DELAYS_SECONDS", (0, 0))
    attempts: dict[str, int] = {}

    def fake_run(command, **kwargs):
        name = Path(command[1]).name
        step_index = module.STEP_SCRIPTS.index(command[1])
        marker = module.STEP_MARKERS[step_index]
        attempts[name] = attempts.get(name, 0) + 1
        if name == "utm_vm_clone_step_07_dependencies.py" and attempts[name] == 1:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="执行报错：utm-vm-clone-step-07；DEPENDENCY_CHECK_FAILED\n",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"步骤{step_index + 2}已操作\n执行成功：step；{marker}\n",
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module.run_step_handoff("stgf")

    assert capsys.readouterr().out.splitlines() == [
        "步骤2已操作",
        "步骤3已操作",
        "步骤4已操作",
        "步骤5已操作",
        "步骤6已操作",
        "步骤7已操作",
        "步骤8已操作",
        "步骤9已操作",
        "步骤10已操作",
    ]


def test_post_clone_reports_only_the_safe_last_step_error(monkeypatch) -> None:
    module = load_post_clone()
    monkeypatch.setattr(module, "STEP_RETRY_DELAYS_SECONDS", (0, 0))

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="开始执行：utm-vm-clone-step-02\n",
            stderr="执行报错：utm-vm-clone-step-02；ACCOUNT_STATE_NOT_VERIFIED\n",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    try:
        module.run_step_handoff("stgf")
    except module.PostCloneError as error:
        assert str(error) == (
            "STEP_RETRIES_EXHAUSTED=UTM_VM_CLONE_STEP_02_ACCOUNT:"
            "ATTEMPTS=3:LAST=ACCOUNT_STATE_NOT_VERIFIED"
        )
    else:
        raise AssertionError("failed step was accepted")


def test_post_clone_preserves_full_last_step_error(monkeypatch) -> None:
    module = load_post_clone()
    monkeypatch.setattr(module, "STEP_RETRY_DELAYS_SECONDS", (0, 0))

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="开始执行：utm-vm-clone-step-02\n",
            stderr=(
                "执行报错：utm-vm-clone-step-02；"
                "SSH password command failed: connection refused\n"
            ),
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(module.PostCloneError) as captured:
        module.run_step_handoff("stgf")
    assert str(captured.value).endswith(
        "LAST=SSH password command failed: connection refused"
    )


def test_account_probe_checks_target_user_not_sudo_root() -> None:
    import inspect

    source = inspect.getsource(clone_steps._pure_account_probe)
    assert '/usr/bin/id -un {vm_name}' in source
    assert 'test "$(/usr/bin/id -un)"' not in source


def test_registry_reopens_exact_bundle_before_declaring_target_mismatch() -> None:
    import inspect

    source = inspect.getsource(clone_steps._registry_entry)
    assert 'subprocess.run(["open", str(bundle)], check=True)' in source
    assert "time.sleep(legacy.WAIT_SECONDS)" in source


def test_registry_reader_falls_back_to_direct_preferences_when_defaults_empty(
    monkeypatch, tmp_path
) -> None:
    preference_path = tmp_path / "com.utmapp.UTM.plist"
    expected = {
        "UUID": {
            "Name": "rbqi",
            "Package": {"Path": "/Volumes/AutoA/images/rbqi.utm"},
        }
    }
    preference_path.write_bytes(plistlib.dumps(expected, fmt=plistlib.FMT_BINARY))
    monkeypatch.setattr(
        clone_steps.subprocess,
        "check_output",
        lambda *_args, **_kwargs: plistlib.dumps({}),
    )

    assert clone_steps._read_utm_preferences(preference_path) == expected


def test_completed_desktop_step_is_reverified_without_replaying_side_effects(
    monkeypatch, tmp_path
) -> None:
    state_path = tmp_path / "stgf.json"
    state = {"steps": [2, 3, 4], "step_evidence": {}}
    context = (
        object(),
        tmp_path / "stgf.utm",
        "764188C2-FC3C-4BC7-BBF1-320827251EB2",
        "ce:c3:e8:e0:c1:0c",
        state_path,
        state,
    )
    calls: list[str] = []
    monkeypatch.setattr(clone_steps, "load_context", lambda vm_name: context)
    monkeypatch.setattr(
        clone_steps,
        "verify_desktop",
        lambda *args: calls.append("verify") or {"desktop": "verified"},
    )
    monkeypatch.setattr(
        clone_steps.legacy,
        "_enter_guest_desktop",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not replay desktop setup")),
    )
    monkeypatch.setattr(
        clone_steps.legacy,
        "_write_state",
        lambda path, value: calls.append("write"),
    )

    clone_steps.run_desktop("stgf")

    assert calls == ["verify", "write"]
    assert state["step_evidence"]["4"]["reverified"] == "verified"


def test_delivery_reverification_uses_guest_copy_verifier() -> None:
    assert getattr(clone_steps.legacy, "verify_copy", None) is post_clone_guest.verify_copy


def test_hardware_reverification_repairs_inventory_after_mac_randomization(
    monkeypatch, tmp_path
) -> None:
    bundle = tmp_path / "rbqi.utm"
    shared = tmp_path / "shared"
    database = tmp_path / "inventory.sqlite3"
    config_uuid = "FC31DB80-A5BB-46D3-B323-D64E734F9CDD"
    final_mac = "0e:63:02:91:b7:2a"
    updates: list[tuple[Path, str, str]] = []
    args = SimpleNamespace(
        vm_name="rbqi", shared_dir=shared, database=database
    )
    state = {"step_evidence": {"3": {"mac": final_mac}}}

    monkeypatch.setattr(clone_steps.legacy, "ensure_stopped", lambda _name: None)
    monkeypatch.setattr(
        clone_steps.legacy, "config_identity", lambda _bundle: (config_uuid, final_mac)
    )
    monkeypatch.setattr(
        clone_steps,
        "_registry_entry",
        lambda *_args: {
            "SharedDirectories": [{"Path": str(shared), "ReadOnly": True}]
        },
    )
    monkeypatch.setattr(
        clone_steps.legacy,
        "update_guest_mac",
        lambda db, name, mac: updates.append((db, name, mac)),
    )

    evidence = clone_steps.verify_hardware(args, bundle, config_uuid, state)

    assert updates == [(database, "rbqi", final_mac)]
    assert evidence["mac"] == final_mac


def test_desktop_reverification_recovers_detected_minibuddy(
    monkeypatch, tmp_path
) -> None:
    bundle = tmp_path / "rbqi.utm"
    states = iter(
        [
            (
                "CONSOLE_USER=rbqi\nFINDER=ready\nSETUP_ASSISTANT=present\n",
                {
                    "CONSOLE_USER": "rbqi",
                    "FINDER": "ready",
                    "SETUP_ASSISTANT": "present",
                },
            ),
            (
                "CONSOLE_USER=rbqi\nFINDER=ready\nSETUP_ASSISTANT=absent\n",
                {
                    "CONSOLE_USER": "rbqi",
                    "FINDER": "ready",
                    "SETUP_ASSISTANT": "absent",
                },
            ),
        ]
    )
    recoveries: list[tuple[str, str, str, Path]] = []

    monkeypatch.setattr(clone_steps.legacy, "config_identity", lambda _bundle: ("UUID", "mac"))
    monkeypatch.setattr(clone_steps.legacy, "ensure_started", lambda _vm_name: None)
    monkeypatch.setattr(clone_steps.legacy, "resolve_ip", lambda _vm_name, _mac: "192.0.2.10")
    monkeypatch.setattr(clone_steps.legacy, "ensure_ssh", lambda _vm_name, _ip: None)
    monkeypatch.setattr(clone_steps.legacy, "read_desktop_state", lambda _vm_name, _ip: next(states))
    monkeypatch.setattr(
        clone_steps.legacy,
        "_enter_guest_desktop",
        lambda user, ip, vm_name, target: recoveries.append(
            (user, ip, vm_name, target)
        ),
    )
    monkeypatch.setattr(clone_steps.legacy, "verify_admin", lambda _vm_name, _ip: None)
    monkeypatch.setattr(clone_steps, "_pure_account_probe", lambda _vm_name, _ip: None)

    evidence = clone_steps.verify_desktop(
        SimpleNamespace(vm_name="rbqi"), bundle, "UUID", {}
    )

    assert recoveries == [("rbqi", "192.0.2.10", "rbqi", bundle)]
    assert evidence["desktop"] == "verified"


def test_desktop_reverification_recovers_login_window(
    monkeypatch, tmp_path
) -> None:
    bundle = tmp_path / "rbqi.utm"
    states = iter(
        [
            (
                "CONSOLE_USER=root\nFINDER=missing\nSETUP_ASSISTANT=absent\n",
                {
                    "CONSOLE_USER": "root",
                    "FINDER": "missing",
                    "SETUP_ASSISTANT": "absent",
                },
            ),
            (
                "CONSOLE_USER=rbqi\nFINDER=ready\nSETUP_ASSISTANT=absent\n",
                {
                    "CONSOLE_USER": "rbqi",
                    "FINDER": "ready",
                    "SETUP_ASSISTANT": "absent",
                },
            ),
        ]
    )
    recoveries: list[tuple[str, str, str, Path]] = []

    monkeypatch.setattr(clone_steps.legacy, "config_identity", lambda _bundle: ("UUID", "mac"))
    monkeypatch.setattr(clone_steps.legacy, "ensure_started", lambda _vm_name: None)
    monkeypatch.setattr(clone_steps.legacy, "resolve_ip", lambda _vm_name, _mac: "192.0.2.10")
    monkeypatch.setattr(clone_steps.legacy, "ensure_ssh", lambda _vm_name, _ip: None)
    monkeypatch.setattr(clone_steps.legacy, "read_desktop_state", lambda _vm_name, _ip: next(states))
    monkeypatch.setattr(
        clone_steps.legacy,
        "_enter_guest_desktop",
        lambda user, ip, vm_name, target: recoveries.append(
            (user, ip, vm_name, target)
        ),
    )
    monkeypatch.setattr(clone_steps.legacy, "verify_admin", lambda _vm_name, _ip: None)
    monkeypatch.setattr(clone_steps, "_pure_account_probe", lambda _vm_name, _ip: None)

    evidence = clone_steps.verify_desktop(
        SimpleNamespace(vm_name="rbqi"), bundle, "UUID", {}
    )

    assert recoveries == [("rbqi", "192.0.2.10", "rbqi", bundle)]
    assert evidence["desktop"] == "verified"


def test_desktop_reverification_fails_closed_for_unknown_guest_state(
    monkeypatch, tmp_path
) -> None:
    bundle = tmp_path / "rbqi.utm"
    state = (
        "CONSOLE_USER=other\nFINDER=missing\nSETUP_ASSISTANT=absent\n",
        {
            "CONSOLE_USER": "other",
            "FINDER": "missing",
            "SETUP_ASSISTANT": "absent",
        },
    )
    recoveries: list[tuple[str, str, str, Path]] = []

    monkeypatch.setattr(clone_steps.legacy, "config_identity", lambda _bundle: ("UUID", "mac"))
    monkeypatch.setattr(clone_steps.legacy, "ensure_started", lambda _vm_name: None)
    monkeypatch.setattr(clone_steps.legacy, "resolve_ip", lambda _vm_name, _mac: "192.0.2.10")
    monkeypatch.setattr(clone_steps.legacy, "ensure_ssh", lambda _vm_name, _ip: None)
    monkeypatch.setattr(clone_steps.legacy, "read_desktop_state", lambda _vm_name, _ip: state)
    monkeypatch.setattr(
        clone_steps.legacy,
        "_enter_guest_desktop",
        lambda user, ip, vm_name, target: recoveries.append(
            (user, ip, vm_name, target)
        ),
    )

    with pytest.raises(clone_steps.legacy.PostCloneError, match="DESKTOP_STATE_NOT_VERIFIED"):
        clone_steps.verify_desktop(SimpleNamespace(vm_name="rbqi"), bundle, "UUID", {})

    assert recoveries == []


def test_dependency_step_requires_all_26_success_lines_and_records_only_after_success(
    monkeypatch, tmp_path
) -> None:
    state_path = tmp_path / "stgf.json"
    state = {"steps": [2, 3, 4, 5, 6], "step_evidence": {}}
    context = (
        object(),
        tmp_path / "stgf.utm",
        "764188C2-FC3C-4BC7-BBF1-320827251EB2",
        "ce:c3:e8:e0:c1:0c",
        state_path,
        state,
    )
    monkeypatch.setattr(clone_steps, "load_context", lambda vm_name: context)
    monkeypatch.setattr(
        clone_steps,
        "run_dependency_checker",
        lambda vm_name: {"dependency_count": 26, "all_installed": "verified"},
    )
    monkeypatch.setattr(clone_steps.legacy, "_write_state", lambda *_args: None)

    clone_steps.run_dependencies("stgf")

    assert state["steps"] == [2, 3, 4, 5, 6, 7]
    assert state["step_evidence"]["7"]["dependency_count"] == 26


def test_dependency_checker_rejects_exit_zero_when_any_line_is_not_installed(
    monkeypatch,
) -> None:
    good = list(clone_steps.EXPECTED_DEPENDENCY_LINES)
    good[-1] = good[-1].removesuffix("已安装") + "未安装"
    monkeypatch.setattr(
        clone_steps.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="\n".join(good) + "\n", stderr=""
        ),
    )

    try:
        clone_steps.run_dependency_checker("stgf")
    except clone_steps.legacy.PostCloneError as error:
        assert str(error) == "DEPENDENCY_CHECK_NOT_ALL_INSTALLED=虚拟机App Store工具"
    else:
        raise AssertionError("未安装 dependency was accepted")


def test_completed_finalize_reaudits_and_shuts_down_when_step_9_left_vm_running(
    monkeypatch, tmp_path
) -> None:
    state_path = tmp_path / "stgf.json"
    state = {"steps": list(range(2, 11)), "step_evidence": {}}
    args = SimpleNamespace(database=tmp_path / "inventory.sqlite3")
    bundle = tmp_path / "stgf.utm"
    context = (
        args,
        bundle,
        "764188C2-FC3C-4BC7-BBF1-320827251EB2",
        "ce:c3:e8:e0:c1:0c",
        state_path,
        state,
    )
    calls: list[str] = []
    monkeypatch.setattr(clone_steps, "load_context", lambda _vm_name: context)
    monkeypatch.setattr(clone_steps.legacy, "_status", lambda _vm_name: "started")
    monkeypatch.setattr(
        clone_steps.legacy,
        "finalize",
        lambda **_kwargs: calls.append("finalize")
        or {"shutdown": "verified", "available": "1"},
    )
    monkeypatch.setattr(
        clone_steps.legacy,
        "_write_state",
        lambda _path, _state: calls.append("write"),
    )

    clone_steps.run_finalize("stgf")

    assert calls == ["finalize", "write"]
    assert state["step_evidence"]["10"]["reverified"] == "verified"
