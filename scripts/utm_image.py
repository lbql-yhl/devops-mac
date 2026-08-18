#!/usr/bin/env python3
"""Run the pre-delivered UTM-IMAGE guest script over configured-password SSH."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.notion_api import api_from_env  # noqa: E402
from scripts.clean_cli import run_clean_cli  # noqa: E402
from scripts.ssh_password import password_environment, ssh_args  # noqa: E402
from scripts.utm_env_download_assets import FeishuClient, load_credentials  # noqa: E402
from scripts.utm_image_source import MANIFEST_NAME, prepare_screenshot_source  # noqa: E402
from scripts.utm_image_delivery import sync_utm_image_guest_file  # noqa: E402
from scripts.utm_direct_context import resolve_direct_context  # noqa: E402
from scripts.utm_notion import resolve_feishu_source  # noqa: E402
from services.feishu_gateway import get_tenant_access_token  # noqa: E402
from services.feishu_bot import find_run, load_config, run_host_machine  # noqa: E402
from services.project_paths import SHARED_DIR  # noqa: E402


GUEST_DIR_TEMPLATE = "/Users/{vm_user}/Downloads/AppleAccountScriptsBackup"
GUEST_ENTRY = "utm_19_one.mjs"
GUEST_PLAYWRIGHT_TEMPLATE = (
    "/Users/{vm_user}/Downloads/Fire_One_en1.3/"
    "node_modules/playwright-core/index.mjs"
)
GUEST_SHARED_ROOT = "/Volumes/My Shared Files/共享文件"
GUEST_DIAGNOSTIC_PATH = PROJECT_ROOT / "runtime" / "utm-image-guest.stderr"
RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,100}")
VM_NAME_RE = re.compile(r"[a-z]{4}")
APP_ID_RE = re.compile(r"[0-9]+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
UTMCTL = str(Path(shutil.which("utmctl") or "/opt/homebrew/bin/utmctl").resolve())
REQUIRED_GUEST_MARKERS = (
    "LOCAL_EDGE_SESSION=reused",
    "SCREENSHOT_PACKAGE=verified",
    "APP_IDENTITY=verified",
    "IPHONE_69_DISPLAY=selected",
    "UTM_19=verified",
)


class UTMImageError(RuntimeError):
    """Raised when the host cannot prove one safe guest screenshot execution."""


def validate_target(vm_name: str, vm_ip: str, vm_user: str) -> None:
    if not VM_NAME_RE.fullmatch(vm_name):
        raise UTMImageError("VM_NAME_INVALID")
    if vm_user != vm_name:
        raise UTMImageError("VM_USER_MISMATCH")
    try:
        address = ipaddress.ip_address(vm_ip)
    except ValueError as error:
        raise UTMImageError("VM_IP_INVALID") from error
    if address.version != 4:
        raise UTMImageError("VM_IP_INVALID")


def guest_script_path(vm_user: str) -> str:
    if not VM_NAME_RE.fullmatch(vm_user):
        raise UTMImageError("VM_USER_INVALID")
    return f"{GUEST_DIR_TEMPLATE.format(vm_user=vm_user)}/{GUEST_ENTRY}"


def _guest_script_digest() -> str:
    source = PROJECT_ROOT / "skills" / "utm-image" / "scripts" / GUEST_ENTRY
    if not source.is_file() or source.is_symlink() or source.stat().st_size == 0:
        raise UTMImageError("HOST_GUEST_SCRIPT_INVALID")
    return hashlib.sha256(source.read_bytes()).hexdigest()


def guest_preflight_command(vm_user: str, expected_sha256: str) -> str:
    if not SHA256_RE.fullmatch(expected_sha256):
        raise UTMImageError("GUEST_SCRIPT_SHA256_INVALID")
    target = guest_script_path(vm_user)
    dependency = GUEST_PLAYWRIGHT_TEMPLATE.format(vm_user=vm_user)
    script = "\n".join(
        (
            "set -euo pipefail",
            f"expected_user={shlex.quote(vm_user)}",
            f"target={shlex.quote(target)}",
            f"dependency={shlex.quote(dependency)}",
            f"expected_sha={shlex.quote(expected_sha256)}",
            '[[ "$(/usr/bin/id -un)" == "$expected_user" ]]',
            '[[ "$HOME" == "/Users/$expected_user" ]]',
            '[[ -f "$target" && ! -L "$target" && -O "$target" && -s "$target" ]]',
            '[[ -f "$dependency" && ! -L "$dependency" && -s "$dependency" ]]',
            'actual_sha="$(/usr/bin/shasum -a 256 "$target" | /usr/bin/awk \'{print $1}\')"',
            '[[ "$actual_sha" == "$expected_sha" ]] || { print -u2 -- EDGE_SCRIPT_HASH_MISMATCH; exit 86; }',
            'node_bin="$(command -v node)"',
            '[[ -n "$node_bin" && -x "$node_bin" ]]',
            '"$node_bin" --check "$target" >/dev/null',
            "pids=\"$(/usr/bin/pgrep -x 'Microsoft Edge' 2>/dev/null || true)\"",
            'pid_count="$(/usr/bin/printf \'%s\\n\' "$pids" | /usr/bin/awk \'NF {n+=1} END {print n+0}\')"',
            '[[ "$pid_count" == "1" ]]',
            'listener="$(/usr/sbin/lsof -nP -iTCP:9222 -sTCP:LISTEN -t 2>/dev/null | /usr/bin/sort -u)"',
            '[[ "$listener" == "$pids" ]]',
            'websocket="$(/usr/bin/curl -fsS --max-time 5 http://127.0.0.1:9222/json/version | /usr/bin/plutil -extract webSocketDebuggerUrl raw -o - -)"',
            '[[ "$websocket" == ws://127.0.0.1:9222/* ]]',
            "exec /usr/bin/python3 -c 'import json,sys;print(json.dumps({\"edge_pid\":int(sys.argv[1]),\"edge_websocket\":sys.argv[2]},separators=(\",\",\":\")))' \"$pids\" \"$websocket\"",
        )
    )
    return shlex.join(("/bin/zsh", "-lic", script))


def guest_execution_command(vm_user: str, expected_sha256: str) -> str:
    if not SHA256_RE.fullmatch(expected_sha256):
        raise UTMImageError("GUEST_SCRIPT_SHA256_INVALID")
    target = guest_script_path(vm_user)
    base = str(Path(target).parent)
    script = "\n".join(
        (
            "set -euo pipefail",
            f"expected_user={shlex.quote(vm_user)}",
            f"base={shlex.quote(base)}",
            f"target={shlex.quote(target)}",
            f"expected_sha={shlex.quote(expected_sha256)}",
            '[[ "$(/usr/bin/id -un)" == "$expected_user" ]]',
            '[[ "$HOME" == "/Users/$expected_user" ]]',
            '[[ -d "$base" && ! -L "$base" ]]',
            '[[ -f "$target" && ! -L "$target" && -O "$target" && -s "$target" ]]',
            'actual_sha="$(/usr/bin/shasum -a 256 "$target" | /usr/bin/awk \'{print $1}\')"',
            '[[ "$actual_sha" == "$expected_sha" ]] || { print -u2 -- GUEST_SCRIPT_HASH_CHANGED; exit 87; }',
            'node_bin="$(command -v node)"',
            '[[ -n "$node_bin" && -x "$node_bin" ]]',
            'exec "$node_bin" "$target"',
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


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _guest_session(stdout: bytes | str) -> tuple[int, str]:
    try:
        payload = json.loads(_decode(stdout).strip())
    except (TypeError, ValueError) as error:
        raise UTMImageError("GUEST_PREFLIGHT_JSON_INVALID") from error
    pid = payload.get("edge_pid")
    websocket = payload.get("edge_websocket")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(websocket, str)
        or not websocket.startswith("ws://127.0.0.1:9222/")
    ):
        raise UTMImageError("GUEST_EDGE_SESSION_INVALID")
    return pid, websocket


def _guest_markers(stdout: bytes | str) -> tuple[str, ...]:
    markers = tuple(line.strip() for line in _decode(stdout).splitlines() if line.strip())
    if len(markers) != len(set(markers)):
        raise UTMImageError("GUEST_MARKERS_DUPLICATED")
    for required in REQUIRED_GUEST_MARKERS:
        if required not in markers:
            raise UTMImageError(f"GUEST_MARKER_MISSING={required.split('=', 1)[0]}")
    source = [line for line in markers if line.startswith("SCREENSHOT_SOURCE_FIELD=")]
    upload = [line for line in markers if line.startswith("SCREENSHOT_UPLOAD=")]
    if len(source) != 1 or source[0] not in {
        "SCREENSHOT_SOURCE_FIELD=beauty_link",
        "SCREENSHOT_SOURCE_FIELD=development_attachment",
    }:
        raise UTMImageError("GUEST_SOURCE_MARKER_INVALID")
    if len(upload) != 1 or not re.fullmatch(
        r"SCREENSHOT_UPLOAD=(?:verified|already_complete)_[1-9][0-9]*_of_10",
        upload[0],
    ):
        raise UTMImageError("GUEST_UPLOAD_MARKER_INVALID")
    return markers


def run_guest_script(
    *,
    vm_name: str,
    vm_ip: str,
    vm_user: str,
    payload: Mapping[str, Any],
    runner: Callable[..., Any] = subprocess.run,
    password_env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    validate_target(vm_name, vm_ip, vm_user)
    expected_digest = _guest_script_digest()
    preflight = _ssh_run(
        vm_user,
        vm_ip,
        guest_preflight_command(vm_user, expected_digest),
        input_bytes=b"",
        timeout=45,
        runner=runner,
        password_env=password_env,
    )
    if preflight.returncode != 0:
        raise UTMImageError("GUEST_PREFLIGHT_FAILED")
    pid, websocket = _guest_session(preflight.stdout)
    guest_payload = dict(payload)
    guest_payload["expectedEdgePid"] = pid
    guest_payload["expectedEdgeWebSocket"] = websocket
    execution = _ssh_run(
        vm_user,
        vm_ip,
        guest_execution_command(vm_user, expected_digest),
        input_bytes=json.dumps(
            guest_payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"),
        timeout=3600,
        runner=runner,
        password_env=password_env,
    )
    guest_payload.clear()
    if execution.returncode != 0:
        diagnostic = bytes(execution.stderr)
        GUEST_DIAGNOSTIC_PATH.parent.mkdir(parents=True, exist_ok=True)
        GUEST_DIAGNOSTIC_PATH.write_bytes(diagnostic)
        GUEST_DIAGNOSTIC_PATH.chmod(0o600)
        match = re.search(rb"(?m)^UTM_19_ERROR=([^\r\n]+)", diagnostic)
        if match:
            detail = match.group(1).decode("utf-8", errors="replace")
            raise UTMImageError(f"GUEST_EXECUTION_FAILED:{detail}")
        raise UTMImageError("GUEST_EXECUTION_FAILED")
    markers = _guest_markers(execution.stdout)
    GUEST_DIAGNOSTIC_PATH.unlink(missing_ok=True)
    return markers


def _secure_source_manifest(path: Path, app_name: str) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
        raise UTMImageError("SOURCE_MANIFEST_UNSAFE")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "verified"
        or payload.get("app_name_sha256")
        != hashlib.sha256(app_name.encode("utf-8")).hexdigest()
    ):
        raise UTMImageError("SOURCE_MANIFEST_IDENTITY_MISMATCH")
    return payload


def _source_result(app_name: str, run_id: str) -> dict[str, Any]:
    source_dir = PROJECT_ROOT / "runtime" / "utm-19" / run_id / "source"
    if source_dir.exists() and (source_dir.is_symlink() or not source_dir.is_dir()):
        raise UTMImageError("SOURCE_DIRECTORY_UNSAFE")
    source_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_dir.chmod(0o700)
    existing = _secure_source_manifest(source_dir / MANIFEST_NAME, app_name)
    if existing is not None:
        return existing
    credentials = load_credentials(PROJECT_ROOT / ".env")
    token = get_tenant_access_token(*credentials)
    app_token, table_id, view_id = resolve_feishu_source(token)
    base_url = (
        f"https://qv0zc1dq6qy.feishu.cn/base/{app_token}"
        f"?table={table_id}&view={view_id}"
    )
    return prepare_screenshot_source(
        base_url=base_url,
        app_name=app_name,
        output_dir=source_dir,
        client=FeishuClient(*credentials),
    )


def _stage_attachment(source: Mapping[str, Any], run_id: str) -> str:
    source_path = Path(str(source.get("path") or ""))
    expected_sha = str(source.get("sha256") or "")
    expected_size = source.get("size")
    if (
        not source_path.is_file()
        or source_path.is_symlink()
        or not SHA256_RE.fullmatch(expected_sha)
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size <= 0
    ):
        raise UTMImageError("ATTACHMENT_SOURCE_INVALID")
    payload = source_path.read_bytes()
    if len(payload) != expected_size or hashlib.sha256(payload).hexdigest() != expected_sha:
        raise UTMImageError("ATTACHMENT_SOURCE_READBACK_MISMATCH")
    suffix = source_path.suffix.lower()
    target_dir = SHARED_DIR / "utm-image" / run_id
    if target_dir.exists() and (target_dir.is_symlink() or not target_dir.is_dir()):
        raise UTMImageError("ATTACHMENT_SHARED_DIRECTORY_UNSAFE")
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target_dir.chmod(0o700)
    target = target_dir / f"source{suffix}"
    if target.exists() or target.is_symlink():
        if (
            target.is_symlink()
            or not target.is_file()
            or target.stat().st_size != expected_size
            or hashlib.sha256(target.read_bytes()).hexdigest() != expected_sha
        ):
            raise UTMImageError("ATTACHMENT_SHARED_CONFLICT")
    else:
        temporary = target_dir / f".source.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    target.chmod(0o600)
    relative = target.relative_to(SHARED_DIR)
    return f"{GUEST_SHARED_ROOT}/{relative.as_posix()}"


def guest_source_payload(
    source: Mapping[str, Any],
    app_name: str,
    run_id: str,
    *,
    source_app_name: str | None = None,
) -> dict[str, Any]:
    app_hash = hashlib.sha256(app_name.encode("utf-8")).hexdigest()
    source_hash = hashlib.sha256(
        (source_app_name or app_name).encode("utf-8")
    ).hexdigest()
    if source.get("app_name_sha256") != source_hash:
        raise UTMImageError("SOURCE_APP_IDENTITY_MISMATCH")
    kind = source.get("kind")
    field = source.get("source_field")
    if kind == "share_url" and field == "美女截图 链接":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise UTMImageError("BEAUTY_SOURCE_INVALID")
        return {
            "kind": "share_url",
            "sourceField": field,
            "url": url,
            "sourceIdentitySha256": source.get("source_identity_sha256"),
            "appNameSha256": app_hash,
        }
    if kind == "attachment" and field == "研发截图":
        return {
            "kind": "attachment",
            "sourceField": field,
            "path": _stage_attachment(source, run_id),
            "sha256": source.get("sha256"),
            "size": source.get("size"),
            "sourceIdentitySha256": source.get("source_identity_sha256"),
            "appNameSha256": app_hash,
        }
    raise UTMImageError("SOURCE_KIND_INVALID")


def _run_context(run_id: str, vm_name: str) -> tuple[str, str]:
    run = find_run(run_id)
    if not isinstance(run, dict):
        raise UTMImageError("FEISHU_RUN_NOT_FOUND")
    if run.get("vm_name") != vm_name:
        raise UTMImageError("FEISHU_RUN_VM_MISMATCH")
    app_name = str(run.get("app_name") or "").strip()
    if not app_name:
        raise UTMImageError("FEISHU_RUN_APP_MISSING")
    parent_title = str(load_config().submission_host_machine or "").strip()
    if not parent_title or run_host_machine(run) != parent_title:
        raise UTMImageError("FEISHU_RUN_HOST_MISMATCH")
    return parent_title, app_name


def _app_store_id(parent_title: str, page_title: str, api: Any) -> str:
    api.verify_parent(parent_title)
    value = api.read_field(page_title, "账号信息", "APP_ID：").strip()
    if not APP_ID_RE.fullmatch(value):
        raise UTMImageError("APP_STORE_APP_ID_INVALID")
    return value


def _verify_notion_app(
    parent_title: str, page_title: str, expected_app_name: str, api: Any
) -> None:
    api.verify_parent(parent_title)
    value = api.read_field(page_title, "应用信息", "应用名: ").strip()
    if value != expected_app_name:
        raise UTMImageError("NOTION_PAGE_APP_MISMATCH")


def _verify_vm_started(vm_name: str, runner: Callable[..., Any]) -> None:
    result = runner(
        [UTMCTL, "status", vm_name],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )
    values = [line.strip().lower() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(values) != 1 or values[0] not in {"started", "running"}:
        raise UTMImageError("VM_NOT_RUNNING")


def run(
    args: argparse.Namespace,
    *,
    api: Any | None = None,
    runner: Callable[..., Any] = subprocess.run,
    password_env: Mapping[str, str] | None = None,
) -> int:
    validate_target(args.vm_name, args.vm_ip, args.vm_user)
    supplied_run_id = getattr(args, "run_id", None)
    direct_mode = not supplied_run_id
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
        app_name = context.app_name
    else:
        if not RUN_ID_RE.fullmatch(str(supplied_run_id)):
            raise UTMImageError("RUN_ID_INVALID")
        context_id = str(supplied_run_id)
        _verify_vm_started(args.vm_name, runner)
        parent_title, app_name = _run_context(context_id, args.vm_name)
        page_title = f"{app_name}-{args.vm_user}"
    notion = api if api is not None else api_from_env()
    _verify_notion_app(parent_title, page_title, app_name, notion)
    app_store_id = _app_store_id(parent_title, page_title, notion)
    asset_app_name = str(getattr(args, "asset_app_name", "") or app_name).strip()
    if not asset_app_name:
        raise UTMImageError("ASSET_APP_NAME_MISSING")
    source = _source_result(asset_app_name, context_id)
    payload = {
        "runId": context_id,
        "vmName": args.vm_name,
        "appName": app_name,
        "appStoreAppId": app_store_id,
        "source": guest_source_payload(
            source,
            app_name,
            context_id,
            source_app_name=asset_app_name,
        ),
    }
    sync_utm_image_guest_file(SHARED_DIR, vm_user=args.vm_user, vm_ip=args.vm_ip)
    markers = run_guest_script(
        vm_name=args.vm_name,
        vm_ip=args.vm_ip,
        vm_user=args.vm_user,
        payload=payload,
        runner=runner,
        password_env=password_env,
    )
    payload.clear()
    print("UTM_IMAGE_HOST_TRANSPORT=password_ssh")
    print("UTM_IMAGE_GUEST_SOURCE=utm-vm-clone")
    print("UTM_IMAGE_FEISHU_SOURCE=verified")
    if direct_mode:
        print("UTM_IMAGE_DIRECT_CONTEXT=verified")
    for marker in markers:
        print(marker)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run UTM-IMAGE through the pre-delivered guest script"
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--run-id")
    selector.add_argument("--page-title")
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-ip", required=True)
    parser.add_argument("--vm-user", required=True)
    parser.add_argument("--asset-app-name")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    def operation() -> int:
        started_at = time.monotonic()
        status = run(args)
        print("UTM-IMAGE 截图步骤已完成")
        print(f"UTM_IMAGE_ELAPSED_SECONDS={round(time.monotonic() - started_at)}")
        return status

    return run_clean_cli(
        skill_name="utm-image",
        success_marker="UTM_19=verified",
        operation=operation,
        preserve_error_detail=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
