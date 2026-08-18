#!/usr/bin/env python3
"""Complete UTM-18 in one resumable host entry."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.notion_api import api_from_env  # noqa: E402
from scripts.clean_cli import run_clean_cli  # noqa: E402
from scripts.utm_18_attempt import (  # noqa: E402
    AttemptResult,
    UTM18AttemptError,
    archive_retryable_attempt,
    classify_attempt,
    create_attempt,
    inspect_remote,
    is_retryable_finished_attempt,
    load_single_attempt,
    mark_attempt,
    precommit_remote,
    run_remote,
)
from scripts.utm_18_edge import UTM18EdgeError, prepare_edge  # noqa: E402
from scripts.utm_18_ssh import build_invocation  # noqa: E402
from scripts.utm_direct_context import resolve_direct_context  # noqa: E402
from services.feishu_bot import find_run, load_config, run_host_machine  # noqa: E402


DEFAULT_ATTEMPTS_ROOT = PROJECT_ROOT / "runtime" / "utm-18-attempts"
APPLE_LOGIN_DIAGNOSTIC = PROJECT_ROOT / "runtime" / "utm-18-apple-login.stderr"
GUEST_HELPER_DIR = "/Users/{vm_user}/Downloads/AppleAccountScriptsBackup"
FILL_DESCRIPTION_WAIT_SECONDS = 3600
FILL_DESCRIPTION_POLL_SECONDS = 5

APPLE_LOGIN_BRIDGE = r'''import json,pathlib,sys
base=pathlib.Path(sys.argv[1])
required=(base/"apple_web_workflow.py",base/"edge_accessibility.py")
if not base.is_dir() or base.is_symlink() or not all(path.is_file() and not path.is_symlink() and path.stat().st_size>0 for path in required): raise SystemExit(61)
sys.path.insert(0,str(base))
payload=json.load(sys.stdin)
from apple_web_workflow import ACCOUNT_URL,_ensure_apple_login
from edge_accessibility import EdgeAXBackend,edge_session_identity
backend=EdgeAXBackend()
before=edge_session_identity(backend.pid)
backend.open_url(ACCOUNT_URL)
_ensure_apple_login(backend,payload)
after=edge_session_identity(backend.pid)
if before != after: raise SystemExit(62)
print(json.dumps({"markers":["APPLE_DEVELOPER_ACCOUNT=verified"],"edge_pid":after[0],"edge_websocket":after[1]}))
'''


class UTM18Error(RuntimeError):
    """Raised when the complete UTM-18 entry cannot safely continue."""


_SAFE_APPLE_LOGIN_MARKER = re.compile(
    rb"AppleWebWorkflowError: ([A-Z][A-Z0-9_]*(?:=[A-Z0-9_]+|=\d+)?)"
)


def _required_field(api: Any, page_title: str, label: str) -> str:
    value = api.read_field(page_title, "账号信息", label).strip()
    if not value:
        raise UTM18Error("NOTION_REQUIRED_FIELD_MISSING")
    return value


def _apple_payload(api: Any, page_title: str) -> dict[str, str]:
    password = api.read_field(
        page_title, "账号信息", "修改后的密码："
    ).strip()
    if not password:
        password = _required_field(api, page_title, "初始密码：")
    payload = {
        "APPLE_ACCOUNT_EMAIL": _required_field(api, page_title, "邮箱："),
        "APPLE_ACCOUNT_PASSWORD": password,
        "APPLE_ACCOUNT_PHONE": _required_field(api, page_title, "电话："),
        "APPLE_ACCOUNT_SMS_URL": _required_field(
            api, page_title, "电话短信接收平台："
        ),
    }
    sms = urlparse(payload["APPLE_ACCOUNT_SMS_URL"])
    if sms.scheme not in {"http", "https"} or not sms.netloc:
        raise UTM18Error("NOTION_SMS_URL_INVALID")
    return payload


def verify_or_login_apple(
    api: Any,
    *,
    parent_title: str,
    page_title: str,
    vm_name: str,
    vm_ip: str,
    vm_user: str,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    if vm_user != vm_name or not re.fullmatch(r"[a-z]{4}", vm_name):
        raise UTM18Error("VM_IDENTITY_INVALID")
    api.verify_parent(parent_title)
    payload = _apple_payload(api, page_title)
    base = GUEST_HELPER_DIR.format(vm_user=vm_user)
    remote = shlex.join(
        ("python3", "-B", "-c", APPLE_LOGIN_BRIDGE, base)
    )
    command, environment = build_invocation(
        vm_name,
        vm_ip,
        ["/bin/zsh", "-lc", f"exec {remote}"],
        connect_timeout=8,
    )
    completed = runner(
        command,
        input=json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
        timeout=300,
    )
    if completed.returncode != 0:
        APPLE_LOGIN_DIAGNOSTIC.parent.mkdir(parents=True, exist_ok=True)
        APPLE_LOGIN_DIAGNOSTIC.write_bytes(bytes(completed.stderr))
        APPLE_LOGIN_DIAGNOSTIC.chmod(0o600)
        marker = _SAFE_APPLE_LOGIN_MARKER.search(bytes(completed.stderr))
        suffix = f":{marker.group(1).decode('ascii')}" if marker else ""
        raise UTM18Error(f"APPLE_DEVELOPER_LOGIN_FAILED{suffix}")
    try:
        result = json.loads(bytes(completed.stdout).decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise UTM18Error("APPLE_DEVELOPER_RESULT_INVALID") from error
    if (
        not isinstance(result, dict)
        or result.get("markers") != ["APPLE_DEVELOPER_ACCOUNT=verified"]
        or not isinstance(result.get("edge_pid"), int)
        or not isinstance(result.get("edge_websocket"), str)
        or not result["edge_websocket"].startswith("ws://127.0.0.1:9222/")
    ):
        raise UTM18Error("APPLE_DEVELOPER_RESULT_INVALID")
    APPLE_LOGIN_DIAGNOSTIC.unlink(missing_ok=True)
    return result


def _classify_with_resident_rechecks(
    attempt: Any,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> AttemptResult:
    log, status, process_running = inspect_remote(attempt)
    result = classify_attempt(
        log,
        status,
        process_running=process_running,
        expected_attempt_id=attempt.attempt_id,
    )
    max_polls = FILL_DESCRIPTION_WAIT_SECONDS // FILL_DESCRIPTION_POLL_SECONDS
    for _ in range(max_polls):
        pending = (
            result.state == "ambiguous"
            and result.mode == "incomplete"
            and (
                process_running
                or any(
                    f"RUN_STATE={state}" in result.reason
                    for state in ("prepared", "launching", "running")
                )
            )
        )
        if not pending:
            break
        sleeper(FILL_DESCRIPTION_POLL_SECONDS)
        log, status, process_running = inspect_remote(attempt)
        result = classify_attempt(
            log,
            status,
            process_running=process_running,
            expected_attempt_id=attempt.attempt_id,
        )
    else:
        return AttemptResult(
            "ambiguous",
            "timeout",
            reason=(
                f"FILL_DESCRIPTION_TIMEOUT_SECONDS={FILL_DESCRIPTION_WAIT_SECONDS};"
                f"{result.reason}"
            ),
        )
    if result.mode == "completed_after_transport_stop":
        expected_count = result.product_count
        for delay in (5, 10):
            sleeper(delay)
            next_log, next_status, next_running = inspect_remote(attempt)
            next_result = classify_attempt(
                next_log,
                next_status,
                process_running=next_running,
                expected_attempt_id=attempt.attempt_id,
            )
            if (
                next_result.mode != "completed_after_transport_stop"
                or next_result.product_count != expected_count
            ):
                return AttemptResult(
                    "ambiguous", "transport_stop_recheck_mismatch"
                )
        return AttemptResult(
            "verified", "completed_after_transport_stop", expected_count
        )
    if result.mode != "resident_after_summary":
        return result
    for delay in (5, 10):
        sleeper(delay)
        next_log, next_status, next_running = inspect_remote(attempt)
        next_result = classify_attempt(
            next_log,
            next_status,
            process_running=next_running,
            expected_attempt_id=attempt.attempt_id,
        )
        if next_result.mode != "resident_after_summary":
            return AttemptResult("ambiguous", "resident_recheck_mismatch")
        log, status, result = next_log, next_status, next_result
    return result


def _run_context(run_id: str, vm_name: str) -> tuple[str, str, str]:
    run = find_run(run_id)
    if not isinstance(run, dict) or run.get("vm_name") != vm_name:
        raise UTM18Error("FEISHU_RUN_CONTEXT_MISMATCH")
    app_name = str(run.get("app_name") or "").strip()
    parent_title = str(load_config().submission_host_machine or "").strip()
    if not app_name or not parent_title or run_host_machine(run) != parent_title:
        raise UTM18Error("FEISHU_RUN_CONTEXT_MISMATCH")
    return parent_title, app_name, f"{app_name}-{vm_name}"


def _attempt_failure_message(result: AttemptResult) -> str:
    message = f"FILL_DESCRIPTION_BLOCKED={result.state}:{result.mode}"
    if result.reason:
        message = f"{message};{result.reason}"
    return message


def run(args: argparse.Namespace) -> int:
    if args.vm_user != args.vm_name:
        raise UTM18Error("VM_USER_MISMATCH")
    attempts_root = Path(args.attempts_root)
    supplied_run_id = getattr(args, "run_id", None)
    direct_mode = not supplied_run_id
    legacy_parent = str(getattr(args, "parent_title", "") or "").strip()
    legacy_page = str(getattr(args, "page_title", "") or "").strip()
    check_notion_app = True
    if direct_mode:
        context = resolve_direct_context(
            page_title=legacy_page,
            vm_name=args.vm_name,
            vm_ip=args.vm_ip,
            vm_user=args.vm_user,
        )
        context_id = context.context_id
        parent_title, page_title, app_name = (
            context.parent_title,
            context.page_title,
            context.app_name,
        )
    elif legacy_parent and legacy_page:
        context_id = str(supplied_run_id)
        parent_title, page_title = legacy_parent, legacy_page
        suffix = f"-{args.vm_name}"
        app_name = page_title[: -len(suffix)] if page_title.endswith(suffix) else ""
        check_notion_app = False
    else:
        context_id = str(supplied_run_id)
        parent_title, app_name, page_title = _run_context(context_id, args.vm_name)
    api = api_from_env()
    api.verify_parent(parent_title)
    if check_notion_app:
        notion_app = api.read_field(page_title, "应用信息", "应用名: ").strip()
        if notion_app != app_name:
            raise UTM18Error("NOTION_PAGE_APP_MISMATCH")

    attempt = load_single_attempt(attempts_root, context_id)
    if attempt is not None:
        if (
            attempt.vm_name != args.vm_name
            or attempt.vm_ip != args.vm_ip
            or attempt.run_id != context_id
        ):
            raise UTM18Error("ATTEMPT_CONTEXT_MISMATCH")
        if attempt.state == "verified":
            print("UTM_18_RESUME=verified")
            print("UTM_18=verified")
            return 0
        if attempt.state in {"running", "completion_observed"}:
            previous_log, previous_status, previous_running = inspect_remote(attempt)
            launch_not_started = (
                not previous_running
                and "RUN_STATE=prepared" in previous_status
            )
            if is_retryable_finished_attempt(
                attempt,
                log=previous_log,
                status=previous_status,
                process_running=previous_running,
            ):
                archive_retryable_attempt(
                    attempt,
                    log=previous_log,
                    status=previous_status,
                    process_running=previous_running,
                )
                attempt = None
            elif launch_not_started:
                attempt = mark_attempt(attempt, state="precommitted")

    if attempt is None:
        verify_or_login_apple(
            api,
            parent_title=parent_title,
            page_title=page_title,
            vm_name=args.vm_name,
            vm_ip=args.vm_ip,
            vm_user=args.vm_user,
        )
        attempt = create_attempt(
            attempts_root,
            run_id=context_id,
            vm_name=args.vm_name,
            vm_ip=args.vm_ip,
        )

    if attempt.state == "prepared":
        precommit_remote(attempt)
        print("UTM_18_LOG_PATH=precommitted")
        attempt = mark_attempt(attempt, state="precommitted")

    if attempt.state == "precommitted":
        attempt = mark_attempt(attempt, state="running")
        ssh_exit = run_remote(attempt)
        print(f"SSH_EXIT={ssh_exit}")

    if attempt.state not in {"running", "completion_observed"}:
        raise UTM18Error(f"ATTEMPT_STATE_INVALID={attempt.state}")

    result = _classify_with_resident_rechecks(attempt)
    if result.state != "verified" or result.product_count is None:
        raise UTM18Error(_attempt_failure_message(result))
    attempt = mark_attempt(attempt, state="verified", mode=result.mode)
    print("UTM_18_ATTEMPT_ID=verified")
    print("UTM_18_LOG=verified")
    print(f"SUMMARY_PRODUCT_COUNT={result.product_count}")
    print(f"FILL_DESCRIPTION_PROCESS={result.mode}")
    print("FILL_DESCRIPTION=verified")
    if direct_mode:
        print("UTM_18_DIRECT_CONTEXT=verified")
    print("UTM_18=verified")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Complete UTM-18 in one entry")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--run-id")
    selector.add_argument("--page-title")
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-ip", required=True)
    parser.add_argument("--vm-user", required=True)
    parser.add_argument(
        "--attempts-root",
        type=Path,
        default=DEFAULT_ATTEMPTS_ROOT,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    def operation() -> int:
        started_at = time.monotonic()
        status = run(args)
        print("UTM-18 应用描述步骤已完成")
        print(f"UTM_18_ELAPSED_SECONDS={round(time.monotonic() - started_at)}")
        return status

    return run_clean_cli(
        skill_name="utm-script",
        success_marker="UTM_18=verified",
        operation=operation,
        preserve_error_detail=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
