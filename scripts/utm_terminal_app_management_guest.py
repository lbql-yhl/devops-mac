#!/usr/bin/env python3
"""AX-only guest program for the one existing Terminal App Management row."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import time
from collections import deque
from typing import Any, Iterator


TERMINAL_MANAGEMENT_MARKER = "TERMINAL_APP_MANAGEMENT=verified"
TERMINAL_APP_PATH = "/System/Applications/Utilities/Terminal.app"
CONTROL_WAIT_SECONDS = 15.0


class TerminalAppManagementGuestError(RuntimeError):
    """Raised when the App Management page or Terminal row is not exact."""


def _configured_guest_password() -> str:
    """Read the host-supplied runtime value without any repository fallback."""
    value = str(os.environ.get("SUBMISSION_GUEST_PASSWORD") or "").strip()
    if re.fullmatch(r"[0-9]{4}", value) is None:
        raise TerminalAppManagementGuestError(
            "SUBMISSION_GUEST_PASSWORD runtime configuration is missing or invalid"
        )
    return value


class AXCommon:
    """Small self-contained AX adapter used by this single stdin process."""

    def __init__(self) -> None:
        import ApplicationServices
        from AppKit import NSRunningApplication

        self.AX = ApplicationServices
        self.NSRunningApplication = NSRunningApplication

    def copy_attribute(self, element: Any, attribute: str) -> Any:
        try:
            error, value = self.AX.AXUIElementCopyAttributeValue(
                element, attribute, None
            )
        except Exception:
            return None
        return value if error == self.AX.kAXErrorSuccess else None

    def copy_actions(self, element: Any) -> list[str]:
        try:
            error, actions = self.AX.AXUIElementCopyActionNames(element, None)
        except Exception:
            return []
        if error != self.AX.kAXErrorSuccess or not actions:
            return []
        return [str(action) for action in actions]

    def describe_element(self, element: Any) -> dict[str, Any]:
        def text(attribute: str) -> str | None:
            value = self.copy_attribute(element, attribute)
            return str(value).strip() if value is not None else None

        return {
            "role": text(self.AX.kAXRoleAttribute),
            "subrole": text(self.AX.kAXSubroleAttribute),
            "title": text(self.AX.kAXTitleAttribute),
            "value": text(self.AX.kAXValueAttribute),
            "description": text(self.AX.kAXDescriptionAttribute),
            "help": text(self.AX.kAXHelpAttribute),
            "identifier": text(self.AX.kAXIdentifierAttribute),
            "enabled": self.copy_attribute(element, self.AX.kAXEnabledAttribute),
        }

    def iter_accessibility_tree(
        self, roots: list[Any], max_nodes: int = 2500
    ) -> Iterator[tuple[Any, int]]:
        queue = deque((root, 0) for root in roots)
        visited = 0
        while queue and visited < max_nodes:
            element, depth = queue.popleft()
            visited += 1
            yield element, depth
            children = self.copy_attribute(element, self.AX.kAXChildrenAttribute)
            if children:
                try:
                    queue.extend((child, depth + 1) for child in children)
                except TypeError:
                    pass

    def get_running_system_settings(self) -> Any:
        applications = self.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            "com.apple.systempreferences"
        )
        if len(applications) != 1:
            raise TerminalAppManagementGuestError(
                "System Settings process is not unique"
            )
        return applications[0]


def _text_values(info: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(value).strip()
        for value in (
            info.get("title"),
            info.get("value"),
            info.get("description"),
            info.get("help"),
            info.get("identifier"),
        )
        if value is not None and str(value).strip()
    )


def _tree(
    common: Any, roots: list[Any], max_nodes: int = 2500
) -> list[tuple[Any, dict[str, Any]]]:
    return [
        (element, common.describe_element(element))
        for element, _ in common.iter_accessibility_tree(roots, max_nodes=max_nodes)
    ]


def _system_settings_roots(common: Any) -> list[Any]:
    application = common.get_running_system_settings()
    app_element = common.AX.AXUIElementCreateApplication(
        int(application.processIdentifier())
    )
    windows = common.copy_attribute(app_element, common.AX.kAXWindowsAttribute)
    roots = list(windows) if windows else []
    page_roots = [
        root
        for root in roots
        if (
            common.describe_element(root).get("role") == "AXWindow"
            and common.describe_element(root).get("subrole") == "AXStandardWindow"
            and common.describe_element(root).get("identifier") == "main"
        )
    ]
    if len(page_roots) != 1:
        raise TerminalAppManagementGuestError(
            "System Settings App Management window count is "
            f"{len(page_roots)} (all_windows={len(roots)})"
        )
    return page_roots


def _read_app_management(common: Any) -> list[Any]:
    roots = _system_settings_roots(common)
    texts = {
        text.casefold()
        for _, info in _tree(common, roots)
        for text in _text_values(info)
    }
    if not any(
        "update or delete other applications" in text for text in texts
    ):
        raise TerminalAppManagementGuestError(
            "System Settings did not open App Management"
        )
    return roots


def _element_key(element: Any) -> str:
    return repr(element)


def _find_terminal_toggle(common: Any, roots: list[Any]) -> Any:
    terminal_nodes = [
        element
        for element, info in _tree(common, roots)
        if "Terminal" in _text_values(info)
    ]
    if not terminal_nodes:
        return None
    toggles: dict[str, Any] = {}
    for terminal in terminal_nodes:
        ancestor = terminal
        for _ in range(8):
            for candidate, info in _tree(common, [ancestor], max_nodes=120):
                if info.get("role") in {"AXCheckBox", "AXSwitch"}:
                    toggles[_element_key(candidate)] = candidate
            if toggles:
                break
            ancestor = common.copy_attribute(ancestor, common.AX.kAXParentAttribute)
            if ancestor is None:
                break
    if len(toggles) != 1:
        if not toggles:
            raise TerminalAppManagementGuestError(
                "App Management Terminal label has no unique toggle"
            )
        raise TerminalAppManagementGuestError(
            "App Management contains multiple Terminal toggles"
        )
    return next(iter(toggles.values()))


def _all_system_settings_tree(
    common: Any, *, max_nodes: int = 5000
) -> tuple[Any, list[Any], list[tuple[Any, dict[str, Any]]]]:
    application = common.get_running_system_settings()
    app_element = common.AX.AXUIElementCreateApplication(
        int(application.processIdentifier())
    )
    windows = common.copy_attribute(app_element, common.AX.kAXWindowsAttribute) or []
    roots = list(windows)
    return app_element, roots, _tree(common, roots, max_nodes=max_nodes)


def _unique_control(
    tree: list[tuple[Any, dict[str, Any]]],
    predicate: Any,
    error_code: str,
) -> Any:
    matches = [element for element, info in tree if predicate(info)]
    if len(matches) != 1:
        raise TerminalAppManagementGuestError(f"{error_code} count is {len(matches)}")
    return matches[0]


def _press_control(common: Any, element: Any, error_code: str) -> None:
    if common.AX.kAXPressAction not in common.copy_actions(element):
        raise TerminalAppManagementGuestError(f"{error_code} has no AXPress action")
    error = common.AX.AXUIElementPerformAction(element, common.AX.kAXPressAction)
    if error != common.AX.kAXErrorSuccess:
        raise TerminalAppManagementGuestError(
            f"{error_code} AXPress failed with code {error}"
        )


def _wait_for_control(common: Any, predicate: Any, error_code: str) -> Any:
    deadline = time.monotonic() + CONTROL_WAIT_SECONDS
    last_count = 0
    while True:
        _, _, tree = _all_system_settings_tree(common)
        matches = [element for element, info in tree if predicate(info)]
        last_count = len(matches)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 or time.monotonic() >= deadline:
            raise TerminalAppManagementGuestError(
                f"{error_code} count is {last_count}"
            )
        time.sleep(0.25)


def _wait_for_authentication_or_open_panel(common: Any) -> tuple[Any, Any] | None:
    deadline = time.monotonic() + CONTROL_WAIT_SECONDS
    password_count = 0
    modify_count = 0
    open_panel_count = 0
    while True:
        _, _, tree = _all_system_settings_tree(common)
        open_panels = [
            element
            for element, info in tree
            if info.get("role") == "AXWindow"
            and info.get("identifier") == "open-panel"
        ]
        passwords = [
            element
            for element, info in tree
            if info.get("role") == "AXTextField"
            and info.get("subrole") == "AXSecureTextField"
            and info.get("description") == "Password"
        ]
        modify_buttons = [
            element
            for element, info in tree
            if info.get("role") == "AXButton"
            and info.get("title") == "Modify Settings"
        ]
        open_panel_count = len(open_panels)
        password_count = len(passwords)
        modify_count = len(modify_buttons)
        if open_panel_count == 1:
            return None
        if open_panel_count > 1 or password_count > 1 or modify_count > 1:
            break
        if password_count == 1 and modify_count == 1:
            return passwords[0], modify_buttons[0]
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    raise TerminalAppManagementGuestError(
        "App Management authentication state is not unique "
        f"(password={password_count}, modify={modify_count}, "
        f"open_panel={open_panel_count})"
    )


def _send_go_to_folder_shortcut(common: Any) -> None:
    import Quartz

    app_element, _, _ = _all_system_settings_tree(common)
    error = common.AX.AXUIElementSetAttributeValue(
        app_element, common.AX.kAXFrontmostAttribute, True
    )
    if error != common.AX.kAXErrorSuccess:
        raise TerminalAppManagementGuestError(
            f"System Settings frontmost failed with code {error}"
        )
    flags = Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskShift
    for pressed in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, 5, pressed)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _ax_pair(common: Any, value: Any, value_type: Any) -> tuple[float, float]:
    try:
        result = common.AX.AXValueGetValue(value, value_type, None)
    except Exception as error:
        raise TerminalAppManagementGuestError("Terminal path geometry is unreadable") from error
    raw = result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], (bool, int)):
        if not result[0]:
            raise TerminalAppManagementGuestError("Terminal path geometry is unreadable")
        raw = result[1]
    for first, second in (("x", "y"), ("width", "height")):
        if hasattr(raw, first) and hasattr(raw, second):
            return float(getattr(raw, first)), float(getattr(raw, second))
    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        return float(raw[0]), float(raw[1])
    raise TerminalAppManagementGuestError("Terminal path geometry is unreadable")


def _double_click_terminal_path(common: Any, element: Any) -> None:
    import Quartz

    position = common.copy_attribute(element, common.AX.kAXPositionAttribute)
    size = common.copy_attribute(element, common.AX.kAXSizeAttribute)
    x, y = _ax_pair(common, position, common.AX.kAXValueCGPointType)
    width, height = _ax_pair(common, size, common.AX.kAXValueCGSizeType)
    point = (x + width / 2, y + height / 2)
    for click_count in (1, 2):
        for event_type in (
            Quartz.kCGEventLeftMouseDown,
            Quartz.kCGEventLeftMouseUp,
        ):
            event = Quartz.CGEventCreateMouseEvent(
                None, event_type, point, Quartz.kCGMouseButtonLeft
            )
            Quartz.CGEventSetIntegerValueField(
                event, Quartz.kCGMouseEventClickState, click_count
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.08)


def _press_unique_open_button(common: Any) -> None:
    deadline = time.monotonic() + CONTROL_WAIT_SECONDS
    while True:
        _, _, tree = _all_system_settings_tree(common)
        open_panels = [
            element
            for element, info in tree
            if info.get("role") == "AXWindow"
            and info.get("identifier") == "open-panel"
        ]
        if not open_panels:
            return
        if len(open_panels) > 1:
            raise TerminalAppManagementGuestError(
                f"App Management open panel count is {len(open_panels)}"
            )
        open_buttons = [
            element
            for element, info in tree
            if info.get("role") == "AXButton"
            and any(text in {"Open", "打开"} for text in _text_values(info))
            and info.get("enabled") is not False
        ]
        if len(open_buttons) == 1:
            _press_control(common, open_buttons[0], "App Management Open button")
            break
        if len(open_buttons) > 1 or time.monotonic() >= deadline:
            raise TerminalAppManagementGuestError(
                f"App Management Open button count is {len(open_buttons)}"
            )
        time.sleep(0.25)

    while True:
        _, _, tree = _all_system_settings_tree(common)
        open_panel_count = sum(
            1
            for _, info in tree
            if info.get("role") == "AXWindow"
            and info.get("identifier") == "open-panel"
        )
        if open_panel_count == 0:
            return
        if open_panel_count > 1 or time.monotonic() >= deadline:
            raise TerminalAppManagementGuestError(
                "App Management open panel remained after Open "
                f"(count={open_panel_count})"
            )
        time.sleep(0.25)


def _add_terminal(common: Any) -> None:
    _, _, current_tree = _all_system_settings_tree(common)
    existing_open_panels = [
        element
        for element, info in current_tree
        if info.get("role") == "AXWindow"
        and info.get("identifier") == "open-panel"
    ]
    if len(existing_open_panels) > 1:
        raise TerminalAppManagementGuestError(
            f"App Management open panel count is {len(existing_open_panels)}"
        )
    ready_terminal_nodes = [
        element
        for element, info in current_tree
        if info.get("role") == "AXStaticText"
        and info.get("value") == "Terminal"
    ]
    ready_open_buttons = [
        element
        for element, info in current_tree
        if info.get("role") == "AXButton"
        and any(text in {"Open", "打开"} for text in _text_values(info))
        and info.get("enabled") is not False
    ]
    if existing_open_panels and (
        len(ready_terminal_nodes) > 1 or len(ready_open_buttons) > 1
    ):
        raise TerminalAppManagementGuestError(
            "ready Terminal selection is not unique "
            f"(terminal={len(ready_terminal_nodes)}, open={len(ready_open_buttons)})"
        )
    terminal_already_ready = bool(
        existing_open_panels
        and len(ready_terminal_nodes) == 1
        and len(ready_open_buttons) == 1
    )
    if not existing_open_panels:
        roots = _read_app_management(common)
        page_tree = _tree(common, roots)
        add_button = _unique_control(
            page_tree,
            lambda info: info.get("role") == "AXButton"
            and info.get("description") == "Add",
            "App Management Add button",
        )
        _press_control(common, add_button, "App Management Add button")

        authentication = _wait_for_authentication_or_open_panel(common)
        if authentication is not None:
            password, modify = authentication
            error = common.AX.AXUIElementSetAttributeValue(
                password,
                common.AX.kAXValueAttribute,
                _configured_guest_password(),
            )
            if error != common.AX.kAXErrorSuccess:
                raise TerminalAppManagementGuestError(
                    f"App Management password write failed with code {error}"
                )
            _press_control(common, modify, "Modify Settings button")
            _wait_for_control(
                common,
                lambda info: info.get("role") == "AXWindow"
                and info.get("identifier") == "open-panel",
                "App Management open panel",
            )
    if terminal_already_ready:
        _press_unique_open_button(common)
    else:
        _send_go_to_folder_shortcut(common)
        path_field = _wait_for_control(
            common,
            lambda info: info.get("role") == "AXTextField"
            and info.get("identifier") == "PathTextField",
            "Go to Folder path field",
        )
        error = common.AX.AXUIElementSetAttributeValue(
            path_field, common.AX.kAXValueAttribute, TERMINAL_APP_PATH
        )
        if error != common.AX.kAXErrorSuccess:
            raise TerminalAppManagementGuestError(
                f"Terminal path write failed with code {error}"
            )
        terminal = _wait_for_control(
            common,
            lambda info: info.get("role") == "AXStaticText"
            and info.get("value") == "Terminal",
            "Terminal path completion",
        )
        _double_click_terminal_path(common, terminal)
        _press_unique_open_button(common)

    deadline = time.monotonic() + CONTROL_WAIT_SECONDS
    while True:
        try:
            roots = _read_app_management(common)
            if _find_terminal_toggle(common, roots) is not None:
                return
        except TerminalAppManagementGuestError:
            pass
        if time.monotonic() >= deadline:
            raise TerminalAppManagementGuestError(
                "Terminal row was not added to App Management"
            )
        time.sleep(0.5)


def _toggle_state(common: Any, element: Any) -> bool | None:
    value = common.copy_attribute(element, common.AX.kAXValueAttribute)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().casefold() if value is not None else ""
    if normalized in {"1", "true", "on", "yes", "selected"}:
        return True
    if normalized in {"0", "false", "off", "no", "not selected"}:
        return False
    return None


def _press(common: Any, element: Any) -> None:
    if common.AX.kAXPressAction not in common.copy_actions(element):
        raise TerminalAppManagementGuestError("Terminal toggle has no AXPress action")
    error = common.AX.AXUIElementPerformAction(element, common.AX.kAXPressAction)
    if error != common.AX.kAXErrorSuccess:
        raise TerminalAppManagementGuestError(
            f"Terminal toggle AXPress failed with code {error}"
        )


def _require_console_identity(vm_user: str) -> None:
    console_user = subprocess.run(
        ["/usr/bin/stat", "-f", "%Su", "/dev/console"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    uid = int(
        subprocess.run(
            ["/usr/bin/id", "-u", vm_user],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    if console_user != vm_user or os.getuid() != uid:
        raise TerminalAppManagementGuestError(
            "AX process is not in the exact console-user session"
        )


def _require_ax_trusted(common: Any) -> None:
    trusted = common.AX.AXIsProcessTrustedWithOptions(
        {common.AX.kAXTrustedCheckOptionPrompt: False}
    )
    if not trusted:
        raise TerminalAppManagementGuestError(
            "guest SSH Accessibility permission is unavailable"
        )


def _fresh_terminal_enabled(common: Any) -> bool:
    roots = _read_app_management(common)
    toggle = _find_terminal_toggle(common, roots)
    return _toggle_state(common, toggle) is True


def run_app_management(vm_user: str) -> str:
    _require_console_identity(vm_user)
    common = AXCommon()
    _require_ax_trusted(common)
    _, _, startup_tree = _all_system_settings_tree(common)
    startup_open_panels = [
        element
        for element, info in startup_tree
        if info.get("role") == "AXWindow"
        and info.get("identifier") == "open-panel"
    ]
    if len(startup_open_panels) > 1:
        raise TerminalAppManagementGuestError(
            f"App Management open panel count is {len(startup_open_panels)}"
        )
    roots = (
        _system_settings_roots(common)
        if startup_open_panels
        else _read_app_management(common)
    )
    toggle = _find_terminal_toggle(common, roots)
    if toggle is None:
        _add_terminal(common)
        roots = _read_app_management(common)
        toggle = _find_terminal_toggle(common, roots)
        if toggle is None:
            raise TerminalAppManagementGuestError(
                "Terminal row was not added to App Management"
            )
    state = _toggle_state(common, toggle)
    if state is None:
        raise TerminalAppManagementGuestError(
            "Terminal App Management switch state is unavailable"
        )
    if state is False:
        _press(common, toggle)

    deadline = time.monotonic() + 20
    while True:
        if _fresh_terminal_enabled(common):
            return "verified"
        if time.monotonic() >= deadline:
            raise TerminalAppManagementGuestError(
                "fresh App Management verification failed"
            )
        time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the existing Terminal switch")
    parser.add_argument("--vm-user", required=True)
    args = parser.parse_args()
    try:
        run_app_management(args.vm_user)
    except Exception as error:
        reason = str(error).replace("\n", " ")[:240]
        print(f"APP_MANAGEMENT_ERROR={reason}")
        return 1
    print(TERMINAL_MANAGEMENT_MARKER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
