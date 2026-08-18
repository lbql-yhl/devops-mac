from pathlib import Path
import ast
import inspect
import sys
from types import SimpleNamespace

import pytest

from scripts import utm_post_clone_guest as guest
from scripts import utm_post_clone as post_clone


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


@pytest.fixture(autouse=True)
def configured_guest_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBMISSION_GUEST_PASSWORD", "9072")


def test_post_clone_is_split_into_three_business_modules() -> None:
    expected = (
        SCRIPTS / "utm_post_clone.py",
        SCRIPTS / "utm_post_clone_guest.py",
        SCRIPTS / "utm_post_clone_finalize.py",
    )
    for path in expected:
        assert path.is_file(), f"missing post-clone module: {path.name}"
        ast.parse(path.read_text(encoding="utf-8"))
    assert len(expected[0].read_text(encoding="utf-8").splitlines()) < 700


def test_main_module_contains_no_ssh_or_guest_gui_implementation() -> None:
    source = (SCRIPTS / "utm_post_clone.py").read_text(encoding="utf-8")
    assert "ssh-copy-id" not in source
    assert "ssh-keygen" not in source
    assert "CGEventCreateMouseEvent" not in source
    assert "SSH_PRIVATE_KEY" not in source
    assert "SSH_PUBLIC_KEY" not in source


def test_raise_failure_is_accepted_only_for_verified_visible_main_window() -> None:
    assert guest.raise_result_acceptable(
        0, frontmost=True, main=True, focused=True, minimized=False
    )
    assert not guest.raise_result_acceptable(
        -25205, frontmost=True, main=True, focused=True, minimized=False
    )
    assert not guest.raise_result_acceptable(
        -25205, frontmost=False, main=True, focused=True, minimized=False
    )
    assert not guest.raise_result_acceptable(
        -25205, frontmost=True, main=False, focused=True, minimized=False
    )
    assert not guest.raise_result_acceptable(
        -25205, frontmost=True, main=True, focused=False, minimized=False
    )
    assert not guest.raise_result_acceptable(
        -25205, frontmost=True, main=True, focused=True, minimized=True
    )


def test_guest_input_events_are_delivered_only_to_the_exact_utm_pid() -> None:
    mouse_source = inspect.getsource(guest._mouse_click)
    key_source = inspect.getsource(guest._key_codes)
    assert "CGEventPostToPid" in mouse_source
    assert "CGEventPostToPid" in key_source
    assert "kCGHIDEventTap" not in mouse_source
    assert "kCGHIDEventTap" not in key_source


def test_focus_recovery_uses_semantic_dock_utm_item_and_rechecks_exact_window() -> None:
    recovery_source = inspect.getsource(guest._activate_utm_via_dock)
    assert "Dock" in recovery_source
    assert "AXApplicationDockItem" in recovery_source
    assert "kAXTitleAttribute" in recovery_source
    assert "UTM" in recovery_source
    assert "kCGHIDEventTap" in recovery_source
    assert "WAIT_SECONDS" in recovery_source
    assert "DOCK_UTM_ACTIVATION_READBACK_FAILED" in recovery_source
    assert "UTM_DOCK_ACTIVATION=verified" in recovery_source

    focus_source = inspect.getsource(guest._focus_exact_utm)
    assert "_activate_utm_via_dock" in focus_source
    assert focus_source.count("exact_guest_window") >= 3


def test_post_clone_has_no_duplicate_execution_doc() -> None:
    assert not (ROOT / "docs" / "utm-post-clone.md").exists()


def test_guest_input_capture_uses_unique_semantic_checkbox_and_readback() -> None:
    source = inspect.getsource(guest._capture_guest_input)
    assert "AXCheckBox" in source
    assert "Capture Input" in source
    assert "kAXPressAction" in source
    assert "kAXValueAttribute" in source
    assert "UTM_CAPTURE_INPUT_COUNT" in source
    assert "UTM_CAPTURE_INPUT_READBACK_FAILED" in source
    focus_source = inspect.getsource(guest._focus_guest_input)
    assert "_capture_guest_input" in focus_source


def test_guest_login_verifies_exact_capture_before_password_input() -> None:
    source = inspect.getsource(guest.enter_guest_desktop)
    assert "_focus_guest_input" in source
    assert source.index("_focus_guest_input") < source.index("_key_codes")
    assert "_mouse_click" in source
    assert "guest_window_point" in source
    assert "time.sleep(WAIT_SECONDS)" in source


def test_guest_login_key_codes_enter_vm_name_then_configured_password(monkeypatch) -> None:
    monkeypatch.setattr(guest, "guest_password", lambda: "9072")
    assert guest.login_key_codes("jexp") == (38, 14, 7, 35, 48, 25, 29, 26, 19, 36)
    with pytest.raises(guest.GuestAutomationError):
        guest.login_key_codes("bad-name")


def test_sudo_password_lines_use_the_runtime_setting(monkeypatch) -> None:
    synthetic_value = "9072"
    monkeypatch.setattr(guest, "guest_password", lambda: synthetic_value)

    assert guest.password_lines(2) == f"{synthetic_value}\n{synthetic_value}\n"


@pytest.mark.parametrize("invalid", ("12", "12345", "12a4", "１２３４"))
def test_guest_login_key_codes_reject_invalid_password_without_echo(
    invalid: str, monkeypatch
) -> None:
    monkeypatch.setattr(guest, "guest_password", lambda: invalid)

    with pytest.raises(guest.ConfigurationError) as captured:
        guest.password_key_codes()

    assert invalid not in str(captured.value)


def test_guest_key_events_are_paced_for_utm_delivery(monkeypatch) -> None:
    sleeps: list[float] = []
    fake_quartz = SimpleNamespace(
        kCGEventLeftMouseDown=1,
        kCGEventLeftMouseUp=2,
        kCGMouseButtonLeft=0,
        CGEventCreateKeyboardEvent=lambda _source, key_code, pressed: (key_code, pressed),
        CGEventPostToPid=lambda _pid, _event: None,
    )
    monkeypatch.setitem(sys.modules, "Quartz", fake_quartz)
    monkeypatch.setattr(guest.time, "sleep", lambda seconds: sleeps.append(seconds))

    guest._key_codes(17, (51, 0))

    assert sleeps == [0.03, 0.03]


def test_selected_user_login_sends_only_configured_password_first(monkeypatch) -> None:
    monkeypatch.setattr(guest, "guest_password", lambda: "9072")
    states = iter(
        (
            {"CONSOLE_USER": "root", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "abcd", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "abcd", "FINDER": "ready", "SETUP_ASSISTANT": "absent"},
        )
    )
    sent: list[tuple[int, ...]] = []

    def fake_read_desktop_state(user: str, ip: str) -> tuple[str, dict[str, str]]:
        values = next(states)
        return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n", values

    monkeypatch.setattr(guest, "read_desktop_state", fake_read_desktop_state)
    monkeypatch.setattr(guest, "loginwindow_full_name_enabled", lambda _user, _ip: False)
    monkeypatch.setattr(guest, "_focus_guest_input", lambda *args: (17, (0, 0, 1000, 800)))
    monkeypatch.setattr(guest, "_mouse_click", lambda *args: None)
    monkeypatch.setattr(guest, "_key_codes", lambda _pid, values: sent.append(tuple(values)))
    monkeypatch.setattr(guest.time, "sleep", lambda _seconds: None)

    result = guest.enter_guest_desktop(
        "abcd", "192.0.2.10", "abcd", Path("/tmp/abcd.utm")
    )

    assert result["CONSOLE_USER"] == "abcd"
    assert sent == [guest.password_key_codes()]


def test_missing_showfullname_uses_default_selected_user_mode(monkeypatch) -> None:
    scripts: list[str] = []

    def fake_ssh_sudo_script(_user: str, _ip: str, script: str) -> str:
        scripts.append(script)
        return "__SHOWFULLNAME_MISSING__\n"

    monkeypatch.setattr(guest, "ssh_sudo_script", fake_ssh_sudo_script)

    assert guest.loginwindow_full_name_enabled("abcd", "192.0.2.10") is False
    assert "2>/dev/null || printf '__SHOWFULLNAME_MISSING__" in scripts[0]


def test_full_name_login_uses_verified_capture_and_keyboard_only(monkeypatch) -> None:
    monkeypatch.setattr(guest, "guest_password", lambda: "9072")
    states = iter(
        (
            {"CONSOLE_USER": "root", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "abcd", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "abcd", "FINDER": "ready", "SETUP_ASSISTANT": "absent"},
        )
    )
    clicks: list[tuple[float, float]] = []
    sent: list[tuple[int, ...]] = []

    def fake_read_desktop_state(user: str, ip: str) -> tuple[str, dict[str, str]]:
        values = next(states)
        return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n", values

    monkeypatch.setattr(guest, "read_desktop_state", fake_read_desktop_state)
    monkeypatch.setattr(
        guest, "loginwindow_full_name_enabled", lambda _user, _ip: True, raising=False
    )
    monkeypatch.setattr(guest, "_focus_guest_input", lambda *args: (17, (0, 0, 1000, 800)))
    monkeypatch.setattr(guest, "_mouse_click", lambda _pid, point: clicks.append(point))
    monkeypatch.setattr(guest, "_key_codes", lambda _pid, values: sent.append(tuple(values)))
    monkeypatch.setattr(guest.time, "sleep", lambda _seconds: None)

    result = guest.enter_guest_desktop(
        "abcd", "192.0.2.10", "abcd", Path("/tmp/abcd.utm")
    )

    assert result["CONSOLE_USER"] == "abcd"
    assert clicks == []
    assert sent == [
        guest.username_key_codes("abcd") + (36,),
        guest.password_key_codes(),
    ]


def test_guest_login_falls_back_to_username_and_password_after_selected_user_retry(
    monkeypatch,
) -> None:
    monkeypatch.setattr(guest, "guest_password", lambda: "9072")
    states = iter(
        (
            {"CONSOLE_USER": "root", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "root", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "root", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "root", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "root", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "abcd", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "abcd", "FINDER": "ready", "SETUP_ASSISTANT": "absent"},
        )
    )
    sent: list[tuple[int, ...]] = []

    def fake_read_desktop_state(user: str, ip: str) -> tuple[str, dict[str, str]]:
        values = next(states)
        return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n", values

    monkeypatch.setattr(guest, "read_desktop_state", fake_read_desktop_state)
    monkeypatch.setattr(guest, "loginwindow_full_name_enabled", lambda _user, _ip: False)
    monkeypatch.setattr(guest, "_focus_guest_input", lambda *args: (17, (0, 0, 1000, 800)))
    monkeypatch.setattr(guest, "_mouse_click", lambda *args: None)
    monkeypatch.setattr(guest, "_key_codes", lambda _pid, values: sent.append(tuple(values)))
    monkeypatch.setattr(guest.time, "sleep", lambda _seconds: None)

    result = guest.enter_guest_desktop(
        "abcd", "192.0.2.10", "abcd", Path("/tmp/abcd.utm")
    )

    assert result["CONSOLE_USER"] == "abcd"
    assert sent == [
        guest.password_key_codes(),
        guest.username_key_codes("abcd"),
        guest.password_key_codes(),
    ]


def test_guest_login_targets_both_fields_and_clears_stale_values(
    monkeypatch,
) -> None:
    monkeypatch.setattr(guest, "guest_password", lambda: "9072")
    states = iter(
        (
            {"CONSOLE_USER": "root", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "root", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "root", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "root", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "root", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "abcd", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "abcd", "FINDER": "ready", "SETUP_ASSISTANT": "absent"},
        )
    )
    clicks: list[tuple[float, float]] = []
    sent: list[tuple[int, ...]] = []

    def fake_read_desktop_state(user: str, ip: str) -> tuple[str, dict[str, str]]:
        values = next(states)
        return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n", values

    monkeypatch.setattr(guest, "read_desktop_state", fake_read_desktop_state)
    monkeypatch.setattr(guest, "loginwindow_full_name_enabled", lambda _user, _ip: False)
    monkeypatch.setattr(guest, "_focus_guest_input", lambda *args: (17, (0, 0, 1000, 800)))
    monkeypatch.setattr(guest, "_mouse_click", lambda _pid, point: clicks.append(point))
    monkeypatch.setattr(guest, "_key_codes", lambda _pid, values: sent.append(tuple(values)))
    monkeypatch.setattr(guest.time, "sleep", lambda _seconds: None)

    result = guest.enter_guest_desktop(
        "abcd", "192.0.2.10", "abcd", Path("/tmp/abcd.utm")
    )

    clear = (51,) * 32
    assert result["CONSOLE_USER"] == "abcd"
    assert clicks == [(520.0, 728.0), (520.0, 696.0), (520.0, 728.0)]
    assert sent == [
        clear + (25, 29, 26, 19, 36),
        clear + (0, 11, 8, 2),
        clear + (25, 29, 26, 19, 36),
    ]


def test_finder_launch_uses_process_readback_when_open_exit_is_ambiguous() -> None:
    source = inspect.getsource(guest.launch_finder_script)
    assert "finder_open_exit=$?" in source
    assert "FINDER_OPEN_EXIT=%s" in source
    assert source.index("finder_open_exit=$?") < source.index("pgrep -u")
    assert "FINDER=ready" in source


def test_desktop_flow_launches_finder_when_setup_is_absent_but_finder_is_missing() -> None:
    source = inspect.getsource(guest.enter_guest_desktop)
    assert 'values.get("SETUP_ASSISTANT") == "absent"' in source
    assert 'values.get("FINDER") == "missing"' in source
    assert "FINDER_AFTER_ABSENT_SETUP_NOT_VERIFIED" in source


def test_desktop_flow_reclassifies_when_finder_launch_reopens_setup_assistant(
    monkeypatch,
) -> None:
    states = iter(
        (
            {"CONSOLE_USER": "abcd", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "abcd", "FINDER": "missing", "SETUP_ASSISTANT": "absent"},
            {"CONSOLE_USER": "abcd", "FINDER": "ready", "SETUP_ASSISTANT": "present"},
            {"CONSOLE_USER": "abcd", "FINDER": "ready", "SETUP_ASSISTANT": "present"},
            {"CONSOLE_USER": "abcd", "FINDER": "ready", "SETUP_ASSISTANT": "absent"},
        )
    )

    def fake_read_desktop_state(user: str, ip: str) -> tuple[str, dict[str, str]]:
        values = next(states)
        output = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
        return output, values

    monkeypatch.setattr(guest, "read_desktop_state", fake_read_desktop_state)
    monkeypatch.setattr(
        guest,
        "ssh_sudo_script",
        lambda *args, **kwargs: "FINDER_OPEN_EXIT=0\nFINDER=ready\n",
    )
    monkeypatch.setattr(
        guest,
        "ssh_script",
        lambda *args, **kwargs: (
            "SETUP_ASSISTANT_ACTION=guest_state_completed\nSETUP_ASSISTANT=absent\n"
        ),
    )

    result = guest.enter_guest_desktop(
        "abcd", "192.0.2.10", "abcd", Path("/tmp/abcd.utm")
    )

    assert result["SETUP_ASSISTANT"] == "absent"
    assert result["FINDER"] == "ready"
    assert result["SETUP_ASSISTANT_ACTION"] == "guest_state_completed"


def test_demo_retirement_uses_a_dedicated_long_timeout() -> None:
    assert "timeout_seconds" in inspect.signature(guest.ssh_sudo_script).parameters
    assert guest.DEMO_RETIREMENT_TIMEOUT_SECONDS >= 180.0
    phase_source = inspect.getsource(post_clone.perform_account_setup)
    assert "timeout_seconds=DEMO_RETIREMENT_TIMEOUT_SECONDS" in phase_source


def test_ssh_identity_probes_have_a_total_timeout() -> None:
    assert "timeout_seconds" in inspect.signature(guest.ssh_capture).parameters
    assert 5.0 < guest.SSH_PROBE_TIMEOUT_SECONDS <= 15.0
    source = inspect.getsource(guest.ensure_ssh)
    assert source.count("timeout_seconds=SSH_PROBE_TIMEOUT_SECONDS") == 2


def test_post_clone_recovery_rules_are_vm_agnostic() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            SCRIPTS / "utm_post_clone.py",
            SCRIPTS / "utm_post_clone_guest.py",
            SCRIPTS / "utm_post_clone_finalize.py",
        )
    )
    for forbidden in ("jexp", "192.168.64.30", "gui/502"):
        assert forbidden not in source
    launcher = guest.launch_finder_script()
    assert 'target_user="${SUDO_USER:-}"' in launcher
    assert 'uid=$(/usr/bin/id -u "$target_user")' in launcher
