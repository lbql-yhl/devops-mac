#!/usr/bin/env python3
"""Run the project-owned UTM-8 Apple Account password-change helper.

The target password is read only from the exact Notion ``修改后的密码：``
field and sent to the guest through SSH stdin JSON.  This entry does not read
any other Notion field, generate a password, or write to Notion.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path


PROJECT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_SOURCE_ROOT))

from scripts.notion_api import api_from_env  # noqa: E402
from scripts.clean_cli import run_clean_cli  # noqa: E402
from scripts.ssh_password import (  # noqa: E402
    password_environment,
    ssh_args as configured_guest_password_ssh_args,
)
from scripts.utm_7_login import (  # noqa: E402
    GUEST_SHARED_HELPER_DIR,
    _ensure_remote_accessibility,
    _ensure_remote_pyobjc,
    _prepare_shared_helpers,
    _remote_python_command,
)
from services.host_config import guest_password  # noqa: E402


REQUIRED_GUEST_MARKERS = {
    "PASSWORD_CHANGE_NAVIGATION=verified",
    "PASSWORD_CHANGE_FIELDS=verified",
    "PASSWORD_CHANGE_BUTTON=enabled",
    "PASSWORD_CHANGE_SUBMISSION=clicked_once",
    "PASSWORD_CHANGE_RESULT=verified",
    "SYSTEM_SETTINGS_CLOSED=verified",
    "PASSWORD_CHANGE=verified",
}

CHANGE_PASSWORD_HELPER_BRIDGE = """\
import io, json, os, runpy, sys
payload = json.load(sys.stdin)
configured_guest_password = str(payload.pop("SUBMISSION_GUEST_PASSWORD"))
script = sys.argv[1]
previous_stdin = sys.stdin
previous_argv = sys.argv
previous_path = list(sys.path)
guest_password_was_present = "SUBMISSION_GUEST_PASSWORD" in os.environ
previous_guest_password = os.environ.get("SUBMISSION_GUEST_PASSWORD")
try:
    os.environ["SUBMISSION_GUEST_PASSWORD"] = configured_guest_password
    sys.stdin = io.StringIO(json.dumps(payload, ensure_ascii=False))
    sys.argv = [script, "--stdin-json"]
    sys.path.insert(0, os.path.dirname(script))
    runpy.run_path(script, run_name="__main__")
finally:
    if guest_password_was_present:
        os.environ["SUBMISSION_GUEST_PASSWORD"] = previous_guest_password
    else:
        os.environ.pop("SUBMISSION_GUEST_PASSWORD", None)
    sys.stdin = previous_stdin
    sys.argv = previous_argv
    sys.path[:] = previous_path
    configured_guest_password = ""
    payload = {}
"""


def _ssh_args(user: str, ip: str) -> list[str]:
    return configured_guest_password_ssh_args(user, ip, connect_timeout=8)


def _decode_output(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _safe_guest_failure_detail(
    stderr: bytes | str | None, sensitive_values: str | Iterable[str]
) -> str:
    """Return the complete guest diagnostic with both passwords redacted."""
    if isinstance(sensitive_values, str):
        sensitive_values = (sensitive_values,)
    lines = [re.sub(r"\s+", " ", line).strip() for line in _decode_output(stderr).splitlines()]
    detail = " | ".join(line for line in lines if line) or "guest helper exited without detail"
    for sensitive_value in sensitive_values:
        if sensitive_value:
            detail = detail.replace(sensitive_value, "<redacted>")
    return detail


def _validate_context(args: argparse.Namespace) -> None:
    if not args.vm_name.isidentifier():
        raise RuntimeError("vm-name must be a simple VM name")
    if not args.vm_user.isidentifier():
        raise RuntimeError("vm-user must be a simple macOS account name")
    if args.vm_user != args.vm_name:
        raise RuntimeError("vm-user must equal the inherited vm-name")
    if not args.vm_ip or any(
        char not in "0123456789abcdefABCDEF:." for char in args.vm_ip
    ):
        raise RuntimeError("vm-ip must be a literal IPv4/IPv6 address")
    if not args.page_title.strip() or not args.page_title.endswith(f"-{args.vm_name}"):
        raise RuntimeError("page-title must be the exact <app-name>-<vm_name> page")
    guest_dir = getattr(args, "guest_dir", "")
    if guest_dir and guest_dir != GUEST_SHARED_HELPER_DIR:
        raise RuntimeError("guest-dir must be the inherited shared helper mount")


def run(args: argparse.Namespace) -> int:
    _validate_context(args)
    configured_guest_password = guest_password()

    api = api_from_env()
    target_password = api.read_field(
        args.page_title, "账号信息", "修改后的密码："
    ).strip()
    if not target_password:
        raise RuntimeError("Notion modified Apple Account password is empty")

    guest_dir = args.guest_dir or GUEST_SHARED_HELPER_DIR
    _prepare_shared_helpers(args.vm_user, args.vm_ip, guest_dir)
    print("APPLE_ACCOUNT_SHARED_BACKUP=verified")
    print("APPLE_ACCOUNT_GUEST_MOUNT=verified")
    _ensure_remote_pyobjc(args.vm_user, args.vm_ip)
    _ensure_remote_accessibility(args.vm_user, args.vm_ip)

    payload = json.dumps(
        {
            "APPLE_ACCOUNT_NEW_PASSWORD": target_password,
            "SUBMISSION_GUEST_PASSWORD": configured_guest_password,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        remote_script = f"{guest_dir}/apple_account_change_password.py"
        result = subprocess.run(
            _ssh_args(args.vm_user, args.vm_ip)
            + [
                _remote_python_command(
                    "-B", "-c", CHANGE_PASSWORD_HELPER_BRIDGE, remote_script
                )
            ],
            input=payload,
            check=False,
            capture_output=True,
            env=password_environment(),
        )
        if result.returncode != 0:
            detail = _safe_guest_failure_detail(
                result.stderr,
                (target_password, configured_guest_password),
            )
            print(f"PASSWORD_CHANGE_GUEST_FAILURE={detail}", file=sys.stderr)
            print(f"PASSWORD_CHANGE=blocked: {detail}", file=sys.stderr)
            return result.returncode
        guest_lines = set(_decode_output(result.stdout).splitlines())
        missing_markers = sorted(REQUIRED_GUEST_MARKERS - guest_lines)
        if missing_markers:
            print(
                "PASSWORD_CHANGE=blocked: guest success markers missing: "
                + ",".join(missing_markers),
                file=sys.stderr,
            )
            return 7
        print("MODIFIED_PASSWORD_SOURCE=verified")
        print("PASSWORD_CHANGE=verified")
        print("UTM_8=verified")
        return 0
    finally:
        target_password = ""
        configured_guest_password = ""
        payload = b""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run UTM-8 Apple Account password change")
    parser.add_argument("--page-title", required=True)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-ip", required=True)
    parser.add_argument("--vm-user", default="")
    parser.add_argument("--guest-dir", default="")
    args = parser.parse_args()
    if not args.vm_user:
        args.vm_user = args.vm_name
    return run_clean_cli(
        skill_name="utm-edit",
        success_marker="UTM_8=verified",
        operation=lambda: run(args),
        preserve_error_detail=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
