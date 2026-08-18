#!/usr/bin/env python3
"""One-command UTM-22 Archive validation, IPA packaging, and API upload.

The public entry accepts either one existing workflow run or one explicit
``<application>-<vm>`` target.  It never reads a source workspace and never
starts, stops, resumes, or switches a VM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


PROJECT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_SOURCE_ROOT))

from scripts.ssh_password import (  # noqa: E402
    password_environment,
    scp_args as configured_guest_password_scp_args,
    ssh_args as configured_guest_password_ssh_args,
)
from scripts.utm_clash_ip_target import (  # noqa: E402
    require_exact_bound_vm,
    resolve_exact_vm_ip,
)
from services.project_paths import PROJECT_ROOT, VM_IMAGES_DIR  # noqa: E402


INVENTORY_DATABASE = PROJECT_ROOT / "runtime" / "vm-inventory.sqlite3"
RUNS_FILE = PROJECT_ROOT / "runtime" / "feishu-runs.json"
DISTRIBUTOR_SOURCE = PROJECT_ROOT / "scripts" / "utm_22_distribute.mjs"
VM_NAME_RE = re.compile(r"^[a-z]{4}$")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
BUILD_RE = re.compile(r"^[1-9][0-9]*$")


class UploadTargetError(RuntimeError):
    """Raised when the upload target cannot be uniquely and safely resolved."""


@dataclass(frozen=True)
class UploadTarget:
    app_name: str
    vm_name: str
    attempt_owner: str


def _safe_app_name(value: str) -> str:
    value = str(value)
    if (
        not value
        or value in {".", ".."}
        or Path(value).name != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise UploadTargetError("APPLICATION_NAME_INVALID")
    return value


def resolve_run_target(
    payload: Mapping[str, Any], run_id: str, local_host: str
) -> UploadTarget:
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise UploadTargetError("RUNS_PAYLOAD_INVALID")
    matches = [run for run in runs if str(run.get("id") or "") == run_id]
    if len(matches) != 1:
        raise UploadTargetError(f"RUN_MATCH_COUNT={len(matches)}")
    run = matches[0]
    data = run.get("submission_data") or {}
    app_name = _safe_app_name(str(run.get("app_name") or ""))
    vm_name = str(run.get("vm_name") or "")
    run_host = str(data.get("host_machine") or run.get("host_machine") or "").strip()
    if not local_host or run_host != local_host:
        raise UploadTargetError("RUN_HOST_OWNERSHIP_MISMATCH")
    if str(data.get("app_name") or "") != app_name:
        raise UploadTargetError("RUN_APPLICATION_IDENTITY_MISMATCH")
    if not VM_NAME_RE.fullmatch(vm_name):
        raise UploadTargetError("RUN_VM_NAME_INVALID")
    return UploadTarget(app_name, vm_name, run_id)


def resolve_manual_target(database: Path, target: str) -> UploadTarget:
    match = re.fullmatch(r"(.+)-([a-z]{4})", str(target))
    if not match:
        raise UploadTargetError("TARGET_FORMAT_INVALID")
    app_name = _safe_app_name(match.group(1))
    vm_name = match.group(2)
    database = Path(database).expanduser()
    if database.is_symlink() or not database.is_file():
        raise UploadTargetError("INVENTORY_DATABASE_UNSAFE")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT vm_name, application_name
              FROM vm_inventory
             WHERE vm_name=? AND application_name=? AND status='complete'
               AND directory_present=1 AND reusable=0 AND available=0
            """,
            (vm_name, app_name),
        ).fetchall()
    if len(rows) != 1:
        raise UploadTargetError(f"TARGET_MATCH_COUNT={len(rows)}")
    return UploadTarget(app_name, vm_name, f"manual:{app_name}-{vm_name}")


def choose_archive(
    candidates: Iterable[Mapping[str, str]],
    app_name: str,
    bundle_id: str,
    archive_path: str | None = None,
) -> dict[str, str]:
    matches = [
        dict(candidate)
        for candidate in candidates
        if candidate.get("bundle_id") == bundle_id
        and (
            (archive_path is None and candidate.get("app_name") == app_name)
            or (archive_path is not None and candidate.get("path") == archive_path)
        )
    ]
    if len(matches) != 1:
        raise UploadTargetError(f"ARCHIVE_MATCH_COUNT={len(matches)}")
    selected = matches[0]
    if not VERSION_RE.fullmatch(str(selected.get("version") or "")):
        raise UploadTargetError("ARCHIVE_VERSION_INVALID")
    if not BUILD_RE.fullmatch(str(selected.get("build") or "")):
        raise UploadTargetError("ARCHIVE_BUILD_INVALID")
    return selected


def ssh_recovery_action(
    returncode: int, *, attempt_exists: bool, remote_process_running: bool
) -> str:
    if returncode != 255:
        return "stop"
    if remote_process_running:
        return "wait_same_process"
    if attempt_exists:
        return "resume_attempt"
    return "prove_not_executed"


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise UploadTargetError(f"JSON_FILE_UNSAFE={path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise UploadTargetError(f"JSON_PAYLOAD_INVALID={path}")
    return payload


def _ssh_command(user: str, ip: str, remote_args: list[str]) -> list[str]:
    base = configured_guest_password_ssh_args(user, ip, connect_timeout=5)
    return [
        *base[:-1],
        "-o",
        "ServerAliveInterval=3",
        "-o",
        "ServerAliveCountMax=2",
        base[-1],
        shlex.join(remote_args),
    ]


def _run_ssh(
    user: str,
    ip: str,
    remote_args: list[str],
    *,
    timeout: int = 20,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _ssh_command(user, ip, remote_args),
        env=password_environment(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _install_distributor(user: str, ip: str) -> str:
    if DISTRIBUTOR_SOURCE.is_symlink() or not DISTRIBUTOR_SOURCE.is_file():
        raise UploadTargetError("DISTRIBUTOR_SOURCE_UNSAFE")
    remote_path = f"/Users/{user}/Downloads/utm_22_distribute.mjs"
    expected_hash = _sha256(DISTRIBUTOR_SOURCE)
    before = _run_ssh(user, ip, ["/usr/bin/shasum", "-a", "256", remote_path])
    current_hash = before.stdout.split()[0] if before.returncode == 0 and before.stdout.split() else ""
    if current_hash != expected_hash:
        copied = subprocess.run(
            configured_guest_password_scp_args(
                user, ip, DISTRIBUTOR_SOURCE, remote_path
            ),
            env=password_environment(),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if copied.returncode != 0:
            raise UploadTargetError("DISTRIBUTOR_COPY_FAILED")
    after = _run_ssh(user, ip, ["/usr/bin/shasum", "-a", "256", remote_path])
    final_hash = after.stdout.split()[0] if after.returncode == 0 and after.stdout.split() else ""
    if final_hash != expected_hash:
        raise UploadTargetError("DISTRIBUTOR_SHA256_MISMATCH")
    return remote_path


def _discover_context(
    target: UploadTarget,
    ip: str,
    distributor: str,
    archive_path: str | None = None,
) -> tuple[dict[str, str], str]:
    archive_root = f"/Users/{target.vm_name}/Library/Developer/Xcode/Archives"
    config_path = f"/Users/{target.vm_name}/Downloads/apple-store-bm/config/prod.yml"
    result = _run_ssh(
        target.vm_name,
        ip,
        [
            "/usr/local/bin/node",
            distributor,
            "discover",
            "--archive-root",
            archive_root,
            "--app-name",
            target.app_name,
            "--config",
            config_path,
        ],
        timeout=30,
    )
    if result.returncode != 0:
        raise UploadTargetError(
            f"ARCHIVE_DISCOVERY_FAILED={result.stderr.strip() or result.stdout.strip()}"
        )
    marker = "ARCHIVE_CANDIDATES_JSON="
    lines = [line for line in result.stdout.splitlines() if line.startswith(marker)]
    if len(lines) != 1:
        raise UploadTargetError("ARCHIVE_DISCOVERY_OUTPUT_INVALID")
    candidates = json.loads(lines[0][len(marker) :])
    if not isinstance(candidates, list):
        raise UploadTargetError("ARCHIVE_CANDIDATES_INVALID")
    bundle_lines = [
        line for line in result.stdout.splitlines() if line.startswith("CONFIG_BUNDLE_ID=")
    ]
    if len(bundle_lines) != 1:
        raise UploadTargetError("CONFIG_BUNDLE_OUTPUT_INVALID")
    bundle_id = bundle_lines[0].split("=", 1)[1]
    return choose_archive(
        candidates,
        target.app_name,
        bundle_id,
        archive_path=archive_path,
    ), config_path


def _attempt_paths(target: UploadTarget, archive: Mapping[str, str]) -> tuple[str, str]:
    owner_hash = hashlib.sha256(target.attempt_owner.encode("utf-8")).hexdigest()[:16]
    version = str(archive["version"])
    build = str(archive["build"])
    base = f"/Users/{target.vm_name}/Downloads"
    output = f"{base}/{target.app_name}-upload-{version}-{build}-{owner_hash}.ipa"
    attempt = f"{base}/utm-22-{owner_hash}-{version}-{build}.upload-attempt.json"
    return output, attempt


def _probe_attempt(
    target: UploadTarget, ip: str, attempt_path: str
) -> tuple[bool, bool]:
    probe_code = (
        "import json,os,subprocess,sys;"
        "p=sys.argv[1];"
        "r=subprocess.run(['/usr/bin/pgrep','-f',p],text=True,capture_output=True);"
        "alive=any(int(x)!=os.getpid() for x in r.stdout.split() if x.isdigit());"
        "print(json.dumps({'attempt_exists':os.path.isfile(p),'process_running':alive}))"
    )
    result = _run_ssh(
        target.vm_name,
        ip,
        ["/usr/bin/python3", "-c", probe_code, attempt_path],
        timeout=12,
    )
    if result.returncode != 0:
        raise UploadTargetError("UPLOAD_ATTEMPT_PROBE_FAILED")
    payload = json.loads(result.stdout)
    return bool(payload["attempt_exists"]), bool(payload["process_running"])


def _run_upload(
    target: UploadTarget,
    ip: str,
    distributor: str,
    archive: Mapping[str, str],
    config_path: str,
    wait_seconds: int,
) -> None:
    output_path, attempt_path = _attempt_paths(target, archive)
    remote_args = [
        "/usr/bin/env",
        "NODE_USE_ENV_PROXY=1",
        "/usr/local/bin/node",
        distributor,
        "upload-config",
        "--archive",
        str(archive["path"]),
        "--output",
        output_path,
        "--config",
        config_path,
        "--attempt-file",
        attempt_path,
        "--wait-seconds",
        str(wait_seconds),
    ]
    combined_output = ""
    for recovery_round in range(1, 4):
        result = subprocess.run(
            _ssh_command(target.vm_name, ip, remote_args),
            env=password_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=wait_seconds + 120,
            check=False,
        )
        combined_output += result.stdout or ""
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode == 0:
            success = (
                "BUILD_UPLOAD_FINAL_STATE=COMPLETE" in combined_output
                and "BUILD_PROCESSING_STATE=VALID" in combined_output
            ) or "BUILD_ALREADY_EXISTS=valid" in combined_output
            if not success:
                raise UploadTargetError("UPLOAD_SUCCESS_MARKERS_MISSING")
            print("UTM_22_UPLOAD=verified")
            return
        if result.returncode != 255:
            raise UploadTargetError(f"UPLOAD_SCRIPT_EXIT={result.returncode}")
        attempt_exists, process_running = _probe_attempt(target, ip, attempt_path)
        action = ssh_recovery_action(
            result.returncode,
            attempt_exists=attempt_exists,
            remote_process_running=process_running,
        )
        if action == "wait_same_process":
            for delay in (5, 10, 20):
                time.sleep(delay)
                attempt_exists, process_running = _probe_attempt(target, ip, attempt_path)
                if not process_running:
                    break
            if process_running:
                raise UploadTargetError("UPLOAD_PROCESS_STILL_RUNNING")
        if recovery_round == 3:
            raise UploadTargetError("SSH_UPLOAD_RECOVERY_EXHAUSTED")
    raise UploadTargetError("SSH_UPLOAD_RECOVERY_EXHAUSTED")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate and upload one exact UTM Xcode Archive without reading source code."
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--run-id")
    selector.add_argument("--target", help="Exact <application>-<four-letter-vm> identity")
    parser.add_argument(
        "--archive-path",
        help="Exact discovered guest .xcarchive path; manual --target mode only",
    )
    parser.add_argument("--wait-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    if args.archive_path and not args.target:
        parser.error("--archive-path requires --target")
    if args.wait_seconds < 120 or args.wait_seconds > 3600:
        parser.error("--wait-seconds must be between 120 and 3600")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.run_id:
            local_host = os.getenv("SUBMISSION_HOST_MACHINE", "").strip()
            target = resolve_run_target(_read_json(RUNS_FILE), args.run_id, local_host)
        else:
            target = resolve_manual_target(INVENTORY_DATABASE, args.target)
        bound = require_exact_bound_vm(
            INVENTORY_DATABASE,
            VM_IMAGES_DIR,
            target.vm_name,
            target.app_name,
        )
        ip = resolve_exact_vm_ip(bound)
        identity = _run_ssh(target.vm_name, ip, ["/usr/bin/id", "-un"], timeout=12)
        if identity.returncode != 0 or identity.stdout.strip() != target.vm_name:
            raise UploadTargetError("SSH_IDENTITY_MISMATCH")
        distributor = _install_distributor(target.vm_name, ip)
        archive, config_path = _discover_context(
            target,
            ip,
            distributor,
            archive_path=args.archive_path,
        )
        _run_upload(
            target,
            ip,
            distributor,
            archive,
            config_path,
            args.wait_seconds,
        )
        return 0
    except (OSError, ValueError, json.JSONDecodeError, UploadTargetError) as error:
        print(f"UTM_22_UPLOAD_ERROR={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
