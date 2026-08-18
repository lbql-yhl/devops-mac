#!/usr/bin/env python3
"""Durable, non-sensitive state ledger for resumable UTM-1 execution."""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PHASES = (
    "handoff_verified",
    "sharing_verified",
    "network_verified",
    "running_verified",
    "login_verified",
)
IDENTITY_FIELDS = (
    "run_id",
    "vm_name",
    "bundle",
    "config_uuid",
    "share_path",
    "clone_marker_sha256",
)


class StateError(RuntimeError):
    """Raised when a UTM-1 attempt cannot safely be reused."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_directory(value: str, field: str) -> str:
    path = Path(value)
    if not path.is_dir() or path.is_symlink():
        raise StateError(f"UTM_1_{field.upper()}_INVALID")
    return str(path.resolve())


def normalize_context(context: dict[str, Any]) -> dict[str, str]:
    run_id = str(context.get("run_id") or "")
    vm_name = str(context.get("vm_name") or "")
    config_uuid = str(context.get("config_uuid") or "").lower()
    marker = str(context.get("clone_marker_sha256") or "").lower()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,127}", run_id):
        raise StateError("UTM_1_RUN_ID_INVALID")
    if not re.fullmatch(r"[a-z]{4}", vm_name):
        raise StateError("UTM_1_VM_NAME_INVALID")
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", config_uuid):
        raise StateError("UTM_1_CONFIG_UUID_INVALID")
    if not re.fullmatch(r"[0-9a-f]{64}", marker):
        raise StateError("UTM_1_CLONE_MARKER_SHA256_INVALID")
    return {
        "run_id": run_id,
        "vm_name": vm_name,
        "bundle": _safe_directory(str(context.get("bundle") or ""), "bundle"),
        "config_uuid": config_uuid,
        "share_path": _safe_directory(str(context.get("share_path") or ""), "share_path"),
        "clone_marker_sha256": marker,
    }


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise StateError("UTM_1_ATTEMPT_FILE_INVALID")
    if path.stat().st_mode & 0o777 != 0o600:
        raise StateError("UTM_1_ATTEMPT_MODE_INVALID")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StateError("UTM_1_ATTEMPT_JSON_INVALID") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise StateError("UTM_1_ATTEMPT_SCHEMA_INVALID")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise StateError("UTM_1_ATTEMPT_DIRECTORY_INVALID")
    os.chmod(path.parent, 0o700)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4()}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
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


def _require_identity(state: dict[str, Any], context: dict[str, str]) -> None:
    for field in IDENTITY_FIELDS:
        if state.get(field) != context[field]:
            raise StateError(f"UTM_1_ATTEMPT_IDENTITY_MISMATCH={field}")


def prepare_attempt(path: Path, context: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_context(context)
    if path.exists() or path.is_symlink():
        state = _read(path)
        _require_identity(state, normalized)
        return state
    state: dict[str, Any] = {
        "schema_version": 1,
        "attempt_id": str(uuid.uuid4()),
        **normalized,
        "created_at": now(),
        "phases": {},
    }
    _write(path, state)
    return _read(path)


def next_phase(state: dict[str, Any]) -> str | None:
    phases = state.get("phases")
    if not isinstance(phases, dict):
        raise StateError("UTM_1_ATTEMPT_PHASES_INVALID")
    for phase in PHASES:
        if phase not in phases:
            return phase
    return None


def record_phase(path: Path, context: dict[str, Any], phase: str, evidence: dict[str, str]) -> dict[str, Any]:
    if phase not in PHASES:
        raise StateError("UTM_1_PHASE_INVALID")
    if not evidence or any(not isinstance(key, str) or not isinstance(value, str) or not value for key, value in evidence.items()):
        raise StateError("UTM_1_PHASE_EVIDENCE_INVALID")
    normalized = normalize_context(context)
    state = _read(path)
    _require_identity(state, normalized)
    phases = state["phases"]
    if phase in phases:
        return state
    expected = next_phase(state)
    if phase != expected:
        raise StateError(f"UTM_1_PHASE_OUT_OF_ORDER={phase}")
    phases[phase] = {"verified_at": now(), "evidence": evidence}
    state["updated_at"] = now()
    _write(path, state)
    return _read(path)


def _context_from_args(args: argparse.Namespace) -> dict[str, str]:
    return {
        "run_id": args.run_id,
        "vm_name": args.vm_name,
        "bundle": args.bundle,
        "config_uuid": args.config_uuid,
        "share_path": args.share_path,
        "clone_marker_sha256": args.clone_marker_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "record", "next"))
    parser.add_argument("--attempt-file", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--vm-name")
    parser.add_argument("--bundle")
    parser.add_argument("--config-uuid")
    parser.add_argument("--share-path")
    parser.add_argument("--clone-marker-sha256")
    parser.add_argument("--phase", choices=PHASES)
    parser.add_argument("--evidence-json")
    args = parser.parse_args()
    path = Path(args.attempt_file)
    if args.action == "next":
        phase = next_phase(_read(path))
        print(f"UTM_1_NEXT_PHASE={phase or 'complete'}")
        return 0
    required = (args.run_id, args.vm_name, args.bundle, args.config_uuid, args.share_path, args.clone_marker_sha256)
    if not all(required):
        raise StateError("UTM_1_CONTEXT_ARGUMENTS_MISSING")
    context = _context_from_args(args)
    if args.action == "prepare":
        state = prepare_attempt(path, context)
        print(f"UTM_1_ATTEMPT_ID={state['attempt_id']}")
        print("UTM_1_ATTEMPT_MODE=600")
        return 0
    if not args.phase or not args.evidence_json:
        raise StateError("UTM_1_RECORD_ARGUMENTS_MISSING")
    evidence = json.loads(args.evidence_json)
    if not isinstance(evidence, dict):
        raise StateError("UTM_1_PHASE_EVIDENCE_INVALID")
    record_phase(path, context, args.phase, evidence)
    print(f"UTM_1_PHASE={args.phase}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StateError as error:
        print(str(error))
        raise SystemExit(2)
