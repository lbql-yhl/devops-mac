#!/usr/bin/env python3
"""Read App Store Connect API credentials from one guest prod.yml and register them in Notion."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ssh_password import password_environment, ssh_args  # noqa: E402
from scripts.clean_cli import run_clean_cli  # noqa: E402
from scripts.utm_direct_context import resolve_direct_context  # noqa: E402
from services.feishu_bot import find_run, load_config, run_host_machine  # noqa: E402
from services.project_paths import SHARED_DIR  # noqa: E402


NOTION_API = PROJECT_ROOT / "scripts" / "notion_api.py"
VM_NAME_RE = re.compile(r"[a-z]{4}")
RUN_ID_RE = re.compile(r"[A-Za-z0-9-]{8,80}")
ISSUER_ID_RE = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
KEY_ID_RE = re.compile(r"[A-Z0-9]{10}")
P8_PATH_RE = re.compile(
    r"/Users/(?P<user>[a-z]{4})/Downloads/apple-store-bm/"
    r"(?P<relative>.+/)?AuthKey_(?P<key>[A-Z0-9]{10})\.p8"
)
PRIVATE_KEY_BEGIN = "-----BEGIN PRIVATE KEY-----"
PRIVATE_KEY_END = "-----END PRIVATE KEY-----"
PUBLIC_SHARED_NAMES = frozenset(
    {"AppleAccountScriptsBackup", "Fire_One_en1.3", "utm-image"}
)


class UTMP8Error(RuntimeError):
    """Raised when the fixed source or independent Notion verification is unsafe."""


def validate_target(vm_name: str, vm_ip: str, vm_user: str) -> None:
    if not VM_NAME_RE.fullmatch(vm_name) or vm_user != vm_name:
        raise UTMP8Error("VM_IDENTITY_INVALID")
    try:
        address = ipaddress.ip_address(vm_ip)
    except ValueError as error:
        raise UTMP8Error("VM_IP_INVALID") from error
    if address.version != 4:
        raise UTMP8Error("VM_IP_INVALID")


def shared_app_asset_names(app_name: str) -> tuple[str, str, str, str]:
    cleaned = str(app_name or "")
    if (
        not cleaned
        or cleaned != cleaned.strip()
        or Path(cleaned).name != cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or any(ord(character) < 32 for character in cleaned)
    ):
        raise UTMP8Error("SHARED_APP_NAME_INVALID")
    if cleaned in PUBLIC_SHARED_NAMES:
        raise UTMP8Error("SHARED_APP_NAME_RESERVED")
    return (
        cleaned,
        f"{cleaned}-git",
        f"{cleaned}.png",
        f"{cleaned}.xlsx",
    )


def cleanup_app_name_from_page(page_title: str, vm_name: str) -> str:
    if not VM_NAME_RE.fullmatch(str(vm_name or "")):
        raise UTMP8Error("CLEANUP_VM_NAME_INVALID")
    title = str(page_title or "").strip()
    suffix = f"-{vm_name}"
    if not title.endswith(suffix) or len(title) <= len(suffix):
        raise UTMP8Error("CLEANUP_PAGE_TITLE_INVALID")
    app_name = title[: -len(suffix)]
    shared_app_asset_names(app_name)
    return app_name


def cleanup_shared_app_assets(
    *,
    app_name: str,
    shared_dir: Path = SHARED_DIR,
    trash_root: Path | None = None,
) -> str:
    """Move only one application's four exact shared artifacts to Trash."""
    names = shared_app_asset_names(app_name)
    shared = Path(shared_dir)
    if shared.is_symlink() or not shared.is_dir():
        raise UTMP8Error("SHARED_DIRECTORY_UNSAFE")

    expected_directories = {names[0], names[1]}
    present: list[Path] = []
    for name in names:
        target = shared / name
        if target.parent != shared or target.is_symlink():
            raise UTMP8Error(f"SHARED_APP_ASSET_UNSAFE:{name}")
        if not target.exists():
            continue
        expected = target.is_dir() if name in expected_directories else target.is_file()
        if not expected:
            raise UTMP8Error(f"SHARED_APP_ASSET_UNSAFE:{name}")
        present.append(target)

    if not present:
        return "already_clean"

    trash = Path(trash_root) if trash_root is not None else Path.home() / ".Trash"
    if trash.is_symlink() or not trash.is_dir():
        raise UTMP8Error("TRASH_DIRECTORY_UNSAFE")
    prefix = f"utm-p8-{hashlib.sha256(app_name.encode('utf-8')).hexdigest()[:12]}-"
    attempt = Path(tempfile.mkdtemp(prefix=prefix, dir=trash))
    attempt.chmod(0o700)
    moved: list[tuple[Path, Path]] = []
    try:
        for source in present:
            destination = attempt / source.name
            if destination.exists() or destination.is_symlink():
                raise UTMP8Error("SHARED_APP_CLEANUP_DESTINATION_CONFLICT")
            shutil.move(str(source), str(destination))
            moved.append((source, destination))
        for source, destination in moved:
            if source.exists() or source.is_symlink() or not destination.exists():
                raise UTMP8Error("SHARED_APP_CLEANUP_READBACK_FAILED")
    except Exception as error:
        rollback_failed = False
        for source, destination in reversed(moved):
            try:
                if not source.exists() and not source.is_symlink() and destination.exists():
                    shutil.move(str(destination), str(source))
            except Exception:
                rollback_failed = True
        try:
            attempt.rmdir()
        except OSError:
            rollback_failed = True
        if rollback_failed:
            raise UTMP8Error("SHARED_APP_CLEANUP_ROLLBACK_FAILED") from error
        if isinstance(error, UTMP8Error):
            raise
        raise UTMP8Error("SHARED_APP_CLEANUP_MOVE_FAILED") from error
    return f"verified_{len(moved)}"


def guest_read_command(vm_user: str) -> str:
    """Build a read-only guest command for the one fixed prod.yml and its P8."""
    if not VM_NAME_RE.fullmatch(vm_user):
        raise UTMP8Error("VM_USER_INVALID")
    config_path = f"/Users/{vm_user}/Downloads/apple-store-bm/config/prod.yml"
    project_root = f"/Users/{vm_user}/Downloads/apple-store-bm"
    source = r'''
import json, os, re, stat, sys

config_path, project_root, expected_user = sys.argv[1:]

def read_regular(path, code):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise SystemExit(code)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size < 1:
            raise SystemExit(code)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read()
    finally:
        os.close(descriptor)
    return payload

def scalar(raw, key):
    value = raw.strip()
    if not value:
        raise SystemExit("PROD_YML_FIELD_EMPTY")
    if value.startswith('"') and value.endswith('"'):
        try:
            parsed = json.loads(value)
        except Exception:
            raise SystemExit("PROD_YML_SCALAR_INVALID")
        if not isinstance(parsed, str):
            raise SystemExit("PROD_YML_SCALAR_INVALID")
        return parsed
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    marker = value.find(" #")
    return value[:marker].strip() if marker >= 0 else value

config_real = os.path.realpath(config_path)
if config_real != config_path or os.path.islink(config_path):
    raise SystemExit("PROD_YML_PATH_INVALID")
try:
    config_text = read_regular(config_path, "PROD_YML_READ_FAILED").decode("utf-8")
except UnicodeDecodeError:
    raise SystemExit("PROD_YML_UTF8_INVALID")

values = {}
for line in config_text.splitlines():
    match = re.match(r"^\s{2}(issuer_id|key_id|private_key_path):\s*(.*?)\s*$", line)
    if not match:
        continue
    key = match.group(1)
    if key in values:
        raise SystemExit("PROD_YML_FIELD_DUPLICATED")
    values[key] = scalar(match.group(2), key)
if set(values) != {"issuer_id", "key_id", "private_key_path"}:
    raise SystemExit("PROD_YML_FIELDS_INVALID")
if not re.fullmatch(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}", values["issuer_id"]):
    raise SystemExit("ISSUER_ID_INVALID")
if not re.fullmatch(r"[A-Z0-9]{10}", values["key_id"]):
    raise SystemExit("KEY_ID_INVALID")

configured = values["private_key_path"]
candidate = configured if os.path.isabs(configured) else os.path.join(project_root, configured)
private_path = os.path.realpath(candidate)
root_real = os.path.realpath(project_root)
try:
    common = os.path.commonpath((root_real, private_path))
except ValueError:
    raise SystemExit("P8_PATH_INVALID")
if common != root_real or private_path == root_real or os.path.islink(candidate):
    raise SystemExit("P8_PATH_INVALID")
if os.path.basename(private_path) != "AuthKey_" + values["key_id"] + ".p8":
    raise SystemExit("P8_FILENAME_KEY_ID_MISMATCH")
private_bytes = read_regular(private_path, "P8_READ_FAILED")
try:
    private_key = private_bytes.decode("utf-8")
except UnicodeDecodeError:
    raise SystemExit("P8_UTF8_INVALID")
if private_key.count("-----BEGIN PRIVATE KEY-----") != 1 or private_key.count("-----END PRIVATE KEY-----") != 1:
    raise SystemExit("P8_PEM_INVALID")
if not private_key.strip().startswith("-----BEGIN PRIVATE KEY-----") or not private_key.strip().endswith("-----END PRIVATE KEY-----"):
    raise SystemExit("P8_PEM_INVALID")

sys.stdout.write(json.dumps({
    "issuer_id": values["issuer_id"],
    "key_id": values["key_id"],
    "private_key_path": private_path,
    "private_key": private_key,
}, separators=(",", ":")))
'''.strip()
    return shlex.join(
        (
            "/usr/bin/python3",
            "-c",
            source,
            config_path,
            project_root,
            vm_user,
        )
    )


def parse_guest_payload(raw: str) -> dict[str, str]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise UTMP8Error("GUEST_PAYLOAD_INVALID") from error
    if not isinstance(payload, dict) or set(payload) != {
        "issuer_id",
        "key_id",
        "private_key_path",
        "private_key",
    }:
        raise UTMP8Error("GUEST_PAYLOAD_INVALID")
    values = {key: str(value) for key, value in payload.items()}
    if not ISSUER_ID_RE.fullmatch(values["issuer_id"]):
        raise UTMP8Error("GUEST_ISSUER_ID_INVALID")
    if not KEY_ID_RE.fullmatch(values["key_id"]):
        raise UTMP8Error("GUEST_KEY_ID_INVALID")
    path_match = P8_PATH_RE.fullmatch(values["private_key_path"])
    if not path_match or path_match.group("key") != values["key_id"]:
        raise UTMP8Error("GUEST_P8_PATH_INVALID")
    normalized_root = f"/Users/{path_match.group('user')}/Downloads/apple-store-bm"
    if os.path.commonpath((normalized_root, values["private_key_path"])) != normalized_root:
        raise UTMP8Error("GUEST_P8_PATH_INVALID")
    private_key = values["private_key"]
    if (
        private_key.count(PRIVATE_KEY_BEGIN) != 1
        or private_key.count(PRIVATE_KEY_END) != 1
        or not private_key.strip().startswith(PRIVATE_KEY_BEGIN)
        or not private_key.strip().endswith(PRIVATE_KEY_END)
    ):
        raise UTMP8Error("GUEST_P8_PEM_INVALID")
    return values


def notion_payload(values: Mapping[str, str]) -> str:
    return (
        f"issuer id: {values['issuer_id']}\n"
        f"key id:{values['key_id']}\n"
        "p8文件内容：\n\n"
        f"{values['private_key'].strip()}\n"
    )


def _private_write(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _notion_call(
    runner: Callable[..., Any],
    arguments: list[str],
    *,
    input_text: str | None = None,
) -> None:
    result = runner(
        [sys.executable, str(NOTION_API), *arguments],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=45,
        check=False,
        cwd=PROJECT_ROOT,
    )
    if result.returncode != 0:
        diagnostic = str(result.stderr or result.stdout or "").strip()
        diagnostic = " ".join(diagnostic.split())[:500]
        if diagnostic:
            raise UTMP8Error(f"NOTION_API_OPERATION_FAILED:{diagnostic}")
        raise UTMP8Error("NOTION_API_OPERATION_FAILED")


def update_notion(
    *,
    parent_title: str,
    page_title: str,
    text: str,
    runner: Callable[..., Any] = subprocess.run,
    temporary_root: Path | None = None,
) -> str:
    """Write one toggle and verify it with a separate read; rollback on mismatch."""
    base = Path(temporary_root) if temporary_root is not None else PROJECT_ROOT / "runtime" / "utm-p8"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    base.chmod(0o700)
    attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=base))
    attempt.chmod(0o700)
    before_path = attempt / "before.txt"
    after_path = attempt / "after.txt"
    rollback_path = attempt / "rollback.txt"
    rollback_verified = False
    completed = False

    def verify_parent() -> None:
        _notion_call(runner, ["verify-parent", "--title", parent_title])

    def read_to(path: Path) -> str:
        verify_parent()
        _notion_call(
            runner,
            [
                "read-toggle-code",
                "--title",
                page_title,
                "--heading",
                "更新信息",
                "--toggle",
                "退款回调及p8",
                "--out",
                str(path),
            ],
        )
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise UTMP8Error("NOTION_READBACK_FILE_INVALID")
        return path.read_text(encoding="utf-8")

    def write_from_stdin(value: str) -> None:
        verify_parent()
        _notion_call(
            runner,
            [
                "write-toggle-code",
                "--title",
                page_title,
                "--heading",
                "更新信息",
                "--toggle",
                "退款回调及p8",
                "--stdin",
                "--replace-existing",
            ],
            input_text=value,
        )

    try:
        before = read_to(before_path)
        if before == text:
            independent = read_to(after_path)
            if independent != text:
                raise UTMP8Error("NOTION_EQUAL_READBACK_MISMATCH")
            completed = True
            return "verified_equal"

        try:
            write_from_stdin(text)
            after = read_to(after_path)
            if after != text:
                raise UTMP8Error("NOTION_AFTER_READBACK_MISMATCH")
        except Exception as original_error:
            write_from_stdin(before)
            rollback = read_to(rollback_path)
            if rollback != before:
                raise UTMP8Error("NOTION_ROLLBACK_FAILED") from original_error
            rollback_verified = True
            if isinstance(original_error, UTMP8Error):
                raise original_error
            raise UTMP8Error("NOTION_WRITE_FAILED") from original_error
        completed = True
        return "written"
    finally:
        if completed or rollback_verified:
            shutil.rmtree(attempt)


def _run_context(run_id: str, vm_name: str) -> tuple[str, str, str]:
    run = find_run(run_id)
    if not isinstance(run, dict):
        raise UTMP8Error("FEISHU_RUN_NOT_FOUND")
    if run.get("vm_name") != vm_name:
        raise UTMP8Error("FEISHU_RUN_VM_MISMATCH")
    app_name = str(run.get("app_name") or "").strip()
    parent_title = str(load_config().submission_host_machine or "").strip()
    if not app_name or not parent_title or run_host_machine(run) != parent_title:
        raise UTMP8Error("FEISHU_RUN_IDENTITY_INVALID")
    return parent_title, f"{app_name}-{vm_name}", app_name


def run(
    args: argparse.Namespace,
    *,
    runner: Callable[..., Any] = subprocess.run,
    password_env: Mapping[str, str] | None = None,
) -> int:
    started = time.monotonic()
    if getattr(args, "cleanup_only", False):
        if args.run_id:
            raise UTMP8Error("CLEANUP_ONLY_REQUIRES_PAGE_TITLE")
        app_name = cleanup_app_name_from_page(args.page_title, args.vm_name)
        cleanup_result = cleanup_shared_app_assets(app_name=app_name)
        print("SHARED_APP_ASSETS_CLEANED=verified")
        print(f"SHARED_APP_ASSET_CLEANUP={cleanup_result}")
        return 0

    validate_target(args.vm_name, args.vm_ip, args.vm_user)
    if args.run_id:
        if not RUN_ID_RE.fullmatch(args.run_id):
            raise UTMP8Error("RUN_ID_INVALID")
        parent_title, page_title, app_name = _run_context(args.run_id, args.vm_name)
    else:
        context = resolve_direct_context(
            page_title=args.page_title,
            vm_name=args.vm_name,
            vm_ip=args.vm_ip,
            vm_user=args.vm_user,
        )
        parent_title = context.parent_title
        page_title = context.page_title
        app_name = context.app_name

    result = runner(
        ssh_args(args.vm_user, args.vm_ip, connect_timeout=8, tty=False)
        + [guest_read_command(args.vm_user)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
        cwd=PROJECT_ROOT,
        env=dict(password_env) if password_env is not None else password_environment(),
    )
    if result.returncode != 0:
        raise UTMP8Error("GUEST_PROD_YML_READ_FAILED")
    values = parse_guest_payload(result.stdout)
    registration = notion_payload(values)
    notion_result = update_notion(
        parent_title=parent_title,
        page_title=page_title,
        text=registration,
        runner=runner,
    )
    cleanup_result = cleanup_shared_app_assets(app_name=app_name)
    values.clear()
    print("PROD_YML_API_CREDENTIALS=verified")
    print("P8_FILE=verified")
    print(f"NOTION_WRITE={notion_result}")
    print("NOTION_REFUND_CALLBACK_P8=verified")
    print("API_CREDENTIALS_REGISTRATION=verified")
    print("SHARED_APP_ASSETS_CLEANED=verified")
    print(f"SHARED_APP_ASSET_CLEANUP={cleanup_result}")
    print("UTM_P8=verified")
    print(f"UTM_P8_ELAPSED_SECONDS={int(time.monotonic() - started)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Register prod.yml API credentials in Notion")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--run-id")
    selector.add_argument("--page-title")
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-ip")
    parser.add_argument("--vm-user")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return run_clean_cli(
        skill_name="utm-p8",
        success_marker=(
            "SHARED_APP_ASSETS_CLEANED=verified"
            if args.cleanup_only
            else "UTM_P8=verified"
        ),
        operation=lambda: run(args),
        preserve_error_detail=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
