#!/usr/bin/env python3
"""UTM-1 host-side Accessibility helper.

This helper drives only semantic Accessibility controls in the UTM app. It
never opens a .utm bundle, starts a VM, or writes UTM preferences directly.
The target is locked by the exact ``UTM – <vm_name>`` window title.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable


MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
VM_RE = re.compile(r"^[a-z]{4}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
WAIT_SECONDS = 3.0


class MACSequenceError(RuntimeError):
    """Raised when a saved Network randomization did not change the MAC."""


class AXTargetError(RuntimeError):
    """Raised when semantic Accessibility lookup is not unique."""


def parse_utm_status(output: str) -> str:
    """Parse one exact ``utmctl status`` result.

    UTM normally emits one state. Multiple non-empty lines are treated as an
    ambiguous read rather than selecting the first line.
    """
    states = [line.strip().lower() for line in output.splitlines() if line.strip()]
    if len(states) != 1 or states[0] not in {"started", "running", "stopped"}:
        raise RuntimeError(f"ambiguous UTM status: {states!r}")
    return states[0]


def running_status_verified(states: Iterable[str]) -> bool:
    """Return true only when every independent status read is running."""
    values = [str(state).strip().lower() for state in states]
    return bool(values) and all(value in {"started", "running"} for value in values)


def stopped_status_verified(states: Iterable[str]) -> bool:
    """Return true only when every independent status read is stopped."""
    values = [str(state).strip().lower() for state in states]
    return bool(values) and all(value == "stopped" for value in values)


def verify_mac_sequence(values: Iterable[str]) -> list[str]:
    """Validate the initial MAC plus each independently saved random value."""
    normalized = [normalize_mac(value) for value in values]
    if len(normalized) < 2:
        raise MACSequenceError("at least one randomization round is required")
    for index, value in enumerate(normalized):
        if not is_valid_mac(value):
            raise MACSequenceError(f"invalid MAC at round {index}")
        if index and value == normalized[index - 1]:
            raise MACSequenceError(f"MAC round {index} unchanged; expected changed value")
    return normalized


def require_unique_ax_node(nodes: Iterable[Any], **criteria: str) -> Any:
    """Return exactly one semantic node, refusing missing/ambiguous controls."""
    matches = matching_ax_nodes(nodes, **criteria)
    if len(matches) != 1:
        raise AXTargetError(f"AX target count={len(matches)} criteria={criteria!r}")
    return matches[0]


def require_unique_labeled_node(
    nodes: Iterable[Any], labels: Iterable[str], *, description: bool = False
) -> Any:
    """Match one control by one of the localized exact labels."""
    wanted = {str(label).casefold() for label in labels}
    matches = []
    for node in nodes:
        fields = (_dict_field(node, "description"),) if description else (
            _dict_field(node, "title"),
            _dict_field(node, "value"),
            _dict_field(node, "description"),
            _dict_field(node, "identifier"),
        )
        if any(field.casefold() in wanted for field in fields if field):
            matches.append(node)
    if len(matches) != 1:
        raise AXTargetError(f"AX label target count={len(matches)} labels={sorted(wanted)!r}")
    return matches[0]


def path_match_count(nodes: Iterable[Any], path: str) -> int:
    """Count exact normalized path values in a normalized AX fixture/tree."""
    wanted = str(Path(path).expanduser().resolve())
    count = 0
    for node in nodes:
        fields = (
            _dict_field(node, "title"),
            _dict_field(node, "value"),
            _dict_field(node, "description"),
        )
        if any(field and str(Path(field).expanduser().resolve()) == wanted for field in fields):
            count += 1
    return count


def checkbox_is_checked(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on", "checked"}


def normalize_mac(value: str) -> str:
    compact = re.sub(r"[:-]", "", value.strip().lower())
    if not re.fullmatch(r"[0-9a-f]{12}", compact):
        raise ValueError("invalid MAC")
    return ":".join(compact[i : i + 2] for i in range(0, 12, 2))


def is_valid_mac(value: str) -> bool:
    try:
        mac = normalize_mac(value)
    except ValueError:
        return False
    first = int(mac[:2], 16)
    return bool(first & 0x02) and not bool(first & 0x01)


def unique_mac(current: str, used: set[str]) -> str:
    current = normalize_mac(current)
    used = {normalize_mac(value) for value in used}
    for _ in range(128):
        raw = bytearray(secrets.token_bytes(6))
        raw[0] = (raw[0] & 0xFC) | 0x02
        candidate = ":".join(f"{part:02x}" for part in raw)
        if candidate != current and candidate not in used and is_valid_mac(candidate):
            return candidate
    raise RuntimeError("could not generate a unique locally-administered MAC")


def is_valid_vm_name(value: str) -> bool:
    return bool(VM_RE.fullmatch(value))


def _dict_field(node: Any, key: str) -> str:
    if isinstance(node, dict):
        return str(node.get(key) or "")
    return ""


def matching_ax_nodes(
    nodes: Iterable[Any],
    *,
    role: str | None = None,
    title: str | None = None,
    description: str | None = None,
    identifier: str | None = None,
    value: str | None = None,
) -> list[Any]:
    """Match test dictionaries and normalized AX metadata by exact fields."""
    result = []
    for node in nodes:
        if role is not None and _dict_field(node, "role") != role:
            continue
        if title is not None and _dict_field(node, "title") != title:
            continue
        if description is not None and _dict_field(node, "description") != description:
            continue
        if identifier is not None and _dict_field(node, "identifier") != identifier:
            continue
        if value is not None and _dict_field(node, "value") != value:
            continue
        result.append(node)
    return result


class AXUnavailable(RuntimeError):
    pass


def _load_ax() -> tuple[Any, Any]:
    try:
        import ApplicationServices as application_services
        from AppKit import NSRunningApplication
    except ImportError as error:  # pragma: no cover - host-only dependency
        raise AXUnavailable("PyObjC ApplicationServices/AppKit is required") from error
    return application_services, NSRunningApplication


def _copy(ax: Any, element: Any, attribute: Any) -> Any:
    error, value = ax.AXUIElementCopyAttributeValue(element, attribute, None)
    return value if error == ax.kAXErrorSuccess else None


def _children(ax: Any, element: Any) -> list[Any]:
    value = _copy(ax, element, ax.kAXChildrenAttribute)
    if value is None:
        return []
    try:
        return list(value)
    except TypeError:
        return []


def ax_tree(ax: Any, root: Any, max_nodes: int = 20_000) -> list[Any]:
    """Breadth-first traversal; PyObjC NSArray must be treated as iterable."""
    result: list[Any] = []
    queue = [root]
    seen: set[int] = set()
    while queue and len(result) < max_nodes:
        element = queue.pop(0)
        identity = id(element)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(element)
        queue.extend(_children(ax, element))
    return result


def ax_text(ax: Any, element: Any) -> tuple[str, str, str, str]:
    return tuple(
        str(_copy(ax, element, attribute) or "")
        for attribute in (
            ax.kAXTitleAttribute,
            ax.kAXValueAttribute,
            ax.kAXDescriptionAttribute,
            ax.kAXIdentifierAttribute,
        )
    )


def ax_role(ax: Any, element: Any) -> str:
    return str(_copy(ax, element, ax.kAXRoleAttribute) or "")


def ax_actions(ax: Any, element: Any) -> list[str]:
    try:
        error, values = ax.AXUIElementCopyActionNames(element, None)
    except Exception:
        return []
    if error != ax.kAXErrorSuccess or values is None:
        return []
    try:
        return [str(value) for value in values]
    except TypeError:
        return []


def ax_set_value(ax: Any, element: Any, value: Any) -> None:
    """Set a semantic AX value only when the control accepts the write."""
    # The setter is authoritative and returns an AX error when a control is
    # not writable; no coordinate or keyboard fallback is permitted.
    error = ax.AXUIElementSetAttributeValue(element, ax.kAXValueAttribute, value)
    if error != ax.kAXErrorSuccess:
        raise RuntimeError(f"AXValue set failed: {error}")


def live_labeled_nodes(
    ax: Any,
    root: Any,
    labels: Iterable[str],
    *,
    exact: bool = True,
    roles: set[str] | None = None,
) -> list[Any]:
    wanted = {str(label).casefold() for label in labels}
    result = []
    for element in ax_tree(ax, root):
        if roles is not None and ax_role(ax, element) not in roles:
            continue
        fields = ax_text(ax, element)
        matches = (
            any(field.casefold() in wanted for field in fields if field)
            if exact
            else any(any(label in field.casefold() for label in wanted) for field in fields if field)
        )
        if matches:
            result.append(element)
    return result


def press_unique_live(
    ax: Any,
    root: Any,
    labels: Iterable[str],
    *,
    exact: bool = True,
    roles: set[str] | None = None,
) -> Any:
    labels = tuple(labels)
    matches = live_labeled_nodes(ax, root, labels, exact=True, roles=roles)
    if not matches and not exact:
        matches = live_labeled_nodes(ax, root, labels, exact=False, roles=roles)
    if len(matches) != 1:
        raise AXTargetError(f"live AX target count={len(matches)} labels={list(labels)!r}")
    press_ax(ax, matches[0])
    return matches[0]


def unique_directory_picker_control(ax: Any, root: Any) -> Any:
    choose_controls = live_labeled_nodes(
        ax,
        root,
        CHOOSE_DIRECTORY_LABELS,
        exact=True,
        roles={"AXButton", "AXMenuButton"},
    )
    if len(choose_controls) > 1:
        raise AXTargetError(
            f"directory picker Choose count={len(choose_controls)}"
        )
    if len(choose_controls) == 1:
        return choose_controls[0]
    shared_controls = live_labeled_nodes(
        ax,
        root,
        ADD_SHARED_LABELS,
        exact=True,
        roles={"AXButton"},
    )
    if len(shared_controls) != 1:
        raise AXTargetError(
            f"directory picker shared control count={len(shared_controls)}"
        )
    return shared_controls[0]


def reacquire_target(ax: Any, pid: int, vm_name: str) -> tuple[Any, list[Any]]:
    """Wait then reacquire the exact target window and its current AX tree."""
    wait_and_reacquire()
    return target_window(ax, pid, vm_name)


def find_ax(ax: Any, root: Any, text: str, *, exact: bool = True) -> list[Any]:
    wanted = text.casefold()
    found = []
    for element in ax_tree(ax, root):
        fields = ax_text(ax, element)
        if any((field.casefold() == wanted if exact else wanted in field.casefold()) for field in fields if field):
            found.append(element)
    return found


def press_ax(ax: Any, element: Any) -> None:
    enabled = _copy(ax, element, ax.kAXEnabledAttribute)
    if enabled is False:
        raise RuntimeError("AX target is disabled")
    error = ax.AXUIElementPerformAction(element, ax.kAXPressAction)
    if error != ax.kAXErrorSuccess:
        raise RuntimeError(f"AXPress failed: {error}")


def wait_and_reacquire(seconds: float = WAIT_SECONDS) -> None:
    time.sleep(seconds)


def utm_pid(open_if_missing: bool = True) -> int:
    try:
        output = subprocess.check_output(["pgrep", "-x", "UTM"], text=True)
        pids = [int(value) for value in output.split() if value.isdigit()]
    except (OSError, subprocess.CalledProcessError):
        pids = []
    if not pids and open_if_missing:
        subprocess.run(["open", "-a", "UTM"], check=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                output = subprocess.check_output(["pgrep", "-x", "UTM"], text=True)
                pids = [int(value) for value in output.split() if value.isdigit()]
            except (OSError, subprocess.CalledProcessError):
                pids = []
            if pids:
                break
            time.sleep(0.5)
    if not pids:
        raise RuntimeError("UTM process not available")
    return pids[0]


def utmctl_status(vm_name: str) -> str:
    """Read the exact VM status without issuing any lifecycle action."""
    try:
        output = subprocess.check_output(["utmctl", "status", vm_name], text=True, stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "output", "") or str(error)
        raise RuntimeError(f"utmctl status failed for {vm_name}: {detail.strip()}") from error
    return parse_utm_status(output)


def require_target_running(vm_name: str, *, sleep_fn: Any = time.sleep) -> list[str]:
    """Perform two independent reads before permitting any GUI write."""
    statuses = [utmctl_status(vm_name)]
    sleep_fn(WAIT_SECONDS)
    statuses.append(utmctl_status(vm_name))
    if not running_status_verified(statuses):
        raise RuntimeError(f"target VM is not already running: {statuses!r}")
    return statuses


def require_target_stopped(vm_name: str, *, sleep_fn: Any = time.sleep) -> list[str]:
    """Perform two independent reads before permitting UTM settings edits."""
    statuses = [utmctl_status(vm_name)]
    sleep_fn(WAIT_SECONDS)
    statuses.append(utmctl_status(vm_name))
    if not stopped_status_verified(statuses):
        raise RuntimeError(f"target VM must be stopped for UTM settings edit: {statuses!r}")
    return statuses


def target_window(ax: Any, pid: int, vm_name: str) -> tuple[Any, list[Any]]:
    app = ax.AXUIElementCreateApplication(pid)
    windows = _copy(ax, app, ax.kAXWindowsAttribute) or []
    accepted = {f"UTM – {vm_name}", f"UTM - {vm_name}", f"UTM — {vm_name}"}
    matches = []
    for window in list(windows):
        title = str(_copy(ax, window, ax.kAXTitleAttribute) or "")
        if title in accepted:
            matches.append(window)
    if len(matches) != 1:
        bare = [
            window for window in list(windows)
            if str(_copy(ax, window, ax.kAXTitleAttribute) or "") == vm_name
        ]
        if len(bare) == 1:
            return bare[0], ax_tree(ax, bare[0])
        titles = [str(_copy(ax, window, ax.kAXTitleAttribute) or "") for window in list(windows)]
        raise RuntimeError(f"target UTM window count={len(matches)} titles={titles}")
    window = matches[0]
    return window, ax_tree(ax, window)


def exact_vm_card_nodes(nodes: Iterable[Any], vm_name: str) -> list[Any]:
    """Return exact VM-name card labels from normalized AX fixture nodes."""
    wanted = str(vm_name)
    return [
        node
        for node in nodes
        if _dict_field(node, "role") == "AXStaticText"
        and any(_dict_field(node, key) == wanted for key in ("title", "value", "description", "identifier"))
    ]


def select_exact_vm_card(ax: Any, pid: int, vm_name: str) -> tuple[Any, list[Any]]:
    """Select the exact sidebar card before any UTM settings operation.

    UTM uses the window title ``UTM – <name>`` only after the sidebar row is
    selected. Setting AXSelected on the exact row is semantic and does not
    start the VM; it makes the precondition deterministic for every run.
    """
    accepted = {f"UTM – {vm_name}", f"UTM - {vm_name}", f"UTM — {vm_name}"}
    windows = app_windows(ax, pid)
    selected = [
        window
        for window in windows
        if str(_copy(ax, window, ax.kAXTitleAttribute) or "") in accepted
    ]
    if len(selected) == 1:
        return selected[0], ax_tree(ax, selected[0])
    if len(selected) > 1:
        raise AXTargetError(f"target UTM window count={len(selected)}")
    # The UTM process may still show the previously selected VM (for example
    # ``UTM – yhlw``). Its sidebar is still the authoritative card list, so
    # search every current UTM window for exactly one target card before
    # falling back to the untitled main window.
    card_windows = []
    for window in windows:
        if str(_copy(ax, window, ax.kAXTitleAttribute) or "") == vm_name:
            continue
        cards = [
            element
            for element in ax_tree(ax, window)
            if ax_role(ax, element) == "AXStaticText" and ax_text(ax, element)[1] == vm_name
        ]
        if cards:
            card_windows.append((window, cards))
    if len(card_windows) != 1 or len(card_windows[0][1]) != 1:
        titles = [str(_copy(ax, window, ax.kAXTitleAttribute) or "") for window in windows]
        count = sum(len(cards) for _, cards in card_windows)
        raise AXTargetError(f"VM card count={count} name={vm_name!r} titles={titles}")
    main, cards = card_windows[0]
    row = _ancestor_with_role(ax, cards[0], {"AXRow"})
    if row is None:
        pressable = _ancestor_with_action(ax, cards[0], ax.kAXPressAction)
        if pressable is not None:
            press_ax(ax, pressable)
        else:
            selectable = _ancestor_with_settable_attribute(
                ax, cards[0], ax.kAXSelectedAttribute
            )
            if selectable is not None:
                error = ax.AXUIElementSetAttributeValue(
                    selectable, ax.kAXSelectedAttribute, True
                )
                if error != ax.kAXErrorSuccess:
                    raise RuntimeError(f"VM card selection failed: {error}")
            else:
                click_ax_element(cards[0])
        wait_and_reacquire()
    elif _copy(ax, row, ax.kAXSelectedAttribute) is not True:
        error = ax.AXUIElementSetAttributeValue(row, ax.kAXSelectedAttribute, True)
        if error != ax.kAXErrorSuccess:
            raise RuntimeError(f"VM card selection failed: {error}")
        wait_and_reacquire()
    return target_window(ax, pid, vm_name)


def config_uuid(bundle: Path) -> str:
    """Read the exact target config UUID used to bind the run."""
    data = plistlib.loads((bundle / "config.plist").read_bytes())
    information = data.get("Information")
    if not isinstance(information, dict):
        raise RuntimeError("config Information is missing")
    value = str(information.get("UUID") or "").strip()
    if not UUID_RE.fullmatch(value):
        raise RuntimeError("config UUID is invalid")
    return value.upper()


def config_mac(bundle: Path) -> str:
    data = plistlib.loads((bundle / "config.plist").read_bytes())
    network = data.get("Network")
    if not isinstance(network, list) or len(network) != 1 or not isinstance(network[0], dict):
        raise RuntimeError("config Network is not a unique list")
    return normalize_mac(str(network[0].get("MacAddress") or ""))


def unique_ax_mac_value(nodes: Iterable[Any]) -> str:
    """Read exactly one valid MAC value from normalized AX text-field nodes."""
    values: set[str] = set()
    for node in nodes:
        if _dict_field(node, "role") != "AXTextField":
            continue
        for key in ("title", "value", "description", "identifier"):
            raw = _dict_field(node, key)
            if not raw:
                continue
            try:
                candidate = normalize_mac(raw)
            except ValueError:
                continue
            if is_valid_mac(candidate):
                values.add(candidate)
    if len(values) != 1:
        raise AXTargetError(f"Network MAC field count={len(values)}")
    return next(iter(values))


def live_network_mac(ax: Any, root: Any) -> str:
    """Read the current unsaved MAC shown by UTM's Network editor."""
    nodes = []
    for element in ax_tree(ax, root):
        title, value, description, identifier = ax_text(ax, element)
        nodes.append(
            {
                "role": ax_role(ax, element),
                "title": title,
                "value": value,
                "description": description,
                "identifier": identifier,
            }
        )
    return unique_ax_mac_value(nodes)


def _path_from_field(value: str) -> str:
    try:
        return str(Path(value).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return ""


def live_path_nodes(ax: Any, root: Any, share: Path) -> list[Any]:
    wanted = str(share.expanduser().resolve())
    result = []
    for element in ax_tree(ax, root):
        # Do not count the same path echoed by Finder's Go to Folder dialog;
        # only the Sharing table is evidence of a configured row.
        current = element
        in_picker = False
        for _ in range(8):
            if ax_role(ax, current) == "AXSheet":
                sheet_text = ax_text(ax, current)
                if sheet_text[3] in {"open-panel", "GoToWindow"} or sheet_text[2] in {"打开", "Open"}:
                    in_picker = True
                    break
            current = _parent(ax, current)
            if current is None:
                break
        if in_picker:
            continue
        fields = ax_text(ax, element)
        if any(field and _path_from_field(field) == wanted for field in fields[:3]):
            result.append(element)
    return result


def _parent(ax: Any, element: Any) -> Any:
    return _copy(ax, element, ax.kAXParentAttribute)


def _ancestor_with_role(ax: Any, element: Any, roles: set[str], limit: int = 8) -> Any:
    current = element
    for _ in range(limit):
        if ax_role(ax, current) in roles:
            return current
        current = _parent(ax, current)
        if current is None:
            break
    return None


def _ancestor_with_action(ax: Any, element: Any, action: Any, limit: int = 8) -> Any:
    current = element
    wanted = str(action)
    for _ in range(limit):
        if wanted in ax_actions(ax, current):
            return current
        current = _parent(ax, current)
        if current is None:
            break
    return None


def _ancestor_with_settable_attribute(
    ax: Any, element: Any, attribute: Any, limit: int = 8
) -> Any:
    current = element
    for _ in range(limit):
        try:
            error, settable = ax.AXUIElementIsAttributeSettable(
                current, attribute, None
            )
        except Exception:
            settable = False
            error = None
        if error == ax.kAXErrorSuccess and settable:
            return current
        current = _parent(ax, current)
        if current is None:
            break
    return None


def click_ax_element(element: Any) -> None:
    try:
        import Quartz
    except ImportError as error:
        raise AXUnavailable("PyObjC Quartz is required for AX click") from error
    ax, _ = _load_ax()
    current = element
    geometry = None
    for _ in range(8):
        position = _copy(ax, current, ax.kAXPositionAttribute)
        size = _copy(ax, current, ax.kAXSizeAttribute)
        try:
            point = _ax_pair(ax, position, ax.kAXValueCGPointType, "x", "y")
            extent = _ax_pair(ax, size, ax.kAXValueCGSizeType, "width", "height")
            values = (*point, *extent)
        except (AttributeError, TypeError, ValueError, AXTargetError):
            values = ()
        if len(values) == 4 and values[2] > 0 and values[3] > 0:
            geometry = values
            break
        current = _parent(ax, current)
        if current is None:
            break
    if geometry is None:
        raise AXTargetError("VM card geometry unreadable")
    x, y, width, height = geometry
    point = (x + width / 2, y + height / 2)
    for event_type in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(None, event_type, point, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _ax_pair(ax: Any, value: Any, value_type: Any, first: str, second: str) -> tuple[float, float]:
    try:
        result = ax.AXValueGetValue(value, value_type, None)
    except Exception as error:
        raise AXTargetError("AX geometry unreadable") from error
    raw = result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], (bool, int)):
        if not result[0]:
            raise AXTargetError("AX geometry unreadable")
        raw = result[1]
    if hasattr(raw, first) and hasattr(raw, second):
        return float(getattr(raw, first)), float(getattr(raw, second))
    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        return float(raw[0]), float(raw[1])
    raise AXTargetError("AX geometry unreadable")


READ_ONLY_LABELS = ("只读?", "只读", "Read Only", "Read-only", "Read only")
ADD_SHARED_LABELS = ("新建共享目录…", "新建共享目录...", "Add Shared Directory…", "Add Shared Directory...")
ADD_ROW_LABELS = ("添加", "Add")
ADD_READ_ONLY_LABELS = ("添加只读", "Add Read-Only", "Add Read Only")
CHOOSE_DIRECTORY_LABELS = ("选择", "Choose")
SAVE_LABELS = ("存储", "保存", "Save", "Done")
SHARING_LABELS = ("共享", "Sharing")
NETWORK_LABELS = ("网络", "Network")
EDIT_LABELS = ("编辑", "Edit")
RANDOM_LABELS = ("随机", "Random", "随机化 MAC", "Randomize MAC")


def read_only_checkbox(ax: Any, root: Any, path_element: Any) -> Any:
    """Find the sole read-only checkbox belonging to the matching path row."""
    # Anchor at the complete table row, not the path cell: UTM exposes the
    # read-only control in the adjacent cell.
    row = _ancestor_with_role(ax, path_element, {"AXRow"}) or root
    candidates = [
        element
        for element in ax_tree(ax, row)
        if ax_role(ax, element) == "AXCheckBox"
        and live_labeled_nodes(ax, element, READ_ONLY_LABELS, exact=True)
    ]
    if len(candidates) == 1:
        return candidates[0]
    # UTM may expose the label on the checkbox's parent rather than the
    # checkbox itself; retain the same-row uniqueness guard.
    candidates = [
        element
        for element in ax_tree(ax, row)
        if ax_role(ax, element) == "AXCheckBox"
        and any(
            field.casefold() in {label.casefold() for label in READ_ONLY_LABELS}
            for field in ax_text(ax, element)
            if field
        )
    ]
    if len(candidates) != 1:
        # UTM 4.7 renders the row's second column as an unlabeled checkbox;
        # the localized label is exposed only by the column header. The row
        # is already anchored by the exact path, so accept one checkbox in
        # that same row only.
        candidates = [
            element for element in ax_tree(ax, row) if ax_role(ax, element) == "AXCheckBox"
        ]
        if len(candidates) != 1:
            raise AXTargetError(f"read-only checkbox count={len(candidates)}")
    return candidates[0]


def app_windows(ax: Any, pid: int) -> list[Any]:
    app = ax.AXUIElementCreateApplication(pid)
    return list(_copy(ax, app, ax.kAXWindowsAttribute) or [])


def native_picker_open(ax: Any, pid: int) -> bool:
    for window in app_windows(ax, pid):
        for element in ax_tree(ax, window):
            if ax_role(ax, element) != "AXSheet":
                continue
            fields = ax_text(ax, element)
            if fields[2] in {"打开", "Open"} or fields[3] == "open-panel":
                return True
    return False


def paste_path_into_utm_panel(share: Path) -> None:
    """Paste a path and let UTM consume it before clearing the clipboard."""
    subprocess.run(["pbcopy"], input=str(share).encode("utf-8"), check=True)
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to tell process "UTM" to set frontmost to true',
                "-e",
                'tell application "System Events" to tell process "UTM" to keystroke "v" using command down',
            ],
            check=True,
        )
        wait_and_reacquire()
    finally:
        subprocess.run(["pbcopy"], input=b"", check=True)


def choose_directory_in_panel(ax: Any, pid: int, share: Path) -> None:
    """Navigate the native UTM folder panel semantically and confirm Open."""
    share = share.expanduser().resolve()

    def picker_sheet() -> Any:
        sheets = []
        for window in app_windows(ax, pid):
            sheets.extend(
                element
                for element in ax_tree(ax, window)
                if ax_role(ax, element) == "AXSheet"
                and (
                    ax_text(ax, element)[2] in {"打开", "Open"}
                    or ax_text(ax, element)[3] == "open-panel"
                )
            )
        if len(sheets) != 1:
            raise AXTargetError(f"directory picker sheet count={len(sheets)}")
        return sheets[0]

    # Finder's native panel has a semantic Go to Folder sheet. Use the
    # system shortcut only after the exact UTM Open panel is present, then
    # paste the complete absolute path and confirm it. This is direct path
    # navigation; it never searches or walks the Finder columns.
    picker_sheet()
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to tell process "UTM" to set frontmost to true',
            "-e",
            'tell application "System Events" to tell process "UTM" to keystroke "g" using {command down, shift down}',
        ],
        check=True,
    )
    wait_and_reacquire()
    windows = app_windows(ax, pid)
    goto_sheets = [
        element
        for window in windows
        for element in ax_tree(ax, window)
        if ax_role(ax, element) == "AXSheet" and ax_text(ax, element)[3] == "GoToWindow"
    ]
    if len(goto_sheets) != 1:
        raise AXTargetError(f"Go to Folder sheet count={len(goto_sheets)}")
    fields = [
        element
        for element in ax_tree(ax, goto_sheets[0])
        if ax_role(ax, element) == "AXTextField" and ax_text(ax, element)[3] == "PathTextField"
    ]
    if len(fields) != 1:
        raise AXTargetError(f"Go to Folder path field count={len(fields)}")
    field = fields[0]
    ax.AXUIElementSetAttributeValue(field, ax.kAXFocusedAttribute, True)
    # AXValue can update the accessibility cache without updating the visible
    # native text field. A real native paste keeps the field and Finder's
    # path resolver synchronized.
    paste_path_into_utm_panel(share)
    windows = app_windows(ax, pid)
    goto_sheets = [
        element
        for window in windows
        for element in ax_tree(ax, window)
        if ax_role(ax, element) == "AXSheet" and ax_text(ax, element)[3] == "GoToWindow"
    ]
    if len(goto_sheets) != 1:
        raise AXTargetError("Go to Folder sheet disappeared before confirm")
    fields = [
        element
        for element in ax_tree(ax, goto_sheets[0])
        if ax_role(ax, element) == "AXTextField" and ax_text(ax, element)[3] == "PathTextField"
        and str(_copy(ax, element, ax.kAXValueAttribute) or "") == str(share)
    ]
    if len(fields) != 1:
        raise AXTargetError("Go to Folder path readback mismatch")
    ax.AXUIElementSetAttributeValue(fields[0], ax.kAXFocusedAttribute, True)
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to tell process "UTM" to set frontmost to true',
            "-e",
            'tell application "System Events" to tell process "UTM" to key code 36',
        ],
        check=True,
    )
    wait_and_reacquire()
    if any(
        ax_role(ax, element) == "AXSheet" and ax_text(ax, element)[3] == "GoToWindow"
        for window in app_windows(ax, pid)
        for element in ax_tree(ax, window)
    ):
        raise AXTargetError("Go to Folder dialog remained open after Return")
    sheet = picker_sheet()
    opens = [
        element
        for element in live_labeled_nodes(ax, sheet, ("打开", "Open"), exact=True)
        if ax_role(ax, element) == "AXButton"
    ]
    if len(opens) != 1:
        raise AXTargetError(f"directory picker Open count={len(opens)}")
    press_ax(ax, opens[0])
    wait_and_reacquire()
    if native_picker_open(ax, pid):
        raise AXTargetError("directory picker remained open after Open")


def save_edit_page(ax: Any, pid: int, vm_name: str) -> tuple[Any, list[Any]]:
    window, _ = target_window(ax, pid, vm_name)
    press_unique_live(ax, window, SAVE_LABELS, exact=True)
    return reacquire_target(ax, pid, vm_name)


def open_edit_category(ax: Any, pid: int, vm_name: str, labels: Iterable[str]) -> tuple[Any, list[Any]]:
    window, _ = target_window(ax, pid, vm_name)
    # A prior interrupted attempt may have left the same Edit sheet open.
    # Reuse it only when the requested sidebar category is uniquely present;
    # otherwise navigate from the exact main-window Edit control.
    current_category = live_labeled_nodes(ax, window, labels, exact=True, roles={"AXUnknown"})
    if len(current_category) != 1:
        press_unique_live(ax, window, EDIT_LABELS, exact=True)
        # UTM can expose the edit sheet asynchronously. Never click Edit a
        # second time; perform up to three independent reads of the same
        # sheet, each separated by the required wait, before declaring the
        # semantic category unavailable.
        for _ in range(3):
            window, _ = reacquire_target(ax, pid, vm_name)
            current_category = live_labeled_nodes(
                ax, window, labels, exact=True, roles={"AXUnknown"}
            )
            if len(current_category) == 1:
                break
        if len(current_category) != 1:
            raise AXTargetError(
                f"edit category count={len(current_category)} labels={list(labels)!r}"
            )
    press_unique_live(ax, window, labels, exact=True, roles={"AXUnknown"})
    return reacquire_target(ax, pid, vm_name)


def configure_sharing(ax: Any, pid: int, vm_name: str, share: Path) -> tuple[Any, list[Any]]:
    window, _ = open_edit_category(ax, pid, vm_name, SHARING_LABELS)
    paths = live_path_nodes(ax, window, share)
    if len(paths) > 1:
        raise AXTargetError(f"shared-directory path count={len(paths)}")
    if not paths:
        # UTM 4.7 first creates an empty row via Add Read-Only + Add; only
        # then does the row expose its native Choose directory button.
        read_only_add = press_unique_live(
            ax, window, ADD_READ_ONLY_LABELS, exact=True, roles={"AXCheckBox"}
        )
        if not checkbox_is_checked(_copy(ax, read_only_add, ax.kAXValueAttribute)):
            press_ax(ax, read_only_add)
            window, _ = reacquire_target(ax, pid, vm_name)
        press_unique_live(ax, window, ADD_ROW_LABELS, exact=True, roles={"AXButton"})
        for _ in range(3):
            window, _ = reacquire_target(ax, pid, vm_name)
            if native_picker_open(ax, pid):
                break
        else:
            press_ax(ax, unique_directory_picker_control(ax, window))
            wait_and_reacquire()
        choose_directory_in_panel(ax, pid, share)
        window, _ = target_window(ax, pid, vm_name)
        paths = live_path_nodes(ax, window, share)
    if len(paths) != 1:
        raise AXTargetError(f"shared-directory path count={len(paths)} after add")
    checkbox = read_only_checkbox(ax, window, paths[0])
    if not checkbox_is_checked(_copy(ax, checkbox, ax.kAXValueAttribute)):
        press_ax(ax, checkbox)
        window, _ = reacquire_target(ax, pid, vm_name)
        paths = live_path_nodes(ax, window, share)
        if len(paths) != 1:
            raise AXTargetError("shared-directory disappeared after read-only change")
        checkbox = read_only_checkbox(ax, window, paths[0])
    if not checkbox_is_checked(_copy(ax, checkbox, ax.kAXValueAttribute)):
        raise RuntimeError("shared-directory read-only state was not enabled")

    # Save once, then reopen the same VM's Sharing page and independently
    # reread the exact path and read-only state before allowing Network.
    save_edit_page(ax, pid, vm_name)
    window, _ = open_edit_category(ax, pid, vm_name, SHARING_LABELS)
    paths = live_path_nodes(ax, window, share)
    if len(paths) != 1:
        raise AXTargetError("saved shared-directory path readback count is not one")
    checkbox = read_only_checkbox(ax, window, paths[0])
    if not checkbox_is_checked(_copy(ax, checkbox, ax.kAXValueAttribute)):
        raise RuntimeError("saved shared-directory read-only readback failed")
    # Close the verification-only settings view without changing the saved
    # values; the next operation starts from the confirmed main window.
    press_unique_live(ax, window, ("取消", "Cancel"), exact=True)
    return reacquire_target(ax, pid, vm_name)


def randomize_network(ax: Any, pid: int, vm_name: str, bundle: Path, rounds: int) -> list[str]:
    if rounds != 3:
        raise ValueError("UTM-1 requires exactly three randomization rounds")
    values = [config_mac(bundle)]
    window, _ = open_edit_category(ax, pid, vm_name, NETWORK_LABELS)
    # Keep one Network editor open: click Random three times first, reading
    # the unsaved MAC field after each click. There is exactly one Save after
    # the three-click sequence.
    for _ in range(rounds):
        press_unique_live(ax, window, RANDOM_LABELS, exact=False)
        window, _ = reacquire_target(ax, pid, vm_name)
        current = live_network_mac(ax, window)
        if current == values[-1] or not is_valid_mac(current):
            raise MACSequenceError("Network Random click did not produce a new valid MAC")
        values.append(current)
    verify_mac_sequence(values)

    # Persist only after all three clicks, then independently verify the
    # saved config and the reopened Network editor.
    save_edit_page(ax, pid, vm_name)
    saved = config_mac(bundle)
    if saved != values[-1] or saved == values[0]:
        raise MACSequenceError("saved Network MAC does not diff from original/final click")
    # Final independent post-check: reopen Network after the last save and
    # verify the saved MAC while the Random control is present, then cancel
    # the verification-only settings view.
    window, _ = open_edit_category(ax, pid, vm_name, NETWORK_LABELS)
    if not live_labeled_nodes(ax, window, RANDOM_LABELS, exact=False):
        raise AXTargetError("Network Random control missing during post-check")
    if live_network_mac(ax, window) != saved:
        raise MACSequenceError("final Network MAC reread mismatch")
    press_unique_live(ax, window, ("取消", "Cancel"), exact=True)
    reacquire_target(ax, pid, vm_name)
    return values


def print_inspection(ax: Any, window: Any, vm_name: str) -> None:
    nodes = ax_tree(ax, window)
    print(f"UTM_AX_VM={vm_name}")
    print(f"UTM_AX_NODE_COUNT={len(nodes)}")
    for element in nodes:
        role = ax_role(ax, element)
        title, value, description, identifier = ax_text(ax, element)
        if role in {"AXButton", "AXRow", "AXCell", "AXCheckBox", "AXTextField"} and any(
            (title, value, description, identifier)
        ):
            print(
                "AX_NODE role={} title={} value={} description={} identifier={}".format(
                    role, title, value, description, identifier
                )
            )


def run(args: argparse.Namespace) -> int:
    if not is_valid_vm_name(args.vm_name):
        raise RuntimeError("vm-name must be four lowercase letters")
    share = Path(args.share_path).expanduser().resolve()
    if not share.is_dir() or share.is_symlink():
        raise RuntimeError("share-path must be an existing non-symlink directory")
    bundle = Path(args.images_dir).expanduser().resolve() / f"{args.vm_name}.utm"
    if not bundle.is_dir() or bundle.is_symlink() or not (bundle / "config.plist").is_file():
        raise RuntimeError("target VM bundle/config.plist is missing")
    target_uuid = config_uuid(bundle)
    before_mac = config_mac(bundle)
    statuses = require_target_stopped(args.vm_name)
    print(f"UTM_TARGET_STATUS_READS={','.join(statuses)}")
    print(f"UTM_CONFIG_UUID={target_uuid}")
    if config_mac(bundle) != before_mac:
        raise RuntimeError("target config MAC changed before UTM-1 settings")
    ax, _ = _load_ax()
    pid = utm_pid(open_if_missing=True)
    try:
        window, _ = select_exact_vm_card(ax, pid, args.vm_name)
    except AXTargetError:
        subprocess.run(["open", str(bundle)], check=True)
        wait_and_reacquire()
        window, _ = target_window(ax, pid, args.vm_name)
    print_inspection(ax, window, args.vm_name)
    if args.inspect_only:
        print("UTM_AX_INSPECTION=verified")
        return 0
    configure_sharing(ax, pid, args.vm_name, share)
    print("SHARING_MATCH_COUNT=1")
    print("SHARING_READ_ONLY=verified")
    mac_values = randomize_network(ax, pid, args.vm_name, bundle, args.random_mac_rounds)
    if config_uuid(bundle) != target_uuid:
        raise RuntimeError("config UUID changed during UTM-1 settings")
    print(f"NETWORK_RANDOM_ROUNDS={len(mac_values) - 1}")
    print("NETWORK_RANDOM_CLICKS=3")
    print("NETWORK_SAVE_COUNT=1")
    print("NETWORK_MAC_CHANGED=verified")
    print("NETWORK_ORIGINAL_FINAL_DIFF=verified")
    for index, value in enumerate(mac_values):
        print(f"MAC_{index}_SHA256={hashlib.sha256(value.encode()).hexdigest()}")
    final_status = require_target_stopped(args.vm_name)
    print(f"UTM_TARGET_STATUS_FINAL={','.join(final_status)}")
    print("UTM_AX_READY=verified")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prototype UTM-1 Accessibility helper")
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--share-path", required=True)
    parser.add_argument("--images-dir", default=os.environ.get("SUBMISSION_VM_IMAGES_DIR", ""))
    parser.add_argument("--random-mac-rounds", type=int, default=3)
    parser.add_argument("--inspect-only", action="store_true")
    args = parser.parse_args()
    if not args.images_dir:
        print("SUBMISSION_VM_IMAGES_DIR is required", file=sys.stderr)
        return 2
    try:
        return run(args)
    except Exception as error:
        print(f"UTM_1_AX=blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
