#!/usr/bin/env python3
"""Durable one-shot execution and classification for UTM-18."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from scripts.utm_18_ssh import build_invocation


RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
VM_NAME_RE = re.compile(r"[a-z]{4}")
ATTEMPT_ID_RE = re.compile(r"[0-9a-f]{32}")
SUMMARY_RE = re.compile(r"统计信息:\s*共处理\s*(\d+)\s*个产品")
COMPLETION_MARKER = "增强版内购创建完成！"
BUSINESS_ERROR_SIGNATURES = (
    "❌ 创建产品",
    "error calling the App Store Connect API",
    "status 500",
    "UNEXPECTED_ERROR",
)
RETRYABLE_PRESAVE_SIGNATURES = (
    "Error: 找不到可点击的inflight 页面保存按钮",
    "browserType.connectOverCDP: connect ECONNREFUSED 127.0.0.1:9222",
)
ERROR_DETAIL_START_RE = re.compile(
    r'^(?:\{|[A-Za-z]*Error\b|Error:|❌|.*(?:无法|找不到|失败|缺少|无效)|\s*["\']errors["\']\s*:)',
    re.IGNORECASE,
)


class UTM18AttemptError(RuntimeError):
    """Raised when a UTM-18 attempt cannot be safely attributed."""


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    run_id: str
    vm_name: str
    vm_ip: str
    log_path: str
    status_path: str
    command_path: str
    ledger_path: Path
    state: str
    prepared_at: str
    mode: str = ""


@dataclass(frozen=True)
class AttemptResult:
    state: str
    mode: str
    product_count: int | None = None
    reason: str = ""


def _validate_binding(run_id: str, vm_name: str, vm_ip: str) -> None:
    import ipaddress

    if not RUN_ID_RE.fullmatch(run_id):
        raise UTM18AttemptError("RUN_ID_INVALID")
    if not VM_NAME_RE.fullmatch(vm_name):
        raise UTM18AttemptError("VM_NAME_INVALID")
    try:
        address = ipaddress.ip_address(vm_ip)
    except ValueError as error:
        raise UTM18AttemptError("VM_IP_INVALID") from error
    if address.version != 4:
        raise UTM18AttemptError("VM_IP_INVALID")


def _payload(attempt: Attempt) -> dict[str, Any]:
    payload = asdict(attempt)
    payload["ledger_path"] = str(attempt.ledger_path)
    return payload


def _write_atomic(path: Path, payload: Mapping[str, Any], *, create: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if create:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    else:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
    os.chmod(path, 0o600)
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    if path.stat().st_mode & 0o777 != 0o600:
        raise UTM18AttemptError("ATTEMPT_LEDGER_MODE_INVALID")


def create_attempt(
    root: Path,
    *,
    run_id: str,
    vm_name: str,
    vm_ip: str,
) -> Attempt:
    _validate_binding(run_id, vm_name, vm_ip)
    attempt_id = uuid.uuid4().hex
    base = f"/Users/{vm_name}/Downloads/utm-18-fill-description-{attempt_id}"
    ledger_path = Path(root) / run_id / f"{attempt_id}.json"
    attempt = Attempt(
        attempt_id=attempt_id,
        run_id=run_id,
        vm_name=vm_name,
        vm_ip=vm_ip,
        log_path=f"{base}.log",
        status_path=f"{base}.log.status",
        command_path=f"{base}.command",
        ledger_path=ledger_path,
        state="prepared",
        prepared_at=datetime.now(timezone.utc).isoformat(),
    )
    _write_atomic(ledger_path, _payload(attempt), create=True)
    return attempt


def _load_attempt(path: Path) -> Attempt:
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
        raise UTM18AttemptError("ATTEMPT_LEDGER_UNSAFE")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        legacy_attempt = "command_path" not in payload
        expected_base = (
            f"/Users/{payload['vm_name']}/Downloads/"
            f"utm-18-fill-description-{payload['attempt_id']}"
        )
        attempt = Attempt(
            attempt_id=payload["attempt_id"],
            run_id=payload["run_id"],
            vm_name=payload["vm_name"],
            vm_ip=payload["vm_ip"],
            log_path=payload["log_path"],
            status_path=payload["status_path"],
            command_path=payload.get("command_path", f"{expected_base}.command"),
            ledger_path=path,
            state=payload["state"],
            prepared_at=payload["prepared_at"],
            mode=(
                payload.get("mode")
                or ("legacy_hidden_ssh" if legacy_attempt else "")
            ),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise UTM18AttemptError("ATTEMPT_LEDGER_INVALID") from error
    _validate_binding(attempt.run_id, attempt.vm_name, attempt.vm_ip)
    if not ATTEMPT_ID_RE.fullmatch(attempt.attempt_id):
        raise UTM18AttemptError("ATTEMPT_ID_INVALID")
    expected_log = (
        f"/Users/{attempt.vm_name}/Downloads/"
        f"utm-18-fill-description-{attempt.attempt_id}.log"
    )
    expected_command = (
        f"/Users/{attempt.vm_name}/Downloads/"
        f"utm-18-fill-description-{attempt.attempt_id}.command"
    )
    if (
        attempt.log_path != expected_log
        or attempt.status_path != f"{expected_log}.status"
        or attempt.command_path != expected_command
    ):
        raise UTM18AttemptError("ATTEMPT_PATH_BINDING_INVALID")
    return attempt


def load_single_attempt(root: Path, run_id: str) -> Attempt | None:
    if not RUN_ID_RE.fullmatch(run_id):
        raise UTM18AttemptError("RUN_ID_INVALID")
    directory = Path(root) / run_id
    if not directory.exists():
        return None
    paths = sorted(directory.glob("*.json"))
    if len(paths) > 1:
        raise UTM18AttemptError("ATTEMPT_LEDGER_NOT_UNIQUE")
    if not paths:
        return None
    return _load_attempt(paths[0])


def mark_attempt(attempt: Attempt, *, state: str, mode: str = "") -> Attempt:
    updated = replace(attempt, state=state, mode=mode)
    _write_atomic(updated.ledger_path, _payload(updated), create=False)
    return updated


def is_retryable_finished_attempt(
    attempt: Attempt,
    *,
    log: str,
    status: str,
    process_running: bool,
) -> bool:
    """Return whether replay is proven safe before any business mutation."""
    if process_running:
        return False
    try:
        values = _parse_status(status, attempt.attempt_id)
    except UTM18AttemptError:
        return False
    known_presave_failure = (
        values.get("REMOTE_NPM_EXIT") == "1"
        and values.get("REMOTE_TEE_EXIT") == "0"
        and any(log.count(signature) == 1 for signature in RETRYABLE_PRESAVE_SIGNATURES)
    )
    interrupted_transport = (
        values.get("REMOTE_NPM_EXIT") == "1"
        and values.get("REMOTE_TEE_EXIT") == "141"
    )
    legacy_finished = (
        attempt.mode == "legacy_hidden_ssh"
        and values.get("RUN_STATE") == "finished"
    )
    return (
        not process_running
        and values.get("RUN_STATE") == "finished"
        and (known_presave_failure or interrupted_transport or legacy_finished)
        and (
            legacy_finished
            or not any(
                signature.casefold() in log.casefold()
                for signature in BUSINESS_ERROR_SIGNATURES
            )
        )
    )


def archive_retryable_attempt(
    attempt: Attempt,
    *,
    log: str,
    status: str,
    process_running: bool,
) -> Path:
    """Archive a finished attempt whose replay is proven safe."""
    if not is_retryable_finished_attempt(
        attempt,
        log=log,
        status=status,
        process_running=process_running,
    ):
        raise UTM18AttemptError("ATTEMPT_NOT_SAFE_TO_ARCHIVE")
    archive_dir = attempt.ledger_path.parent.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive_dir.chmod(0o700)
    archived = archive_dir / attempt.ledger_path.name
    if archived.exists() or archived.is_symlink():
        raise UTM18AttemptError("ATTEMPT_ARCHIVE_CONFLICT")
    os.replace(attempt.ledger_path, archived)
    archived.chmod(0o600)
    return archived


def _parse_status(status: str, expected_attempt_id: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in status.splitlines():
        if not raw_line or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        if key in values:
            raise UTM18AttemptError("ATTEMPT_STATUS_DUPLICATE_KEY")
        values[key] = value
    if values.get("ATTEMPT_ID") != expected_attempt_id:
        raise UTM18AttemptError("ATTEMPT_STATUS_ID_MISMATCH")
    return values


def classify_attempt(
    log: str,
    status: str,
    *,
    process_running: bool,
    expected_attempt_id: str,
) -> AttemptResult:
    values = _parse_status(status, expected_attempt_id)
    if any(signature.casefold() in log.casefold() for signature in BUSINESS_ERROR_SIGNATURES):
        return AttemptResult("blocked_business_error", "business_error")
    counts = [int(match) for match in SUMMARY_RE.findall(log)]
    if COMPLETION_MARKER not in log or len(counts) != 1 or counts[0] < 1:
        details = [f"RUN_STATE={values.get('RUN_STATE', 'missing')}"]
        for key in ("REMOTE_NPM_EXIT", "REMOTE_TEE_EXIT"):
            if key in values:
                details.append(f"{key}={values[key]}")
        if process_running:
            details.append("PROCESS_RUNNING=true")
        raw_lines = log.splitlines()
        lines = [line.strip() for line in raw_lines if line.strip()]
        if lines:
            details.append(f"LAST_LOG_LINE={lines[-1]}")
        error_starts = [
            index
            for index, line in enumerate(raw_lines)
            if ERROR_DETAIL_START_RE.search(line.strip())
        ]
        if error_starts:
            error_detail = "\n".join(raw_lines[error_starts[-1] :]).strip()
            details.append(f"ERROR_DETAIL={error_detail}")
        return AttemptResult(
            "ambiguous",
            "incomplete",
            reason=";".join(details),
        )
    count = counts[0]
    if process_running and values.get("RUN_STATE") in {"running", "completion_observed"}:
        return AttemptResult("verified", "resident_after_summary", count)
    if (
        not process_running
        and values.get("RUN_STATE") == "finished"
        and values.get("REMOTE_NPM_EXIT") == "0"
        and values.get("REMOTE_TEE_EXIT") == "0"
    ):
        return AttemptResult("verified", "exited_zero", count)
    if not process_running and values.get("RUN_STATE") in {"running", "finished"}:
        return AttemptResult(
            "verification_pending", "completed_after_transport_stop", count
        )
    return AttemptResult("ambiguous", "state_mismatch", count)


REMOTE_PRECOMMIT = r'''import os,pathlib,sys
log=pathlib.Path(sys.argv[1]); status=pathlib.Path(sys.argv[2]); attempt_id=sys.argv[3]
command=pathlib.Path(sys.argv[4]); command_text=sys.argv[5]
if any(path.exists() or path.is_symlink() for path in (log,status,command)): raise SystemExit(41)
payloads=((log,b"",0o600),(status,f"ATTEMPT_ID={attempt_id}\nRUN_STATE=prepared\n".encode("utf-8"),0o600),(command,command_text.encode("utf-8"),0o700))
for path,payload,mode in payloads:
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,mode)
    with os.fdopen(fd,"wb") as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    os.chmod(path,mode)
'''


def precommit_remote(
    attempt: Attempt,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    command, environment = build_invocation(
        attempt.vm_name,
        attempt.vm_ip,
        [
            "python3",
            "-B",
            "-c",
            REMOTE_PRECOMMIT,
            attempt.log_path,
            attempt.status_path,
            attempt.attempt_id,
            attempt.command_path,
            _remote_run_script(attempt),
        ],
    )
    result = runner(command, env=environment, capture_output=True, check=False)
    if result.returncode != 0:
        raise UTM18AttemptError("ATTEMPT_REMOTE_PRECOMMIT_FAILED")


def _remote_run_script(attempt: Attempt) -> str:
    return f'''#!/bin/zsh -l
set -o pipefail
attempt_id={attempt.attempt_id!r}
log_path={attempt.log_path!r}
status_path={attempt.status_path!r}
runner_pid=$$
printf 'ATTEMPT_ID=%s\nRUNNER_PID=%s\nRUN_STATE=launching\n' "$attempt_id" "$runner_pid" > "$status_path"
/bin/chmod 600 "$log_path" "$status_path"
if ! cd "$HOME"; then
  printf '%s\n' 'TERMINAL_COMMAND_ERROR=HOME_UNAVAILABLE' | /usr/bin/tee "$log_path"
  printf 'ATTEMPT_ID=%s\nRUNNER_PID=%s\nRUN_STATE=finished\nREMOTE_NPM_EXIT=90\nREMOTE_TEE_EXIT=0\n' "$attempt_id" "$runner_pid" > "$status_path"
  /bin/chmod 600 "$status_path"
  exec /bin/zsh -l
fi
printf 'ATTEMPT_ID=%s\nRUNNER_PID=%s\nRUN_STATE=running\n' "$attempt_id" "$runner_pid" > "$status_path"
cd Downloads/Fire_One_en1.3 && npm run fill:description 2>&1 | /usr/bin/tee "{attempt.log_path}"
statuses=("${{pipestatus[@]}}")
npm_rc="${{statuses[1]}}"; tee_rc="${{statuses[2]}}"
printf 'ATTEMPT_ID=%s\nRUNNER_PID=%s\nRUN_STATE=finished\nREMOTE_NPM_EXIT=%s\nREMOTE_TEE_EXIT=%s\n' "$attempt_id" "$runner_pid" "$npm_rc" "$tee_rc" > "$status_path"
/bin/chmod 600 "$status_path"
exec /bin/zsh -l
'''


def run_remote(
    attempt: Attempt,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    command, environment = build_invocation(
        attempt.vm_name,
        attempt.vm_ip,
        ["/usr/bin/open", "-a", "Terminal", attempt.command_path],
        connect_timeout=8,
    )
    try:
        result = runner(
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise UTM18AttemptError("TERMINAL_LAUNCH_TIMEOUT_SECONDS=30") from error
    if result.returncode != 0:
        detail = str(result.stderr or "").strip()
        suffix = f";{detail}" if detail else ""
        raise UTM18AttemptError(
            f"TERMINAL_LAUNCH_EXIT={result.returncode}{suffix}"
        )
    return int(result.returncode)


REMOTE_INSPECT = r'''import json,pathlib,subprocess,sys
log=pathlib.Path(sys.argv[1]); status=pathlib.Path(sys.argv[2]); expected_attempt_id=sys.argv[3]
if not log.is_file() or log.is_symlink() or not status.is_file() or status.is_symlink(): raise SystemExit(51)
status_text=status.read_text(encoding="utf-8",errors="strict")
values={}
for line in status_text.splitlines():
    if "=" in line:
        key,value=line.split("=",1); values[key]=value
if values.get("ATTEMPT_ID") != expected_attempt_id: raise SystemExit(52)
process=subprocess.run(["/bin/ps","-axo","pid=,ppid=,command="],capture_output=True,text=True,check=True)
parents={}
for line in process.stdout.splitlines():
    parts=line.strip().split(None,2)
    if len(parts)!=3: continue
    parents[int(parts[0])]=int(parts[1])
runner_value=values.get("RUNNER_PID","")
runner_pid=int(runner_value) if runner_value.isdecimal() and int(runner_value)>0 else None
def belongs_to_runner(pid):
    seen=set()
    while pid not in seen:
        if pid == runner_pid: return True
        seen.add(pid)
        if pid not in parents: return False
        pid=parents[pid]
    return False
runner_live=runner_pid is not None and any(belongs_to_runner(pid) for pid in parents)
running=values.get("RUN_STATE") in {"launching","running","completion_observed"} and runner_live
print(json.dumps({"log":log.read_text(encoding="utf-8",errors="replace"),"status":status_text,"process_running":running}))
'''


def inspect_remote(
    attempt: Attempt,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[str, str, bool]:
    command, environment = build_invocation(
        attempt.vm_name,
        attempt.vm_ip,
        [
            "python3",
            "-B",
            "-c",
            REMOTE_INSPECT,
            attempt.log_path,
            attempt.status_path,
            attempt.attempt_id,
        ],
        connect_timeout=8,
    )
    result = runner(
        command,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise UTM18AttemptError("ATTEMPT_REMOTE_INSPECT_FAILED")
    try:
        payload = json.loads(result.stdout)
        return (
            str(payload["log"]),
            str(payload["status"]),
            bool(payload["process_running"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise UTM18AttemptError("ATTEMPT_REMOTE_INSPECT_INVALID") from error
