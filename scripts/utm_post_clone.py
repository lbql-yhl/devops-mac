#!/usr/bin/env python3
"""Coordinate ten post-clone steps for one SQLite-bound UTM VM."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.project_paths import PROJECT_ROOT, SHARED_DIR, VM_IMAGES_DIR  # noqa: E402
from scripts.utm_1_ax import normalize_mac  # noqa: E402
from scripts.utm_business_delivery import (  # noqa: E402
    BUSINESS_GUEST_FILES,
    stage_business_guest_files,
)
from scripts.utm_apps_delivery import stage_utm_apps_playwright_files  # noqa: E402
from scripts.utm_image_delivery import stage_utm_image_guest_file  # noqa: E402
from scripts.utm_post_clone_finalize import (  # noqa: E402
    FinalizationError,
    finalize,
    ping_until_ready,
)
from scripts.utm_post_clone_guest import (  # noqa: E402
    DEMO_RETIREMENT_TIMEOUT_SECONDS,
    GuestAutomationError,
    bootstrap_clash_app_data,
    complete_setup_assistant_script,
    copy_shared,
    disable_screen_lock,
    demo_cleanup_script,
    demo_cleanup_verified,
    demo_retirement_script,
    desktop_state_verified,
    ensure_ssh,
    enter_guest_desktop as _guest_enter_desktop,
    guest_shutdown_script,
    guest_window_point as _guest_window_point,
    launch_finder_script,
    parse_key_values,
    read_desktop_state,
    read_guest_identity,
    residual_demo_home_cleanup_script,
    settings_read_script,
    settings_verified,
    settings_write_script,
    setup_assistant_probe,
    setup_assistant_verified,
    ssh_capture,
    ssh_password_command,
    ssh_script,
    ssh_sudo_script,
    ssh_sudo_script_allow_marker,
    verify_admin,
    verify_copy,
)
from scripts.clean_cli import emit_progress  # noqa: E402
from scripts.utm_post_clone_guest import _focus_exact_utm as _focus_utm_target  # noqa: E402
from scripts.vm_inventory import (  # noqa: E402
    InventoryError,
    get_record,
    list_records,
    record_guest_identity,
    update_guest_mac,
)


VM_RE = re.compile(r"^[a-z]{4}$")
UUID_RE = re.compile(r"^[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}$")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
WAIT_SECONDS = 3.0
UTMCTL = str(Path(shutil.which("utmctl") or "/opt/homebrew/bin/utmctl").resolve())
UTM_1_SCRIPT = str((PROJECT_ROOT / "scripts" / "utm_1_ax.py").resolve())
TERMINAL_APP_MANAGEMENT_SCRIPT = str(
    (PROJECT_ROOT / "scripts" / "utm_terminal_app_management.py").resolve()
)
STEP_SCRIPTS = tuple(
    str((PROJECT_ROOT / "scripts" / name).resolve())
    for name in (
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
)
STEP_MARKERS = tuple(f"STEP_{step:02d}_{name}=verified" for step, name in (
    (2, "ACCOUNT"),
    (3, "HARDWARE"),
    (4, "DESKTOP"),
    (5, "SETTINGS"),
    (6, "DELIVERY"),
    (7, "DEPENDENCIES"),
    (8, "ACCESSIBILITY"),
    (9, "APP_MANAGEMENT"),
    (10, "FINALIZE"),
))
STEP_RETRY_DELAYS_SECONDS = (5.0, 10.0)
STEP_ERROR_PREFIX_RE = re.compile(r"^执行报错：[^；]+；(.+)$")
class PostCloneError(GuestAutomationError):
    """Raised when the exact post-clone target cannot be safely verified."""


setup_assistant_script = setup_assistant_probe
_settings_write_script = settings_write_script
_settings_read_script = settings_read_script


def child_error_reason(detail: str, returncode: int) -> str:
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    for line in reversed(lines):
        match = STEP_ERROR_PREFIX_RE.fullmatch(line)
        if match:
            return match.group(1).strip()
    return lines[-1] if lines else f"EXIT_{returncode}"


def guest_window_point(
    bounds: tuple[float, float, float, float], normalized_x: float, normalized_y: float
) -> tuple[float, float]:
    try:
        return _guest_window_point(bounds, normalized_x, normalized_y)
    except GuestAutomationError as error:
        raise PostCloneError(str(error)) from error
def _enter_guest_desktop(
    user: str, ip: str, vm_name: str, bundle: Path
) -> dict[str, str]:
    return _guest_enter_desktop(user, ip, vm_name, bundle)
def _verify_admin(user: str, ip: str) -> None:
    verify_admin(user, ip)
def target_bundle(images_dir: Path, vm_name: str) -> Path:
    if not VM_RE.fullmatch(vm_name):
        raise PostCloneError("VM name must be four lowercase letters")
    images = Path(images_dir).expanduser()
    if images.is_symlink() or not images.is_dir():
        raise PostCloneError(f"images directory is invalid: {images}")
    images = images.resolve()
    bundle = images / f"{vm_name}.utm"
    if bundle.is_symlink() or not bundle.is_dir() or not (bundle / "config.plist").is_file():
        raise PostCloneError(f"exact target bundle/config.plist is missing: {bundle}")
    return bundle.resolve()
def config_identity(bundle: Path) -> tuple[str, str]:
    try:
        data = plistlib.loads((bundle / "config.plist").read_bytes())
        information = data["Information"]
        network = data["Network"]
        config_uuid = str(information["UUID"]).upper()
        if not isinstance(network, list) or len(network) != 1:
            raise ValueError("Network count is not one")
        mac = normalize_mac(str(network[0]["MacAddress"]))
    except (OSError, KeyError, IndexError, TypeError, ValueError) as error:
        raise PostCloneError(f"invalid target config identity: {error}") from error
    if not UUID_RE.fullmatch(config_uuid):
        raise PostCloneError("target config UUID is invalid")
    return config_uuid, mac
def choose_unique_ip(utm_ips: Iterable[str], arp_ips: Iterable[str]) -> str:
    left = {str(value).strip() for value in utm_ips if IP_RE.fullmatch(str(value).strip())}
    right = {str(value).strip() for value in arp_ips if IP_RE.fullmatch(str(value).strip())}
    matches = sorted(left & right)
    if len(matches) != 1:
        raise PostCloneError(f"IP_MATCH_COUNT={len(matches)}")
    return matches[0]
def manifest(root: Path) -> dict[str, dict[str, Any]]:
    root = Path(root).resolve()
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = {"type": "symlink", "target": os.readlink(path)}
        elif path.is_dir():
            result[relative] = {"type": "directory"}
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[relative] = {
                "type": "file",
                "size": path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
        else:
            raise PostCloneError(f"unsupported manifest entry: {relative}")
    return result
def _run(command: list[str], *, timeout_seconds: float | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"EXIT_{result.returncode}").strip()
            raise PostCloneError(f"command failed: {detail}")
        return result.stdout
    except subprocess.TimeoutExpired as error:
        raise PostCloneError(f"command timed out after {timeout_seconds:g} seconds") from error
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stdout", "") or getattr(error, "output", "") or str(error)
        raise PostCloneError(f"command failed: {str(detail).strip()}") from error


def _choose_utm_run_control(controls: list[dict[str, Any]], vm_name: str) -> Any:
    toolbar_controls = [
        item
        for item in controls
        if item["window_title"].endswith(f"– {vm_name}")
        and item.get("container_role") == "AXToolbar"
        and item["identifier"] == "play"
    ]
    if len(toolbar_controls) == 1:
        return toolbar_controls[0]["node"]
    detail_controls = [
        item
        for item in controls
        if item["window_title"].endswith(f"– {vm_name}")
        and item["identifier"] == "play.circle.fill"
    ]
    if len(detail_controls) == 1:
        return detail_controls[0]["node"]
    console_controls = [
        item
        for item in controls
        if item["window_title"] == vm_name
        and item["description"] in {"run", "start", "运行", "开始"}
    ]
    if len(console_controls) == 1:
        return console_controls[0]["node"]
    if len(controls) == 1:
        return controls[0]["node"]
    raise PostCloneError("UTM_RUN_CONTROL_NOT_UNIQUE")


def _start_stopped_vm(vm_name: str) -> None:
    bundle = target_bundle(VM_IMAGES_DIR, vm_name)
    subprocess.run(["open", str(bundle)], check=True)
    time.sleep(WAIT_SECONDS)

    try:
        import ApplicationServices as ax

        from scripts.utm_1_ax import _copy, ax_tree
    except ImportError as error:
        raise PostCloneError("UTM_AX_IMPORT_FAILED") from error

    result = subprocess.run(
        ["/usr/bin/pgrep", "-x", "UTM"],
        text=True,
        capture_output=True,
        check=False,
    )
    pids = [int(value) for value in result.stdout.split() if value.isdigit()]
    if result.returncode != 0 or len(pids) != 1:
        raise PostCloneError("UTM_PROCESS_NOT_UNIQUE")
    application = ax.AXUIElementCreateApplication(pids[0])
    windows = list(_copy(ax, application, ax.kAXWindowsAttribute) or [])
    target_windows = []
    for window in windows:
        title = str(_copy(ax, window, ax.kAXTitleAttribute) or "")
        if title == vm_name or title.endswith(f"– {vm_name}"):
            target_windows.append(window)
    if not target_windows:
        raise PostCloneError("UTM_TARGET_WINDOW_NOT_UNIQUE")

    controls: list[dict[str, Any]] = []
    for window in target_windows:
        window_title = str(_copy(ax, window, ax.kAXTitleAttribute) or "")
        for node in ax_tree(ax, window):
            role = str(_copy(ax, node, ax.kAXRoleAttribute) or "")
            identifier = str(_copy(ax, node, ax.kAXIdentifierAttribute) or "")
            description = str(
                _copy(ax, node, ax.kAXDescriptionAttribute) or ""
            ).casefold()
            parent = _copy(ax, node, ax.kAXParentAttribute)
            container_role = str(
                _copy(ax, parent, ax.kAXRoleAttribute) or ""
            ) if parent is not None else ""
            enabled = _copy(ax, node, ax.kAXEnabledAttribute)
            if (
                role == "AXButton"
                and enabled is not False
                and identifier in {"play.circle.fill", "play", ""}
                and description in {"run", "start", "运行", "开始"}
            ):
                controls.append(
                    {
                        "window_title": window_title,
                        "identifier": identifier,
                        "description": description,
                        "container_role": container_role,
                        "node": node,
                    }
                )
    control = _choose_utm_run_control(controls, vm_name)
    if ax.AXUIElementPerformAction(control, ax.kAXPressAction) != ax.kAXErrorSuccess:
        raise PostCloneError("UTM_RUN_CONTROL_PRESS_FAILED")


def _status(vm_name: str) -> str:
    values = [
        line.strip().lower()
        for line in _run([UTMCTL, "status", vm_name]).splitlines()
        if line.strip()
    ]
    if len(values) != 1 or values[0] not in {
        "started",
        "running",
        "starting",
        "stopped",
        "stopping",
        "suspended",
    }:
        raise PostCloneError(f"UTM_STATUS_AMBIGUOUS={values!r}")
    return values[0]


def _wait_status(vm_name: str, expected: set[str], reads: int = 60) -> None:
    values: list[str] = []
    for index in range(reads):
        current = _status(vm_name)
        values.append(current)
        if current in expected:
            return
        if index + 1 < reads:
            time.sleep(WAIT_SECONDS)
    raise PostCloneError(f"UTM_STATUS_EXPECTED={sorted(expected)} GOT={values}")


def ensure_started(vm_name: str) -> None:
    current = _status(vm_name)
    if current == "stopped":
        _start_stopped_vm(vm_name)
    elif current not in {"started", "running", "starting"}:
        raise PostCloneError(f"target is not safely startable: {current}")
    _wait_status(vm_name, {"started", "running"})
def ensure_stopped(vm_name: str) -> None:
    values: list[str] = []
    for index in range(8):
        values.append(_status(vm_name))
        if len(values) >= 2 and values[-2:] == ["stopped", "stopped"]:
            return
        if index < 7:
            time.sleep(WAIT_SECONDS)
    raise PostCloneError(f"UTM_STATUS_EXPECTED=['stopped'] GOT={values}")
def resolve_ip(vm_name: str, config_mac: str) -> str:
    wanted = normalize_mac(config_mac)
    for attempt, delay in enumerate((0.0, 2.0, 5.0)):
        if delay:
            time.sleep(delay)
        result = subprocess.run(
            [UTMCTL, "ip-address", vm_name], text=True, capture_output=True, check=False
        )
        utm_ips = sorted(set(IP_RE.findall(result.stdout))) if result.returncode == 0 else []
        arp_output = _run(["/usr/sbin/arp", "-an"])
        arp_ips: list[str] = []
        for ip, mac in re.findall(r"\(([^)]+)\)\s+at\s+([0-9A-Fa-f:.-]+)", arp_output):
            try:
                padded = ":".join(part.zfill(2) for part in mac.split(":"))
                if IP_RE.fullmatch(ip) and normalize_mac(padded) == wanted:
                    arp_ips.append(ip)
            except ValueError:
                continue
        matches = sorted(set(utm_ips) & set(arp_ips)) if utm_ips else sorted(set(arp_ips))
        if len(matches) == 1:
            ping_until_ready(matches[0])
            return matches[0]
        if attempt == 2:
            raise PostCloneError(
                f"IP_MATCH_COUNT={len(matches)} UTM_IPS={utm_ips} ARP_IPS={arp_ips}"
            )
    raise PostCloneError("IP resolution exhausted")
def _state_path(vm_name: str) -> Path:
    path = (PROJECT_ROOT / "runtime" / "post-clone" / f"{vm_name}.json").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    return path
def _write_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)
def _load_state(path: Path, vm_name: str, bundle: Path, config_uuid: str) -> dict[str, Any]:
    if path.is_symlink():
        raise PostCloneError("post-clone state must not be a symlink")
    if path.is_file():
        if path.stat().st_mode & 0o777 != 0o600:
            raise PostCloneError("post-clone state mode must be 600")
        state = json.loads(path.read_text(encoding="utf-8"))
        expected = (vm_name, str(bundle), config_uuid)
        actual = (state.get("vm_name"), state.get("bundle"), state.get("config_uuid"))
        if actual != expected:
            raise PostCloneError("post-clone state target mismatch")
        changed = False
        if "steps" not in state:
            state["steps"] = []
            changed = True
        if "step_evidence" not in state:
            state["step_evidence"] = {}
            changed = True
        if changed:
            _write_state(path, state)
        return state
    state = {
        "schema_version": 1,
        "vm_name": vm_name,
        "bundle": str(bundle),
        "config_uuid": config_uuid,
        "attempt_id": str(uuid.uuid4()),
        "steps": [],
        "step_evidence": {},
        "created_at": time.time(),
    }
    _write_state(path, state)
    return state
def _preflight(args: argparse.Namespace) -> tuple[Path, str, str]:
    bundle = target_bundle(args.images_dir, args.vm_name)
    if args.shared_dir.is_symlink() or not args.shared_dir.is_dir():
        raise PostCloneError(f"shared directory is invalid: {args.shared_dir}")
    config_uuid, config_mac = config_identity(bundle)
    record = get_record(args.database, args.vm_name)
    if str(record.get("bundle_path") or "") != str(bundle):
        raise PostCloneError("inventory bundle does not match exact target")
    if str(record.get("config_uuid") or "").upper() != config_uuid:
        raise PostCloneError("inventory UUID does not match exact target")
    if record.get("status") != "complete" or int(record.get("reusable") or 0) != 0:
        raise PostCloneError("inventory target is not an active completed clone")
    for other in list_records(args.database):
        if other.get("vm_name") == args.vm_name or int(other.get("reusable") or 0) == 1:
            continue
        if str(other.get("config_uuid") or "").upper() == config_uuid:
            raise PostCloneError("config UUID is not unique in inventory")
        other_macs = {str(other.get(key) or "").lower() for key in ("mac_address", "guest_mac_address")}
        if config_mac.lower() in other_macs:
            raise PostCloneError("config MAC is not unique in inventory")
    _status(args.vm_name)
    return bundle, config_uuid, config_mac
def perform_account_setup(
    args: argparse.Namespace,
    bundle: Path,
    config_uuid: str,
    config_mac: str,
) -> dict[str, Any]:
    ensure_started(args.vm_name)
    ip = resolve_ip(args.vm_name, config_mac)
    bootstrap_user = "demo"
    try:
        ensure_ssh(bootstrap_user, ip)
    except GuestAutomationError:
        bootstrap_user = args.vm_name
        ensure_ssh(bootstrap_user, ip)
    identity = read_guest_identity(bootstrap_user, ip, config_mac)
    record_guest_identity(
        args.database,
        args.vm_name,
        identity.serial_number,
        identity.platform_uuid,
        identity.mac,
    )
    if bootstrap_user == "demo":
        create_script = f'''set -e
name={shlex.quote(args.vm_name)}
if ! /usr/bin/id "$name" >/dev/null 2>&1; then
  /usr/sbin/sysadminctl -addUser "$name" -fullName "$name" -admin -password -
fi
/usr/bin/id -Gn "$name" | /usr/bin/grep -Eq '(^|[[:space:]])admin([[:space:]]|$)'
printf 'ADMIN_USER=verified\n'
'''
        ssh_sudo_script("demo", ip, create_script)
        ensure_ssh(args.vm_name, ip)
        verify_admin(args.vm_name, ip)
        token = ssh_sudo_script_allow_marker(
            "demo",
            ip,
            f"/usr/sbin/sysadminctl -secureTokenStatus {shlex.quote(args.vm_name)}\n",
            "ENABLED",
        )
        if "ENABLED" not in token:
            command = (
                f"/usr/sbin/sysadminctl -secureTokenOn {shlex.quote(args.vm_name)} "
                "-password - -adminUser demo -adminPassword -"
            )
            code, _ = ssh_password_command("demo", ip, command, password_inputs=2)
            if code != 0:
                raise PostCloneError("ADMIN_SECURE_TOKEN_ENABLE_FAILED")
            token = ssh_sudo_script_allow_marker(
                "demo",
                ip,
                f"/usr/sbin/sysadminctl -secureTokenStatus {shlex.quote(args.vm_name)}\n",
                "ENABLED",
            )
        if "ENABLED" not in token:
            raise PostCloneError("ADMIN_SECURE_TOKEN_NOT_ENABLED")
    retirement = ssh_sudo_script(
        args.vm_name,
        ip,
        demo_retirement_script(),
        timeout_seconds=DEMO_RETIREMENT_TIMEOUT_SECONDS,
    )
    if "DEMO_ACCOUNT=absent" not in retirement:
        raise PostCloneError("DEMO_RETIREMENT_NOT_VERIFIED")
    try:
        cleanup = ssh_sudo_script(args.vm_name, ip, demo_cleanup_script())
    except GuestAutomationError as error:
        if "STALE_DEMO_PATH_PROCESS=present" not in str(error):
            raise
        try:
            ssh_sudo_script(args.vm_name, ip, guest_shutdown_script())
        except GuestAutomationError:
            pass
        ensure_stopped(args.vm_name)
        ensure_started(args.vm_name)
        ip = resolve_ip(args.vm_name, config_mac)
        ensure_ssh(args.vm_name, ip)
        cleanup = ssh_sudo_script(args.vm_name, ip, demo_cleanup_script())
    if not demo_cleanup_verified(cleanup):
        raise PostCloneError("DEMO_CLEANUP_NOT_VERIFIED")
    residual = ssh_sudo_script(args.vm_name, ip, residual_demo_home_cleanup_script())
    if "DEMO_RESIDUAL_HOME=absent" not in residual.splitlines():
        raise PostCloneError("DEMO_RESIDUAL_HOME_NOT_VERIFIED")
    ensure_ssh(args.vm_name, ip)
    try:
        ssh_sudo_script(args.vm_name, ip, guest_shutdown_script())
    except GuestAutomationError:
        pass
    ensure_stopped(args.vm_name)
    evidence = {
        "ip": ip,
        "guest_serial": identity.serial_number,
        "guest_uuid": identity.platform_uuid,
        "guest_mac": identity.mac,
        "ssh_password_auth": "verified",
        "shutdown": "verified",
    }
    return evidence


def perform_hardware_setup(
    args: argparse.Namespace,
    bundle: Path,
    config_uuid: str,
) -> dict[str, Any]:
    ensure_stopped(args.vm_name)
    _run(
        [
            sys.executable,
            UTM_1_SCRIPT,
            "--vm-name",
            args.vm_name,
            "--share-path",
            str(args.shared_dir),
            "--images-dir",
            str(args.images_dir),
            "--random-mac-rounds",
            "3",
        ]
    )
    final_uuid, final_mac = config_identity(bundle)
    if final_uuid != config_uuid:
        raise PostCloneError("config UUID changed during UTM-1")
    ensure_stopped(args.vm_name)
    update_guest_mac(args.database, args.vm_name, final_mac)
    return {
        "config_uuid": final_uuid,
        "mac": final_mac,
        "sharing": "verified",
        "mac_random_rounds": "3",
        "save_count": "1",
    }
def run_step_handoff(vm_name: str) -> None:
    if not VM_RE.fullmatch(vm_name):
        raise PostCloneError("VM name must be four lowercase letters")
    for step, (script, marker) in enumerate(
        zip(STEP_SCRIPTS, STEP_MARKERS, strict=True), start=2
    ):
        result: subprocess.CompletedProcess[str] | None = None
        failures: list[str] = []
        for attempt in range(len(STEP_RETRY_DELAYS_SECONDS) + 1):
            if attempt:
                time.sleep(STEP_RETRY_DELAYS_SECONDS[attempt - 1])
            result = subprocess.run(
                [sys.executable, script, "--vm-name", vm_name],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0 and marker in result.stdout:
                progress = [
                    line.strip()
                    for line in result.stdout.splitlines()
                    if re.fullmatch(
                        rf"步骤{step}(?:已操作|已完成，跳过)", line.strip()
                    )
                ]
                if len(progress) != 1:
                    raise PostCloneError(f"STEP_ACTION_OUTPUT_INVALID={step}")
                emit_progress(progress[0])
                break
            detail = (result.stderr or result.stdout or f"EXIT_{result.returncode}").strip()
            failures.append(detail)
        if result is None or result.returncode != 0 or marker not in result.stdout:
            last = failures[-1] if failures else "NO_RESULT"
            last = child_error_reason(last, result.returncode if result else 1)
            raise PostCloneError(
                f"STEP_RETRIES_EXHAUSTED={Path(script).stem.upper()}:"
                f"ATTEMPTS={len(failures)}:LAST={last}"
            )
def main() -> int:
    parser = argparse.ArgumentParser(description="Run the exact UTM clone steps 2-10")
    parser.add_argument(
        "--vm-name",
        default=os.getenv("VM_NAME", "").strip(),
        help="exact four-letter VM name; defaults to the current workflow VM_NAME environment variable",
    )
    args = parser.parse_args()
    try:
        run_step_handoff(args.vm_name)
        return 0
    except (
        PostCloneError,
        GuestAutomationError,
        FinalizationError,
        InventoryError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"UTM_POST_CLONE=blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
