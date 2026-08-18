#!/usr/bin/env python3
"""Run UTM-9 through Notion API, configured-password SSH, and guest certtool."""

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
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar


SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scripts.notion_api import api_from_env  # noqa: E402
from scripts.clean_cli import run_clean_cli  # noqa: E402
from scripts.utm_clash_ip_target import (  # noqa: E402
    UTMCTL,
    TargetVMError,
    parse_utm_status,
    require_exact_bound_vm,
    resolve_exact_vm_ip,
)
from scripts.vm_inventory import InventoryError  # noqa: E402
from scripts.ssh_password import (  # noqa: E402
    password_environment,
    scp_args,
    ssh_args,
)
from services.host_config import guest_password  # noqa: E402
from services.project_paths import PROJECT_ROOT, VM_IMAGES_DIR  # noqa: E402


RUNS_FILE = PROJECT_ROOT / "runtime" / "feishu-runs.json"
ATTEMPT_ROOT = PROJECT_ROOT / "runtime" / "attempts"
GUEST_HELPER_SOURCE = Path(__file__).with_name("utm_9_guest.py")
INVENTORY_DATABASE = PROJECT_ROOT / "runtime" / "vm-inventory.sqlite3"
GUEST_HELPER_MANAGED_MARKER = 'UTM9_GUEST_HELPER_ID = "mac-submission-utm-9-v1"'
LEGACY_MANAGED_HELPER_SHA256 = frozenset(
    {"2a576faa6f717a151e1bdf088c27ef0e297eb614b4020ffe7f452bd059f89e0c"}
)
CSR_FILENAME = "CertificateSigningRequest.certSigningRequest"
CONTEXT_FIELDS = (
    "run_id",
    "app_name",
    "vm_name",
    "vm_ip",
    "vm_user",
    "csr_path",
)


class UTM9Error(RuntimeError):
    """Raised when UTM-9 cannot continue without risking another CSR/key."""


T = TypeVar("T")


def retry_read_only(
    operation: Callable[[], T],
    *,
    label: str,
    sleeper: Callable[[float], None] = time.sleep,
) -> T:
    if not re.fullmatch(r"[A-Z0-9_]+", label):
        raise ValueError("read-only recovery label is invalid")
    last_error: Exception | None = None
    for attempt, delay in enumerate((0, 5, 10), start=1):
        if delay:
            sleeper(delay)
        try:
            return operation()
        except Exception as error:
            last_error = error
            if attempt == 3:
                break
    classification = str(last_error or "")
    if isinstance(last_error, UTM9Error) and re.fullmatch(
        r"[A-Z0-9_.=:-]+", classification
    ):
        raise UTM9Error(
            f"{label}_RECOVERY_EXHAUSTED:{classification}"
        ) from last_error
    raise UTM9Error(f"{label}_RECOVERY_EXHAUSTED") from last_error


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise UTM9Error("RUN_ID_INVALID")
    return value


def exact_owned_run(
    payload: Mapping[str, Any], *, run_id: str, vm_name: str, local_host: str
) -> dict[str, Any]:
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise UTM9Error("RUNS_PAYLOAD_INVALID")
    matches = [run for run in runs if isinstance(run, dict) and str(run.get("id") or "") == run_id]
    if len(matches) != 1:
        raise UTM9Error(f"RUN_ID_MATCH_COUNT={len(matches)}")
    run = matches[0]
    submission = run.get("submission_data")
    if not isinstance(submission, dict):
        submission = {}
    run_host = str(submission.get("host_machine") or run.get("host_machine") or "").strip()
    if not local_host or run_host != local_host:
        raise UTM9Error("RUN_HOST_OWNERSHIP_MISMATCH")
    if str(run.get("vm_name") or "").strip() != vm_name:
        raise UTM9Error("RUN_VM_NAME_MISMATCH")
    app_name = str(run.get("app_name") or submission.get("app_name") or "").strip()
    if not app_name or "\0" in app_name or "\n" in app_name or "\r" in app_name:
        raise UTM9Error("RUN_APP_NAME_INVALID")
    return run


def _read_exact_vm_status(vm_name: str) -> str:
    try:
        result = subprocess.run(
            [UTMCTL, "status", vm_name],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UTM9Error("INDEPENDENT_VM_STATUS_FAILED") from error
    if result.returncode != 0:
        raise UTM9Error("INDEPENDENT_VM_STATUS_FAILED")
    try:
        return parse_utm_status(result.stdout)
    except TargetVMError as error:
        raise UTM9Error("INDEPENDENT_VM_STATUS_AMBIGUOUS") from error


def verify_independent_test_target(
    *,
    app_name: str,
    vm_name: str,
    vm_ip: str,
    database: Path = INVENTORY_DATABASE,
    images_dir: Path = VM_IMAGES_DIR,
    bound_loader: Callable[..., Any] = require_exact_bound_vm,
    status_loader: Callable[[str], str] = _read_exact_vm_status,
    ip_loader: Callable[[Any], str] = resolve_exact_vm_ip,
) -> None:
    """Lock no-run tests to one running SQLite/bundle/MAC/IP identity."""
    try:
        target = bound_loader(database, images_dir, vm_name, app_name)
    except (InventoryError, OSError, TargetVMError, ValueError) as error:
        raise UTM9Error("INDEPENDENT_VM_BINDING_BLOCKED") from error
    status = status_loader(vm_name)
    if status not in {"started", "running"}:
        raise UTM9Error("INDEPENDENT_VM_NOT_RUNNING")
    try:
        resolved_ip = str(ipaddress.IPv4Address(ip_loader(target)))
        requested_ip = str(ipaddress.IPv4Address(vm_ip))
    except (OSError, TargetVMError, ValueError) as error:
        raise UTM9Error("INDEPENDENT_VM_IP_BLOCKED") from error
    if resolved_ip != requested_ip:
        raise UTM9Error("INDEPENDENT_VM_IP_MISMATCH")


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise UTM9Error("ATTEMPT_DIRECTORY_INVALID")
    os.chmod(path.parent, 0o700)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_attempt(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise UTM9Error("ATTEMPT_FILE_INVALID")
    if path.stat().st_mode & 0o777 != 0o600:
        raise UTM9Error("ATTEMPT_MODE_INVALID")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise UTM9Error("ATTEMPT_JSON_INVALID") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise UTM9Error("ATTEMPT_SCHEMA_INVALID")
    return payload


def prepare_attempt(
    path: Path, context: Mapping[str, str], desktop_before: Mapping[str, Any]
) -> dict[str, Any]:
    for field in CONTEXT_FIELDS:
        if not isinstance(context.get(field), str) or not context[field]:
            raise UTM9Error(f"ATTEMPT_CONTEXT_INVALID={field}")
    before = {
        "desktop_count": desktop_before.get("desktop_count"),
        "desktop_digest": desktop_before.get("desktop_digest"),
    }
    if (
        not isinstance(before["desktop_count"], int)
        or before["desktop_count"] < 0
        or not isinstance(before["desktop_digest"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", before["desktop_digest"])
    ):
        raise UTM9Error("ATTEMPT_DESKTOP_BEFORE_INVALID")
    if path.exists() or path.is_symlink():
        existing_state = _read_attempt(path)
        for field in CONTEXT_FIELDS:
            if existing_state.get(field) != context[field]:
                raise UTM9Error(f"ATTEMPT_CONTEXT_MISMATCH={field}")
        if existing_state.get("desktop_before") != before:
            raise UTM9Error("ATTEMPT_DESKTOP_BEFORE_MISMATCH")
        return existing_state
    attempt_id = str(uuid.uuid4())
    token = attempt_id.replace("-", "")[:16]
    new_state: dict[str, Any] = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        **{field: context[field] for field in CONTEXT_FIELDS},
        "key_label": f"utm-9-{token}",
        "challenge": f"utm-9-{token}",
        "desktop_before": before,
        "status": "prepared",
        "create_invocations": 0,
        "created_at": now(),
    }
    _atomic_write(path, new_state)
    return _read_attempt(path)


def update_attempt(path: Path, state: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    current = _read_attempt(path)
    if current.get("attempt_id") != state.get("attempt_id"):
        raise UTM9Error("ATTEMPT_ID_CHANGED")
    updated = {**current, **changes, "updated_at": now()}
    _atomic_write(path, updated)
    return _read_attempt(path)


def remote_helper_command(*, vm_user: str, guest_dir: str, mode: str) -> list[str]:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", vm_user):
        raise UTM9Error("VM_USER_INVALID")
    expected_dir = f"/Users/{vm_user}/Downloads"
    if guest_dir != expected_dir:
        raise UTM9Error("GUEST_DIR_INVALID")
    if mode not in {"probe", "create", "verify"}:
        raise UTM9Error("GUEST_MODE_INVALID")
    return [
        "/usr/bin/python3",
        "-B",
        f"{guest_dir}/{GUEST_HELPER_SOURCE.name}",
        "--mode",
        mode,
    ]


def _guest_base_payload(vm_user: str, attempt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "expected_user": vm_user,
        "csr_path": attempt["csr_path"],
        "key_label": attempt["key_label"],
        "challenge": attempt["challenge"],
        "keychain_password": guest_password(),
    }


def build_create_payload(
    *, vm_user: str, email: str, common_name: str, attempt: Mapping[str, Any]
) -> bytes:
    payload = {
        **_guest_base_payload(vm_user, attempt),
        "email": email,
        "common_name": common_name,
        "desktop_before": attempt["desktop_before"],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _run_ssh(
    vm_user: str,
    vm_ip: str,
    remote: list[str] | str,
    *,
    input_bytes: bytes | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[bytes]:
    command = ssh_args(vm_user, vm_ip, connect_timeout=5)
    if isinstance(remote, str):
        command.append(remote)
    else:
        command.extend(remote)
    return subprocess.run(
        command,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env=password_environment(),
        cwd=PROJECT_ROOT,
    )


def _parse_helper_state(output: bytes, vm_user: str) -> dict[str, Any]:
    text = output.decode("utf-8", errors="replace").strip()
    if text == "ABSENT":
        return {"status": "absent"}
    parts = text.split()
    if (
        len(parts) != 5
        or parts[0] != "PRESENT"
        or not re.fullmatch(r"[0-9a-f]{64}", parts[1])
        or parts[2] != vm_user
        or not re.fullmatch(r"[0-7]{3,4}", parts[3])
        or parts[4] not in {"0", "1"}
    ):
        raise UTM9Error("GUEST_HELPER_STATE_INVALID")
    return {
        "status": "present",
        "sha256": parts[1],
        "owner": parts[2],
        "mode": parts[3],
        "managed_marker": int(parts[4]),
    }


def install_guest_helper(
    vm_user: str,
    vm_ip: str,
    guest_dir: str,
    *,
    ssh_runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
    scp_runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
    token_factory: Callable[[], str] | None = None,
) -> None:
    ssh_runner = ssh_runner or _run_ssh
    scp_runner = scp_runner or subprocess.run
    token_factory = token_factory or (lambda: uuid.uuid4().hex)
    if not GUEST_HELPER_SOURCE.is_file() or GUEST_HELPER_SOURCE.is_symlink():
        raise UTM9Error("GUEST_HELPER_SOURCE_INVALID")
    source = GUEST_HELPER_SOURCE.read_bytes()
    try:
        source_text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise UTM9Error("GUEST_HELPER_SOURCE_INVALID") from error
    try:
        compile(source_text, str(GUEST_HELPER_SOURCE), "exec")
    except SyntaxError as error:
        raise UTM9Error("GUEST_HELPER_SOURCE_SYNTAX_INVALID") from error
    marker_count = source_text.splitlines().count(GUEST_HELPER_MANAGED_MARKER)
    if marker_count != 1:
        raise UTM9Error("GUEST_HELPER_SOURCE_MARKER_INVALID")
    digest = hashlib.sha256(source).hexdigest()
    remote_path = f"{guest_dir}/{GUEST_HELPER_SOURCE.name}"
    quoted = shlex.quote(remote_path)
    marker = shlex.quote(GUEST_HELPER_MANAGED_MARKER)
    probe_command = (
        f"if test -e {quoted} || test -L {quoted}; then "
        f"test -f {quoted} && test ! -L {quoted} || exit 21; "
        f"digest=$(/usr/bin/shasum -a 256 {quoted} | /usr/bin/awk '{{print $1}}') || exit 22; "
        f"owner=$(/usr/bin/stat -f '%Su' {quoted}) || exit 23; "
        f"mode=$(/usr/bin/stat -f '%Lp' {quoted}) || exit 24; "
        f"markers=$(/usr/bin/grep -Fxc {marker} {quoted} || true); "
        "/usr/bin/printf 'PRESENT %s %s %s %s\\n' \"$digest\" \"$owner\" \"$mode\" \"$markers\"; "
        "else /usr/bin/printf 'ABSENT\\n'; fi"
    )

    def read_helper_state() -> dict[str, Any]:
        result = ssh_runner(vm_user, vm_ip, probe_command)
        if result.returncode != 0:
            raise UTM9Error("GUEST_HELPER_PROBE_FAILED")
        return _parse_helper_state(result.stdout, vm_user)

    state = retry_read_only(read_helper_state, label="GUEST_HELPER_PROBE")
    current_digest = str(state.get("sha256") or "")
    if state["status"] == "absent" or current_digest != digest:
        managed = (
            state["status"] == "absent"
            or int(state.get("managed_marker") or 0) == 1
            or current_digest in LEGACY_MANAGED_HELPER_SHA256
        )
        if not managed:
            raise UTM9Error("GUEST_HELPER_CONFLICT")
        token = token_factory()
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", token):
            raise UTM9Error("GUEST_HELPER_TOKEN_INVALID")
        temporary_path = f"{remote_path}.tmp-{token}"
        quoted_temporary = shlex.quote(temporary_path)
        copied = scp_runner(
            scp_args(
                vm_user,
                vm_ip,
                GUEST_HELPER_SOURCE,
                temporary_path,
                connect_timeout=8,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
            env=password_environment(),
            cwd=PROJECT_ROOT,
        )
        if copied.returncode != 0:
            raise UTM9Error("GUEST_HELPER_COPY_FAILED")
        quoted_lock = shlex.quote(f"{remote_path}.update-lock")
        expected_state = (
            f"test ! -e {quoted} && test ! -L {quoted}"
            if state["status"] == "absent"
            else (
                f"test -f {quoted} && test ! -L {quoted} && "
                f"test \"$(/usr/bin/shasum -a 256 {quoted} | /usr/bin/awk '{{print $1}}')\" = {shlex.quote(current_digest)}"
            )
        )
        update_command = (
            f"/bin/mkdir {quoted_lock} || exit 31; "
            f"trap '/bin/rmdir {quoted_lock}' EXIT; "
            f"{expected_state} || exit 32; "
            f"test -f {quoted_temporary} && test ! -L {quoted_temporary} || exit 33; "
            f"test \"$(/usr/bin/shasum -a 256 {quoted_temporary} | /usr/bin/awk '{{print $1}}')\" = {shlex.quote(digest)} || exit 34; "
            f"/bin/chmod 700 {quoted_temporary} || exit 35; "
            f"/bin/mv -f {quoted_temporary} {quoted} || exit 36; "
            "/usr/bin/printf 'UPDATED\\n'"
        )
        updated = ssh_runner(vm_user, vm_ip, update_command)
        if updated.returncode != 0 or updated.stdout.decode(
            "utf-8", errors="replace"
        ).strip() != "UPDATED":
            cleanup = f"test ! -L {quoted_temporary} && /bin/rm -f {quoted_temporary}"
            ssh_runner(vm_user, vm_ip, cleanup)
            raise UTM9Error("GUEST_HELPER_ATOMIC_UPDATE_FAILED")

    def read_helper_hash() -> dict[str, Any]:
        result = ssh_runner(
            vm_user,
            vm_ip,
            f"test -f {quoted} && test ! -L {quoted} && /bin/chmod 700 {quoted} && {probe_command}",
        )
        if result.returncode != 0:
            raise UTM9Error("GUEST_HELPER_VERIFY_FAILED")
        return _parse_helper_state(result.stdout, vm_user)

    verified = retry_read_only(read_helper_hash, label="GUEST_HELPER_VERIFY")
    if (
        verified.get("sha256") != digest
        or verified.get("mode") != "700"
        or verified.get("managed_marker") != 1
    ):
        raise UTM9Error("GUEST_HELPER_HASH_MISMATCH")


def call_guest(
    *,
    vm_user: str,
    vm_ip: str,
    guest_dir: str,
    mode: str,
    payload: bytes,
    timeout: int = 150,
) -> dict[str, Any]:
    result = _run_ssh(
        vm_user,
        vm_ip,
        remote_helper_command(vm_user=vm_user, guest_dir=guest_dir, mode=mode),
        input_bytes=payload,
        timeout=timeout,
    )
    if result.returncode != 0:
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in result.stderr.decode("utf-8", errors="replace").splitlines()
        ]
        detail = " | ".join(line for line in lines if line) or "guest exited without detail"
        password_pattern = re.escape(guest_password())
        detail = re.sub(
            rf"(?i)(\b(?:keychain_)?password\b[\"']?\s*[:=]\s*[\"']?)"
            rf"{password_pattern}([\"']?)",
            r"\1<redacted>\2",
            detail,
        )
        detail = re.sub(
            rf"(?i)(eppc://[^:\s]+:){password_pattern}(@)",
            r"\1<redacted>\2",
            detail,
        )
        raise UTM9Error(
            f"GUEST_{mode.upper()}_EXIT={result.returncode}: {detail}"
        )
    try:
        response = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UTM9Error(f"GUEST_{mode.upper()}_OUTPUT_INVALID") from error
    if not isinstance(response, dict):
        raise UTM9Error(f"GUEST_{mode.upper()}_OUTPUT_INVALID")
    return response


def _payload_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _probe_payload(vm_user: str, csr_path: str, key_label: str, challenge: str) -> bytes:
    return _payload_bytes(
        {
            "expected_user": vm_user,
            "csr_path": csr_path,
            "key_label": key_label,
            "challenge": challenge,
            "keychain_password": guest_password(),
        }
    )


def _verification_equal(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    fields = (
        "status",
        "csr_bytes",
        "csr_sha256",
        "key_application_label",
        "desktop_count",
        "desktop_digest",
    )
    return all(first.get(field) == second.get(field) for field in fields)


def run(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[a-z]{4}", args.vm_name):
        raise UTM9Error("VM_NAME_INVALID")
    if args.vm_user != args.vm_name:
        raise UTM9Error("VM_USER_NAME_MISMATCH")
    ipaddress.ip_address(args.vm_ip)
    api = api_from_env()
    local_host = os.getenv("SUBMISSION_HOST_MACHINE", "").strip()
    supplied_run_id = getattr(args, "run_id", None)
    supplied_page_title = getattr(args, "page_title", None)
    if supplied_run_id:
        run_id = _safe_run_id(supplied_run_id)
        if not RUNS_FILE.is_file() or RUNS_FILE.is_symlink():
            raise UTM9Error("RUNS_FILE_INVALID")
        runs_payload = json.loads(RUNS_FILE.read_text(encoding="utf-8"))
        run_record = exact_owned_run(
            runs_payload,
            run_id=run_id,
            vm_name=args.vm_name,
            local_host=local_host,
        )
        submission = run_record.get("submission_data")
        if not isinstance(submission, dict):
            submission = {}
        app_name = str(run_record.get("app_name") or submission.get("app_name") or "").strip()
        page_title = f"{app_name}-{args.vm_name}"
    else:
        page_title = str(supplied_page_title or "").strip()
        suffix = f"-{args.vm_name}"
        if not page_title.endswith(suffix) or len(page_title) <= len(suffix):
            raise UTM9Error("NOTION_PAGE_TITLE_VM_MISMATCH")
        if any(ord(character) < 32 for character in page_title):
            raise UTM9Error("NOTION_PAGE_TITLE_INVALID")
        app_name = page_title[:-len(suffix)]
        verify_independent_test_target(
            app_name=app_name,
            vm_name=args.vm_name,
            vm_ip=args.vm_ip,
        )
        digest = hashlib.sha256(page_title.encode("utf-8")).hexdigest()[:12]
        run_id = _safe_run_id(f"independent-utm-9-{args.vm_name}-{digest}")
    def read_notion_subject() -> tuple[str, str]:
        api.verify_parent(local_host)
        current_email = api.read_field(page_title, "账号信息", "邮箱：").strip()
        current_name = api.read_field(page_title, "账号信息", "用户名：").strip()
        if not current_email or not current_name:
            raise UTM9Error("NOTION_CSR_SUBJECT_MISSING")
        return current_email, current_name

    email, common_name = retry_read_only(
        read_notion_subject, label="NOTION_CSR_SUBJECT"
    )

    guest_dir = f"/Users/{args.vm_user}/Downloads"
    csr_path = f"/Users/{args.vm_user}/Desktop/{CSR_FILENAME}"
    install_guest_helper(args.vm_user, args.vm_ip, guest_dir)

    dummy_label = "utm-9-0000000000000000"
    def read_initial_probe() -> dict[str, Any]:
        result = call_guest(
            vm_user=args.vm_user,
            vm_ip=args.vm_ip,
            guest_dir=guest_dir,
            mode="probe",
            payload=_probe_payload(args.vm_user, csr_path, dummy_label, dummy_label),
        )
        if not result.get("identity_verified") or not result.get("keychain_unlocked"):
            raise UTM9Error("GUEST_IDENTITY_OR_KEYCHAIN_BLOCKED")
        return result

    initial_probe = retry_read_only(read_initial_probe, label="GUEST_INITIAL_PROBE")
    context = {
        "run_id": run_id,
        "app_name": app_name,
        "vm_name": args.vm_name,
        "vm_ip": str(ipaddress.ip_address(args.vm_ip)),
        "vm_user": args.vm_user,
        "csr_path": csr_path,
    }
    attempt_path = ATTEMPT_ROOT / run_id / "utm-9.json"
    state = prepare_attempt(
        attempt_path,
        context,
        {
            "desktop_count": initial_probe.get("desktop_count"),
            "desktop_digest": initial_probe.get("desktop_digest"),
        },
    )
    base_payload = build_create_payload(
        vm_user=args.vm_user,
        email=email,
        common_name=common_name,
        attempt=state,
    )
    def read_current_probe() -> dict[str, Any]:
        result = call_guest(
            vm_user=args.vm_user,
            vm_ip=args.vm_ip,
            guest_dir=guest_dir,
            mode="probe",
            payload=_probe_payload(
                args.vm_user, csr_path, str(state["key_label"]), str(state["challenge"])
            ),
        )
        if not result.get("identity_verified") or not result.get("keychain_unlocked"):
            raise UTM9Error("GUEST_IDENTITY_OR_KEYCHAIN_BLOCKED")
        return result

    current_probe = retry_read_only(read_current_probe, label="GUEST_CURRENT_PROBE")
    if state.get("status") == "complete":
        if not current_probe.get("csr_exists") or not current_probe.get("key_exists"):
            raise UTM9Error("COMPLETE_ATTEMPT_EVIDENCE_MISSING")
    elif state.get("status") == "creating":
        csr_exists = bool(current_probe.get("csr_exists"))
        key_exists = bool(current_probe.get("key_exists"))
        if csr_exists and key_exists:
            pass
        elif not csr_exists and not key_exists and int(state.get("create_invocations") or 0) < 2:
            state = update_attempt(attempt_path, state, status="prepared")
        else:
            raise UTM9Error("CREATE_ATTEMPT_PARTIAL_OR_EXHAUSTED")
    elif state.get("status") != "prepared":
        raise UTM9Error("ATTEMPT_STATUS_INVALID")

    if state.get("status") == "prepared":
        if current_probe.get("csr_exists") or current_probe.get("key_exists"):
            raise UTM9Error("CSR_OR_KEY_PREEXISTS")
        if {
            "desktop_count": current_probe.get("desktop_count"),
            "desktop_digest": current_probe.get("desktop_digest"),
        } != state["desktop_before"]:
            raise UTM9Error("DESKTOP_BEFORE_CHANGED")
        invocations = int(state.get("create_invocations") or 0) + 1
        state = update_attempt(
            attempt_path,
            state,
            status="creating",
            create_invocations=invocations,
        )
        call_guest(
            vm_user=args.vm_user,
            vm_ip=args.vm_ip,
            guest_dir=guest_dir,
            mode="create",
            payload=base_payload,
        )

    def read_verification() -> dict[str, Any]:
        return call_guest(
            vm_user=args.vm_user,
            vm_ip=args.vm_ip,
            guest_dir=guest_dir,
            mode="verify",
            payload=base_payload,
        )

    first = retry_read_only(read_verification, label="CSR_VERIFY_FIRST")
    second = retry_read_only(read_verification, label="CSR_VERIFY_SECOND")
    if not _verification_equal(first, second) or first.get("status") != "verified":
        raise UTM9Error("CSR_DOUBLE_VERIFY_MISMATCH")
    state = update_attempt(
        attempt_path,
        state,
        status="complete",
        completed_at=now(),
        evidence={
            "csr_bytes": first["csr_bytes"],
            "csr_sha256": first["csr_sha256"],
            "key_application_label": first["key_application_label"],
            "verification_reads": 2,
        },
    )
    if _read_attempt(attempt_path) != state:
        raise UTM9Error("ATTEMPT_READBACK_MISMATCH")
    print(f"CSR_ATTEMPT_ID={state['attempt_id']}")
    print(f"CSR_PATH={csr_path}")
    print("CSR_PRIVATE_KEY=verified")
    print("CSR_DISK=verified")
    print("CERTIFICATE_REQUEST_SAVED=verified")
    print("CERTIFICATE_REQUEST_LOCATION=Desktop")
    print("UTM_9=verified")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run command-only UTM-9 CSR creation")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--run-id")
    selector.add_argument("--page-title")
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-ip", required=True)
    parser.add_argument("--vm-user", required=True)
    args = parser.parse_args()
    return run_clean_cli(
        skill_name="utm-key",
        success_marker="UTM_9=verified",
        operation=lambda: run(args),
        preserve_error_detail=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
