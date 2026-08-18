#!/usr/bin/env python3
"""Clone the UTM-21 Codeup repository without exposing credentials or URL in argv."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit


PROJECT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_SOURCE_ROOT))

from scripts.notion_api import api_from_env
from scripts.ssh_password import (  # noqa: E402
    password_environment,
    ssh_args as configured_guest_password_ssh_args,
)
from scripts.utm_direct_context import resolve_direct_context
from services.project_paths import PROJECT_ROOT


RUNS_FILE = PROJECT_ROOT / "runtime" / "feishu-runs.json"
DIRECT_RUN_RE = re.compile(r"direct-v1-[a-z]{4}-[0-9a-f]{24}")
REMOTE_CLONE_SCRIPT = r'''
set +x
setopt NO_XTRACE
IFS= read -r -d '' CODEUP_USERNAME || exit 81
IFS= read -r -d '' CODEUP_PASSWORD || exit 82
IFS= read -r -d '' CODE_LINK || exit 83
IFS= read -r -d '' REPO_PATH || exit 84
test -n "$CODEUP_USERNAME" && test -n "$CODEUP_PASSWORD" || exit 85
test -n "$CODE_LINK" && test -n "$REPO_PATH" || exit 86
expected_user="${REPO_PATH#/Users/}"
expected_user="${expected_user%%/*}"
test "$(id -un)" = "$expected_user" || exit 87
test "$REPO_PATH" = "/Users/$expected_user/StudioProjects/${REPO_PATH:t}" || exit 88
test ! -e "$REPO_PATH" || exit 90
/bin/mkdir -p "${REPO_PATH:h}" || exit 89
export CODEUP_USERNAME CODEUP_PASSWORD GIT_TERMINAL_PROMPT=0
git -c credential.helper= \
  -c 'credential.helper=!f() { test "$1" = get && printf "username=%s\npassword=%s\n" "$CODEUP_USERNAME" "$CODEUP_PASSWORD"; }; f' \
  clone --branch main --single-branch -- "$CODE_LINK" "$REPO_PATH"
clone_rc=$?
if test "$clone_rc" -eq 0; then
  cd "$REPO_PATH" || clone_rc=91
fi
if test "$clone_rc" -eq 0; then
  origin_url="$(git remote get-url origin)" || clone_rc=92
  test "$origin_url" = "$CODE_LINK" || clone_rc=93
fi
if test "$clone_rc" -eq 0; then
  test "$(git branch --show-current)" = main || clone_rc=94
  test "$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')" = origin/main || clone_rc=95
fi
if test "$clone_rc" -eq 0; then
  local_head="$(git rev-parse HEAD)" || clone_rc=96
  tracking_head="$(git rev-parse refs/remotes/origin/main)" || clone_rc=97
  remote_head="$(git -c credential.helper= \
    -c 'credential.helper=!f() { test "$1" = get && printf "username=%s\npassword=%s\n" "$CODEUP_USERNAME" "$CODEUP_PASSWORD"; }; f' \
    ls-remote --exit-code "$CODE_LINK" refs/heads/main | /usr/bin/awk 'NR == 1 { print $1 }')" || clone_rc=98
  test "$local_head" = "$tracking_head" && test "$local_head" = "$remote_head" || clone_rc=99
fi
if test "$clone_rc" -eq 0; then
  test -z "$(git status --porcelain)" || clone_rc=100
  git fsck --full >/dev/null || clone_rc=101
fi
unset CODEUP_USERNAME CODEUP_PASSWORD CODE_LINK REPO_PATH
printf 'CLONE_EXIT=%s\n' "$clone_rc"
test "$clone_rc" -ne 0 || printf 'CLONE_VERIFY=verified\n'
exit "$clone_rc"
'''.strip()


def validate_repo_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "codeup.aliyun.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or not parsed.path.endswith(".git")
        or any(part in {"", ".", ".."} for part in parsed.path.split("/")[1:])
    ):
        raise ValueError("CODEUP_REPOSITORY_URL_INVALID")
    return value


def clone_target(vm_name: str, repo_url: str) -> Path:
    if not re.fullmatch(r"[a-z]{4}", vm_name):
        raise ValueError("VM_NAME_INVALID")
    repo_name = unquote(Path(urlsplit(validate_repo_url(repo_url)).path).name[:-4])
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repo_name) or repo_name in {".", ".."}:
        raise ValueError("REPOSITORY_NAME_INVALID")
    return Path("/Users") / vm_name / "StudioProjects" / repo_name


def build_payload(username: str, password: str, repo_url: str, target: Path) -> bytes:
    values = (username, password, validate_repo_url(repo_url), str(target))
    if any(not value or "\0" in value or "\r" in value or "\n" in value for value in values):
        raise ValueError("CODEUP_PAYLOAD_INVALID")
    return b"\0".join(value.encode("utf-8") for value in values) + b"\0"


def exact_owned_run(run_id: str, vm_name: str) -> dict:
    if not RUNS_FILE.is_file():
        raise RuntimeError("RUNS_FILE_MISSING")
    payload = json.loads(RUNS_FILE.read_text(encoding="utf-8"))
    matches = [run for run in payload.get("runs", []) if str(run.get("id") or "") == run_id]
    if len(matches) != 1:
        raise RuntimeError(f"RUN_ID_MATCH_COUNT={len(matches)}")
    run = matches[0]
    submission_data = run.get("submission_data") or {}
    local_host = os.getenv("SUBMISSION_HOST_MACHINE", "").strip()
    run_host = str(submission_data.get("host_machine") or run.get("host_machine") or "").strip()
    if not local_host or run_host != local_host:
        raise RuntimeError("RUN_HOST_OWNERSHIP_MISMATCH")
    if str(run.get("vm_name") or "") != vm_name:
        raise RuntimeError("RUN_VM_NAME_MISMATCH")
    return run


def verify_execution_context(
    *,
    run_id: str,
    parent_title: str,
    page_title: str,
    vm_name: str,
    vm_ip: str,
    vm_user: str,
    direct_resolver: Callable[..., Any] = resolve_direct_context,
) -> None:
    if run_id.startswith("direct-"):
        if not DIRECT_RUN_RE.fullmatch(run_id):
            raise RuntimeError("DIRECT_RUN_ID_INVALID")
        context = direct_resolver(
            parent_title=parent_title,
            page_title=page_title,
            vm_name=vm_name,
            vm_ip=vm_ip,
            vm_user=vm_user,
        )
        if (
            context.context_id != run_id
            or context.parent_title != parent_title
            or context.page_title != page_title
            or context.vm_name != vm_name
            or context.vm_ip != vm_ip
            or context.vm_user != vm_user
        ):
            raise RuntimeError("DIRECT_CONTEXT_MISMATCH")
        return
    exact_owned_run(run_id, vm_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-ip", required=True)
    parser.add_argument("--parent-title", required=True)
    parser.add_argument("--page-title", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()

    ipaddress.ip_address(args.vm_ip)
    verify_execution_context(
        run_id=args.run_id,
        parent_title=args.parent_title,
        page_title=args.page_title,
        vm_name=args.vm_name,
        vm_ip=args.vm_ip,
        vm_user=args.vm_name,
    )
    username = os.getenv("CODEUP_USERNAME", "")
    password = os.getenv("CODEUP_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("CODEUP_CREDENTIALS_MISSING")

    api = api_from_env()
    api.verify_parent(args.parent_title)
    repo_url = validate_repo_url(
        api.read_field(args.page_title, "账号信息", "代码链接：").strip()
    )
    target = clone_target(args.vm_name, repo_url)
    payload = build_payload(username, password, repo_url, target)
    remote_command = "/bin/zsh -lc " + shlex.quote(REMOTE_CLONE_SCRIPT)
    command = configured_guest_password_ssh_args(
        args.vm_name, args.vm_ip, connect_timeout=5
    )
    command.insert(1, "-T")
    command.append(remote_command)
    result = subprocess.run(
        command,
        input=payload,
        cwd=PROJECT_ROOT,
        timeout=args.timeout_seconds,
        check=False,
        env=password_environment(),
    )
    del payload
    if result.returncode != 0:
        print(f"UTM_21_CLONE_EXIT={result.returncode}")
        return result.returncode
    print("UTM_21_RUN_HOST=verified")
    print("UTM_21_CODEUP_CREDENTIAL_CHANNEL=stdin_memory_only")
    print("UTM_21_CLONE_EXIT=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
