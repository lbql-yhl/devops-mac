#!/usr/bin/env python3
"""Run the four UTM Apps Playwright scripts through password SSH."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.notion_api import api_from_env
from scripts.clean_cli import run_clean_cli
from scripts.ssh_password import connection_options, password_environment, ssh_args
from scripts.utm_apps_delivery import sync_utm_apps_playwright_files
from scripts.utm_direct_context import resolve_direct_context
from services.feishu_bot import find_run, load_config, run_host_machine
from services.host_config import guest_password
from services.project_paths import SHARED_DIR


GUEST_DIR_TEMPLATE = "/Users/{vm_user}/Downloads/AppleAccountScriptsBackup"
GUEST_WRAPPERS: Mapping[str, str] = {
    "utm-10": "utm_10_login.mjs",
    "utm-11": "utm_11_one.mjs",
    "utm-12": "utm_12_one.mjs",
    "utm-13": "utm_13_one.mjs",
}
GUEST_WRAPPER_FILENAMES = (
    "session.mjs",
    "utm_10_login.mjs",
    "utm_11_one.mjs",
    "utm_12_one.mjs",
    "utm_13_one.mjs",
)
PLAYWRIGHT_MODULE_TEMPLATE = (
    "/Users/{vm_user}/Downloads/Fire_One_en1.3/"
    "node_modules/playwright-core/index.mjs"
)
RUN_ID_RE = re.compile(r"[A-Za-z0-9-]{8,80}")
STAGE_MARKERS: Mapping[str, tuple[str, ...]] = {
    "utm-10": ("UTM_10=verified",),
    "utm-11": (
        "UTM_11=verified",
        "SMALL_BUSINESS_SUCCESS_MESSAGES=verified",
        "REVIEW_SCREENSHOT_05=verified",
    ),
    "utm-12": ("MEMBERSHIP_DETAILS=verified", "UTM_12=verified"),
    "utm-13": ("UTM_13=verified",),
}
STAGE_LABELS: Mapping[str, str] = {
    "utm-10": "UTM-10 登陆账号步骤",
    "utm-11": "UTM-11 Business 步骤",
    "utm-12": "UTM-12 App Store Connect步骤和app信息登记步骤",
    "utm-13": "UTM-13证书步骤和Profile步骤",
}
STAGE_TIMEOUT_SECONDS: Mapping[str, int] = {
    "utm-10": 180,
    "utm-11": 60,
    "utm-12": 180,
    "utm-13": 60,
}


class AppleWebError(RuntimeError):
    """Raised when a Playwright stage cannot be safely attributed."""


@dataclass(frozen=True)
class GuestWorkflowResult:
    markers: tuple[str, ...]
    data: Mapping[str, Any]


def _print_stage_start(stage: str) -> None:
    print(f"开始 {STAGE_LABELS[stage]}", flush=True)


def _print_stage_result(stage: str, *, skipped_existing: bool) -> None:
    suffix = "检查已存在，跳过" if skipped_existing else "已完成"
    print(f"{STAGE_LABELS[stage]}{suffix}", flush=True)


def validate_target(vm_name: str, vm_ip: str, vm_user: str) -> None:
    if not re.fullmatch(r"[a-z]{4}", vm_name):
        raise AppleWebError("VM_NAME_INVALID")
    if vm_user != vm_name:
        raise AppleWebError("VM_USER_MISMATCH")
    try:
        address = ipaddress.ip_address(vm_ip)
    except ValueError as error:
        raise AppleWebError("VM_IP_INVALID") from error
    if address.version != 4:
        raise AppleWebError("VM_IP_INVALID")


def guest_stage_path(vm_user: str, stage: str) -> str:
    if not re.fullmatch(r"[a-z]{4}", vm_user):
        raise AppleWebError("VM_USER_INVALID")
    try:
        filename = GUEST_WRAPPERS[stage]
    except KeyError as error:
        raise AppleWebError("UTM_APPS_STAGE_INVALID") from error
    return f"{GUEST_DIR_TEMPLATE.format(vm_user=vm_user)}/{filename}"


guest_script_path = guest_stage_path


def guest_preflight_command(vm_user: str) -> str:
    base = GUEST_DIR_TEMPLATE.format(vm_user=vm_user)
    checks = [
        "set -euo pipefail",
        f"expected_user={shlex.quote(vm_user)}",
        f"base={shlex.quote(base)}",
        '[[ "$(/usr/bin/id -un)" == "$expected_user" ]]',
        '[[ "$HOME" == "/Users/$expected_user" ]]',
        '[[ -d "$base" && ! -L "$base" ]]',
    ]
    for index, filename in enumerate(GUEST_WRAPPER_FILENAMES):
        source = PROJECT_ROOT / "scripts" / filename
        if (
            not source.is_file()
            or source.is_symlink()
            or source.stat().st_size == 0
        ):
            raise AppleWebError("UTM_APPS_GUEST_SOURCE_INVALID")
        expected_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        path = f"{base}/{filename}"
        checks.extend(
            (
                f"target_{index}={shlex.quote(path)}",
                f'[[ -f "$target_{index}" && ! -L "$target_{index}" && '
                f'-O "$target_{index}" && -s "$target_{index}" && '
                f'-r "$target_{index}" ]]',
                f"test \"$(/usr/bin/shasum -a 256 \"$target_{index}\" | "
                f"/usr/bin/awk '{{print $1}}')\" = {shlex.quote(expected_sha256)}",
            )
        )
    module = PLAYWRIGHT_MODULE_TEMPLATE.format(vm_user=vm_user)
    node_checks = ["set -euo pipefail"]
    node_checks.extend(
        f"node --check {shlex.quote(f'{base}/{filename}')}"
        for filename in GUEST_WRAPPER_FILENAMES
    )
    node_checks.extend(
        (
            f"test -f {shlex.quote(module)}",
            shlex.join(
                (
                    "node",
                    "--input-type=module",
                    "-e",
                    "const m=await import(process.argv[1]);"
                    "if(!m.chromium||typeof m.chromium.connectOverCDP!=="
                    "'function')process.exit(2)",
                    module,
                )
            ),
        )
    )
    checks.append(shlex.join(("/bin/zsh", "-lic", "\n".join(node_checks))))
    return shlex.join(("/bin/zsh", "-lc", "\n".join(checks)))


guest_verification_command = guest_preflight_command


def guest_execution_command(
    vm_user: str, stage: str, *, adopt_current_session: bool = False
) -> str:
    target = guest_stage_path(vm_user, stage)
    base = str(Path(target).parent)
    stage_code = stage.upper().replace("-", "_")
    timeout_seconds = STAGE_TIMEOUT_SECONDS[stage]
    exact_process = f"^node {re.escape(target)}$"
    identity_probe = (
        "import { currentSessionIdentity } from "
        f"{json.dumps(f'file://{base}/session.mjs')};"
        "console.log(JSON.stringify(await currentSessionIdentity()))"
    )
    node_args = ["node", target]
    if stage == "utm-12" and adopt_current_session:
        node_args.append("adopt-current-session")
    node_stage = "\n".join(
        (
            "set -euo pipefail",
            f"/usr/bin/pkill -f {shlex.quote(exact_process)} 2>/dev/null || true",
            "set +e",
            shlex.join(("/usr/bin/perl", "-e", "alarm shift; exec @ARGV", str(timeout_seconds), *node_args)),
            "stage_status=$?",
            "set -e",
            f'if [[ "$stage_status" -eq 142 ]]; then print -u2 -- {shlex.quote(f"{stage_code}_ERROR=TIMEOUT_{timeout_seconds}_SECONDS")}; fi',
            'if [[ "$stage_status" -ne 0 ]]; then exit "$stage_status"; fi',
            shlex.join(("node", "--input-type=module", "-e", identity_probe)),
        )
    )
    script = "\n".join(
        (
            "set -euo pipefail",
            f"expected_user={shlex.quote(vm_user)}",
            f"base={shlex.quote(base)}",
            f"target={shlex.quote(target)}",
            '[[ "$(/usr/bin/id -un)" == "$expected_user" ]]',
            '[[ "$HOME" == "/Users/$expected_user" ]]',
            '[[ -d "$base" && ! -L "$base" ]]',
            '[[ -f "$target" && ! -L "$target" && -O "$target" && '
            '-r "$target" && -s "$target" ]]',
            f"exec /bin/zsh -lic {shlex.quote(node_stage)}",
        )
    )
    return shlex.join(("/bin/zsh", "-lc", script))


def guest_utm12_notion_fields_command(vm_user: str) -> str:
    target = guest_stage_path(vm_user, "utm-12")
    base = str(Path(target).parent)
    identity_probe = (
        "import { currentSessionIdentity } from "
        f"{json.dumps(f'file://{base}/session.mjs')};"
        "console.log(JSON.stringify(await currentSessionIdentity()))"
    )
    node_stage = "\n".join(
        (
            "set -euo pipefail",
            f"node {shlex.quote(target)} notion-fields",
            shlex.join(("node", "--input-type=module", "-e", identity_probe)),
        )
    )
    script = "\n".join(
        (
            "set -euo pipefail",
            f"expected_user={shlex.quote(vm_user)}",
            f"base={shlex.quote(base)}",
            f"target={shlex.quote(target)}",
            '[[ "$(/usr/bin/id -un)" == "$expected_user" ]]',
            '[[ "$HOME" == "/Users/$expected_user" ]]',
            '[[ -d "$base" && ! -L "$base" ]]',
            '[[ -f "$target" && ! -L "$target" && -O "$target" && '
            '-r "$target" && -s "$target" ]]',
            f"exec /bin/zsh -lic {shlex.quote(node_stage)}",
        )
    )
    return shlex.join(("/bin/zsh", "-lc", script))


def guest_utm12_notion_fields_preflight_command(vm_user: str) -> str:
    base = GUEST_DIR_TEMPLATE.format(vm_user=vm_user)
    session = f"{base}/session.mjs"
    target = f"{base}/utm_12_one.mjs"
    module = PLAYWRIGHT_MODULE_TEMPLATE.format(vm_user=vm_user)
    checks = "\n".join(
        (
            "set -euo pipefail",
            f"test -f {shlex.quote(session)}",
            f"test -f {shlex.quote(target)}",
            f"test -f {shlex.quote(module)}",
            f"node --check {shlex.quote(session)}",
            f"node --check {shlex.quote(target)}",
        )
    )
    return shlex.join(("/bin/zsh", "-lic", checks))


def _ssh_run(
    vm_user: str,
    vm_ip: str,
    remote_command: str,
    *,
    input_bytes: bytes,
    timeout: int,
    runner: Callable[..., Any],
    password_env: Mapping[str, str] | None,
) -> Any:
    return runner(
        ssh_args(vm_user, vm_ip, connect_timeout=8, tty=False) + [remote_command],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env=dict(password_env) if password_env is not None else password_environment(),
        cwd=PROJECT_ROOT,
    )


def verify_guest_scripts(
    vm_ip: str,
    vm_user: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    password_env: Mapping[str, str] | None = None,
) -> None:
    if runner is subprocess.run:
        try:
            sync_utm_apps_playwright_files(
                SHARED_DIR,
                vm_user=vm_user,
                vm_ip=vm_ip,
            )
        except Exception as error:
            raise AppleWebError("UTM_APPS_GUEST_SYNC_FAILED") from error
    verified = _ssh_run(
        vm_user,
        vm_ip,
        guest_preflight_command(vm_user),
        input_bytes=b"",
        timeout=60,
        runner=runner,
        password_env=password_env,
    )
    if verified.returncode != 0:
        raise AppleWebError("UTM_APPS_GUEST_PLAYWRIGHT_FILES_INVALID")


def verify_utm12_notion_fields_scripts(
    vm_ip: str,
    vm_user: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    password_env: Mapping[str, str] | None = None,
) -> None:
    verified = _ssh_run(
        vm_user,
        vm_ip,
        guest_utm12_notion_fields_preflight_command(vm_user),
        input_bytes=b"",
        timeout=10,
        runner=runner,
        password_env=password_env,
    )
    if verified.returncode != 0:
        raise AppleWebError("UTM_12_NOTION_FIELDS_GUEST_FILES_INVALID")


preflight_guest_scripts = verify_guest_scripts


def copy_review_screenshot(
    *,
    vm_ip: str,
    vm_user: str,
    remote_path: str,
    run_id: str,
    filename: str = "05-small-business.png",
    scp_runner: Callable[..., Any] = subprocess.run,
    password_env: Mapping[str, str] | None = None,
) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise AppleWebError("RUN_ID_INVALID")
    if filename != "05-small-business.png":
        raise AppleWebError("SCREENSHOT_FILENAME_INVALID")
    if remote_path != f"/Users/{vm_user}/Downloads/1.png":
        raise AppleWebError("SCREENSHOT_REMOTE_PATH_MISMATCH")
    target_dir = (PROJECT_ROOT / "runtime" / "review-screenshots" / run_id).resolve()
    screenshot_root = (PROJECT_ROOT / "runtime" / "review-screenshots").resolve()
    if target_dir.parent != screenshot_root:
        raise AppleWebError("SCREENSHOT_TARGET_ESCAPED")
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target_dir.chmod(0o700)
    target = target_dir / filename
    environment = (
        dict(password_env) if password_env is not None else password_environment()
    )
    with tempfile.NamedTemporaryFile(dir=target_dir, prefix=".05-", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        copied = scp_runner(
            [
                "/usr/bin/scp",
                "-q",
                *connection_options(8),
                f"{vm_user}@{vm_ip}:{remote_path}",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=environment,
        )
        if copied.returncode != 0:
            raise AppleWebError("SCREENSHOT_COPY_FAILED")
        image = temporary.read_bytes()
        if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) < 100:
            raise AppleWebError("SCREENSHOT_PNG_INVALID")
        temporary.chmod(0o600)
        os.replace(temporary, target)
        target.chmod(0o600)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _guest_failure_detail(stderr: bytes | str, marker: str) -> str:
    text = _decode(stderr)
    prefix = f"{marker}="
    lines = text.splitlines()
    details: list[str] = []
    for index, line in enumerate(lines):
        if not line.startswith(prefix):
            continue
        segment = [line.removeprefix(prefix)]
        for continuation in lines[index + 1 :]:
            if re.match(r"^[A-Z][A-Z0-9_]*=", continuation):
                break
            segment.append(continuation)
        details.append("\n".join(segment).rstrip())
    if details:
        return "\n".join(details)
    return text.rstrip("\r\n") or "UNKNOWN_GUEST_ERROR"


def _parse_playwright_guest_result(
    stage: str, stdout: bytes | str, vm_user: str
) -> GuestWorkflowResult:
    lines = [line for line in _decode(stdout).splitlines() if line.strip()]
    if len(lines) != 2:
        raise AppleWebError("PLAYWRIGHT_RESULT_LINE_COUNT_INVALID")
    try:
        result = json.loads(lines[0])
        identity = json.loads(lines[1])
    except (TypeError, ValueError) as error:
        raise AppleWebError("PLAYWRIGHT_RESULT_JSON_INVALID") from error
    if not isinstance(result, dict) or not isinstance(identity, dict):
        raise AppleWebError("PLAYWRIGHT_RESULT_SHAPE_INVALID")
    pid = identity.get("edge_pid")
    websocket = identity.get("edge_websocket")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(websocket, str)
        or not websocket.startswith("ws://127.0.0.1:9222/")
    ):
        raise AppleWebError("EDGE_SESSION_RESULT_INVALID")

    data: dict[str, Any] = dict(result)
    if stage == "utm-10":
        if result.get("UTM_10") != "verified":
            raise AppleWebError("UTM_APPS_STAGE_MARKERS_MISSING")
        markers = ("UTM_10=verified",)
        data["existing"] = result.get("EXISTING_ACCOUNT") is True
    elif stage == "utm-11":
        if (
            result.get("UTM_11") != "verified"
            or result.get("SMALL_BUSINESS_SUCCESS_MESSAGES") != "verified"
            or result.get("REVIEW_SCREENSHOT_05") != "verified"
        ):
            raise AppleWebError("UTM_APPS_STAGE_MARKERS_MISSING")
        markers = STAGE_MARKERS[stage]
        data["screenshot_path"] = f"/Users/{vm_user}/Downloads/1.png"
        data["existing"] = result.get("EXISTING_BUSINESS") is True
    elif stage == "utm-12":
        team_id = result.get("team_id")
        renewal_date = result.get("renewal_date")
        app_id = result.get("NUMERIC_APP_ID")
        if (
            result.get("UTM_12") != "verified"
            or not isinstance(team_id, str)
            or re.fullmatch(r"[A-Z0-9]{10}", team_id) is None
            or not isinstance(renewal_date, str)
            or not renewal_date.strip()
            or not isinstance(app_id, str)
            or not app_id.isdigit()
        ):
            raise AppleWebError("UTM_12_RESULT_INVALID")
        markers = STAGE_MARKERS[stage]
        data["renewal_date"] = renewal_date.strip()
        data["numeric_app_id"] = app_id
        data["existing"] = (
            result.get("EXISTING_IDENTIFIER") is True
            and result.get("EXISTING_APP") is True
        )
    elif stage == "utm-13":
        if result.get("UTM_13") != "verified":
            raise AppleWebError("UTM_APPS_STAGE_MARKERS_MISSING")
        markers = STAGE_MARKERS[stage]
        data["existing"] = (
            result.get("EXISTING_CERTIFICATE") is True
            and result.get("EXISTING_PROFILE") is True
        )
    else:
        raise AppleWebError("UTM_APPS_STAGE_INVALID")
    data.update({"edge_pid": pid, "edge_websocket": websocket})
    return GuestWorkflowResult(markers, data)


def _stage_stdin(stage: str, payload: Mapping[str, Any]) -> bytes:
    if stage == "utm-10":
        value = {
            "email": str(payload.get("APPLE_ACCOUNT_EMAIL") or ""),
            "password": str(payload.get("APPLE_ACCOUNT_PASSWORD") or ""),
            "phone": str(payload.get("APPLE_ACCOUNT_PHONE") or ""),
            "smsUrl": str(payload.get("APPLE_ACCOUNT_SMS_URL") or ""),
            "userName": str(payload.get("APPLE_ACCOUNT_USER_NAME") or ""),
        }
    elif stage == "utm-11":
        return b""
    elif stage == "utm-12":
        value = {
            "APP_NAME": str(payload.get("APP_NAME") or ""),
            "BUNDLE_ID": str(payload.get("BUNDLE_ID") or ""),
        }
        if payload.get("RESET_FROM_IDENTIFIERS") is True:
            value["RESET_FROM_IDENTIFIERS"] = True
    elif stage == "utm-13":
        value = {
            "authorizationAttemptId": "current-run",
            "systemKeychainPassword": guest_password(),
            "certificateUserName": str(payload.get("APPLE_ACCOUNT_USER_NAME") or ""),
        }
    else:
        raise AppleWebError("UTM_APPS_STAGE_INVALID")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def run_guest_stage(
    *,
    stage: str,
    vm_ip: str,
    vm_user: str,
    payload: Mapping[str, Any],
    vm_name: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    password_env: Mapping[str, str] | None = None,
    **_unused: Any,
) -> GuestWorkflowResult:
    if stage not in STAGE_MARKERS:
        raise AppleWebError("UTM_APPS_STAGE_INVALID")
    validate_target(vm_name or vm_user, vm_ip, vm_user)
    completed = _ssh_run(
        vm_user,
        vm_ip,
        guest_execution_command(
            vm_user,
            stage,
            adopt_current_session=(
                stage == "utm-12" and payload.get("ADOPT_CURRENT_SESSION") is True
            ),
        ),
        input_bytes=_stage_stdin(stage, payload),
        timeout=STAGE_TIMEOUT_SECONDS[stage] + 5,
        runner=runner,
        password_env=password_env,
    )
    if completed.returncode != 0:
        stage_code = stage.upper().replace("-", "_")
        reason = _guest_failure_detail(completed.stderr, f"{stage_code}_ERROR")
        raise AppleWebError(f"UTM_APPS_STAGE_FAILED={stage_code}:{reason}")
    return _parse_playwright_guest_result(stage, completed.stdout, vm_user)


def run_utm12_notion_fields(
    *,
    vm_ip: str,
    vm_user: str,
    vm_name: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    password_env: Mapping[str, str] | None = None,
) -> GuestWorkflowResult:
    validate_target(vm_name or vm_user, vm_ip, vm_user)
    completed = _ssh_run(
        vm_user,
        vm_ip,
        guest_utm12_notion_fields_command(vm_user),
        input_bytes=b"",
        timeout=30,
        runner=runner,
        password_env=password_env,
    )
    if completed.returncode != 0:
        reason = _guest_failure_detail(completed.stderr, "UTM_12_ERROR")
        raise AppleWebError(f"UTM_APPS_STAGE_FAILED=UTM_12_NOTION_FIELDS:{reason}")
    return _parse_playwright_guest_result("utm-12", completed.stdout, vm_user)


def _required_field(api: Any, page_title: str, section: str, label: str) -> str:
    value = api.read_field(page_title, section, label).strip()
    if not value:
        raise AppleWebError("NOTION_REQUIRED_FIELD_MISSING")
    return value


def _notion_payload(api: Any, page_title: str) -> dict[str, str]:
    password = api.read_field(page_title, "账号信息", "修改后的密码：").strip()
    if not password:
        password = _required_field(api, page_title, "账号信息", "初始密码：")
    payload = {
        "APPLE_ACCOUNT_EMAIL": _required_field(api, page_title, "账号信息", "邮箱："),
        "APPLE_ACCOUNT_PASSWORD": password,
        "APPLE_ACCOUNT_PHONE": _required_field(api, page_title, "账号信息", "电话："),
        "APPLE_ACCOUNT_USER_NAME": _required_field(api, page_title, "账号信息", "用户名："),
        "APPLE_ACCOUNT_SMS_URL": _required_field(
            api, page_title, "账号信息", "电话短信接收平台："
        ),
        "APP_NAME": _required_field(api, page_title, "应用信息", "应用名: "),
        "BUNDLE_ID": _required_field(api, page_title, "应用信息", "正式包名: "),
    }
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", payload["BUNDLE_ID"]):
        raise AppleWebError("NOTION_BUNDLE_ID_INVALID")
    sms_url = urlparse(payload["APPLE_ACCOUNT_SMS_URL"])
    if sms_url.scheme not in {"http", "https"} or not sms_url.netloc:
        raise AppleWebError("NOTION_SMS_URL_INVALID")
    return payload


def _registered_utm12_fields(api: Any, page_title: str) -> dict[str, str] | None:
    team_id = api.read_field(page_title, "账号信息", "team ID:").strip()
    renewal_date = api.read_field(page_title, "账号信息", "Renewal date：").strip()
    app_id = api.read_field(page_title, "账号信息", "APP_ID：").strip()
    if not re.fullmatch(r"[A-Z0-9]{10}", team_id):
        return None
    if not renewal_date or not app_id.isdigit():
        return None
    return {"team_id": team_id, "renewal_date": renewal_date, "numeric_app_id": app_id}


def _write_membership(
    api: Any,
    parent_title: str,
    page_title: str,
    data: Mapping[str, Any],
) -> tuple[str, str]:
    team_id = data.get("team_id")
    renewal_date = data.get("renewal_date")
    if not isinstance(team_id, str) or not re.fullmatch(r"[A-Z0-9]{10}", team_id):
        raise AppleWebError("TEAM_ID_RESULT_INVALID")
    if not isinstance(renewal_date, str) or not renewal_date.strip():
        raise AppleWebError("RENEWAL_DATE_RESULT_INVALID")
    renewal_date = renewal_date.strip()
    api.verify_parent(parent_title)
    before_team = api.read_field(page_title, "账号信息", "team ID:")
    before_renewal = api.read_field(page_title, "账号信息", "Renewal date：")
    try:
        api.set_field(page_title, "账号信息", "team ID:", team_id)
        api.verify_parent(parent_title)
        api.set_field(page_title, "账号信息", "Renewal date：", renewal_date)
        api.verify_parent(parent_title)
        saved_team = api.read_field(page_title, "账号信息", "team ID:").strip()
        saved_renewal = api.read_field(page_title, "账号信息", "Renewal date：").strip()
        if saved_team != team_id or saved_renewal != renewal_date:
            raise AppleWebError("NOTION_MEMBERSHIP_READBACK_MISMATCH")
    except Exception as original_error:
        try:
            api.verify_parent(parent_title)
            api.set_field(page_title, "账号信息", "team ID:", before_team, replace_existing=True)
            api.set_field(
                page_title,
                "账号信息",
                "Renewal date：",
                before_renewal,
                replace_existing=True,
            )
            if api.read_field(page_title, "账号信息", "team ID:") != before_team:
                raise AppleWebError("TEAM_ID_ROLLBACK_MISMATCH")
            if api.read_field(page_title, "账号信息", "Renewal date：") != before_renewal:
                raise AppleWebError("RENEWAL_DATE_ROLLBACK_MISMATCH")
        except Exception as rollback_error:
            raise AppleWebError("NOTION_MEMBERSHIP_ROLLBACK_FAILED") from rollback_error
        raise original_error
    return saved_team, saved_renewal


def _write_app_id(api: Any, parent_title: str, page_title: str, app_id: Any) -> str:
    if not isinstance(app_id, str) or not app_id.isdigit():
        raise AppleWebError("APP_ID_RESULT_INVALID")
    api.verify_parent(parent_title)
    before = api.read_field(page_title, "账号信息", "APP_ID：")
    if before == app_id:
        status = "equal"
    elif before:
        raise AppleWebError("APP_ID_NOTION_CONFLICT")
    else:
        try:
            api.set_field(page_title, "账号信息", "APP_ID：", app_id)
            api.verify_parent(parent_title)
            if api.read_field(page_title, "账号信息", "APP_ID：") != app_id:
                raise AppleWebError("APP_ID_NOTION_READBACK_MISMATCH")
        except Exception as original_error:
            try:
                api.verify_parent(parent_title)
                api.set_field(
                    page_title,
                    "账号信息",
                    "APP_ID：",
                    before,
                    replace_existing=True,
                )
                if api.read_field(page_title, "账号信息", "APP_ID：") != before:
                    raise AppleWebError("APP_ID_NOTION_ROLLBACK_MISMATCH")
            except Exception as rollback_error:
                raise AppleWebError("APP_ID_NOTION_ROLLBACK_FAILED") from rollback_error
            raise original_error
        status = "written"
    api.verify_parent(parent_title)
    if api.read_field(page_title, "账号信息", "APP_ID：") != app_id:
        raise AppleWebError("APP_ID_NOTION_READBACK_MISMATCH")
    return status


def _write_notion_fields_batch(
    api: Any,
    page_title: str,
    data: Mapping[str, Any],
) -> tuple[str, str, str]:
    team_id = data.get("team_id")
    renewal_date = data.get("renewal_date")
    app_id = data.get("numeric_app_id")
    if not isinstance(team_id, str) or not re.fullmatch(r"[A-Z0-9]{10}", team_id):
        raise AppleWebError("TEAM_ID_RESULT_INVALID")
    if not isinstance(renewal_date, str) or not renewal_date.strip():
        raise AppleWebError("RENEWAL_DATE_RESULT_INVALID")
    if not isinstance(app_id, str) or not app_id.isdigit():
        raise AppleWebError("APP_ID_RESULT_INVALID")
    renewal_date = renewal_date.strip()
    expected = {
        "team ID:": team_id,
        "Renewal date：": renewal_date,
        "APP_ID：": app_id,
    }

    def parse(text: str) -> tuple[list[str], dict[str, str]]:
        lines = text.splitlines()
        values: dict[str, str] = {}
        for label in expected:
            indexes = [index for index, line in enumerate(lines) if line.startswith(label)]
            if len(indexes) != 1:
                raise AppleWebError("NOTION_FIELD_LABEL_INVALID")
            values[label] = lines[indexes[0]][len(label):]
        return lines, values

    before = api.read_section(page_title, "账号信息")
    lines, current = parse(before)
    for label, value in expected.items():
        if current[label] and current[label] != value:
            raise AppleWebError("NOTION_FIELD_CONFLICT")
        index = next(index for index, line in enumerate(lines) if line.startswith(label))
        lines[index] = label + value
    updated = "\n".join(lines)
    changed = updated != before
    try:
        if changed:
            api.write_section(
                page_title,
                "账号信息",
                updated,
                replace_existing=True,
            )
        _, saved = parse(api.read_section(page_title, "账号信息"))
        if saved != expected:
            raise AppleWebError("NOTION_FIELDS_READBACK_MISMATCH")
    except Exception as original_error:
        if changed:
            try:
                api.write_section(
                    page_title,
                    "账号信息",
                    before,
                    replace_existing=True,
                )
                if api.read_section(page_title, "账号信息") != before:
                    raise AppleWebError("NOTION_FIELDS_ROLLBACK_MISMATCH")
            except Exception as rollback_error:
                raise AppleWebError("NOTION_FIELDS_ROLLBACK_FAILED") from rollback_error
        raise original_error
    return team_id, renewal_date, "written" if changed else "equal"


def _session_from_result(result: GuestWorkflowResult) -> tuple[int, str]:
    pid = result.data.get("edge_pid")
    websocket = result.data.get("edge_websocket")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(websocket, str)
        or not websocket.startswith("ws://127.0.0.1:9222/")
    ):
        raise AppleWebError("EDGE_SESSION_RESULT_INVALID")
    return pid, websocket


def _assert_same_session(result: GuestWorkflowResult, expected: tuple[int, str]) -> None:
    if _session_from_result(result) != expected:
        raise AppleWebError("EDGE_PROCESS_CHANGED")


def _run_context(run_id: str, vm_name: str) -> tuple[str, str, str]:
    run = find_run(run_id)
    if not isinstance(run, dict):
        raise AppleWebError("FEISHU_RUN_NOT_FOUND")
    if run.get("vm_name") != vm_name:
        raise AppleWebError("FEISHU_RUN_VM_MISMATCH")
    app_name = str(run.get("app_name") or "").strip()
    parent_title = str(load_config().submission_host_machine or "").strip()
    if not app_name or not parent_title or run_host_machine(run) != parent_title:
        raise AppleWebError("FEISHU_RUN_CONTEXT_MISMATCH")
    return parent_title, app_name, f"{app_name}-{vm_name}"


def _resolve_context(args: argparse.Namespace) -> tuple[bool, str, str, str, str]:
    supplied_run_id = getattr(args, "run_id", None)
    supplied_page_title = getattr(args, "page_title", None)
    direct_mode = not supplied_run_id
    if direct_mode:
        context = resolve_direct_context(
            page_title=str(supplied_page_title or ""),
            vm_name=args.vm_name,
            vm_ip=args.vm_ip,
            vm_user=args.vm_user,
        )
        return (
            True,
            context.context_id,
            context.parent_title,
            context.page_title,
            context.app_name,
        )
    if not RUN_ID_RE.fullmatch(str(supplied_run_id)):
        raise AppleWebError("RUN_ID_INVALID")
    parent_title, app_name, page_title = _run_context(str(supplied_run_id), args.vm_name)
    return False, str(supplied_run_id), parent_title, page_title, app_name


def run(
    args: argparse.Namespace,
    *,
    api: Any | None = None,
    runner: Callable[..., Any] = subprocess.run,
    scp_runner: Callable[..., Any] = subprocess.run,
    password_env: Mapping[str, str] | None = None,
) -> int:
    validate_target(args.vm_name, args.vm_ip, args.vm_user)
    notion = api if api is not None else api_from_env()
    direct_mode, context_id, parent_title, page_title, expected_app_name = _resolve_context(args)
    notion.verify_parent(parent_title)
    requested_stage = str(getattr(args, "stage", "all") or "all")
    if requested_stage == "notion-fields":
        _print_stage_start("utm-12")
        if page_title != f"{expected_app_name}-{args.vm_user}":
            raise AppleWebError("NOTION_PAGE_IDENTITY_MISMATCH")
        verify_utm12_notion_fields_scripts(
            vm_ip=args.vm_ip,
            vm_user=args.vm_user,
            runner=runner,
            password_env=password_env,
        )
        recovered = run_utm12_notion_fields(
            vm_name=args.vm_name,
            vm_ip=args.vm_ip,
            vm_user=args.vm_user,
            runner=runner,
            password_env=password_env,
        )
        team_id, renewal_date, app_id_status = _write_notion_fields_batch(
            notion,
            page_title,
            recovered.data,
        )
        _print_stage_result("utm-12", skipped_existing=app_id_status == "equal")
        return 0
    payload: dict[str, Any] = _notion_payload(notion, page_title)
    if page_title != f"{payload['APP_NAME']}-{args.vm_user}":
        raise AppleWebError("NOTION_PAGE_IDENTITY_MISMATCH")
    if payload["APP_NAME"] != expected_app_name:
        raise AppleWebError("NOTION_PAGE_APP_MISMATCH")
    payload.update({"RUN_ID": context_id, "VM_NAME": args.vm_name, "VM_USER": args.vm_user})
    verify_guest_scripts(
        vm_ip=args.vm_ip,
        vm_user=args.vm_user,
        runner=runner,
        password_env=password_env,
    )

    if requested_stage in {
        "utm-12",
        "utm-12-onward",
        "utm-12-identifiers-onward",
        "utm-12-adopt-current",
    }:
        _print_stage_start("utm-12")
        registered_utm12 = _registered_utm12_fields(notion, page_title)
        stage_payload = dict(payload)
        if requested_stage == "utm-12-identifiers-onward":
            stage_payload["RESET_FROM_IDENTIFIERS"] = True
        if requested_stage == "utm-12-adopt-current":
            stage_payload["ADOPT_CURRENT_SESSION"] = True
        stage_12 = run_guest_stage(
            stage="utm-12",
            vm_name=args.vm_name,
            vm_ip=args.vm_ip,
            vm_user=args.vm_user,
            payload=stage_payload,
            runner=runner,
            password_env=password_env,
        )
        if registered_utm12:
            team_id = registered_utm12["team_id"]
            renewal_date = registered_utm12["renewal_date"]
            numeric_app_id = registered_utm12["numeric_app_id"]
            if (
                team_id != stage_12.data["team_id"]
                or renewal_date != stage_12.data["renewal_date"]
                or numeric_app_id != stage_12.data["numeric_app_id"]
            ):
                raise AppleWebError("NOTION_UTM12_FIELDS_CONFLICT")
            app_id_status = "equal"
        else:
            team_id, renewal_date = _write_membership(
                notion, parent_title, page_title, stage_12.data
            )
            app_id_status = _write_app_id(
                notion, parent_title, page_title, stage_12.data.get("numeric_app_id")
            )
            numeric_app_id = stage_12.data["numeric_app_id"]
        _print_stage_result(
            "utm-12",
            skipped_existing=bool(stage_12.data.get("existing")) and app_id_status == "equal",
        )
        if requested_stage in {"utm-12", "utm-12-adopt-current"}:
            return 0
        edge_session = _session_from_result(stage_12)
        _print_stage_start("utm-13")
        stage_13 = run_guest_stage(
            stage="utm-13",
            vm_name=args.vm_name,
            vm_ip=args.vm_ip,
            vm_user=args.vm_user,
            payload=payload,
            runner=runner,
            password_env=password_env,
        )
        _assert_same_session(stage_13, edge_session)
        _print_stage_result("utm-13", skipped_existing=bool(stage_13.data.get("existing")))
        print("UTM Apps 已完成", flush=True)
        return 0
    if requested_stage == "utm-13":
        _print_stage_start("utm-13")
        stage_13 = run_guest_stage(
            stage="utm-13",
            vm_name=args.vm_name,
            vm_ip=args.vm_ip,
            vm_user=args.vm_user,
            payload=payload,
            runner=runner,
            password_env=password_env,
        )
        edge_session = _session_from_result(stage_13)
        _print_stage_result("utm-13", skipped_existing=bool(stage_13.data.get("existing")))
        return 0
    if requested_stage not in {"all", "utm-11-onward"}:
        raise AppleWebError("UTM_APPS_STAGE_SELECTOR_INVALID")

    edge_session: tuple[int, str] | None = None
    if requested_stage == "all":
        _print_stage_start("utm-10")
        stage_10 = run_guest_stage(
            stage="utm-10",
            vm_name=args.vm_name,
            vm_ip=args.vm_ip,
            vm_user=args.vm_user,
            payload=payload,
            runner=runner,
            password_env=password_env,
        )
        edge_session = _session_from_result(stage_10)
        _print_stage_result(
            "utm-10", skipped_existing=bool(stage_10.data.get("existing"))
        )
    _print_stage_start("utm-11")
    stage_11 = run_guest_stage(
        stage="utm-11",
        vm_name=args.vm_name,
        vm_ip=args.vm_ip,
        vm_user=args.vm_user,
        payload=payload,
        runner=runner,
        password_env=password_env,
    )
    if edge_session is None:
        edge_session = _session_from_result(stage_11)
    else:
        _assert_same_session(stage_11, edge_session)
    copy_review_screenshot(
        vm_ip=args.vm_ip,
        vm_user=args.vm_user,
        remote_path=str(stage_11.data.get("screenshot_path") or ""),
        run_id=context_id,
        scp_runner=scp_runner,
        password_env=password_env,
    )
    _print_stage_result("utm-11", skipped_existing=bool(stage_11.data.get("existing")))
    _print_stage_start("utm-12")
    stage_12 = run_guest_stage(
        stage="utm-12",
        vm_name=args.vm_name,
        vm_ip=args.vm_ip,
        vm_user=args.vm_user,
        payload=payload,
        runner=runner,
        password_env=password_env,
    )
    _assert_same_session(stage_12, edge_session)
    registered_utm12 = _registered_utm12_fields(notion, page_title)
    if registered_utm12:
        if (
            registered_utm12["team_id"] != stage_12.data["team_id"]
            or registered_utm12["renewal_date"] != stage_12.data["renewal_date"]
            or registered_utm12["numeric_app_id"] != stage_12.data["numeric_app_id"]
        ):
            raise AppleWebError("NOTION_UTM12_FIELDS_CONFLICT")
        team_id = registered_utm12["team_id"]
        renewal_date = registered_utm12["renewal_date"]
        app_id = registered_utm12["numeric_app_id"]
        app_id_status = "equal"
    else:
        team_id, renewal_date = _write_membership(
            notion, parent_title, page_title, stage_12.data
        )
        app_id = str(stage_12.data.get("numeric_app_id") or "")
        app_id_status = _write_app_id(notion, parent_title, page_title, app_id)
    _print_stage_result(
        "utm-12",
        skipped_existing=bool(stage_12.data.get("existing")) and app_id_status == "equal",
    )
    _print_stage_start("utm-13")
    stage_13 = run_guest_stage(
        stage="utm-13",
        vm_name=args.vm_name,
        vm_ip=args.vm_ip,
        vm_user=args.vm_user,
        payload=payload,
        runner=runner,
        password_env=password_env,
    )
    _assert_same_session(stage_13, edge_session)
    _print_stage_result("utm-13", skipped_existing=bool(stage_13.data.get("existing")))
    print("UTM Apps 已完成", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run UTM Apps Playwright stages")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--run-id")
    selector.add_argument("--page-title")
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-ip", required=True)
    parser.add_argument("--vm-user", required=True)
    parser.add_argument(
        "--stage",
        choices=(
            "all",
            "utm-11-onward",
            "utm-12",
            "utm-12-onward",
            "utm-12-identifiers-onward",
            "utm-12-adopt-current",
            "utm-13",
            "notion-fields",
        ),
        default="all",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return run_clean_cli(
        skill_name="utm-apps",
        success_marker="UTM_APPS=verified",
        operation=lambda: run(args),
        preserve_error_detail=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
