#!/usr/bin/env python3
"""Contract tests for the single-pass clone-time App Management step."""

from pathlib import Path
from types import SimpleNamespace
import ast
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "scripts" / "utm_terminal_app_management.py"
GUEST = ROOT / "scripts" / "utm_terminal_app_management_guest.py"
sys.path.insert(0, str(ROOT))

import scripts.utm_terminal_app_management as terminal  # noqa: E402
import scripts.utm_terminal_app_management_guest as guest  # noqa: E402


@pytest.fixture(autouse=True)
def configured_guest_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBMISSION_GUEST_PASSWORD", "9072")


def test_step_9_contains_only_deep_link_existing_terminal_ax_and_reread() -> None:
    host_source = HOST.read_text(encoding="utf-8")
    guest_source = GUEST.read_text(encoding="utf-8")
    combined = host_source + guest_source

    for required in (
        "Privacy_AppBundles",
        "open_app_management_settings",
        "_read_app_management",
        "_find_terminal_toggle",
        "Terminal toggle AXPress",
        "fresh App Management verification failed",
    ):
        assert required in combined

    host_tree = ast.parse(host_source)
    transport = next(
        node
        for node in host_tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "TerminalPermissionTransport"
    )
    assert {
        node.name for node in transport.body if isinstance(node, ast.FunctionDef)
    } == {
        "__init__",
        "_run",
        "probe_identity",
        "open_app_management_settings",
        "run_ax",
    }


def test_host_opens_app_management_with_background_deep_link() -> None:
    transport = terminal.TerminalPermissionTransport.__new__(
        terminal.TerminalPermissionTransport
    )
    commands: list[str] = []

    def fake_run(command: str, *, timeout: int = 120, input_text: str | None = None):
        assert input_text is None
        commands.append(command)
        return SimpleNamespace(stdout="SETTINGS_OPENED=1\n")

    transport._run = fake_run
    transport.open_app_management_settings()

    assert commands == [
        "/usr/bin/open '"
        + terminal.DEEP_LINK
        + "' && /usr/bin/printf 'SETTINGS_OPENED=1\\n'"
    ]


def test_host_runs_one_nonpersistent_ax_process_in_console_session(monkeypatch) -> None:
    transport = terminal.TerminalPermissionTransport.__new__(
        terminal.TerminalPermissionTransport
    )
    transport.vm_name = "stgf"
    dummy_value = "9072"
    calls: list[tuple[str, str | None]] = []

    def fake_run(command: str, *, timeout: int = 120, input_text: str | None = None):
        calls.append((command, input_text))
        return SimpleNamespace(stdout="TERMINAL_APP_MANAGEMENT=verified\n")

    transport._run = fake_run
    monkeypatch.setattr(terminal, "guest_password", lambda: dummy_value)
    transport.run_ax()

    assert len(calls) == 1
    command, input_text = calls[0]
    assert command.endswith("exec /usr/bin/python3 -B - --vm-user stgf")
    assert "launchctl" not in command
    assert "sudo" not in command
    assert dummy_value not in command
    assert "SUBMISSION_GUEST_PASSWORD" in command
    assert input_text == dummy_value + "\n" + GUEST.read_text(encoding="utf-8")


def test_guest_requires_runtime_password_without_echoing_invalid_value(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SUBMISSION_GUEST_PASSWORD", raising=False)
    with pytest.raises(guest.TerminalAppManagementGuestError) as missing:
        guest._configured_guest_password()
    assert "SUBMISSION_GUEST_PASSWORD" in str(missing.value)

    invalid = "not-a-pin"
    monkeypatch.setenv("SUBMISSION_GUEST_PASSWORD", invalid)
    with pytest.raises(guest.TerminalAppManagementGuestError) as captured:
        guest._configured_guest_password()
    assert invalid not in str(captured.value)


def test_guest_authorization_uses_only_the_runtime_password() -> None:
    source = GUEST.read_text(encoding="utf-8")

    assert "_configured_guest_password()" in source
    assert 'kAXValueAttribute, "1234"' not in source


def test_host_preserves_safe_stderr_when_guest_process_fails_before_marker(
    monkeypatch,
) -> None:
    transport = terminal.TerminalPermissionTransport.__new__(
        terminal.TerminalPermissionTransport
    )
    transport.vm_name = "stgf"
    transport.vm_ip = "192.0.2.10"
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Could not switch to audit session 0x186a3: Operation not permitted\n",
        ),
    )

    with pytest.raises(
        terminal.TerminalAppManagementError,
        match="Could not switch to audit session 0x186a3: Operation not permitted",
    ):
        transport._run("false")


def test_toggle_state_normalizes_supported_ax_values() -> None:
    class FakeAX:
        kAXValueAttribute = "value"

    class FakeCommon:
        AX = FakeAX()

        @staticmethod
        def copy_attribute(element, _attribute):
            return element

    assert guest._toggle_state(FakeCommon(), True) is True
    assert guest._toggle_state(FakeCommon(), 0) is False
    assert guest._toggle_state(FakeCommon(), "on") is True
    assert guest._toggle_state(FakeCommon(), "off") is False
    assert guest._toggle_state(FakeCommon(), "unknown") is None


def test_find_terminal_toggle_requires_one_existing_terminal_row() -> None:
    terminal_text = object()
    row = object()
    toggle = object()

    class FakeAX:
        kAXParentAttribute = "parent"

    class FakeCommon:
        AX = FakeAX()

        @staticmethod
        def describe_element(element):
            if element is terminal_text:
                return {"role": "AXStaticText", "value": "Terminal"}
            if element is toggle:
                return {"role": "AXSwitch"}
            return {"role": "AXRow"}

        @staticmethod
        def iter_accessibility_tree(roots, max_nodes=2500):
            if roots == [row]:
                yield toggle, 1
            else:
                yield terminal_text, 1

        @staticmethod
        def copy_attribute(element, attribute):
            assert attribute == "parent"
            return row if element is terminal_text else None

    assert guest._find_terminal_toggle(FakeCommon(), [object()]) is toggle


def test_find_terminal_toggle_rejects_a_missing_terminal_row() -> None:
    class FakeCommon:
        @staticmethod
        def iter_accessibility_tree(_roots, max_nodes=2500):
            return iter(())

        @staticmethod
        def describe_element(_element):
            raise AssertionError("no element expected")

    assert guest._find_terminal_toggle(FakeCommon(), [object()]) is None


def test_read_app_management_requires_the_exact_page(monkeypatch) -> None:
    root = object()
    monkeypatch.setattr(guest, "_system_settings_roots", lambda _common: [root])
    monkeypatch.setattr(
        guest,
        "_tree",
        lambda _common, _roots, max_nodes=2500: [
            (
                root,
                {
                    "role": "AXStaticText",
                    "value": "Allow the applications below to update or delete other applications.",
                },
            )
        ],
    )
    assert guest._read_app_management(object()) == [root]


def test_authentication_wait_accepts_an_already_open_file_panel(monkeypatch) -> None:
    open_panel = object()
    monkeypatch.setattr(
        guest,
        "_all_system_settings_tree",
        lambda _common: (
            object(),
            [open_panel],
            [
                (
                    open_panel,
                    {"role": "AXWindow", "identifier": "open-panel"},
                )
            ],
        ),
    )

    assert guest._wait_for_authentication_or_open_panel(object()) is None


def test_system_settings_roots_keeps_app_management_when_open_panel_exists(
    monkeypatch,
) -> None:
    page = object()
    panel = object()

    class FakeAX:
        kAXWindowsAttribute = "windows"

        @staticmethod
        def AXUIElementCreateApplication(_pid):
            return "app"

    class FakeApp:
        @staticmethod
        def processIdentifier():
            return 17

    class FakeCommon:
        AX = FakeAX()

        @staticmethod
        def get_running_system_settings():
            return FakeApp()

        @staticmethod
        def copy_attribute(element, attribute):
            assert element == "app"
            assert attribute == "windows"
            return [page, panel]

        @staticmethod
        def describe_element(element):
            if element is page:
                return {
                    "role": "AXWindow",
                    "subrole": "AXStandardWindow",
                    "identifier": "main",
                    "title": "Sign in",
                }
            return {
                "role": "AXWindow",
                "subrole": "AXDialog",
                "identifier": "open-panel",
                "title": "Open",
            }

    def fake_tree(_common, roots, max_nodes=2500):
        assert max_nodes == 2500
        if roots == [page]:
            return [(page, FakeCommon.describe_element(page))]
        return [(panel, {"role": "AXWindow", "identifier": "open-panel"})]

    monkeypatch.setattr(guest, "_tree", fake_tree)

    assert guest._system_settings_roots(FakeCommon()) == [page]


def test_add_terminal_resumes_open_panel_and_clicks_open(monkeypatch) -> None:
    common = SimpleNamespace(
        AX=SimpleNamespace(
            kAXValueAttribute="value",
            kAXErrorSuccess=0,
            AXUIElementSetAttributeValue=lambda *_args: 0,
        )
    )
    page = object()
    panel = object()
    path_field = object()
    terminal = object()
    toggle = object()
    pressed: list[object] = []
    open_clicked: list[object] = []
    controls = iter((path_field, terminal))

    monkeypatch.setattr(guest, "_read_app_management", lambda _common: [page])
    monkeypatch.setattr(
        guest,
        "_tree",
        lambda _common, _roots, max_nodes=2500: [
            (object(), {"role": "AXButton", "description": "Add"})
        ],
    )
    monkeypatch.setattr(
        guest,
        "_all_system_settings_tree",
        lambda _common: (
            object(),
            [page, panel],
            [(panel, {"role": "AXWindow", "identifier": "open-panel"})],
        ),
    )
    monkeypatch.setattr(
        guest, "_press_control", lambda _common, element, _code: pressed.append(element)
    )
    monkeypatch.setattr(guest, "_wait_for_authentication_or_open_panel", lambda _common: None)
    monkeypatch.setattr(guest, "_send_go_to_folder_shortcut", lambda _common: None)
    monkeypatch.setattr(
        guest,
        "_wait_for_control",
        lambda _common, _predicate, _code: next(controls),
    )
    monkeypatch.setattr(guest, "_double_click_terminal_path", lambda *_args: None)
    monkeypatch.setattr(
        guest,
        "_press_unique_open_button",
        lambda value: open_clicked.append(value),
        raising=False,
    )
    monkeypatch.setattr(guest, "_find_terminal_toggle", lambda *_args: toggle)

    guest._add_terminal(common)

    assert pressed == []
    assert open_clicked == [common]


def test_add_terminal_clicks_open_immediately_when_terminal_is_ready(
    monkeypatch,
) -> None:
    common = object()
    page = object()
    panel = object()
    terminal = object()
    open_button = object()
    toggle = object()
    opened: list[object] = []

    monkeypatch.setattr(guest, "_read_app_management", lambda _common: [page])
    monkeypatch.setattr(
        guest,
        "_all_system_settings_tree",
        lambda _common: (
            object(),
            [page, panel],
            [
                (panel, {"role": "AXWindow", "identifier": "open-panel"}),
                (terminal, {"role": "AXStaticText", "value": "Terminal"}),
                (
                    open_button,
                    {"role": "AXButton", "title": "Open", "enabled": True},
                ),
            ],
        ),
    )
    monkeypatch.setattr(
        guest,
        "_press_unique_open_button",
        lambda value: opened.append(value),
    )
    monkeypatch.setattr(
        guest,
        "_send_go_to_folder_shortcut",
        lambda _common: pytest.fail("ready Terminal must not reopen Go to Folder"),
    )
    monkeypatch.setattr(guest, "_find_terminal_toggle", lambda *_args: toggle)

    guest._add_terminal(common)

    assert opened == [common]


def test_run_resumes_unique_open_panel_before_page_text_is_visible(monkeypatch) -> None:
    common = object()
    page = object()
    panel = object()
    toggle = object()
    added: list[object] = []
    reads: list[bool] = []
    finds = iter((None, toggle))

    monkeypatch.setattr(guest, "_require_console_identity", lambda _user: None)
    monkeypatch.setattr(guest, "AXCommon", lambda: common)
    monkeypatch.setattr(guest, "_require_ax_trusted", lambda _common: None)
    monkeypatch.setattr(
        guest,
        "_all_system_settings_tree",
        lambda _common: (
            object(),
            [page, panel],
            [(panel, {"role": "AXWindow", "identifier": "open-panel"})],
        ),
    )
    monkeypatch.setattr(guest, "_system_settings_roots", lambda _common: [page])

    def read_after_add(_common):
        assert added == [common]
        reads.append(True)
        return [page]

    monkeypatch.setattr(guest, "_read_app_management", read_after_add)
    monkeypatch.setattr(guest, "_find_terminal_toggle", lambda *_args: next(finds))
    monkeypatch.setattr(guest, "_add_terminal", lambda value: added.append(value))
    monkeypatch.setattr(guest, "_toggle_state", lambda *_args: True)
    monkeypatch.setattr(guest, "_fresh_terminal_enabled", lambda _common: True)

    assert guest.run_app_management("jaux") == "verified"
    assert reads == [True]


def test_disabled_terminal_is_pressed_once_then_reread(monkeypatch) -> None:
    common = object()
    first_roots = [object()]
    fresh_roots = [object()]
    first_toggle = object()
    fresh_toggle = object()
    reads = iter((first_roots, fresh_roots))
    finds = iter((first_toggle, fresh_toggle))
    pressed: list[object] = []

    monkeypatch.setattr(guest, "_require_console_identity", lambda _vm_user: None)
    monkeypatch.setattr(guest, "AXCommon", lambda: common)
    monkeypatch.setattr(guest, "_require_ax_trusted", lambda _common: None)
    monkeypatch.setattr(
        guest,
        "_all_system_settings_tree",
        lambda _common: (object(), [], []),
    )
    monkeypatch.setattr(guest, "_read_app_management", lambda _common: next(reads))
    monkeypatch.setattr(
        guest,
        "_find_terminal_toggle",
        lambda _common, _roots: next(finds),
    )
    monkeypatch.setattr(
        guest,
        "_toggle_state",
        lambda _common, toggle: toggle is fresh_toggle,
    )
    monkeypatch.setattr(guest, "_press", lambda _common, value: pressed.append(value))

    assert guest.run_app_management("stgf") == "verified"
    assert pressed == [first_toggle]


def test_missing_terminal_row_is_added_then_reread(monkeypatch) -> None:
    common = object()
    roots = [object()]
    toggle = object()
    finds = iter((None, toggle, toggle))
    added: list[object] = []

    monkeypatch.setattr(guest, "_require_console_identity", lambda _vm_user: None)
    monkeypatch.setattr(guest, "AXCommon", lambda: common)
    monkeypatch.setattr(guest, "_require_ax_trusted", lambda _common: None)
    monkeypatch.setattr(
        guest,
        "_all_system_settings_tree",
        lambda _common: (object(), [], []),
    )
    monkeypatch.setattr(guest, "_read_app_management", lambda _common: roots)
    monkeypatch.setattr(
        guest,
        "_find_terminal_toggle",
        lambda _common, _roots: next(finds),
    )
    monkeypatch.setattr(
        guest, "_add_terminal", lambda value: added.append(value), raising=False
    )
    monkeypatch.setattr(
        guest,
        "_toggle_state",
        lambda _common, element: True if element is toggle else None,
    )

    assert guest.run_app_management("stgf") == "verified"
    assert added == [common]


def test_enabled_terminal_is_only_reread(monkeypatch) -> None:
    common = object()
    roots = [object()]
    toggle = object()
    pressed: list[object] = []

    monkeypatch.setattr(guest, "_require_console_identity", lambda _vm_user: None)
    monkeypatch.setattr(guest, "AXCommon", lambda: common)
    monkeypatch.setattr(guest, "_require_ax_trusted", lambda _common: None)
    monkeypatch.setattr(
        guest,
        "_all_system_settings_tree",
        lambda _common: (object(), [], []),
    )
    monkeypatch.setattr(guest, "_read_app_management", lambda _common: roots)
    monkeypatch.setattr(
        guest,
        "_find_terminal_toggle",
        lambda _common, _roots: toggle,
    )
    monkeypatch.setattr(guest, "_toggle_state", lambda _common, _toggle: True)
    monkeypatch.setattr(guest, "_press", lambda _common, value: pressed.append(value))

    assert guest.run_app_management("stgf") == "verified"
    assert pressed == []
