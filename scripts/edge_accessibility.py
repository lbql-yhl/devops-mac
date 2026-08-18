#!/usr/bin/env python3
"""Semantic Accessibility driver for the guest Microsoft Edge.

UTM-10 has one explicit fixed-command exception that starts Edge with CDP.
Every URL navigation after that is allowed only while exactly one main Edge PID
is observed and remains unchanged.  Page interaction uses Accessibility;
coordinates, keyboard text entry, and clipboard transport are not used.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlparse


EDGE_BUNDLE_ID = "com.microsoft.edgemac"
ALLOWED_HOSTS = {"developer.apple.com", "appstoreconnect.apple.com"}
TEXT_ROLES = {"AXTextField", "AXSecureTextField", "AXComboBox"}
ACTION_ROLES = {
    "AXButton",
    "AXCheckBox",
    "AXRadioButton",
    "AXLink",
    "AXMenuItem",
    "AXPopUpButton",
    "AXRow",
}
MAX_NODES = 30_000
MINIMUM_SETTLE_SECONDS = 0.0
UTM10_EDGE_LAUNCH_SCRIPT = r'''pkill "Microsoft Edge"
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if ! /usr/bin/pgrep -x 'Microsoft Edge' >/dev/null 2>&1; then
    break
  fi
  /bin/sleep 1
done
if /usr/bin/pgrep -x 'Microsoft Edge' >/dev/null 2>&1; then
  exit 91
fi
nohup "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/edge-debug-profile \
  --no-first-run \
  --start-maximized \
  --force-renderer-accessibility \
  >/dev/null 2>&1 </dev/null &
'''


class EdgeAccessibilityError(RuntimeError):
    """Raised when an Edge page target is absent, ambiguous, or unsafe."""


def _edge_pids() -> tuple[int, ...]:
    result = subprocess.run(
        ["/usr/bin/pgrep", "-x", "Microsoft Edge"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise EdgeAccessibilityError("EDGE_PROCESS_READ_FAILED")
    pids: list[int] = []
    for line in result.stdout.splitlines():
        if line.strip().isdigit():
            pids.append(int(line.strip()))
    return tuple(sorted(set(pids)))


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise EdgeAccessibilityError("EDGE_URL_NOT_ALLOWED")


def _utm10_edge_ready(pid: int, runner: Callable[..., Any]) -> bool:
    process = runner(
        ["/bin/ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        return False
    command = process.stdout
    required = (
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "--remote-debugging-port=9222",
        "--user-data-dir=/tmp/edge-debug-profile",
        "--no-first-run",
        "--start-maximized",
    )
    if not all(value in command for value in required):
        return False
    listener = runner(
        ["/usr/sbin/lsof", "-nP", "-iTCP:9222", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
        check=False,
    )
    listener_pids = {
        int(line.strip()) for line in listener.stdout.splitlines() if line.strip().isdigit()
    }
    if listener.returncode != 0 or listener_pids != {pid}:
        return False
    version = runner(
        [
            "/usr/bin/curl",
            "-fsS",
            "--max-time",
            "5",
            "http://127.0.0.1:9222/json/version",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if version.returncode != 0:
        return False
    try:
        websocket = json.loads(version.stdout).get("webSocketDebuggerUrl")
    except (TypeError, ValueError):
        return False
    return isinstance(websocket, str) and websocket.startswith("ws://127.0.0.1:9222/")


def start_utm10_edge(
    *,
    pid_reader: Callable[[], tuple[int, ...]] = _edge_pids,
    runner: Callable[..., Any] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Run the fixed UTM-10 pkill/nohup commands and verify one CDP Edge."""
    launched = runner(
        ["/bin/zsh", "-lc", UTM10_EDGE_LAUNCH_SCRIPT],
        capture_output=True,
        text=True,
        check=False,
    )
    if launched.returncode != 0:
        raise EdgeAccessibilityError("EDGE_UTM10_LAUNCH_FAILED")
    for wait_seconds in (0, 5, 10):
        if wait_seconds:
            sleeper(wait_seconds)
        pids = pid_reader()
        if len(pids) == 1 and _utm10_edge_ready(pids[0], runner):
            return pids[0]
    raise EdgeAccessibilityError("EDGE_UTM10_START_UNVERIFIED")


def reuse_utm10_edge(
    *,
    pid_reader: Callable[[], tuple[int, ...]] = _edge_pids,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    """Reuse one already-running UTM-10 Edge without stopping or launching it."""
    pids = pid_reader()
    if len(pids) != 1:
        raise EdgeAccessibilityError("EDGE_REUSE_PROCESS_NOT_UNIQUE")
    if not _utm10_edge_ready(pids[0], runner):
        raise EdgeAccessibilityError("EDGE_REUSE_PROCESS_UNVERIFIED")
    return pids[0]


def edge_session_identity(
    expected_pid: int,
    *,
    pid_reader: Callable[[], tuple[int, ...]] = _edge_pids,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[int, str]:
    """Return the unique verified Edge PID and CDP WebSocket identity."""
    pids = pid_reader()
    if pids != (expected_pid,) or not _utm10_edge_ready(expected_pid, runner):
        raise EdgeAccessibilityError("EDGE_SESSION_IDENTITY_UNVERIFIED")
    version = runner(
        [
            "/usr/bin/curl",
            "-fsS",
            "--max-time",
            "5",
            "http://127.0.0.1:9222/json/version",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if version.returncode != 0:
        raise EdgeAccessibilityError("EDGE_SESSION_VERSION_UNAVAILABLE")
    try:
        websocket = json.loads(version.stdout).get("webSocketDebuggerUrl")
    except (TypeError, ValueError) as error:
        raise EdgeAccessibilityError("EDGE_SESSION_WEBSOCKET_INVALID") from error
    if not isinstance(websocket, str) or not websocket.startswith(
        "ws://127.0.0.1:9222/"
    ):
        raise EdgeAccessibilityError("EDGE_SESSION_WEBSOCKET_INVALID")
    return expected_pid, websocket


def open_url_in_existing_edge(
    url: str,
    *,
    pid_reader: Callable[[], tuple[int, ...]] = _edge_pids,
    runner: Callable[..., Any] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Open one approved URL while proving the same Edge process is reused."""
    _validate_url(url)
    before = pid_reader()
    if len(before) != 1:
        raise EdgeAccessibilityError("EDGE_PROCESS_NOT_UNIQUE")
    result = runner(
        ["/usr/bin/open", "-b", EDGE_BUNDLE_ID, url],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise EdgeAccessibilityError("EDGE_URL_COMMAND_FAILED")
    after = pid_reader()
    if after != before:
        raise EdgeAccessibilityError("EDGE_PROCESS_CHANGED")
    return before[0]


def _load_ax() -> tuple[Any, Any]:
    try:
        import ApplicationServices as ax
        from AppKit import NSRunningApplication

        return ax, NSRunningApplication
    except ModuleNotFoundError as error:
        if error.name not in {"ApplicationServices", "AppKit"}:
            raise
        install = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--user",
                "pyobjc-framework-ApplicationServices",
                "pyobjc-framework-Cocoa",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if install.returncode != 0:
            raise EdgeAccessibilityError("PYOBJC_INSTALL_FAILED") from error
        importlib.invalidate_caches()
        import ApplicationServices as ax
        from AppKit import NSRunningApplication

        return ax, NSRunningApplication


def _enable_manual_accessibility(
    ax: Any,
    pid: int,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    application = ax.AXUIElementCreateApplication(pid)
    error = ax.AXUIElementSetAttributeValue(
        application,
        "AXManualAccessibility",
        True,
    )
    unsupported = getattr(ax, "kAXErrorAttributeUnsupported", -25205)
    if error not in {ax.kAXErrorSuccess, unsupported}:
        raise EdgeAccessibilityError(f"EDGE_MANUAL_ACCESSIBILITY_FAILED={error}")


class EdgeAXBackend:
    """Small semantic API used by the four scripted Apple web workflows."""

    def __init__(
        self,
        *,
        settle_seconds: float = MINIMUM_SETTLE_SECONDS,
        expected_pid: int | None = None,
    ) -> None:
        self.ax, self.running_application = _load_ax()
        self.settle_seconds = max(MINIMUM_SETTLE_SECONDS, settle_seconds)
        pids = _edge_pids()
        if len(pids) != 1:
            raise EdgeAccessibilityError("EDGE_PROCESS_NOT_UNIQUE")
        if expected_pid is not None and pids != (expected_pid,):
            raise EdgeAccessibilityError("EDGE_PROCESS_CHANGED")
        self.pid = pids[0]
        _enable_manual_accessibility(self.ax, self.pid)

    def _attribute(self, element: Any, attribute: str) -> Any:
        error, value = self.ax.AXUIElementCopyAttributeValue(element, attribute, None)
        return value if error == self.ax.kAXErrorSuccess else None

    def _set(self, element: Any, attribute: str, value: Any) -> None:
        error = self.ax.AXUIElementSetAttributeValue(element, attribute, value)
        if error != self.ax.kAXErrorSuccess:
            raise EdgeAccessibilityError(f"AX_SET_FAILED={attribute}:{error}")

    def _press(self, element: Any) -> None:
        error = self.ax.AXUIElementPerformAction(element, self.ax.kAXPressAction)
        if error != self.ax.kAXErrorSuccess:
            raise EdgeAccessibilityError(f"AX_PRESS_FAILED={error}")

    def _ensure_frontmost(self) -> None:
        application = self.ax.AXUIElementCreateApplication(self.pid)
        if self._attribute(application, self.ax.kAXFrontmostAttribute) in {True, 1}:
            return
        self._set(application, self.ax.kAXFrontmostAttribute, True)
        if self._attribute(application, self.ax.kAXFrontmostAttribute) not in {True, 1}:
            raise EdgeAccessibilityError("EDGE_FRONTMOST_READBACK_FAILED")

    def _root(self) -> Any:
        if _edge_pids() != (self.pid,):
            raise EdgeAccessibilityError("EDGE_PROCESS_CHANGED")
        return self.ax.AXUIElementCreateApplication(self.pid)

    def _nodes(self, root: Any | None = None) -> list[Any]:
        queue = deque([root or self._root()])
        seen: set[int] = set()
        nodes: list[Any] = []
        while queue and len(nodes) < MAX_NODES:
            node = queue.popleft()
            identity = id(node)
            if identity in seen:
                continue
            seen.add(identity)
            nodes.append(node)
            children = self._attribute(node, self.ax.kAXChildrenAttribute)
            if children is not None and not isinstance(children, (str, bytes)):
                try:
                    queue.extend(iter(children))
                except TypeError:
                    pass
        return nodes

    def _strings(self, node: Any) -> tuple[str, ...]:
        values: list[str] = []
        for attribute in (
            self.ax.kAXTitleAttribute,
            self.ax.kAXDescriptionAttribute,
            self.ax.kAXHelpAttribute,
            self.ax.kAXValueAttribute,
            "AXIdentifier",
            "AXPlaceholderValue",
        ):
            value = self._attribute(node, attribute)
            if not isinstance(value, str):
                string_method = getattr(value, "string", None)
                if callable(string_method):
                    value = string_method()
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
        return tuple(dict.fromkeys(values))

    def _role(self, node: Any) -> str:
        value = self._attribute(node, self.ax.kAXRoleAttribute)
        return value if isinstance(value, str) else ""

    def _enabled(self, node: Any) -> bool:
        value = self._attribute(node, self.ax.kAXEnabledAttribute)
        return value is not False

    def page_text(self) -> str:
        parts: list[str] = []
        for node in self._nodes():
            parts.extend(self._strings(node))
        return "\n".join(dict.fromkeys(parts))

    def diagnostic_fingerprint(self) -> str:
        nodes = self._nodes()
        role_specs = (
            ("A", "AXApplication"),
            ("W", "AXWindow"),
            ("WA", "AXWebArea"),
            ("ST", "AXStaticText"),
            ("TF", "AXTextField"),
            ("B", "AXButton"),
            ("G", "AXGroup"),
        )
        role_counts = {
            abbreviation: sum(1 for node in nodes if self._role(node) == role)
            for abbreviation, role in role_specs
        }
        page_text = "\n".join(
            value for node in nodes for value in self._strings(node)
        ).casefold()
        flags = "".join(
            "1" if text.casefold() in page_text else "0"
            for text in (
                "Sign in to Apple Developer",
                "Email or Phone Number",
                "Membership details",
            )
        )
        counts = "-".join(
            f"{abbreviation}{role_counts[abbreviation]}"
            for abbreviation, _role in role_specs
        )
        return f"N{len(nodes)}-{counts}-F{flags}"

    def has_text(self, text: str) -> bool:
        needle = text.casefold()
        return any(needle in value.casefold() for node in self._nodes() for value in self._strings(node))

    def wait_for_text(self, texts: str | Sequence[str], *, timeout: float = 60) -> str:
        expected = (texts,) if isinstance(texts, str) else tuple(texts)
        deadline = time.monotonic() + timeout
        while True:
            current = self.page_text().casefold()
            for text in expected:
                if text.casefold() in current:
                    return text
            if time.monotonic() >= deadline:
                raise EdgeAccessibilityError("PAGE_TEXT_TIMEOUT=" + "|".join(expected))
            time.sleep(0.05)

    def _matching_nodes(
        self,
        texts: str | Sequence[str],
        *,
        roles: Iterable[str] | None = None,
        exact: bool = False,
        enabled_only: bool = True,
    ) -> list[Any]:
        expected = (texts,) if isinstance(texts, str) else tuple(texts)
        allowed = set(roles) if roles is not None else None
        matches: list[Any] = []
        for node in self._nodes():
            if allowed is not None and self._role(node) not in allowed:
                continue
            if enabled_only and not self._enabled(node):
                continue
            values = self._strings(node)
            if any(
                (value.casefold() == text.casefold() if exact else text.casefold() in value.casefold())
                for value in values
                for text in expected
            ):
                matches.append(node)
        return matches

    def click_text(
        self,
        texts: str | Sequence[str],
        *,
        exact: bool = False,
        optional: bool = False,
    ) -> bool:
        matches = self._matching_nodes(texts, roles=ACTION_ROLES, exact=exact)
        if not matches and optional:
            return False
        if len(matches) != 1:
            all_matches = self._matching_nodes(
                texts,
                roles=ACTION_ROLES,
                exact=exact,
                enabled_only=False,
            )
            raise EdgeAccessibilityError(
                f"CLICK_TARGET_COUNT={len(matches)}:{len(all_matches)}"
            )
        self._press(matches[0])
        return True

    def set_field(
        self,
        labels: str | Sequence[str],
        value: str,
        *,
        native_text: bool = False,
    ) -> None:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise EdgeAccessibilityError("FIELD_VALUE_INVALID")
        matches = self._matching_nodes(labels, roles=TEXT_ROLES)
        fallback_count = len(matches)
        extra_role_counts = (0, 0, 0)
        if not matches:
            all_nodes = self._nodes()
            fields = [
                node
                for node in all_nodes
                if self._role(node) in TEXT_ROLES and self._enabled(node)
            ]
            fallback_count = len(fields)
            extra_role_counts = tuple(
                sum(1 for node in all_nodes if self._role(node) == role)
                for role in ("AXTextArea", "AXSearchField", "AXGroup")
            )
            if len(fields) == 1:
                matches = fields
        if len(matches) != 1:
            raise EdgeAccessibilityError(
                "FIELD_TARGET_COUNT="
                f"{len(matches)}:{fallback_count}:"
                + ":".join(str(count) for count in extra_role_counts)
            )
        self._set(matches[0], self.ax.kAXFocusedAttribute, True)
        if native_text:
            raise EdgeAccessibilityError("SYSTEM_KEYBOARD_TEXT_FORBIDDEN")
        self._set(matches[0], self.ax.kAXValueAttribute, value)
        current = self._attribute(matches[0], self.ax.kAXValueAttribute)
        if isinstance(current, str) and current != value:
            masked = (
                self._role(matches[0]) == "AXSecureTextField"
                and len(current) == len(value)
                and set(current).issubset({"•", "●", "·", "*"})
            )
            if not masked:
                raise EdgeAccessibilityError("FIELD_READBACK_MISMATCH")

    def set_checkbox(self, text: str, checked: bool = True) -> None:
        matches = self._matching_nodes(text, roles={"AXCheckBox"})
        if len(matches) != 1:
            raise EdgeAccessibilityError(f"CHECKBOX_TARGET_COUNT={len(matches)}")
        current = self._attribute(matches[0], self.ax.kAXValueAttribute)
        current_checked = current in {True, 1, "1"}
        if current_checked != checked:
            self._press(matches[0])
        reread = self._attribute(matches[0], self.ax.kAXValueAttribute)
        if (reread in {True, 1, "1"}) != checked:
            raise EdgeAccessibilityError("CHECKBOX_READBACK_MISMATCH")

    def select_radio(self, question: str, answer: str) -> None:
        question_nodes = self._matching_nodes(question)
        if len(question_nodes) != 1:
            raise EdgeAccessibilityError(f"QUESTION_TARGET_COUNT={len(question_nodes)}")
        parent = question_nodes[0]
        for _ in range(8):
            candidates = []
            for node in self._nodes(parent):
                if self._role(node) != "AXRadioButton" or not self._enabled(node):
                    continue
                if any(answer.casefold() == value.casefold() for value in self._strings(node)):
                    candidates.append(node)
            if len(candidates) == 1:
                current = self._attribute(candidates[0], self.ax.kAXValueAttribute)
                if current not in {True, 1, "1"}:
                    self._press(candidates[0])
                return
            parent = self._attribute(parent, self.ax.kAXParentAttribute)
            if parent is None:
                break
        raise EdgeAccessibilityError("RADIO_ANSWER_NOT_UNIQUE")

    def select_option(self, label: str, option: str) -> None:
        self.click_text(label)
        self.click_text(option, exact=True)
        if not self.has_text(option):
            raise EdgeAccessibilityError("OPTION_READBACK_FAILED")

    def read_value_after_label(self, label: str) -> str:
        lines = [line.strip() for line in self.page_text().splitlines() if line.strip()]
        matches: list[str] = []
        for index, line in enumerate(lines):
            if line.casefold() == label.casefold() and index + 1 < len(lines):
                matches.append(lines[index + 1])
            elif line.casefold().startswith(label.casefold() + ":"):
                matches.append(line.split(":", 1)[1].strip())
        matches = [value for value in dict.fromkeys(matches) if value]
        if len(matches) != 1:
            raise EdgeAccessibilityError(f"LABEL_VALUE_COUNT={label}:{len(matches)}")
        return matches[0]

    def choose_file(self, button_text: str, file_path: str) -> None:
        target = Path(file_path)
        if not target.is_absolute() or target.name != "CertificateSigningRequest.certSigningRequest":
            raise EdgeAccessibilityError("FILE_TARGET_INVALID")
        self.click_text(button_text)
        matches = self._matching_nodes(target.name, exact=True)
        if len(matches) != 1:
            raise EdgeAccessibilityError(f"FILE_PICKER_TARGET_COUNT={len(matches)}")
        self._press(matches[0])
        self.click_text(("Open", "Choose"), exact=True)

    def open_url(self, url: str) -> None:
        pid = open_url_in_existing_edge(url)
        if pid != self.pid:
            raise EdgeAccessibilityError("EDGE_PROCESS_CHANGED")

    def capture_png(self, path: str) -> None:
        target = Path(path)
        if not target.is_absolute() or target.name != "05-small-business.png":
            raise EdgeAccessibilityError("SCREENSHOT_PATH_INVALID")
        result = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-t", "png", str(target)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            raise EdgeAccessibilityError("SCREENSHOT_CAPTURE_FAILED")
        target.chmod(0o600)
