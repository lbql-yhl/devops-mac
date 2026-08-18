#!/usr/bin/env python3
"""Resolve a no-run submission context without touching Feishu runtime state."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from scripts.utm_clash_ip_target import (
    UTMCTL,
    BoundVM,
    TargetVMError,
    parse_utm_status,
    require_exact_bound_vm,
    resolve_exact_vm_ip,
)
from scripts.vm_inventory import InventoryError
from services.project_paths import PROJECT_ROOT, VM_IMAGES_DIR


INVENTORY_DATABASE = PROJECT_ROOT / "runtime" / "vm-inventory.sqlite3"
VM_NAME_RE = re.compile(r"[a-z]{4}")


class DirectContextError(RuntimeError):
    """Raised when a direct submission target cannot be uniquely proven."""


@dataclass(frozen=True)
class DirectSubmissionContext:
    context_id: str
    parent_title: str
    page_title: str
    app_name: str
    vm_name: str
    vm_ip: str
    vm_user: str
    config_uuid: str
    mac_address: str


def _read_status(vm_name: str) -> str:
    try:
        completed = subprocess.run(
            [UTMCTL, "status", vm_name],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DirectContextError("DIRECT_CONTEXT_STATUS_BLOCKED") from error
    if completed.returncode != 0:
        raise DirectContextError("DIRECT_CONTEXT_STATUS_BLOCKED")
    try:
        return parse_utm_status(completed.stdout)
    except TargetVMError as error:
        raise DirectContextError("DIRECT_CONTEXT_STATUS_BLOCKED") from error


def _clean_identity(value: str, error_code: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned or any(ord(character) < 32 for character in cleaned):
        raise DirectContextError(error_code)
    return cleaned


def resolve_direct_context(
    *,
    parent_title: str | None = None,
    page_title: str,
    vm_name: str,
    vm_ip: str,
    vm_user: str,
    database: Path = INVENTORY_DATABASE,
    images_dir: Path = VM_IMAGES_DIR,
    bound_loader: Callable[..., BoundVM] = require_exact_bound_vm,
    status_loader: Callable[[str], str] = _read_status,
    ip_loader: Callable[[BoundVM], str] = resolve_exact_vm_ip,
) -> DirectSubmissionContext:
    """Bind one Notion page to one exact running inventory VM.

    The stable context ID intentionally excludes mutable IP addresses and host
    paths so all direct-capable skills share one guest/runtime namespace.
    """
    parent_title = _clean_identity(
        parent_title if parent_title is not None else os.getenv("SUBMISSION_HOST_MACHINE", ""),
        "DIRECT_CONTEXT_PARENT_INVALID",
    )
    page_title = _clean_identity(page_title, "DIRECT_CONTEXT_PAGE_INVALID")
    if not VM_NAME_RE.fullmatch(vm_name) or vm_user != vm_name:
        raise DirectContextError("DIRECT_CONTEXT_VM_IDENTITY_INVALID")
    suffix = f"-{vm_name}"
    if not page_title.endswith(suffix) or len(page_title) <= len(suffix):
        raise DirectContextError("DIRECT_CONTEXT_PAGE_VM_MISMATCH")
    app_name = _clean_identity(
        page_title[: -len(suffix)], "DIRECT_CONTEXT_APP_INVALID"
    )
    try:
        requested_ip = str(ipaddress.IPv4Address(vm_ip))
    except ValueError as error:
        raise DirectContextError("DIRECT_CONTEXT_IP_INVALID") from error
    try:
        target = bound_loader(
            Path(database), Path(images_dir), vm_name, app_name
        )
    except (InventoryError, OSError, TargetVMError, ValueError) as error:
        raise DirectContextError("DIRECT_CONTEXT_BINDING_BLOCKED") from error
    if target.vm_name != vm_name:
        raise DirectContextError("DIRECT_CONTEXT_BINDING_BLOCKED")
    try:
        status = status_loader(vm_name)
    except DirectContextError:
        raise
    except Exception as error:
        raise DirectContextError("DIRECT_CONTEXT_STATUS_BLOCKED") from error
    if status not in {"started", "running"}:
        raise DirectContextError("DIRECT_CONTEXT_VM_NOT_RUNNING")
    try:
        registered_ip = str(ipaddress.IPv4Address(ip_loader(target)))
    except (OSError, TargetVMError, ValueError) as error:
        raise DirectContextError("DIRECT_CONTEXT_IP_BLOCKED") from error
    if registered_ip != requested_ip:
        raise DirectContextError("DIRECT_CONTEXT_IP_MISMATCH")
    identity: dict[str, Any] = {
        "schema_version": 1,
        "parent_title": parent_title,
        "page_title": page_title,
        "app_name": app_name,
        "vm_name": vm_name,
        "config_uuid": target.config_uuid.upper(),
        "mac_address": target.mac_address.lower(),
    }
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:24]
    return DirectSubmissionContext(
        context_id=f"direct-v1-{vm_name}-{digest}",
        parent_title=parent_title,
        page_title=page_title,
        app_name=app_name,
        vm_name=vm_name,
        vm_ip=requested_ip,
        vm_user=vm_user,
        config_uuid=target.config_uuid.upper(),
        mac_address=target.mac_address.lower(),
    )
