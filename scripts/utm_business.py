#!/usr/bin/env python3
"""Run UTM Business through the existing guest Edge Playwright session."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.notion_api import api_from_env
from scripts.clean_cli import run_clean_cli
from scripts.ssh_password import password_environment, ssh_args
from scripts.utm_business_delivery import sync_business_guest_files
from scripts.utm_direct_context import resolve_direct_context
from services.feishu_bot import find_run, load_config, run_host_machine
from services.project_paths import SHARED_DIR

GUEST_DIR_TEMPLATE = "/Users/{vm_user}/Downloads/AppleAccountScriptsBackup"
GUEST_FILES = (
    "session.mjs",
    "utm_business_one.mjs",
)
PLAYWRIGHT_MODULE_TEMPLATE = (
    "/Users/{vm_user}/Downloads/Fire_One_en1.3/"
    "node_modules/playwright-core/index.mjs"
)
RUN_ID_RE = re.compile(r"[A-Za-z0-9-]{8,80}")
PROGRESS_PREFIX = "UTM_BUSINESS_PROGRESS="
REQUIRED_MARKERS = (
    "EDGE_EXISTING_PID=verified",
    "BUSINESS_RESULT=verified",
    "DSA_COMPLIANCE=verified",
    "PAID_APPS_AGREEMENT=accepted_or_existing",
    "US_TAX_QUESTIONNAIRE=No_No_saved",
    "FOREIGN_STATUS_FORM=verified",
    "W8BEN_SUBMIT=verified",
    "BANK_ACCOUNT_PROCESSING=verified",
    "DAC7_READBACK=No_saved",
    "UTM_BUSINESS=verified",
)


class UTMBusinessError(RuntimeError):
    """Raised when UTM Business cannot be verified safely."""


@dataclass(frozen=True)
class GuestBusinessResult:
    markers: tuple[str, ...]
    data: Mapping[str, Any]


def validate_target(vm_name: str, vm_ip: str, vm_user: str) -> None:
    if not re.fullmatch(r"[a-z]{4}", vm_name):
        raise UTMBusinessError("VM_NAME_INVALID")
    if vm_user != vm_name:
        raise UTMBusinessError("VM_USER_MISMATCH")
    try:
        address = ipaddress.ip_address(vm_ip)
    except ValueError as error:
        raise UTMBusinessError("VM_IP_INVALID") from error
    if address.version != 4:
        raise UTMBusinessError("VM_IP_INVALID")


def _required_field(api: Any, page_title: str, section: str, label: str) -> str:
    value = api.read_field(page_title, section, label).strip()
    if not value:
        raise UTMBusinessError("NOTION_REQUIRED_FIELD_MISSING")
    return value


def _normalize_birthday(value: str) -> str:
    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", value.strip())
    if not match:
        raise UTMBusinessError("NOTION_BIRTHDAY_INVALID")
    try:
        parsed = date(*(int(match.group(index)) for index in (1, 2, 3)))
    except ValueError as error:
        raise UTMBusinessError("NOTION_BIRTHDAY_INVALID") from error
    return parsed.isoformat()


def notion_payload(
    api: Any,
    *,
    parent_title: str,
    page_title: str,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    api.verify_parent(parent_title)
    app_name = _required_field(api, page_title, "应用信息", "应用名: ")
    bundle_id = _required_field(api, page_title, "应用信息", "正式包名: ")
    birthday = _normalize_birthday(
        _required_field(api, page_title, "账号信息", "生日（格式年/月/日）：")
    )
    phone = _required_field(api, page_title, "账号信息", "电话：")
    sms_url = _required_field(api, page_title, "账号信息", "电话短信接收平台：")
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", bundle_id):
        raise UTMBusinessError("NOTION_BUNDLE_ID_INVALID")
    parsed_sms = urlparse(sms_url)
    if parsed_sms.scheme not in {"http", "https"} or not parsed_sms.netloc:
        raise UTMBusinessError("NOTION_SMS_URL_INVALID")

    routing_number = ""
    account_number = ""
    for index, wait_seconds in enumerate((0, 5, 10)):
        if wait_seconds:
            sleeper(wait_seconds)
        api.verify_parent(parent_title)
        first = (
            api.read_field(page_title, "账号信息", "ABA Routing Number：").strip(),
            api.read_field(page_title, "账号信息", "Account Number：").strip(),
        )
        second = (
            api.read_field(page_title, "账号信息", "ABA Routing Number：").strip(),
            api.read_field(page_title, "账号信息", "Account Number：").strip(),
        )
        if first != second:
            if index == 2:
                raise UTMBusinessError("NOTION_BANK_INFO_UNSTABLE")
            continue
        routing_number, account_number = first
        if routing_number and account_number:
            break
    routing_number = re.sub(r"\s+", "", routing_number)
    account_number = re.sub(r"\s+", "", account_number)
    if not routing_number or not account_number:
        raise UTMBusinessError("NOTION_BANK_INFO_MISSING")
    if not re.fullmatch(r"\d{9}", routing_number):
        raise UTMBusinessError("NOTION_ROUTING_NUMBER_INVALID")
    if not re.fullmatch(r"\d{4,17}", account_number):
        raise UTMBusinessError("NOTION_ACCOUNT_NUMBER_INVALID")
    return {
        "APP_NAME": app_name,
        "BUNDLE_ID": bundle_id,
        "BIRTHDAY": birthday,
        "APPLE_ACCOUNT_PHONE": phone,
        "APPLE_ACCOUNT_SMS_URL": sms_url,
        "ABA_ROUTING_NUMBER": routing_number,
        "ACCOUNT_NUMBER": account_number,
    }


def guest_preflight_command(vm_user: str, context_id: str) -> str:
    if not RUN_ID_RE.fullmatch(context_id):
        raise UTMBusinessError("CONTEXT_ID_INVALID")
    base = GUEST_DIR_TEMPLATE.format(vm_user=vm_user)
    module = PLAYWRIGHT_MODULE_TEMPLATE.format(vm_user=vm_user)
    checks = [
        "set -euo pipefail",
        f"expected_user={shlex.quote(vm_user)}",
        '[[ "$(/usr/bin/id -un)" == "$expected_user" ]]',
        '[[ "$HOME" == "/Users/$expected_user" ]]',
        f"base={shlex.quote(base)}",
        '[[ -d "$base" && ! -L "$base" ]]',
    ]
    for filename in GUEST_FILES:
        path = f"{base}/{filename}"
        checks.append(
            f"[[ -f {shlex.quote(path)} && ! -L {shlex.quote(path)} "
            f"&& -O {shlex.quote(path)} && -s {shlex.quote(path)} ]]"
        )
        checks.append(f"node --check {shlex.quote(path)}")
    checks.append(f"[[ -f {shlex.quote(module)} ]]")
    identity_probe = (
        "const s=await import(process.argv[1]);"
        "console.log(JSON.stringify(await s.currentSessionIdentity()))"
    )
    checks.append(
        shlex.join(
            (
                "node",
                "--input-type=module",
                "-e",
                identity_probe,
                f"file://{base}/session.mjs",
            )
        )
    )
    return shlex.join(("/bin/zsh", "-lic", "\n".join(checks)))


def guest_execution_command(vm_user: str) -> str:
    base = GUEST_DIR_TEMPLATE.format(vm_user=vm_user)
    target = f"{base}/utm_business_one.mjs"
    script = "\n".join(
        (
            "set -euo pipefail",
            f"expected_user={shlex.quote(vm_user)}",
            '[[ "$(/usr/bin/id -un)" == "$expected_user" ]]',
            '[[ "$HOME" == "/Users/$expected_user" ]]',
            f"target={shlex.quote(target)}",
            '[[ -f "$target" && ! -L "$target" && -O "$target" && -s "$target" ]]',
            'exec node "$target"',
        )
    )
    return shlex.join(("/bin/zsh", "-lic", script))


def _ssh_run(
    vm_user: str,
    vm_ip: str,
    remote_command: str,
    *,
    input_bytes: bytes,
    timeout: int,
    runner: Callable[..., Any],
    password_env: Mapping[str, str] | None,
    stream_progress: bool = False,
) -> Any:
    command = ssh_args(vm_user, vm_ip, connect_timeout=8, tty=False) + [
        remote_command
    ]
    environment = (
        dict(password_env) if password_env is not None else password_environment()
    )
    if not stream_progress or runner is not subprocess.run:
        return runner(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=environment,
            cwd=PROJECT_ROOT,
        )

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        cwd=PROJECT_ROOT,
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    progress_lines: list[str] = []

    def read_stdout() -> None:
        assert process.stdout is not None
        stdout_chunks.append(process.stdout.read())

    def read_stderr() -> None:
        assert process.stderr is not None
        for raw_line in iter(process.stderr.readline, b""):
            stderr_chunks.append(raw_line)
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith(PROGRESS_PREFIX):
                message = line.removeprefix(PROGRESS_PREFIX)
                progress_lines.append(message)
                print(message, flush=True)

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    assert process.stdin is not None
    try:
        process.stdin.write(input_bytes)
        process.stdin.close()
    except BrokenPipeError:
        pass
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        stdout_thread.join()
        stderr_thread.join()
    completed = subprocess.CompletedProcess(
        command,
        returncode,
        stdout=b"".join(stdout_chunks),
        stderr=b"".join(stderr_chunks),
    )
    completed.progress_lines = tuple(progress_lines)  # type: ignore[attr-defined]
    return completed


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _read_guest_session(
    vm_ip: str,
    vm_user: str,
    context_id: str,
    *,
    runner: Callable[..., Any],
    password_env: Mapping[str, str] | None,
) -> tuple[int, str]:
    completed = _ssh_run(
        vm_user,
        vm_ip,
        guest_preflight_command(vm_user, context_id),
        input_bytes=b"",
        timeout=30,
        runner=runner,
        password_env=password_env,
    )
    if completed.returncode != 0:
        raise UTMBusinessError("UTM_BUSINESS_PLAYWRIGHT_PREFLIGHT_INVALID")
    try:
        value = json.loads(_decode(completed.stdout).strip())
    except (TypeError, ValueError) as error:
        raise UTMBusinessError("UTM_BUSINESS_EDGE_SESSION_INVALID") from error
    pid = value.get("edge_pid")
    websocket = value.get("edge_websocket")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(websocket, str)
        or not websocket.startswith("ws://127.0.0.1:9222/")
    ):
        raise UTMBusinessError("UTM_BUSINESS_EDGE_SESSION_INVALID")
    return pid, websocket


def parse_guest_result(stdout: bytes | str) -> GuestBusinessResult:
    lines = [line for line in _decode(stdout).splitlines() if line.strip()]
    if len(lines) != 1:
        raise UTMBusinessError("GUEST_RESULT_LINE_COUNT_INVALID")
    try:
        payload = json.loads(lines[0])
    except (TypeError, ValueError) as error:
        raise UTMBusinessError("GUEST_RESULT_JSON_INVALID") from error
    if not isinstance(payload, dict):
        raise UTMBusinessError("GUEST_RESULT_SHAPE_INVALID")
    markers = tuple(
        f"{key}={payload.get(key)}"
        for key in (
            "EDGE_EXISTING_PID",
            "BUSINESS_RESULT",
            "DSA_COMPLIANCE",
            "PAID_APPS_AGREEMENT",
            "US_TAX_QUESTIONNAIRE",
            "FOREIGN_STATUS_FORM",
            "W8BEN_SUBMIT",
            "BANK_ACCOUNT_PROCESSING",
            "DAC7_READBACK",
            "UTM_BUSINESS",
        )
    )
    if markers != REQUIRED_MARKERS:
        raise UTMBusinessError("UTM_BUSINESS_MARKERS_INCOMPLETE")
    for status_key in (
        "DSA_STATUS",
        "LEGAL_ENTITY_STATUS",
        "PAID_APPS_STATUS",
        "TAX_QUESTIONNAIRE_STATUS",
        "FOREIGN_STATUS_FORM_STATUS",
        "W8BEN_STATUS",
        "BANK_ACCOUNT_STATUS",
        "DAC7_STATUS",
    ):
        if payload.get(status_key) not in {"completed", "existing"}:
            raise UTMBusinessError(f"{status_key}_INVALID")
    return GuestBusinessResult(markers, payload)


def parse_bank_guest_result(stdout: bytes | str) -> GuestBusinessResult:
    lines = [line for line in _decode(stdout).splitlines() if line.strip()]
    if len(lines) != 1:
        raise UTMBusinessError("GUEST_RESULT_LINE_COUNT_INVALID")
    try:
        payload = json.loads(lines[0])
    except (TypeError, ValueError) as error:
        raise UTMBusinessError("GUEST_RESULT_JSON_INVALID") from error
    if not isinstance(payload, dict):
        raise UTMBusinessError("GUEST_RESULT_SHAPE_INVALID")
    if (
        payload.get("EDGE_EXISTING_PID") != "verified"
        or payload.get("UTM_BUSINESS_BANK_STAGE") != "verified"
        or payload.get("BANK_ACCOUNT_STATUS") not in {"completed", "existing"}
    ):
        raise UTMBusinessError("UTM_BUSINESS_BANK_MARKERS_INCOMPLETE")
    return GuestBusinessResult(("UTM_BUSINESS_BANK_STAGE=verified",), payload)


def assert_same_edge_session(
    result: GuestBusinessResult, expected: tuple[int, str]
) -> None:
    if (
        result.data.get("EDGE_PID"),
        result.data.get("EDGE_WEBSOCKET"),
    ) != expected:
        raise UTMBusinessError("EDGE_PROCESS_CHANGED")


def write_business_section(
    api: Any,
    *,
    parent_title: str,
    page_title: str,
    business_text: str,
) -> str:
    if not business_text.strip() or "\x00" in business_text:
        raise UTMBusinessError("BUSINESS_TEXT_INVALID")
    api.verify_parent(parent_title)
    before = api.read_section(page_title, "商务")
    if before == business_text:
        api.verify_parent(parent_title)
        if api.read_section(page_title, "商务") != business_text:
            raise UTMBusinessError("NOTION_BUSINESS_EQUAL_READBACK_MISMATCH")
        return "verified_equal"
    if before.strip():
        raise UTMBusinessError("NOTION_BUSINESS_CONFLICT")
    try:
        api.write_section(page_title, "商务", business_text)
        api.verify_parent(parent_title)
        if api.read_section(page_title, "商务") != business_text:
            raise UTMBusinessError("NOTION_BUSINESS_WRITE_READBACK_MISMATCH")
    except Exception as original_error:
        try:
            api.verify_parent(parent_title)
            api.write_section(page_title, "商务", before, replace_existing=True)
            api.verify_parent(parent_title)
            if api.read_section(page_title, "商务") != before:
                raise UTMBusinessError("NOTION_BUSINESS_ROLLBACK_MISMATCH")
        except Exception as rollback_error:
            raise UTMBusinessError("NOTION_BUSINESS_ROLLBACK_FAILED") from rollback_error
        raise original_error
    return "written"


def _run_context(run_id: str, vm_name: str) -> tuple[str, str]:
    run = find_run(run_id)
    if not isinstance(run, dict):
        raise UTMBusinessError("FEISHU_RUN_NOT_FOUND")
    if run.get("vm_name") != vm_name:
        raise UTMBusinessError("FEISHU_RUN_VM_MISMATCH")
    app_name = str(run.get("app_name") or "").strip()
    parent_title = str(load_config().submission_host_machine or "").strip()
    if not app_name or not parent_title or run_host_machine(run) != parent_title:
        raise UTMBusinessError("FEISHU_RUN_CONTEXT_MISMATCH")
    return parent_title, app_name


def run(
    args: argparse.Namespace,
    *,
    api: Any | None = None,
    runner: Callable[..., Any] = subprocess.run,
    password_env: Mapping[str, str] | None = None,
) -> int:
    started = time.monotonic()
    stage = getattr(args, "stage", "all")
    validate_target(args.vm_name, args.vm_ip, args.vm_user)
    direct_mode = not getattr(args, "run_id", None)
    if direct_mode:
        context = resolve_direct_context(
            page_title=str(getattr(args, "page_title", "") or ""),
            vm_name=args.vm_name,
            vm_ip=args.vm_ip,
            vm_user=args.vm_user,
        )
        context_id = context.context_id
        parent_title = context.parent_title
        page_title = context.page_title
        expected_app_name = context.app_name
    else:
        context_id = str(args.run_id)
        if not RUN_ID_RE.fullmatch(context_id):
            raise UTMBusinessError("RUN_ID_INVALID")
        parent_title, expected_app_name = _run_context(context_id, args.vm_name)
        page_title = f"{expected_app_name}-{args.vm_user}"

    notion = api if api is not None else api_from_env()
    payload = notion_payload(notion, parent_title=parent_title, page_title=page_title)
    if payload["APP_NAME"] != expected_app_name:
        raise UTMBusinessError("NOTION_APP_MISMATCH")
    if runner is subprocess.run:
        try:
            sync_business_guest_files(
                SHARED_DIR,
                vm_user=args.vm_user,
                vm_ip=args.vm_ip,
            )
        except Exception as error:
            raise UTMBusinessError("UTM_BUSINESS_GUEST_SYNC_FAILED") from error
    expected_session = _read_guest_session(
        args.vm_ip,
        args.vm_user,
        context_id,
        runner=runner,
        password_env=password_env,
    )
    payload.update(
        {
            "CONTEXT_ID": context_id,
            "VM_NAME": args.vm_name,
            "EXPECTED_EDGE_PID": expected_session[0],
            "EXPECTED_EDGE_WEBSOCKET": expected_session[1],
            "STAGE": stage,
        }
    )
    completed = _ssh_run(
        args.vm_user,
        args.vm_ip,
        guest_execution_command(args.vm_user),
        input_bytes=json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"),
        timeout=3600,
        runner=runner,
        password_env=password_env,
        stream_progress=True,
    )
    for key in (
        "BIRTHDAY",
        "APPLE_ACCOUNT_PHONE",
        "APPLE_ACCOUNT_SMS_URL",
        "ABA_ROUTING_NUMBER",
        "ACCOUNT_NUMBER",
    ):
        payload[key] = ""
    if completed.returncode != 0:
        guest_error = " ".join(
            line.strip()
            for line in _decode(completed.stderr).splitlines()
            if line.strip() and not line.startswith(PROGRESS_PREFIX)
        )
        if guest_error.startswith("UTM_BUSINESS_ERROR="):
            guest_error = guest_error.removeprefix("UTM_BUSINESS_ERROR=").strip()
        if not guest_error:
            guest_error = "UNKNOWN_GUEST_ERROR"
        raise UTMBusinessError(
            f"UTM_BUSINESS_PLAYWRIGHT_STAGE_FAILED:{guest_error}"
        )
    if stage == "bank":
        result = parse_bank_guest_result(completed.stdout)
        assert_same_edge_session(result, expected_session)
        status = result.data.get("BANK_ACCOUNT_STATUS")
        if status == "existing":
            print("UTM-Business 银行账户步骤检查已存在，跳过", flush=True)
        else:
            print("UTM-Business 银行账户步骤已完成", flush=True)
        print("UTM_BUSINESS_BANK_STAGE=verified", flush=True)
        print(
            f"UTM_BUSINESS_ELAPSED_SECONDS={int(time.monotonic() - started)}",
            flush=True,
        )
        return 0
    result = parse_guest_result(completed.stdout)
    assert_same_edge_session(result, expected_session)
    business_text = result.data.get("BUSINESS_TEXT")
    if not isinstance(business_text, str):
        raise UTMBusinessError("BUSINESS_TEXT_RESULT_INVALID")
    notion_status = write_business_section(
        notion,
        parent_title=parent_title,
        page_title=page_title,
        business_text=business_text,
    )
    def report_step(label: str, status_key: str) -> None:
        status = result.data.get(status_key)
        if status == "existing":
            print(f"{label}检查已存在，跳过", flush=True)
        elif status == "completed":
            print(f"{label}已完成", flush=True)
        else:
            raise UTMBusinessError(f"{status_key}_INVALID")

    if not getattr(completed, "progress_lines", ()):
        report_step("UTM-Business DSA步骤", "DSA_STATUS")
        report_step("UTM-Business Legal Entity步骤", "LEGAL_ENTITY_STATUS")
        report_step("UTM-Business Paid Apps Agreement步骤", "PAID_APPS_STATUS")
        report_step(
            "UTM-Business U.S. Tax Questionnaire步骤",
            "TAX_QUESTIONNAIRE_STATUS",
        )
        report_step(
            "UTM-Business Foreign Status步骤", "FOREIGN_STATUS_FORM_STATUS"
        )
        report_step("UTM-Business W-8BEN步骤", "W8BEN_STATUS")
        report_step("UTM-Business 银行账户步骤", "BANK_ACCOUNT_STATUS")
        report_step("UTM-Business DAC7步骤", "DAC7_STATUS")
    if notion_status == "verified_equal":
        print("UTM-Business Notion商务登记步骤检查已存在，跳过", flush=True)
    else:
        print("UTM-Business Notion商务登记步骤已完成", flush=True)
    print(f"UTM_BUSINESS_APP_NAME={payload['APP_NAME']}", flush=True)
    print(f"UTM_BUSINESS_VM_NAME={args.vm_name}", flush=True)
    print(f"UTM_BUSINESS_EDGE_PID={expected_session[0]}", flush=True)
    print(
        "UTM_BUSINESS_NOTION="
        + ("verified_equal" if notion_status == "verified_equal" else "written"),
        flush=True,
    )
    print(
        f"UTM_BUSINESS_ELAPSED_SECONDS={int(time.monotonic() - started)}",
        flush=True,
    )
    print("UTM_BUSINESS=verified", flush=True)
    print("UTM_ENV_HANDOFF=ready", flush=True)
    print("UTM-Business App Store Connect Business步骤已完成", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run UTM Business with Playwright")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--run-id")
    selector.add_argument("--page-title")
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-ip", required=True)
    parser.add_argument("--vm-user", required=True)
    parser.add_argument("--stage", choices=("all", "bank"), default="all")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return run_clean_cli(
        skill_name="utm-business",
        success_marker="UTM_BUSINESS=verified",
        operation=lambda: run(args),
        preserve_error_detail=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
