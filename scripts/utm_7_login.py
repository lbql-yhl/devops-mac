#!/usr/bin/env python3
"""Run the project-owned Apple Account login helper with live Notion fields.

Secrets are read through the Notion API, sent to the guest only through SSH
stdin as JSON, and never placed in argv, logs, or a temporary plaintext file.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

# When invoked as ``python3 scripts/utm_7_login.py`` Python puts only the
# scripts directory on ``sys.path``.  Add the project root before importing
# the shared ``scripts`` and ``services`` packages so the documented entry
# point works from any current working directory.
PROJECT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_SOURCE_ROOT))

from scripts.notion_api import api_from_env  # noqa: E402
from scripts.clean_cli import run_clean_cli  # noqa: E402
from scripts.ssh_password import (  # noqa: E402
    password_environment,
    ssh_args as shared_ssh_args,
)
from services.host_config import guest_password  # noqa: E402
from services.project_paths import SHARED_DIR, VM_IMAGES_DIR  # noqa: E402


LOGIN_FILES = (
    "apple_account_login.py",
    "find_system_settings_general.py",
    "apple_account_post_login.py",
    "mac_password_prompt.py",
)

SHARED_HELPER_FILES = (
    "apple_account_change_password.py",
    *LOGIN_FILES,
    "apple_account_profile.py",
    "apple_web_workflow.py",
    "edge_accessibility.py",
)
SHARED_HELPER_DIR_NAME = "AppleAccountScriptsBackup"
GUEST_SHARED_HELPER_DIR = (
    "/Volumes/My Shared Files/共享文件/AppleAccountScriptsBackup"
)
PYOBJC_PACKAGES = (
    "pyobjc-framework-ApplicationServices",
    "pyobjc-framework-Cocoa",
)
PYOBJC_IMPORT_PROBE = (
    "import ApplicationServices;"
    "from AppKit import NSRunningApplication;"
    "print('PYOBJC_IMPORT=verified')"
)
ACCESSIBILITY_PROMPT_PROBE = (
    "import ApplicationServices as A;"
    "raise SystemExit(0 if A.AXIsProcessTrustedWithOptions("
    "{A.kAXTrustedCheckOptionPrompt:True}) else 1)"
)
EXISTING_HELPER_BRIDGE = """\
import json, os, runpy, sys
payload = json.load(sys.stdin)
allowed = (
    "APPLE_ACCOUNT_EMAIL",
    "APPLE_ACCOUNT_PASSWORD",
    "APPLE_ACCOUNT_PHONE",
    "APPLE_ACCOUNT_SMS_URL",
    "SUBMISSION_GUEST_PASSWORD",
)
previous = {key: os.environ[key] for key in allowed if key in os.environ}
try:
    os.environ.update({key: str(payload[key]) for key in allowed})
    script = sys.argv[1]
    sys.path.insert(0, os.path.dirname(script))
    sys.argv = [script]
    runpy.run_path(script, run_name="__main__")
finally:
    for key in allowed:
        if key in previous:
            os.environ[key] = previous[key]
        else:
            os.environ.pop(key, None)
"""


def _read_field(api, page_title: str, label: str) -> str:
    value = api.read_field(page_title, "账号信息", label).strip()
    if not value:
        raise RuntimeError(f"Notion field {label!r} is empty")
    return value


def _redacted_process_detail(
    value: bytes | str | None, sensitive_values: tuple[str, ...]
) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(value or "").splitlines()]
    detail = " | ".join(line for line in lines if line) or "process exited without detail"
    for sensitive in sensitive_values:
        if sensitive:
            detail = detail.replace(sensitive, "<redacted>")
    return re.sub(r"https?://\S+", "<redacted-url>", detail)


def _ssh_args(user: str, ip: str) -> list[str]:
    return shared_ssh_args(user, ip, connect_timeout=8)


def _remote_python_command(*arguments: str) -> str:
    """Build one safely quoted remote command for OpenSSH's remote shell."""
    return shlex.join(("python3", *arguments))


def _validated_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    name = profile.get("name")
    year = profile.get("birth_year")
    month = profile.get("birth_month")
    day = profile.get("birth_day")
    if (
        not isinstance(name, str)
        or not name.strip()
        or "@" in name
        or "\n" in name
        or "\r" in name
    ):
        raise RuntimeError("APPLE_ACCOUNT_PROFILE_INVALID")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (year, month, day)):
        raise RuntimeError("APPLE_ACCOUNT_PROFILE_INVALID")
    try:
        parsed = date(year, month, day)
    except ValueError as error:
        raise RuntimeError("APPLE_ACCOUNT_PROFILE_INVALID") from error
    return {
        "name": name.strip(),
        "birth_year": parsed.year,
        "birth_month": parsed.month,
        "birth_day": parsed.day,
    }


def format_notion_birthday(profile: Mapping[str, Any]) -> str:
    validated = _validated_profile(profile)
    return (
        f"{validated['birth_year']}/"
        f"{validated['birth_month']}/"
        f"{validated['birth_day']}"
    )


def _read_guest_profile(
    user: str,
    ip: str,
    guest_dir: str,
    expected_email: str,
    expected_name: str = "",
) -> dict[str, Any]:
    script = f"{guest_dir}/apple_account_profile.py"
    result = subprocess.run(
        _ssh_args(user, ip)
        + [_remote_python_command("-B", script, "--stdin-json")],
        input=json.dumps(
            {"expected_email": expected_email, "expected_name": expected_name},
            ensure_ascii=False,
        ).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=password_environment(),
    )
    if result.returncode != 0:
        stderr = bytes(result.stderr or b"").decode("utf-8", errors="replace")
        match = re.search(
            r"APPLE_ACCOUNT_PROFILE=blocked reason="
            r"([A-Z][A-Z0-9_]*(?::[A-Z][A-Z0-9_]*)?)",
            stderr,
        )
        reason = match.group(1) if match else f"EXIT_{result.returncode}"
        raise RuntimeError(f"APPLE_ACCOUNT_PROFILE_READ_FAILED:{reason}")
    try:
        payload = json.loads(bytes(result.stdout).decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise RuntimeError("APPLE_ACCOUNT_PROFILE_INVALID") from error
    if not isinstance(payload, dict):
        raise RuntimeError("APPLE_ACCOUNT_PROFILE_INVALID")
    return _validated_profile(payload)


def write_profile_to_notion(
    api: Any,
    page_title: str,
    profile: Mapping[str, Any],
) -> None:
    validated = _validated_profile(profile)
    desired = {
        "用户名：": validated["name"],
        "生日（格式年/月/日）：": format_notion_birthday(validated),
    }
    before = {
        label: api.read_field(page_title, "账号信息", label)
        for label in desired
    }
    for label, value in desired.items():
        current = before[label]
        if label == "用户名：" and current and current != value:
            raise RuntimeError(f"NOTION_PROFILE_CONFLICT={label}")

    changed: list[str] = []
    try:
        for label, value in desired.items():
            if before[label] == value:
                continue
            api.set_field(
                page_title,
                "账号信息",
                label,
                value,
                replace_existing=bool(before[label]),
            )
            changed.append(label)
        for label, value in desired.items():
            if api.read_field(page_title, "账号信息", label) != value:
                raise RuntimeError(f"NOTION_PROFILE_READBACK_FAILED={label}")
    except Exception as original_error:
        try:
            for label in reversed(changed):
                api.set_field(
                    page_title,
                    "账号信息",
                    label,
                    before[label],
                    replace_existing=True,
                )
            for label, value in before.items():
                if api.read_field(page_title, "账号信息", label) != value:
                    raise RuntimeError(f"NOTION_PROFILE_ROLLBACK_MISMATCH={label}")
        except Exception as rollback_error:
            raise RuntimeError("NOTION_PROFILE_ROLLBACK_FAILED") from rollback_error
        raise original_error


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validated_helper_payload(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"project helper is not a regular file: {path.name}")
    payload = path.read_bytes()
    if not payload:
        raise RuntimeError(f"project helper is empty: {path.name}")
    ast.parse(payload.decode("utf-8"), filename=str(path))
    return payload


def sync_helpers_to_shared_backup(
    source_dir: Path, shared_root: Path
) -> dict[str, str]:
    """Atomically synchronize the exact helper set into the host shared backup."""
    source_dir = Path(source_dir).resolve()
    shared_root = Path(shared_root).resolve()
    if not shared_root.is_dir() or shared_root.is_symlink():
        raise RuntimeError("configured SUBMISSION_SHARED_DIR is missing or unsafe")

    backup_dir = shared_root / SHARED_HELPER_DIR_NAME
    if backup_dir.exists():
        if not backup_dir.is_dir() or backup_dir.is_symlink():
            raise RuntimeError("AppleAccountScriptsBackup is not a safe directory")
    else:
        backup_dir.mkdir(mode=0o755)

    expected: dict[str, str] = {}
    replaced = False
    for name in SHARED_HELPER_FILES:
        payload = _validated_helper_payload(source_dir / name)
        digest = _sha256_bytes(payload)
        expected[name] = digest
        target = backup_dir / name
        if target.exists() or target.is_symlink():
            if not target.is_file() or target.is_symlink():
                raise RuntimeError(f"shared helper target is unsafe: {name}")
            if _sha256_bytes(target.read_bytes()) == digest:
                continue

        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".utm-7-sync-", dir=backup_dir
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, 0o644)
            os.replace(temporary_path, target)
            replaced = True
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    if replaced:
        directory_fd = os.open(backup_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    for name, digest in expected.items():
        target = backup_dir / name
        if (
            not target.is_file()
            or target.is_symlink()
            or _sha256_bytes(target.read_bytes()) != digest
        ):
            raise RuntimeError(f"shared helper readback mismatch: {name}")
    return expected


def _verify_guest_shared_helpers(
    user: str,
    ip: str,
    guest_dir: str,
    expected_hashes: dict[str, str],
) -> None:
    """Independently verify the shared mount has the exact host helper bytes."""
    names = tuple(SHARED_HELPER_FILES)
    remote_paths = [f"{guest_dir}/{name}" for name in names]
    verifier = (
        "import ast,hashlib,pathlib,sys;"
        "count=int(sys.argv[1]);"
        "paths=[pathlib.Path(value) for value in sys.argv[2:2+count]];"
        "digests=sys.argv[2+count:];"
        "assert all(path.is_file() and not path.is_symlink() and path.stat().st_size>0 "
        "for path in paths);"
        "[ast.parse(path.read_text(encoding='utf-8'),filename=str(path)) for path in paths];"
        "assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]==digests"
    )
    subprocess.run(
        _ssh_args(user, ip)
        + [
            _remote_python_command(
                "-B",
                "-c",
                verifier,
                str(len(names)),
                *remote_paths,
                *(expected_hashes[name] for name in names),
            )
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=password_environment(),
    )


def _refresh_guest_shared_mount(user: str, ip: str, guest_dir: str) -> None:
    """Refresh one stale AppleVirtIOFS mount without copying helpers elsewhere."""
    if guest_dir != GUEST_SHARED_HELPER_DIR:
        raise RuntimeError("shared-mount refresh is allowed only for the canonical guest path")
    from scripts.utm_post_clone_guest import ssh_sudo_script

    script = r"""set -e
target='/Volumes/My Shared Files'
mount_line=$(/sbin/mount | /usr/bin/grep -F " on $target " || true)
if [ -z "$mount_line" ] || ! /bin/echo "$mount_line" | /usr/bin/grep -Fq '(AppleVirtIOFS,'; then
  printf 'SHARED_MOUNT=unexpected\n'
  exit 2
fi
device=$(/usr/sbin/diskutil info "$target" | /usr/bin/awk -F: '/Device Identifier/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}')
if ! /bin/echo "$device" | /usr/bin/grep -Eq '^disk[0-9]+$'; then
  printf 'SHARED_DEVICE=invalid\n'
  exit 3
fi
count=$(/usr/bin/pgrep -f '/Volumes/My Shared Files/共享文件/AppleAccountScriptsBackup' | /usr/bin/wc -l | /usr/bin/tr -d ' ' || true)
if [ "$count" != '0' ]; then
  printf 'SHARED_HELPER_PROCESS=busy\n'
  exit 4
fi
/usr/sbin/diskutil unmount "$target" >/dev/null
/bin/sleep 3
if /sbin/mount | /usr/bin/grep -Fq " on $target "; then
  printf 'SHARED_UNMOUNT=failed\n'
  exit 5
fi
/usr/sbin/diskutil mount "/dev/$device" >/dev/null
/bin/sleep 3
mount_line=$(/sbin/mount | /usr/bin/grep -F " on $target " || true)
if [ -z "$mount_line" ] || ! /bin/echo "$mount_line" | /usr/bin/grep -Fq '(AppleVirtIOFS,'; then
  printf 'SHARED_REMOUNT=failed\n'
  exit 6
fi
printf 'SHARED_REMOUNT=verified\n'
"""
    output = ssh_sudo_script(user, ip, script, timeout_seconds=30)
    if output.strip() != "SHARED_REMOUNT=verified":
        raise RuntimeError("guest AppleVirtIOFS refresh readback failed")


def _prepare_shared_helpers(
    user: str, ip: str, guest_dir: str
) -> dict[str, str]:
    """Repair host backup drift and independently recheck the guest mount."""
    last_error: Exception | None = None
    mount_refreshed = False
    for delay_seconds in (0, 5, 10):
        if delay_seconds:
            time.sleep(delay_seconds)
        try:
            expected_hashes = sync_helpers_to_shared_backup(
                PROJECT_SOURCE_ROOT / "scripts", SHARED_DIR
            )
            _verify_guest_shared_helpers(
                user, ip, guest_dir, expected_hashes
            )
            return expected_hashes
        except Exception as error:
            last_error = error
            if guest_dir == GUEST_SHARED_HELPER_DIR and not mount_refreshed:
                try:
                    _refresh_guest_shared_mount(user, ip, guest_dir)
                except Exception as refresh_error:
                    last_error = refresh_error
                else:
                    mount_refreshed = True
                    print("APPLE_ACCOUNT_GUEST_MOUNT_REFRESH=verified")
    raise RuntimeError("shared Apple Account helper recovery exhausted") from last_error


def _probe_remote_pyobjc(user: str, ip: str) -> bool:
    result = subprocess.run(
        _ssh_args(user, ip)
        + [_remote_python_command("-B", "-c", PYOBJC_IMPORT_PROBE)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=password_environment(),
    )
    return result.returncode == 0


def _ensure_remote_pyobjc(user: str, ip: str) -> None:
    """Install missing PyObjC in a separate process and verify in a fresh one."""
    if _probe_remote_pyobjc(user, ip):
        return
    for _attempt in range(3):
        install = subprocess.run(
            _ssh_args(user, ip)
            + [
                _remote_python_command(
                    "-m", "pip", "install", "--user", *PYOBJC_PACKAGES
                )
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=password_environment(),
        )
        if install.returncode != 0:
            subprocess.run(
                _ssh_args(user, ip)
                + [_remote_python_command("-m", "pip", "install", *PYOBJC_PACKAGES)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                env=password_environment(),
            )
        if _probe_remote_pyobjc(user, ip):
            return
    raise RuntimeError("guest PyObjC automatic recovery exhausted")


def _probe_remote_accessibility(user: str, ip: str) -> bool:
    result = subprocess.run(
        _ssh_args(user, ip)
        + [_remote_python_command("-B", "-c", ACCESSIBILITY_PROMPT_PROBE)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=password_environment(),
    )
    return result.returncode == 0


def _accessibility_recovery_applescript(user: str, ip: str) -> str:
    """Return the semantic three-dialog recovery used through guest AEServer."""
    configured_password = guest_password()
    machine = f"eppc://{user}:{configured_password}@{ip}"
    password = configured_password.replace("\\", "\\\\").replace('"', '\\"')
    return f'''
using terms from application "System Events"
 tell application "System Events" of machine "{machine}"
  if exists process "universalAccessAuthWarn" then
   tell process "universalAccessAuthWarn"
    set openButtons to every button of window 1 whose name is "Open System Settings"
    if (count of openButtons) is not 1 then error "OPEN_SETTINGS_NOT_UNIQUE"
    click item 1 of openButtons
   end tell
   delay 3
  end if
  if not (exists process "System Settings") then error "SYSTEM_SETTINGS_NOT_OPEN"
  tell process "System Settings"
   if name of window 1 is not "Accessibility" then error "ACCESSIBILITY_PAGE_NOT_OPEN"
   tell outline 1 of scroll area 1 of group 1 of scroll area 1 of group 1 of group 2 of splitter group 1 of group 1 of window 1
    set matchingRows to {{}}
    repeat with candidateRow in rows
     try
      set candidateCell to UI element 1 of candidateRow
      if (name of static text 1 of candidateCell) is "sshd-keygen-wrapper" then set end of matchingRows to candidateRow
     end try
    end repeat
    if (count of matchingRows) is not 1 then error "SSHD_ROW_NOT_UNIQUE"
    set targetCell to UI element 1 of item 1 of matchingRows
    set targetToggle to checkbox 1 of targetCell
    if (value of targetToggle as integer) is 0 then click targetToggle
   end tell
   delay 3
   if (count of sheets of window 1) is 1 then
    tell sheet 1 of window 1
     set passwordFields to every text field whose name is "Password"
     if (count of passwordFields) is not 1 then error "PASSWORD_FIELD_NOT_UNIQUE"
     set focused of item 1 of passwordFields to true
    end tell
    delay 3
    if focused of text field "Password" of sheet 1 of window 1 is not true then error "PASSWORD_FIELD_NOT_FOCUSED"
    keystroke "a" using command down
    key code 51
    delay 3
    keystroke "{password}"
    delay 3
    tell sheet 1 of window 1
     set modifyButtons to every button whose name is "Modify Settings"
     if (count of modifyButtons) is not 1 then error "MODIFY_BUTTON_NOT_UNIQUE"
     click item 1 of modifyButtons
    end tell
    delay 3
   end if
   if (count of sheets of window 1) is not 0 then error "AUTH_SHEET_STILL_OPEN"
   tell outline 1 of scroll area 1 of group 1 of scroll area 1 of group 1 of group 2 of splitter group 1 of group 1 of window 1
    set verifiedRows to {{}}
    repeat with candidateRow in rows
     try
      set candidateCell to UI element 1 of candidateRow
      if (name of static text 1 of candidateCell) is "sshd-keygen-wrapper" then set end of verifiedRows to candidateRow
     end try
    end repeat
    if (count of verifiedRows) is not 1 then error "SSHD_ROW_VERIFY_NOT_UNIQUE"
    set verifiedCell to UI element 1 of item 1 of verifiedRows
    if (value of checkbox 1 of verifiedCell as integer) is not 1 then error "SSHD_ACCESSIBILITY_NOT_ENABLED"
   end tell
  end tell
  return "ACCESSIBILITY_UI_RECOVERY=verified"
 end tell
end using terms from
'''


def _start_remote_system_events(user: str, ip: str) -> bool:
    """Start the exact console user's System Events before an EPPC recovery."""
    probe = (
        "import os,subprocess,time;"
        "r=subprocess.run(['/usr/bin/open','-gja','System Events'],"
        "capture_output=True,text=True,timeout=10);"
        "time.sleep(3);"
        "p=subprocess.run(['/usr/bin/pgrep','-u',str(os.getuid()),'-x',"
        "'System Events'],capture_output=True,text=True,timeout=5);"
        "raise SystemExit(0 if r.returncode==0 and p.returncode==0 "
        "and len(p.stdout.splitlines())==1 else 1)"
    )
    result = subprocess.run(
        _ssh_args(user, ip)
        + [_remote_python_command("-B", "-c", probe)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=password_environment(),
    )
    return result.returncode == 0


def _ensure_remote_accessibility(user: str, ip: str) -> None:
    """Recover the exact sshd Accessibility permission with three bounded tries."""
    if _probe_remote_accessibility(user, ip):
        return
    source = _accessibility_recovery_applescript(user, ip)
    for _attempt in range(3):
        if not _start_remote_system_events(user, ip):
            continue
        result = subprocess.run(
            ["/usr/bin/osascript", "-"],
            input=source,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode == 0 and _probe_remote_accessibility(user, ip):
            print("REMOTE_SYSTEM_EVENTS=verified")
            return
    raise RuntimeError("guest Accessibility automatic recovery exhausted")


def _recover_guest_desktop(user: str, ip: str) -> None:
    """Enter the bound UTM guest desktop with the fixed VM credentials."""
    from scripts.utm_post_clone_guest import enter_guest_desktop

    bundle = Path(VM_IMAGES_DIR) / f"{user}.utm"
    if not bundle.is_dir():
        raise RuntimeError(f"UTM_GUEST_BUNDLE_NOT_FOUND: {bundle}")
    enter_guest_desktop(user, ip, user, bundle)


def run(args: argparse.Namespace) -> int:
    if not args.vm_user.isidentifier():
        raise RuntimeError("vm-user must be a simple macOS account name")
    if not args.vm_ip or any(char not in "0123456789abcdefABCDEF:." for char in args.vm_ip):
        raise RuntimeError("vm-ip must be a literal IPv4/IPv6 address")
    configured_guest_password = guest_password()

    api = api_from_env()
    api.verify_parent(args.parent_title)
    email = _read_field(api, args.page_title, "邮箱：")
    password = _read_field(api, args.page_title, "初始密码：")
    phone = _read_field(api, args.page_title, "电话：")
    sms_url = _read_field(api, args.page_title, "电话短信接收平台：")
    parsed_sms = urlparse(sms_url)
    if parsed_sms.scheme not in {"http", "https"} or not parsed_sms.netloc:
        raise RuntimeError("Notion SMS URL is not a valid http(s) URL")

    guest_dir = args.guest_dir or GUEST_SHARED_HELPER_DIR
    _prepare_shared_helpers(args.vm_user, args.vm_ip, guest_dir)
    print("APPLE_ACCOUNT_SHARED_BACKUP=verified")
    print("APPLE_ACCOUNT_GUEST_MOUNT=verified")
    _ensure_remote_pyobjc(args.vm_user, args.vm_ip)
    _ensure_remote_accessibility(args.vm_user, args.vm_ip)

    payload = {
        "APPLE_ACCOUNT_EMAIL": email,
        "APPLE_ACCOUNT_PASSWORD": password,
        "APPLE_ACCOUNT_PHONE": phone,
        "APPLE_ACCOUNT_SMS_URL": sms_url,
        "SUBMISSION_GUEST_PASSWORD": configured_guest_password,
    }
    remote_script = f"{guest_dir}/apple_account_login.py"
    login_command = _ssh_args(args.vm_user, args.vm_ip) + [
        _remote_python_command("-B", "-c", EXISTING_HELPER_BRIDGE, remote_script)
    ]
    login_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(3):
        result = subprocess.run(
            login_command,
            input=login_payload,
            check=False,
            capture_output=True,
            env=password_environment(),
        )
        if result.returncode == 0:
            break
        raw_detail = result.stderr or result.stdout or b""
        detail = _redacted_process_detail(
            raw_detail,
            (email, password, phone, sms_url, configured_guest_password),
        )
        if attempt < 2:
            if "短信页面中未找到唯一可识别的六位验证码" in detail:
                time.sleep(10)
            else:
                _recover_guest_desktop(args.vm_user, args.vm_ip)
                time.sleep(2)
                _ensure_remote_accessibility(args.vm_user, args.vm_ip)
            continue
        raise RuntimeError(
            f"APPLE_ACCOUNT_HELPER_FAILED_EXIT_{result.returncode}: {detail}"
        )
    print("APPLE_ACCOUNT=verified")
    expected_name = api.read_field(
        args.page_title, "账号信息", "用户名："
    ).strip()
    profile = _read_guest_profile(
        args.vm_user,
        args.vm_ip,
        guest_dir,
        email,
        expected_name,
    )
    write_profile_to_notion(api, args.page_title, profile)
    print("APPLE_ACCOUNT_PROFILE=verified")
    print("NOTION_USERNAME=verified")
    print("NOTION_BIRTHDAY=verified")
    print("UTM_7=verified")
    return 0


def main() -> int:
    started_at = time.monotonic()
    parser = argparse.ArgumentParser(description="Run UTM-7 Apple Account login")
    parser.add_argument("--parent-title", required=True)
    parser.add_argument("--page-title", required=True)
    parser.add_argument("--vm-ip", required=True)
    parser.add_argument("--vm-user", required=True)
    parser.add_argument("--guest-dir", default="")
    args = parser.parse_args()
    def operation() -> int:
        try:
            return run(args)
        finally:
            elapsed = max(0.0, time.monotonic() - started_at)
            print(f"UTM_7_ELAPSED_SECONDS={elapsed:.2f}")

    return run_clean_cli(
        skill_name="utm-login",
        success_marker="UTM_7=verified",
        operation=operation,
        preserve_error_detail=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
