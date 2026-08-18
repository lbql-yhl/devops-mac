#!/usr/bin/env python3
"""Clone step 07 implementation for the three required Accessibility rows."""

from __future__ import annotations

import argparse
import ipaddress
import json
import plistlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple

from PIL import Image, ImageChops


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.host_config import ConfigurationError, guest_password  # noqa: E402
from services.project_paths import VM_IMAGES_DIR  # noqa: E402
from scripts.ssh_password import password_environment  # noqa: E402


INVENTORY_DATABASE = PROJECT_ROOT / "runtime" / "vm-inventory.sqlite3"
ACTIVE_WORKFLOW_PATH = PROJECT_ROOT / "runtime" / "utm-vm-clone-active.json"
VISION_SOURCE = PROJECT_ROOT / "scripts" / "utm_vm_clone_accessibility_vision.swift"
UTMCTL = str(Path(shutil.which("utmctl") or "/opt/homebrew/bin/utmctl").resolve())
TARGETS = ("AEServer", "sshd-keygen-wrapper", "Terminal")
WAIT_SECONDS = 3.0
DIGIT_KEY_CODES = {
    "0": 29,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "5": 23,
    "6": 22,
    "7": 26,
    "8": 28,
    "9": 25,
}
VM_NAME_RE = re.compile(r"^[a-z]{4}$")
UUID_RE = re.compile(r"^[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}$")
MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
IP_RE = re.compile(
    r"(?<![0-9])(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])"
    r"(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}(?![0-9])"
)
SETTINGS_URL = (
    "x-apple.systempreferences:"
    "com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility"
)


class UTMAError(RuntimeError):
    """Fail closed with a non-sensitive stable reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class VMTarget(NamedTuple):
    vm_name: str
    bundle_path: Path
    config_uuid: str
    mac_address: str


class PixelBox(NamedTuple):
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)


class OCRObservation(NamedTuple):
    text: str
    confidence: float
    box: PixelBox


class SwitchState(NamedTuple):
    enabled: bool
    center: tuple[float, float]
    box: PixelBox


class AuthorizationDialog(NamedTuple):
    username_box: PixelBox
    password_box: PixelBox
    modify_box: PixelBox
    password_point: tuple[float, float]
    modify_point: tuple[float, float]


class UTMWindow(NamedTuple):
    pid: int
    window_id: int
    bounds: tuple[float, float, float, float]


def validate_vm_name(value: str) -> str:
    value = str(value).strip()
    if not VM_NAME_RE.fullmatch(value):
        raise UTMAError("vm_name_invalid")
    return value


def normalize_mac(value: str) -> str:
    parts = str(value).strip().lower().replace("-", ":").split(":")
    normalized = ":".join(part.zfill(2) for part in parts)
    if not MAC_RE.fullmatch(normalized):
        raise UTMAError("inventory_MAC_invalid")
    return normalized


def resolve_inventory_target(
    database: Path,
    images_dir: Path,
    vm_name: str,
    *,
    active_workflow_path: Path | None = None,
    state_path: Path | None = None,
) -> VMTarget:
    vm_name = validate_vm_name(vm_name)
    database = Path(database).expanduser()
    if database.is_symlink() or not database.is_file():
        raise UTMAError("inventory_unavailable")
    database = database.resolve()
    images_dir = Path(images_dir).expanduser()
    if images_dir.is_symlink() or not images_dir.is_dir():
        raise UTMAError("images_directory_invalid")
    images_dir = images_dir.resolve()
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            """SELECT vm_name, config_uuid, bundle_path, mac_address,
                      status, directory_present
               FROM vm_inventory WHERE vm_name=?""",
            (vm_name,),
        ).fetchall()
    except sqlite3.Error as error:
        raise UTMAError("inventory_read_failed") from error
    finally:
        if "connection" in locals():
            connection.close()
    if len(rows) != 1:
        raise UTMAError("inventory_target_not_unique")
    row = rows[0]
    if str(row["status"]) != "complete":
        raise UTMAError("inventory_target_incomplete")
    expected_bundle = (images_dir / f"{vm_name}.utm").resolve()
    registered_bundle = Path(str(row["bundle_path"] or "")).expanduser()
    if registered_bundle.is_symlink() or registered_bundle.resolve() != expected_bundle:
        raise UTMAError("inventory_bundle_mismatch")
    if expected_bundle.is_symlink() or not expected_bundle.is_dir():
        raise UTMAError("inventory_bundle_unavailable")
    config_path = expected_bundle / "config.plist"
    if config_path.is_symlink() or not config_path.is_file():
        raise UTMAError("inventory_config_unavailable")
    try:
        payload = plistlib.loads(config_path.read_bytes())
        config_uuid = str(payload["Information"]["UUID"]).upper()
        networks = payload["Network"]
        if not isinstance(networks, list) or len(networks) != 1:
            raise ValueError("network count")
        config_mac = normalize_mac(str(networks[0]["MacAddress"]))
    except (OSError, KeyError, TypeError, ValueError, plistlib.InvalidFileException) as error:
        raise UTMAError("inventory_config_invalid") from error
    registered_uuid = str(row["config_uuid"] or "").upper()
    if not UUID_RE.fullmatch(config_uuid) or config_uuid != registered_uuid:
        raise UTMAError("inventory_UUID_mismatch")
    if int(row["directory_present"] or 0) == 1:
        registered_mac = normalize_mac(str(row["mac_address"] or ""))
        if config_mac != registered_mac:
            raise UTMAError("inventory_MAC_mismatch")
    else:
        active_path = Path(active_workflow_path or ACTIVE_WORKFLOW_PATH)
        current_state_path = Path(
            state_path
            or PROJECT_ROOT / "runtime" / "post-clone" / f"{vm_name}.json"
        )
        try:
            active = json.loads(active_path.read_text(encoding="utf-8"))
            state = json.loads(current_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise UTMAError("active_clone_state_unavailable") from error
        expected_active = {
            "schema_version": 1,
            "vm_name": vm_name,
            "config_uuid": config_uuid,
            "bundle_path": str(expected_bundle),
        }
        if active != expected_active:
            raise UTMAError("active_clone_identity_mismatch")
        completed_steps = [int(value) for value in state.get("steps", [])]
        if (
            state.get("vm_name") != vm_name
            or state.get("bundle") != str(expected_bundle)
            or str(state.get("config_uuid") or "").upper() != config_uuid
            or completed_steps != list(range(2, 2 + len(completed_steps)))
            or completed_steps[:5] != [2, 3, 4, 5, 6]
        ):
            raise UTMAError("active_clone_step_state_mismatch")
    return VMTarget(vm_name, expected_bundle, config_uuid, config_mac)


def parse_utm_status(output: str) -> str:
    values = [line.strip().casefold() for line in output.splitlines() if line.strip()]
    if len(values) != 1 or values[0] not in {"started", "running"}:
        raise UTMAError("vm_not_running")
    return values[0]


def _command(
    args: list[str], *, timeout: float = 20.0
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UTMAError("readonly_command_failed") from error


def require_running(vm_name: str) -> None:
    result = _command([UTMCTL, "status", validate_vm_name(vm_name)])
    if result.returncode != 0:
        raise UTMAError("vm_status_unavailable")
    parse_utm_status(result.stdout)


def _arp_ips_for_mac(output: str, wanted_mac: str) -> set[str]:
    matches: set[str] = set()
    for ip_text, mac_text in re.findall(
        r"\(([^)]+)\)\s+at\s+([0-9A-Fa-f:.-]+)", output
    ):
        try:
            if normalize_mac(mac_text) == wanted_mac:
                matches.add(str(ipaddress.IPv4Address(ip_text)))
        except (UTMAError, ValueError):
            continue
    return matches


def match_registered_ip(utm_output: str, arp_output: str, mac_address: str) -> str:
    wanted_mac = normalize_mac(mac_address)
    utm_ips = {str(ipaddress.IPv4Address(value)) for value in IP_RE.findall(utm_output)}
    arp_ips = _arp_ips_for_mac(arp_output, wanted_mac)
    matches = sorted(utm_ips & arp_ips) if utm_ips else sorted(arp_ips)
    if len(matches) != 1:
        raise UTMAError("vm_ip_not_unique")
    return matches[0]


def resolve_exact_ip(
    target: VMTarget,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    for delay in (0.0, 5.0, 10.0):
        if delay:
            sleep_fn(delay)
        ip_result = _command([UTMCTL, "ip-address", target.vm_name])
        for candidate in IP_RE.findall(ip_result.stdout if ip_result.returncode == 0 else ""):
            _command(["/sbin/ping", "-c", "1", "-W", "1000", candidate], timeout=3.0)
        arp_result = _command(["/usr/sbin/arp", "-an"])
        try:
            return match_registered_ip(
                ip_result.stdout if ip_result.returncode == 0 else "",
                arp_result.stdout if arp_result.returncode == 0 else "",
                target.mac_address,
            )
        except UTMAError:
            continue
    raise UTMAError("vm_ip_not_unique")


def password_prompt_delta(output: bytes, seen: int) -> tuple[int, int]:
    current = len(re.findall(br"(?i)password\s*:", output))
    if current < seen or current - seen > 1 or current > 1:
        raise UTMAError("ssh_password_prompt_ambiguous")
    return current, current - seen


def redact_ssh_transcript(output: bytes) -> str:
    configured_password = guest_password()
    text = output.decode("utf-8", errors="replace").replace("\r", "")
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
        and configured_password not in line
        and not re.search(r"(?i)password\s*:", line)
    ]
    return "\n".join(lines)


def has_marker(output: str, marker: str) -> bool:
    return sum(line.strip() == marker for line in output.splitlines()) == 1


def _ssh_arguments(user: str, ip: str, remote_command: str) -> list[str]:
    validate_vm_name(user)
    try:
        ip = str(ipaddress.ip_address(ip))
    except ValueError as error:
        raise UTMAError("vm_ip_invalid") from error
    return [
        "/usr/bin/ssh",
        "-o", "BatchMode=no",
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "PubkeyAuthentication=no",
        "-o", "KbdInteractiveAuthentication=yes",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
        f"{user}@{ip}",
        remote_command,
    ]


def run_ssh(user: str, ip: str, remote_command: str, *, timeout: float = 45.0) -> str:
    try:
        result = subprocess.run(
            _ssh_arguments(user, ip, remote_command),
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=password_environment(),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise UTMAError("ssh_timeout") from error
    except OSError as error:
        raise UTMAError("ssh_process_failed") from error
    if result.returncode != 0:
        raise UTMAError("ssh_command_failed")
    return redact_ssh_transcript(result.stdout.encode("utf-8"))


def verify_guest_identity(vm_name: str, ip: str) -> None:
    output = run_ssh(
        vm_name,
        ip,
        "/usr/bin/printf 'USER=%s\\nHOME=%s\\nCONSOLE=%s\\n' "
        '"$(/usr/bin/id -un)" "$HOME" '
        '"$(/usr/bin/stat -f %Su /dev/console)"',
    )
    values = dict(
        line.split("=", 1) for line in output.splitlines() if "=" in line
    )
    if values != {
        "USER": vm_name,
        "HOME": f"/Users/{vm_name}",
        "CONSOLE": vm_name,
    }:
        raise UTMAError("guest_identity_mismatch")


def open_accessibility_settings(vm_name: str, ip: str) -> None:
    command = (
        f"/usr/bin/open {shlex.quote(SETTINGS_URL)} && "
        "/usr/bin/printf 'SETTINGS_OPENED=1\\n'"
    )
    if not has_marker(run_ssh(vm_name, ip, command), "SETTINGS_OPENED=1"):
        raise UTMAError("settings_open_failed")


def parse_ocr_output(payload: str, width: int, height: int) -> list[OCRObservation]:
    try:
        values = json.loads(payload)
    except json.JSONDecodeError as error:
        raise UTMAError("ocr_output_invalid") from error
    if not isinstance(values, list) or width <= 0 or height <= 0:
        raise UTMAError("ocr_output_invalid")
    observations: list[OCRObservation] = []
    for value in values:
        try:
            text = str(value["text"]).strip()
            confidence = float(value["confidence"])
            x = float(value["x"])
            y = float(value["y"])
            box_width = float(value["width"])
            box_height = float(value["height"])
        except (KeyError, TypeError, ValueError) as error:
            raise UTMAError("ocr_output_invalid") from error
        if not text or confidence < 0.55:
            continue
        box = PixelBox(
            max(0, round(x * width)),
            max(0, round((1.0 - y - box_height) * height)),
            min(width, round((x + box_width) * width)),
            min(height, round((1.0 - y) * height)),
        )
        if box.width > 0 and box.height > 0:
            observations.append(OCRObservation(text, confidence, box))
    return observations


def _normalize_text(value: str) -> str:
    return re.sub(r"[\s‐‑‒–—−]", "", value).casefold()


def find_unique_target(
    observations: list[OCRObservation], target: str
) -> OCRObservation:
    wanted = _normalize_text(target)
    matches = [item for item in observations if wanted in _normalize_text(item.text)]
    if len(matches) != 1:
        code = "target_row_missing" if not matches else "target_row_ambiguous"
        raise UTMAError(code)
    return matches[0]


def locate_authorization_dialog(
    observations: list[OCRObservation], vm_name: str, image_size: tuple[int, int]
) -> AuthorizationDialog:
    validate_vm_name(vm_name)
    if image_size[0] <= 0 or image_size[1] <= 0:
        raise UTMAError("authorization_geometry_invalid")
    password_instruction = find_unique_target(observations, "Enter your password")
    modify = find_unique_target(observations, "Modify Settings")
    owner_matches = [
        item
        for item in observations
        if _normalize_text(item.text) == _normalize_text("Privacy & Security")
    ]
    if len(owner_matches) != 1:
        raise UTMAError("authorization_ownership_ambiguous")
    owner = owner_matches[0]
    wanted_user = _normalize_text(vm_name)
    username_candidates = [
        item
        for item in observations
        if _normalize_text(item.text) == wanted_user
        and password_instruction.box.center[1] < item.box.center[1] < modify.box.center[1]
        and item.box.center[1] - password_instruction.box.center[1]
        <= image_size[1] * 0.15
        and abs(item.box.center[0] - password_instruction.box.center[0])
        <= image_size[0] * 0.30
    ]
    if len(username_candidates) != 1:
        raise UTMAError("authorization_username_ambiguous")
    username = username_candidates[0]
    if owner.box.center[1] >= password_instruction.box.center[1]:
        raise UTMAError("authorization_ownership_ambiguous")
    password_point = (
        username.box.center[0],
        username.box.center[1]
        + (modify.box.center[1] - username.box.center[1]) * 0.45,
    )
    if not (0 <= password_point[0] <= image_size[0] and 0 <= password_point[1] <= image_size[1]):
        raise UTMAError("authorization_geometry_invalid")
    return AuthorizationDialog(
        username.box,
        password_instruction.box,
        modify.box,
        password_point,
        modify.box.center,
    )


def password_field_has_content(
    observations: list[OCRObservation],
    dialog: AuthorizationDialog,
    image_size: tuple[int, int],
) -> bool:
    width, height = image_size
    if width <= 0 or height <= 0:
        raise UTMAError("authorization_geometry_invalid")
    x, y = dialog.password_point
    y_tolerance = max(12.0, height * 0.06)
    x_tolerance = width * 0.35
    return any(
        abs(item.box.center[1] - y) <= y_tolerance
        and abs(item.box.center[0] - x) <= x_tolerance
        for item in observations
    )


def _components(mask: list[list[bool]], x_offset: int, y_offset: int) -> list[PixelBox]:
    height = len(mask)
    width = len(mask[0]) if height else 0
    seen: set[tuple[int, int]] = set()
    boxes: list[PixelBox] = []
    for y in range(height):
        for x in range(width):
            if not mask[y][x] or (x, y) in seen:
                continue
            queue = [(x, y)]
            seen.add((x, y))
            xs: list[int] = []
            ys: list[int] = []
            while queue:
                current_x, current_y = queue.pop()
                xs.append(current_x)
                ys.append(current_y)
                for neighbor in (
                    (current_x - 1, current_y),
                    (current_x + 1, current_y),
                    (current_x, current_y - 1),
                    (current_x, current_y + 1),
                ):
                    nx, ny = neighbor
                    if (
                        0 <= nx < width
                        and 0 <= ny < height
                        and mask[ny][nx]
                        and neighbor not in seen
                    ):
                        seen.add(neighbor)
                        queue.append(neighbor)
            boxes.append(
                PixelBox(
                    min(xs) + x_offset,
                    min(ys) + y_offset,
                    max(xs) + x_offset + 1,
                    max(ys) + y_offset + 1,
                )
            )
    return boxes


def _switch_candidates(
    image: Image.Image, label_box: PixelBox, *, enabled: bool
) -> list[PixelBox]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    x1 = max(label_box.x2 + 12, round(width * 0.55))
    x2 = width - 8
    row_padding = max(24, label_box.height)
    y1 = max(0, round(label_box.center[1] - row_padding))
    y2 = min(height, round(label_box.center[1] + row_padding))
    if x2 <= x1 or y2 <= y1:
        raise UTMAError("switch_search_region_invalid")
    pixels = rgb.load()
    mask: list[list[bool]] = []
    for y in range(y1, y2):
        row: list[bool] = []
        for x in range(x1, x2):
            red, green, blue = pixels[x, y]
            if enabled:
                match = blue >= 170 and blue - red >= 80 and green >= 70
            else:
                match = (
                    60 <= red <= 210
                    and abs(red - green) <= 10
                    and abs(green - blue) <= 10
                )
            row.append(match)
        mask.append(row)
    candidates = []
    for box in _components(mask, x1, y1):
        ratio = box.width / max(1, box.height)
        if 20 <= box.width <= 150 and 10 <= box.height <= 90 and 1.2 <= ratio <= 4.5:
            candidates.append(box)
    return candidates


def detect_switch(image: Image.Image, label_box: PixelBox) -> SwitchState:
    on_candidates = _switch_candidates(image, label_box, enabled=True)
    if len(on_candidates) == 1:
        box = on_candidates[0]
        return SwitchState(True, box.center, box)
    if len(on_candidates) > 1:
        raise UTMAError("switch_state_ambiguous")
    off_candidates = _switch_candidates(image, label_box, enabled=False)
    if len(off_candidates) != 1:
        raise UTMAError("switch_state_ambiguous")
    box = off_candidates[0]
    return SwitchState(False, box.center, box)


def _ax_pair(ax: Any, value: Any, value_type: Any) -> tuple[float, float]:
    result = ax.AXValueGetValue(value, value_type, None)
    raw = result[1] if isinstance(result, tuple) and len(result) == 2 else result
    if hasattr(raw, "x") and hasattr(raw, "y"):
        return float(raw.x), float(raw.y)
    if hasattr(raw, "width") and hasattr(raw, "height"):
        return float(raw.width), float(raw.height)
    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        return float(raw[0]), float(raw[1])
    raise UTMAError("utm_window_geometry_invalid")


def _window_bounds(ax: Any, window: Any) -> tuple[float, float, float, float]:
    from scripts.utm_1_ax import _copy

    position = _copy(ax, window, ax.kAXPositionAttribute)
    size = _copy(ax, window, ax.kAXSizeAttribute)
    if position is None or size is None:
        raise UTMAError("utm_window_geometry_invalid")
    x, y = _ax_pair(ax, position, ax.kAXValueCGPointType)
    width, height = _ax_pair(ax, size, ax.kAXValueCGSizeType)
    if width <= 0 or height <= 0:
        raise UTMAError("utm_window_geometry_invalid")
    return x, y, width, height


def _exact_utm_pid() -> int:
    result = _command(["/usr/bin/pgrep", "-x", "UTM"])
    pids = [int(value) for value in result.stdout.split() if value.isdigit()]
    if result.returncode != 0 or len(pids) != 1:
        raise UTMAError("utm_process_not_unique")
    return pids[0]


def _exact_ax_window(ax: Any, pid: int, vm_name: str) -> Any:
    from scripts.utm_1_ax import _copy

    app = ax.AXUIElementCreateApplication(pid)
    windows = list(_copy(ax, app, ax.kAXWindowsAttribute) or [])
    matches = [
        item
        for item in windows
        if str(_copy(ax, item, ax.kAXTitleAttribute) or "") == vm_name
    ]
    if len(matches) != 1:
        raise UTMAError("utm_window_not_unique")
    return matches[0]


def _capture_input(ax: Any, pid: int, vm_name: str) -> None:
    from scripts.utm_1_ax import _copy, ax_tree

    def control() -> Any:
        window = _exact_ax_window(ax, pid, vm_name)
        matches = []
        for node in ax_tree(ax, window):
            role = str(_copy(ax, node, ax.kAXRoleAttribute) or "")
            description = str(_copy(ax, node, ax.kAXDescriptionAttribute) or "")
            if role == "AXCheckBox" and description in {"Capture Input", "捕获输入"}:
                matches.append(node)
        if len(matches) != 1:
            raise UTMAError("capture_input_not_unique")
        return matches[0]

    item = control()
    if _copy(ax, item, ax.kAXEnabledAttribute) is False:
        raise UTMAError("capture_input_disabled")
    value = _copy(ax, item, ax.kAXValueAttribute)
    checked = value is True or str(value).strip().casefold() in {"1", "true", "on", "yes"}
    if not checked:
        if ax.AXUIElementPerformAction(item, ax.kAXPressAction) != ax.kAXErrorSuccess:
            raise UTMAError("capture_input_press_failed")
        time.sleep(WAIT_SECONDS)
    value = _copy(ax, control(), ax.kAXValueAttribute)
    if not (value is True or str(value).strip().casefold() in {"1", "true", "on", "yes"}):
        raise UTMAError("capture_input_readback_failed")


def lock_utm_window(vm_name: str) -> UTMWindow:
    import ApplicationServices as ax

    from scripts.utm_1_ax import _copy

    vm_name = validate_vm_name(vm_name)
    pid = _exact_utm_pid()
    app = ax.AXUIElementCreateApplication(pid)
    window = _exact_ax_window(ax, pid, vm_name)
    if ax.AXUIElementSetAttributeValue(app, ax.kAXFrontmostAttribute, True) != ax.kAXErrorSuccess:
        raise UTMAError("utm_focus_failed")
    if ax.AXUIElementPerformAction(window, ax.kAXRaiseAction) != ax.kAXErrorSuccess:
        raise UTMAError("utm_raise_failed")
    for attribute in (ax.kAXMainAttribute, ax.kAXFocusedAttribute):
        ax.AXUIElementSetAttributeValue(window, attribute, True)
    time.sleep(WAIT_SECONDS)
    window = _exact_ax_window(ax, pid, vm_name)
    if (
        _copy(ax, app, ax.kAXFrontmostAttribute) is not True
        or _copy(ax, window, ax.kAXFocusedAttribute) is not True
        or _copy(ax, window, ax.kAXMinimizedAttribute) is True
    ):
        raise UTMAError("utm_focus_readback_failed")
    _capture_input(ax, pid, vm_name)
    bounds = _window_bounds(ax, _exact_ax_window(ax, pid, vm_name))

    import Quartz

    entries = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
    )
    matches = []
    for entry in entries:
        if int(entry.get(Quartz.kCGWindowOwnerPID, -1)) != pid:
            continue
        if str(entry.get(Quartz.kCGWindowName, "")) != vm_name:
            continue
        matches.append(entry)
    if len(matches) != 1:
        raise UTMAError("utm_capture_window_not_unique")
    entry = matches[0]
    window_id = int(entry[Quartz.kCGWindowNumber])
    cg_bounds = entry[Quartz.kCGWindowBounds]
    values = (
        float(cg_bounds["X"]),
        float(cg_bounds["Y"]),
        float(cg_bounds["Width"]),
        float(cg_bounds["Height"]),
    )
    if any(abs(left - right) > 6 for left, right in zip(bounds, values)):
        raise UTMAError("utm_window_geometry_mismatch")
    return UTMWindow(pid, window_id, values)


def capture_window(window: UTMWindow, path: Path) -> Image.Image:
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise UTMAError("screenshot_target_unsafe")
    result = _command(
        ["/usr/sbin/screencapture", "-x", "-l", str(window.window_id), str(path)],
        timeout=20.0,
    )
    if result.returncode != 0 or not path.is_file() or path.stat().st_size == 0:
        raise UTMAError("window_screenshot_failed")
    path.chmod(0o600)
    try:
        image = Image.open(path).convert("RGB")
        image.load()
        return image
    except OSError as error:
        raise UTMAError("window_screenshot_invalid") from error


class VisionOCR:
    def __init__(self, runtime_dir: Path) -> None:
        if VISION_SOURCE.is_symlink() or not VISION_SOURCE.is_file():
            raise UTMAError("vision_source_unavailable")
        self.binary = Path(runtime_dir) / "utm-vm-clone-accessibility-vision"
        result = _command(
            ["/usr/bin/xcrun", "swiftc", str(VISION_SOURCE), "-o", str(self.binary)],
            timeout=120.0,
        )
        if result.returncode != 0 or not self.binary.is_file():
            raise UTMAError("vision_compile_failed")
        self.binary.chmod(0o700)

    def read(self, image_path: Path, image_size: tuple[int, int]) -> list[OCRObservation]:
        result = _command([str(self.binary), str(image_path)], timeout=60.0)
        if result.returncode != 0:
            raise UTMAError("vision_ocr_failed")
        return parse_ocr_output(result.stdout, image_size[0], image_size[1])


def _map_point(
    point: tuple[float, float], image_size: tuple[int, int], window: UTMWindow
) -> tuple[float, float]:
    width, height = image_size
    if width <= 0 or height <= 0:
        raise UTMAError("screenshot_geometry_invalid")
    x, y, window_width, window_height = window.bounds
    return (
        x + point[0] * window_width / width,
        y + point[1] * window_height / height,
    )


def _click(window: UTMWindow, point: tuple[float, float], image_size: tuple[int, int]) -> None:
    import Quartz

    screen_point = _map_point(point, image_size, window)
    for event_type in (
        Quartz.kCGEventMouseMoved,
        Quartz.kCGEventLeftMouseDown,
        Quartz.kCGEventLeftMouseUp,
    ):
        event = Quartz.CGEventCreateMouseEvent(
            None, event_type, screen_point, Quartz.kCGMouseButtonLeft
        )
        Quartz.CGEventPostToPid(window.pid, event)


def _send_password(window: UTMWindow) -> None:
    import Quartz

    password = guest_password()
    if re.fullmatch(r"[0-9]{4}", password) is None:
        raise ConfigurationError(
            "invalid host setting: SUBMISSION_GUEST_PASSWORD must be exactly "
            "four ASCII digits"
        )
    for key_code in (DIGIT_KEY_CODES[digit] for digit in password):
        for pressed in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, key_code, pressed)
            Quartz.CGEventPostToPid(window.pid, event)


def _send_key(window: UTMWindow, key_code: int) -> None:
    import Quartz

    for pressed in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, key_code, pressed)
        Quartz.CGEventPostToPid(window.pid, event)


def _crop_around(point: tuple[float, float], image: Image.Image) -> Image.Image:
    x, y = point
    box = (
        max(0, round(x - 110)),
        max(0, round(y - 24)),
        min(image.width, round(x + 110)),
        min(image.height, round(y + 24)),
    )
    return image.crop(box)


def _region_changed(before: Image.Image, after: Image.Image) -> bool:
    if before.size != after.size:
        return False
    difference = ImageChops.difference(before, after).convert("L")
    histogram = difference.histogram()
    changed = sum(histogram[8:])
    return changed >= 12


def _snapshot(
    runtime_dir: Path,
    index: int,
    window: UTMWindow,
    ocr: VisionOCR,
) -> tuple[Path, Image.Image, list[OCRObservation]]:
    path = runtime_dir / f"capture-{index:03d}.png"
    image = capture_window(window, path)
    observations = ocr.read(path, image.size)
    return path, image, observations


def _accessibility_page(observations: list[OCRObservation]) -> None:
    matches = [
        item for item in observations if "accessibility" in item.text.casefold()
    ]
    if len(matches) < 1:
        raise UTMAError("accessibility_page_not_verified")


def cancel_stale_file_picker(
    window: UTMWindow,
    image: Image.Image,
    observations: list[OCRObservation],
) -> bool:
    cancel = [item for item in observations if _normalize_text(item.text) == "cancel"]
    opened = [item for item in observations if _normalize_text(item.text) == "open"]
    if len(cancel) != 1 or len(opened) != 1:
        return False
    _send_key(window, 53)
    return True


def target_states(
    image: Image.Image, observations: list[OCRObservation]
) -> dict[str, SwitchState]:
    _accessibility_page(observations)
    return {
        target: detect_switch(image, find_unique_target(observations, target).box)
        for target in TARGETS
    }


def verify_all_targets(
    image: Image.Image, observations: list[OCRObservation]
) -> dict[str, SwitchState]:
    states = target_states(image, observations)
    if not all(value.enabled for value in states.values()):
        raise UTMAError("accessibility_targets_not_enabled")
    return states


def _has_modify(observations: list[OCRObservation]) -> bool:
    matches = [
        item
        for item in observations
        if _normalize_text("Modify Settings") in _normalize_text(item.text)
    ]
    if len(matches) > 1:
        raise UTMAError("authorization_dialog_ambiguous")
    return len(matches) == 1


def authorize_if_needed(
    runtime_dir: Path,
    counter: list[int],
    window: UTMWindow,
    ocr: VisionOCR,
    vm_name: str,
    image: Image.Image,
    observations: list[OCRObservation],
) -> tuple[Image.Image, list[OCRObservation]]:
    if not _has_modify(observations):
        return image, observations
    dialog = locate_authorization_dialog(observations, vm_name, image.size)
    _click(window, dialog.password_point, image.size)
    time.sleep(WAIT_SECONDS)
    counter[0] += 1
    _, focused_image, focused_observations = _snapshot(runtime_dir, counter[0], window, ocr)
    focused_dialog = locate_authorization_dialog(
        focused_observations, vm_name, focused_image.size
    )
    if password_field_has_content(
        focused_observations, focused_dialog, focused_image.size
    ):
        raise UTMAError("authorization_password_not_empty")
    before_region = _crop_around(focused_dialog.password_point, focused_image)
    _send_password(window)
    time.sleep(WAIT_SECONDS)
    counter[0] += 1
    _, filled_image, filled_observations = _snapshot(runtime_dir, counter[0], window, ocr)
    filled_dialog = locate_authorization_dialog(
        filled_observations, vm_name, filled_image.size
    )
    after_region = _crop_around(filled_dialog.password_point, filled_image)
    if (
        not _region_changed(before_region, after_region)
        or not password_field_has_content(
            filled_observations, filled_dialog, filled_image.size
        )
    ):
        raise UTMAError("authorization_password_not_verified")
    _click(window, filled_dialog.modify_point, filled_image.size)
    time.sleep(WAIT_SECONDS)
    counter[0] += 1
    _, result_image, result_observations = _snapshot(runtime_dir, counter[0], window, ocr)
    if _has_modify(result_observations):
        raise UTMAError("authorization_dialog_still_open")
    return result_image, result_observations


def ssh_accessibility_probe_source() -> str:
    return (
        "import ctypes;"
        "A=ctypes.CDLL('/System/Library/Frameworks/"
        "ApplicationServices.framework/ApplicationServices');"
        "A.AXIsProcessTrusted.restype=ctypes.c_bool;"
        "raise SystemExit(0 if A.AXIsProcessTrusted() else 1)"
    )


def verify_ssh_accessibility(vm_name: str, ip: str) -> None:
    source = ssh_accessibility_probe_source()
    command = f"/usr/bin/python3 -c {shlex.quote(source)}"
    run_ssh(vm_name, ip, command)


def run(vm_name: str) -> None:
    vm_name = validate_vm_name(vm_name)
    target = resolve_inventory_target(INVENTORY_DATABASE, VM_IMAGES_DIR, vm_name)
    require_running(vm_name)
    ip = resolve_exact_ip(target)
    verify_guest_identity(vm_name, ip)
    print("CLONE_ACCESSIBILITY_TARGET=verified")

    open_accessibility_settings(vm_name, ip)
    time.sleep(WAIT_SECONDS)
    window = lock_utm_window(vm_name)
    print("CLONE_ACCESSIBILITY_WINDOW=verified")

    with tempfile.TemporaryDirectory(prefix="utm-vm-clone-accessibility-") as directory:
        runtime_dir = Path(directory)
        runtime_dir.chmod(0o700)
        ocr = VisionOCR(runtime_dir)
        counter = [1]
        _, image, observations = _snapshot(runtime_dir, counter[0], window, ocr)
        if cancel_stale_file_picker(window, image, observations):
            time.sleep(WAIT_SECONDS)
            open_accessibility_settings(vm_name, ip)
            time.sleep(WAIT_SECONDS)
            counter[0] += 1
            _, image, observations = _snapshot(runtime_dir, counter[0], window, ocr)
        states = target_states(image, observations)
        for target_name in TARGETS:
            state = states[target_name]
            if not state.enabled:
                _click(window, state.center, image.size)
                time.sleep(WAIT_SECONDS)
                counter[0] += 1
                _, image, observations = _snapshot(runtime_dir, counter[0], window, ocr)
                image, observations = authorize_if_needed(
                    runtime_dir, counter, window, ocr, vm_name, image, observations
                )
                states = target_states(image, observations)
                if not states[target_name].enabled:
                    raise UTMAError("accessibility_toggle_readback_failed")

        counter[0] += 1
        _, final_image_one, final_observations_one = _snapshot(
            runtime_dir, counter[0], window, ocr
        )
        verify_all_targets(final_image_one, final_observations_one)
        time.sleep(WAIT_SECONDS)
        counter[0] += 1
        _, final_image_two, final_observations_two = _snapshot(
            runtime_dir, counter[0], window, ocr
        )
        verify_all_targets(final_image_two, final_observations_two)

    verify_ssh_accessibility(vm_name, ip)
    print("AESERVER_ACCESSIBILITY=verified")
    print("SSHD_KEYGEN_WRAPPER_ACCESSIBILITY=verified")
    print("TERMINAL_ACCESSIBILITY=verified")
    print("SSHD_AX_TRUST=verified")
    print("CLONE_ACCESSIBILITY=verified")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enable three existing Accessibility rows in one exact running UTM guest"
    )
    parser.add_argument("--vm-name", required=True)
    args = parser.parse_args()
    try:
        run(args.vm_name)
    except UTMAError as error:
        print(f"CLONE_ACCESSIBILITY=blocked reason={error.code}")
        return 1
    except Exception:
        print("CLONE_ACCESSIBILITY=blocked reason=unexpected_error")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
