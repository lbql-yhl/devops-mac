#!/usr/bin/env python3
"""Shared state and business functions for segmented utm-vm-clone steps."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.project_paths import SHARED_DIR, VM_IMAGES_DIR, VM_TEMPLATE  # noqa: E402
from scripts.clean_cli import emit_progress, run_clean_cli  # noqa: E402
from scripts.utm_apps_delivery import PLAYWRIGHT_GUEST_FILES  # noqa: E402
from scripts.utm_image_delivery import GUEST_FILE as IMAGE_GUEST_FILE  # noqa: E402
from scripts.check_host_vm_dependencies import GUEST_LABELS, HOST_LABELS  # noqa: E402
import scripts.utm_post_clone as legacy  # noqa: E402


DATABASE = (PROJECT_ROOT / "runtime" / "vm-inventory.sqlite3").resolve()
DEPENDENCY_CHECK_SCRIPT = (
    PROJECT_ROOT / "scripts" / "check_host_vm_dependencies.py"
).resolve()
EXPECTED_DEPENDENCY_LINES = tuple(
    f"{label}已安装" for label in (*HOST_LABELS, *GUEST_LABELS)
)
STEP_NAMES = {
    2: "ACCOUNT",
    3: "HARDWARE",
    4: "DESKTOP",
    5: "SETTINGS",
    6: "DELIVERY",
    7: "DEPENDENCIES",
    8: "ACCESSIBILITY",
    9: "APP_MANAGEMENT",
    10: "FINALIZE",
}


def step_marker(step: int) -> str:
    return f"STEP_{step:02d}_{STEP_NAMES[step]}=verified"


def _arguments(vm_name: str) -> argparse.Namespace:
    return argparse.Namespace(
        vm_name=vm_name,
        images_dir=VM_IMAGES_DIR.resolve(),
        shared_dir=SHARED_DIR.resolve(),
        database=DATABASE,
    )


def load_context(vm_name: str) -> tuple[argparse.Namespace, Path, str, str, Path, dict[str, Any]]:
    args = _arguments(vm_name)
    bundle, config_uuid, config_mac = legacy._preflight(args)
    state_path = legacy._state_path(vm_name)
    state = legacy._load_state(state_path, vm_name, bundle, config_uuid)
    return args, bundle, config_uuid, config_mac, state_path, state


def _prepare_step(
    vm_name: str, step: int
) -> tuple[tuple[argparse.Namespace, Path, str, str, Path, dict[str, Any]], bool]:
    context = load_context(vm_name)
    state = context[-1]
    done = [int(value) for value in state.get("steps", [])]
    if done != list(range(2, 2 + len(done))):
        raise legacy.PostCloneError(f"CLONE_STEP_STATE_INVALID={done}")
    completed = step in done
    if not completed and done != list(range(2, step)):
        raise legacy.PostCloneError(f"CLONE_STEP_OUT_OF_ORDER={step}:DONE={done}")
    return context, completed


def _record_reverification(
    state_path: Path,
    state: dict[str, Any],
    step: int,
    evidence: dict[str, Any],
) -> None:
    evidence = {**evidence, "reverified": "verified", "reverified_at": time.time()}
    state.setdefault("step_evidence", {})[str(step)] = evidence
    state["updated_at"] = time.time()
    legacy._write_state(state_path, state)


def _pure_account_probe(vm_name: str, ip: str) -> None:
    output = legacy.ssh_sudo_script(
        vm_name,
        ip,
        "set -e\n"
        f"test \"$(/usr/bin/id -un {vm_name})\" = {vm_name}\n"
        f"/usr/bin/id -Gn {vm_name} | /usr/bin/grep -Eq '(^|[[:space:]])admin([[:space:]]|$)'\n"
        "! /usr/bin/id demo >/dev/null 2>&1\n"
        "! /usr/bin/dscl . -read /Users/demo >/dev/null 2>&1\n"
        "test ! -e /Users/demo -a ! -L /Users/demo\n"
        "printf 'ACCOUNT_STATE=verified\\n'\n",
    )
    if "ACCOUNT_STATE=verified" not in output.splitlines():
        raise legacy.PostCloneError("ACCOUNT_STATE_NOT_VERIFIED")
    token = legacy.ssh_sudo_script_allow_marker(
        vm_name,
        ip,
        f"/usr/sbin/sysadminctl -secureTokenStatus {vm_name}\n",
        "ENABLED",
    )
    if "ENABLED" not in token:
        raise legacy.PostCloneError("ADMIN_SECURE_TOKEN_NOT_ENABLED")


def verify_account(
    args: argparse.Namespace,
    bundle: Path,
    config_uuid: str,
    state: dict[str, Any],
    *,
    shutdown_after: bool,
) -> dict[str, Any]:
    current_uuid, config_mac = legacy.config_identity(bundle)
    if current_uuid != config_uuid:
        raise legacy.PostCloneError("target config UUID no longer matches state")
    legacy.ensure_started(args.vm_name)
    ip = legacy.resolve_ip(args.vm_name, config_mac)
    legacy.ensure_ssh(args.vm_name, ip)
    legacy.verify_admin(args.vm_name, ip)
    cleanup = legacy.ssh_sudo_script(args.vm_name, ip, legacy.demo_cleanup_script())
    if not legacy.demo_cleanup_verified(cleanup):
        raise legacy.PostCloneError("DEMO_CLEANUP_NOT_VERIFIED")
    residual = legacy.ssh_sudo_script(
        args.vm_name, ip, legacy.residual_demo_home_cleanup_script()
    )
    if "DEMO_RESIDUAL_HOME=absent" not in residual.splitlines():
        raise legacy.PostCloneError("DEMO_RESIDUAL_HOME_NOT_VERIFIED")
    _pure_account_probe(args.vm_name, ip)
    identity = legacy.read_guest_identity(args.vm_name, ip, config_mac)
    record = legacy.get_record(args.database, args.vm_name)
    expected = (
        str(record.get("guest_serial_number") or ""),
        str(record.get("guest_platform_uuid") or "").upper(),
    )
    actual = (identity.serial_number, identity.platform_uuid.upper())
    if not all(expected) or actual != expected:
        raise legacy.PostCloneError("ACCOUNT_GUEST_IDENTITY_MISMATCH")
    if shutdown_after:
        try:
            legacy.ssh_sudo_script(args.vm_name, ip, legacy.guest_shutdown_script())
        except legacy.GuestAutomationError:
            pass
        legacy.ensure_stopped(args.vm_name)
    return {"ip": ip, "account": "verified", "shutdown": "verified" if shutdown_after else "not_requested"}


def _read_utm_preferences(preference_path: Path | None = None) -> dict[str, Any]:
    raw = subprocess.check_output(
        ["defaults", "export", "com.utmapp.UTM", "-"],
        stderr=subprocess.STDOUT,
    )
    payload = plistlib.loads(raw)
    if not isinstance(payload, dict):
        raise legacy.PostCloneError("UTM_REGISTRY_ROOT_INVALID")
    if payload:
        return payload
    direct_path = preference_path or (
        Path.home() / "Library" / "Preferences" / "com.utmapp.UTM.plist"
    )
    if direct_path.is_symlink():
        raise legacy.PostCloneError("UTM_REGISTRY_PATH_INVALID")
    if direct_path.is_file():
        direct = plistlib.loads(direct_path.read_bytes())
        if not isinstance(direct, dict):
            raise legacy.PostCloneError("UTM_REGISTRY_ROOT_INVALID")
        if direct:
            return direct
    return payload


def _registry_entry(config_uuid: str, vm_name: str, bundle: Path) -> dict[str, Any]:
    def read_entry() -> dict[str, Any] | None:
        try:
            payload = _read_utm_preferences()
            entries = payload.get("Registry", payload)
            value = entries.get(config_uuid)
            if not isinstance(value, dict):
                candidates = [
                    item
                    for key, item in entries.items()
                    if str(key).upper() == config_uuid.upper() and isinstance(item, dict)
                ]
                value = candidates[0] if len(candidates) == 1 else None
            if not isinstance(value, dict):
                candidates = [
                    item
                    for item in entries.values()
                    if isinstance(item, dict)
                    and (
                        item.get("Name") == vm_name
                        or (
                            isinstance(item.get("Package"), dict)
                            and item["Package"].get("Path") == str(bundle)
                        )
                    )
                ]
                value = candidates[0] if len(candidates) == 1 else None
            return value if isinstance(value, dict) else None
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException) as error:
            raise legacy.PostCloneError("UTM_REGISTRY_READ_FAILED") from error

    def matches(entry: dict[str, Any] | None) -> bool:
        return bool(
            isinstance(entry, dict)
            and entry.get("Name") == vm_name
            and isinstance(entry.get("Package"), dict)
            and entry["Package"].get("Path") == str(bundle)
        )

    entry = read_entry()
    if not matches(entry):
        subprocess.run(["open", str(bundle)], check=True)
        time.sleep(legacy.WAIT_SECONDS)
        entry = read_entry()
    if not matches(entry):
        subprocess.run(
            ["osascript", "-e", 'tell application "UTM" to quit'],
            check=True,
        )
        time.sleep(legacy.WAIT_SECONDS)
        subprocess.run(["open", str(bundle)], check=True)
        time.sleep(legacy.WAIT_SECONDS)
        entry = read_entry()
    if not matches(entry):
        try:
            payload = _read_utm_preferences()
            entries = payload.get("Registry", payload)
            template_data = plistlib.loads((VM_TEMPLATE / "config.plist").read_bytes())
            template_uuid = str(template_data["Information"]["UUID"]).upper()
            source = entries.get(template_uuid)
            if not isinstance(source, dict):
                candidates = [
                    value
                    for key, value in entries.items()
                    if str(key).upper() == template_uuid and isinstance(value, dict)
                ]
                source = candidates[0] if len(candidates) == 1 else None
            sources = [
                shared
                for shared in (source.get("SharedDirectories", []) if isinstance(source, dict) else [])
                if isinstance(shared, dict)
                and shared.get("Path") == str(SHARED_DIR.resolve())
                and shared.get("ReadOnly") is True
                and isinstance(shared.get("Bookmark"), bytes)
                and shared["Bookmark"]
            ]
            if sources:
                shared = sources[0]
            else:
                authorized = []
                for key in sorted(entries, key=str):
                    value = entries[key]
                    if not isinstance(value, dict):
                        continue
                    for item in value.get("SharedDirectories", []):
                        if (
                            isinstance(item, dict)
                            and item.get("Path") == str(SHARED_DIR.resolve())
                            and item.get("ReadOnly") is True
                            and isinstance(item.get("Bookmark"), bytes)
                            and item["Bookmark"]
                        ):
                            authorized.append(item)
                if not authorized:
                    try:
                        from Foundation import NSURL

                        url = NSURL.fileURLWithPath_(str(SHARED_DIR.resolve()))
                        result = url.bookmarkDataWithOptions_includingResourceValuesForKeys_relativeToURL_error_(
                            (1 << 11) | (1 << 12), None, None, None
                        )
                        data = result[0] if isinstance(result, tuple) else result
                        bookmark = bytes(data)
                    except Exception as error:
                        raise legacy.PostCloneError(
                            "UTM_REGISTRY_BOOKMARK_CREATE_FAILED"
                        ) from error
                    if not bookmark:
                        raise legacy.PostCloneError("UTM_REGISTRY_BOOKMARK_CREATE_EMPTY")
                    shared = {
                        "Path": str(SHARED_DIR.resolve()),
                        "ReadOnly": True,
                        "Bookmark": bookmark,
                    }
                else:
                    shared = authorized[0]
            entries[config_uuid] = {
                "Name": vm_name,
                "Package": {"Path": str(bundle)},
                "SharedDirectories": [dict(shared)],
            }
            with tempfile.TemporaryDirectory(prefix="utm-registry-repair.") as directory:
                preferences = Path(directory) / "preferences.plist"
                preferences.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_BINARY))
                subprocess.run(
                    ["osascript", "-e", 'tell application "UTM" to quit'],
                    check=True,
                )
                time.sleep(legacy.WAIT_SECONDS)
                subprocess.run(
                    ["defaults", "import", "com.utmapp.UTM", str(preferences)],
                    check=True,
                )
                preference_path = (
                    Path.home()
                    / "Library"
                    / "Preferences"
                    / "com.utmapp.UTM.plist"
                )
                descriptor, temporary = tempfile.mkstemp(
                    prefix=".com.utmapp.UTM.", dir=preference_path.parent
                )
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(preferences.read_bytes())
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, preference_path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                subprocess.run(["killall", "cfprefsd"], check=False)
                time.sleep(legacy.WAIT_SECONDS)
            entry = read_entry()
        except legacy.PostCloneError:
            raise
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException) as error:
            raise legacy.PostCloneError("UTM_REGISTRY_REPAIR_FAILED") from error
    if not matches(entry):
        actual_name = entry.get("Name") if isinstance(entry, dict) else None
        package = entry.get("Package") if isinstance(entry, dict) else None
        actual_path = package.get("Path") if isinstance(package, dict) else None
        raise legacy.PostCloneError(
            "UTM_REGISTRY_TARGET_MISMATCH:"
            f"EXPECTED_NAME={vm_name}:ACTUAL_NAME={actual_name}:"
            f"EXPECTED_PATH={bundle}:ACTUAL_PATH={actual_path}"
        )
    return entry


def verify_hardware(
    args: argparse.Namespace, bundle: Path, config_uuid: str, state: dict[str, Any]
) -> dict[str, Any]:
    legacy.ensure_stopped(args.vm_name)
    current_uuid, config_mac = legacy.config_identity(bundle)
    step = state.get("step_evidence", {}).get("3", {})
    expected_mac = str(step.get("mac") or "").lower()
    if current_uuid != config_uuid or not expected_mac or config_mac != expected_mac:
        raise legacy.PostCloneError("HARDWARE_CONFIG_READBACK_MISMATCH")
    entry = _registry_entry(config_uuid, args.vm_name, bundle)
    shares = entry.get("SharedDirectories")
    matches = [
        item
        for item in shares or []
        if isinstance(item, dict)
        and item.get("Path") == str(args.shared_dir)
        and item.get("ReadOnly") is True
    ]
    if len(matches) != 1 or len(shares or []) != 1:
        raise legacy.PostCloneError("HARDWARE_SHARING_READBACK_MISMATCH")
    legacy.update_guest_mac(args.database, args.vm_name, config_mac)
    return {"config_uuid": current_uuid, "mac": config_mac, "sharing": "verified"}


def verify_desktop(
    args: argparse.Namespace, bundle: Path, config_uuid: str, state: dict[str, Any]
) -> dict[str, Any]:
    current_uuid, config_mac = legacy.config_identity(bundle)
    if current_uuid != config_uuid:
        raise legacy.PostCloneError("target config UUID no longer matches state")
    legacy.ensure_started(args.vm_name)
    ip = legacy.resolve_ip(args.vm_name, config_mac)
    legacy.ensure_ssh(args.vm_name, ip)
    current, values = legacy.read_desktop_state(args.vm_name, ip)
    recoverable_login_window = (
        values.get("CONSOLE_USER") == "root"
        and values.get("FINDER") == "missing"
        and values.get("SETUP_ASSISTANT") == "absent"
    )
    recoverable_user_transition = (
        values.get("CONSOLE_USER") == args.vm_name
        and values.get("FINDER") in {"missing", "ready"}
        and values.get("SETUP_ASSISTANT") in {"absent", "present"}
    )
    if (
        not legacy.desktop_state_verified(current, args.vm_name)
        and (recoverable_login_window or recoverable_user_transition)
    ):
        legacy._enter_guest_desktop(args.vm_name, ip, args.vm_name, bundle)
        current, values = legacy.read_desktop_state(args.vm_name, ip)
    if not legacy.desktop_state_verified(current, args.vm_name):
        raise legacy.PostCloneError(f"DESKTOP_STATE_NOT_VERIFIED={values}")
    legacy.verify_admin(args.vm_name, ip)
    _pure_account_probe(args.vm_name, ip)
    return {"ip": ip, "desktop": "verified", "setup_assistant": "absent", "finder": "ready"}


def _verify_clash_app_data(vm_name: str, ip: str) -> None:
    output = legacy.ssh_script(
        vm_name,
        ip,
        "set -e\n"
        "base=\"$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev\"\n"
        "test -d \"$base\" -a ! -L \"$base\"\n"
        "for name in verge.yaml config.yaml profiles.yaml; do test -f \"$base/$name\" -a ! -L \"$base/$name\"; done\n"
        "printf 'CLASH_APP_DATA=initialized_verified\\n'\n",
    )
    if "CLASH_APP_DATA=initialized_verified" not in output.splitlines():
        raise legacy.PostCloneError("CLASH_APP_DATA_NOT_VERIFIED")


def verify_settings(
    args: argparse.Namespace, bundle: Path, config_uuid: str
) -> dict[str, Any]:
    ip = _running_ip(args.vm_name, bundle, config_uuid)
    settings = legacy.parse_key_values(
        legacy.ssh_sudo_script(args.vm_name, ip, legacy.settings_read_script())
    )
    if not legacy.settings_verified(settings):
        raise legacy.PostCloneError(f"SYSTEM_SETTINGS_NOT_PERMANENT={settings}")
    _verify_clash_app_data(args.vm_name, ip)
    return {"ip": ip, "settings": "verified", "clash_app_data": "initialized_verified"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_same_file(source: Path, target: Path, code: str) -> None:
    if (
        not source.is_file()
        or source.is_symlink()
        or not target.is_file()
        or target.is_symlink()
        or _sha256(source) != _sha256(target)
    ):
        raise legacy.PostCloneError(code)


def verify_delivery(args: argparse.Namespace, bundle: Path, config_uuid: str) -> dict[str, Any]:
    ip = _running_ip(args.vm_name, bundle, config_uuid)
    target = args.shared_dir / "AppleAccountScriptsBackup"
    for name in legacy.BUSINESS_GUEST_FILES:
        _require_same_file(PROJECT_ROOT / "scripts" / name, target / name, "BUSINESS_DELIVERY_MISMATCH")
    for name in PLAYWRIGHT_GUEST_FILES:
        _require_same_file(PROJECT_ROOT / "scripts" / name, target / name, "APPS_DELIVERY_MISMATCH")
    image_name = IMAGE_GUEST_FILE
    _require_same_file(
        PROJECT_ROOT / "skills" / "utm-image" / "scripts" / image_name,
        target / image_name,
        "IMAGE_DELIVERY_MISMATCH",
    )
    copied = legacy.verify_copy(args.vm_name, ip)
    return {"ip": ip, "copy": "verified", "source_entries": copied.get("SOURCE_ENTRIES")}


def _record_step(
    state_path: Path,
    state: dict[str, Any],
    step: int,
    evidence: dict[str, Any],
) -> None:
    done = [int(value) for value in state.get("steps", [])]
    if step in done:
        return
    if done != list(range(2, step)):
        raise legacy.PostCloneError(f"CLONE_STEP_OUT_OF_ORDER={step}:DONE={done}")
    state["steps"] = done + [step]
    state.setdefault("step_evidence", {})[str(step)] = evidence
    legacy._write_state(state_path, state)


def run_account(vm_name: str) -> None:
    context, completed = _prepare_step(vm_name, 2)
    args, bundle, config_uuid, config_mac, state_path, state = context
    if completed:
        evidence = verify_account(args, bundle, config_uuid, state, shutdown_after=True)
        _record_reverification(state_path, state, 2, evidence)
        return
    evidence = legacy.perform_account_setup(args, bundle, config_uuid, config_mac)
    _record_step(state_path, state, 2, evidence)
    verified = legacy._load_state(state_path, vm_name, bundle, config_uuid)
    verify_account(args, bundle, config_uuid, verified, shutdown_after=True)


def run_hardware(vm_name: str) -> None:
    context, completed = _prepare_step(vm_name, 3)
    args, bundle, config_uuid, _, state_path, state = context
    if completed:
        evidence = verify_hardware(args, bundle, config_uuid, state)
        _record_reverification(state_path, state, 3, evidence)
        return
    evidence = legacy.perform_hardware_setup(args, bundle, config_uuid)
    _record_step(state_path, state, 3, evidence)
    verified = legacy._load_state(state_path, vm_name, bundle, config_uuid)
    verify_hardware(args, bundle, config_uuid, verified)


def run_desktop(vm_name: str) -> None:
    context, completed = _prepare_step(vm_name, 4)
    args, bundle, config_uuid, _, state_path, state = context
    if completed:
        evidence = verify_desktop(args, bundle, config_uuid, state)
        _record_reverification(state_path, state, 4, evidence)
        return
    current_uuid, config_mac = legacy.config_identity(bundle)
    if current_uuid != config_uuid:
        raise legacy.PostCloneError("target config UUID no longer matches state")
    legacy.ensure_started(vm_name)
    ip = legacy.resolve_ip(vm_name, config_mac)
    legacy.ensure_ssh(vm_name, ip)
    desktop = legacy._enter_guest_desktop(vm_name, ip, vm_name, bundle)
    legacy.verify_admin(vm_name, ip)
    cleanup = legacy.ssh_sudo_script(vm_name, ip, legacy.demo_cleanup_script())
    if not legacy.demo_cleanup_verified(cleanup):
        raise legacy.PostCloneError("DEMO_CLEANUP_NOT_VERIFIED")
    _record_step(
        state_path,
        state,
        4,
        {
            "ip": ip,
            "desktop": "verified",
            "setup_assistant_action": desktop.get("SETUP_ASSISTANT_ACTION", "already_absent"),
        },
    )


def _running_ip(vm_name: str, bundle: Path, config_uuid: str) -> str:
    current_uuid, config_mac = legacy.config_identity(bundle)
    if current_uuid != config_uuid:
        raise legacy.PostCloneError("target config UUID no longer matches state")
    legacy.ensure_started(vm_name)
    ip = legacy.resolve_ip(vm_name, config_mac)
    legacy.ensure_ssh(vm_name, ip)
    return ip


def run_settings(vm_name: str) -> None:
    context, completed = _prepare_step(vm_name, 5)
    args, bundle, config_uuid, _, state_path, state = context
    if completed:
        evidence = verify_settings(args, bundle, config_uuid)
        _record_reverification(state_path, state, 5, evidence)
        return

    ip = _running_ip(vm_name, bundle, config_uuid)
    legacy.disable_screen_lock(vm_name, ip)
    legacy.ssh_sudo_script(vm_name, ip, legacy.settings_write_script())
    settings = legacy.parse_key_values(
        legacy.ssh_sudo_script(vm_name, ip, legacy.settings_read_script())
    )
    if not legacy.settings_verified(settings):
        raise legacy.PostCloneError(f"SYSTEM_SETTINGS_NOT_PERMANENT={settings}")
    legacy.bootstrap_clash_app_data(vm_name, ip)
    _record_step(
        state_path,
        state,
        5,
        {"ip": ip, "settings": settings, "clash_app_data": "initialized_verified"},
    )


def run_delivery(vm_name: str) -> None:
    context, completed = _prepare_step(vm_name, 6)
    args, bundle, config_uuid, _, state_path, state = context
    if completed:
        evidence = verify_delivery(args, bundle, config_uuid)
        _record_reverification(state_path, state, 6, evidence)
        return
    ip = _running_ip(vm_name, bundle, config_uuid)
    staged_business = legacy.stage_business_guest_files(args.shared_dir)
    staged_apps = legacy.stage_utm_apps_playwright_files(args.shared_dir)
    staged_image = legacy.stage_utm_image_guest_file(args.shared_dir)
    copied = legacy.copy_shared(vm_name, ip)
    _record_step(
        state_path,
        state,
        6,
        {
            "ip": ip,
            "source_entries": copied.get("SOURCE_ENTRIES"),
            "copy": "verified",
            "business": sorted(staged_business),
            "apps": sorted(staged_apps),
            "image": sorted(staged_image),
        },
    )


def run_dependency_checker(vm_name: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            sys.executable,
            str(DEPENDENCY_CHECK_SCRIPT),
            "--vm-name",
            vm_name,
            "--active-clone",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0:
        raise legacy.PostCloneError("DEPENDENCY_CHECK_COMMAND_FAILED")
    missing = [line.removesuffix("未安装") for line in lines if line.endswith("未安装")]
    if missing:
        raise legacy.PostCloneError(
            "DEPENDENCY_CHECK_NOT_ALL_INSTALLED=" + ",".join(missing)
        )
    if tuple(lines) != EXPECTED_DEPENDENCY_LINES or len(lines) != 26:
        raise legacy.PostCloneError("DEPENDENCY_CHECK_OUTPUT_INVALID")
    return {"dependency_count": 26, "all_installed": "verified"}


def run_dependencies(vm_name: str) -> None:
    context, completed = _prepare_step(vm_name, 7)
    _, _, _, _, state_path, state = context
    evidence = run_dependency_checker(vm_name)
    if completed:
        _record_reverification(state_path, state, 7, evidence)
    else:
        _record_step(state_path, state, 7, evidence)


def record_accessibility(vm_name: str) -> None:
    context, completed = _prepare_step(vm_name, 8)
    _, bundle, config_uuid, _, state_path, state = context
    ip = _running_ip(vm_name, bundle, config_uuid)
    if completed:
        _record_reverification(
            state_path,
            state,
            8,
            {"ip": ip, "accessibility": "verified"},
        )
        return
    _record_step(state_path, state, 8, {"ip": ip, "accessibility": "verified"})


def run_app_management(vm_name: str) -> None:
    context, completed = _prepare_step(vm_name, 9)
    _, bundle, config_uuid, _, state_path, state = context
    ip = _running_ip(vm_name, bundle, config_uuid)
    output = legacy._run(
        [
            sys.executable,
            legacy.TERMINAL_APP_MANAGEMENT_SCRIPT,
            "--vm-name",
            vm_name,
            "--vm-ip",
            ip,
        ],
        timeout_seconds=300,
    )
    for marker in (
        "APP_MANAGEMENT_PAGE=verified",
        "TERMINAL_APP_MANAGEMENT=verified",
    ):
        if marker not in output.splitlines():
            raise legacy.PostCloneError(f"TERMINAL_APP_MANAGEMENT_MISSING={marker}")
    evidence = {"ip": ip, "app_management": "verified"}
    if completed:
        _record_reverification(state_path, state, 9, evidence)
    else:
        _record_step(state_path, state, 9, evidence)


def run_finalize(vm_name: str) -> None:
    context, completed = _prepare_step(vm_name, 10)
    args, bundle, config_uuid, _, state_path, state = context
    if completed:
        if legacy._status(vm_name) != "stopped":
            evidence = legacy.finalize(
                vm_name=vm_name,
                bundle=bundle,
                expected_config_uuid=config_uuid,
                database=args.database,
            )
            _record_reverification(state_path, state, 10, evidence)
            return
        record = legacy.get_record(args.database, vm_name)
        if (
            str(record.get("bundle_path") or "") != str(bundle)
            or str(record.get("config_uuid") or "").upper() != config_uuid
            or int(record.get("available") or 0) != 1
            or int(record.get("directory_present") or 0) != 1
        ):
            raise legacy.PostCloneError("FINAL_INVENTORY_REVERIFICATION_FAILED")
        _record_reverification(
            state_path,
            state,
            10,
            {"shutdown": "verified", "available": "1"},
        )
        return
    evidence = legacy.finalize(
        vm_name=vm_name,
        bundle=bundle,
        expected_config_uuid=config_uuid,
        database=args.database,
    )
    _record_step(state_path, state, 10, evidence)


def main_for_step(step: int, operation: Callable[[str], None]) -> int:
    parser = argparse.ArgumentParser(description=f"utm-vm-clone segmented step {step}")
    parser.add_argument("--vm-name", required=True)
    args = parser.parse_args()
    marker = step_marker(step)

    def run_step() -> None:
        completed = step in [
            int(value) for value in load_context(args.vm_name)[-1].get("steps", [])
        ]
        operation(args.vm_name)
        if completed:
            emit_progress(f"步骤{step}已完成，跳过")
        else:
            emit_progress(f"步骤{step}已操作")

    return run_clean_cli(
        skill_name=f"utm-vm-clone-step-{step:02d}",
        success_marker=marker,
        operation=run_step,
        progress_pattern=r"步骤(?:[1-9]|10)(?:已操作|已完成，跳过)",
        preserve_error_detail=True,
    )
