#!/usr/bin/env python3
"""Exact inventory, power, and IP guards for ``utm-clash-ip``.

This module is intentionally command-only.  It never opens UTM or selects a
VM in the GUI; lifecycle commands always carry the already validated exact
four-letter inventory name.
"""

from __future__ import annotations

import ipaddress
import plistlib
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from scripts.vm_inventory import InventoryError, get_record, normalize_guest_mac


NAME_RE = re.compile(r"^[a-z]{4}$")
UUID_RE = re.compile(
    r"^[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}$"
)
IP_RE = re.compile(
    r"(?<![0-9])(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])"
    r"(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}(?![0-9])"
)
UTMCTL = str(Path(shutil.which("utmctl") or "/opt/homebrew/bin/utmctl").resolve())


class TargetVMError(RuntimeError):
    """Raised when the exact inventory VM cannot be proven safe to operate."""


@dataclass(frozen=True)
class BoundVM:
    vm_name: str
    bundle_path: Path
    config_uuid: str
    mac_address: str


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=20,
            check=True,
        )
    except subprocess.TimeoutExpired as error:
        raise TargetVMError("exact VM command timed out") from error
    except (OSError, subprocess.CalledProcessError) as error:
        raise TargetVMError("exact VM command failed") from error


def _config_identity(bundle: Path) -> tuple[str, str]:
    config = bundle / "config.plist"
    if config.is_symlink() or not config.is_file():
        raise TargetVMError("exact VM config is missing or unsafe")
    try:
        payload = plistlib.loads(config.read_bytes())
        config_uuid = str(payload["Information"]["UUID"]).upper()
        network = payload["Network"]
        if not isinstance(network, list) or len(network) != 1:
            raise ValueError("network count is not one")
        mac_address = normalize_guest_mac(str(network[0]["MacAddress"]))
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise TargetVMError("exact VM config identity is invalid") from error
    if not UUID_RE.fullmatch(config_uuid):
        raise TargetVMError("exact VM config UUID is invalid")
    return config_uuid, mac_address


def require_exact_bound_vm(
    database: Path,
    images_dir: Path,
    vm_name: str,
    application_name: str,
) -> BoundVM:
    """Require one complete present bundle bound to the requested application."""
    if not NAME_RE.fullmatch(vm_name):
        raise TargetVMError("exact VM name must contain four lowercase letters")
    if not application_name.strip():
        raise TargetVMError("application name is required")
    database = Path(database).expanduser()
    if database.is_symlink() or not database.is_file():
        raise TargetVMError("inventory database is missing or unsafe")
    images_dir = Path(images_dir).expanduser()
    if images_dir.is_symlink() or not images_dir.is_dir():
        raise TargetVMError("VM images directory is missing or unsafe")
    images_dir = images_dir.resolve()
    record = get_record(database.resolve(), vm_name)
    if record.get("application_name") != application_name:
        raise TargetVMError("exact VM application binding does not match")
    if (
        record.get("status") != "complete"
        or int(record.get("directory_present") or 0) != 1
        or int(record.get("reusable") or 0) != 0
        or int(record.get("available") or 0) != 0
    ):
        raise TargetVMError("exact VM inventory state is not bound and complete")
    expected_bundle = (images_dir / f"{vm_name}.utm").resolve()
    registered_bundle = Path(str(record.get("bundle_path") or "")).expanduser()
    if registered_bundle.is_symlink() or registered_bundle.resolve() != expected_bundle:
        raise TargetVMError("exact VM bundle path does not match inventory")
    if expected_bundle.is_symlink() or not expected_bundle.is_dir():
        raise TargetVMError("exact VM bundle is missing or unsafe")
    config_uuid, mac_address = _config_identity(expected_bundle)
    if config_uuid != str(record.get("config_uuid") or "").upper():
        raise TargetVMError("exact VM config UUID does not match inventory")
    if mac_address != normalize_guest_mac(str(record.get("mac_address") or "")):
        raise TargetVMError("exact VM config MAC does not match inventory")
    return BoundVM(vm_name, expected_bundle, config_uuid, mac_address)


def parse_utm_status(output: str) -> str:
    values = [line.strip().lower() for line in output.splitlines() if line.strip()]
    if len(values) != 1 or values[0] not in {
        "started",
        "running",
        "stopped",
        "suspended",
    }:
        raise TargetVMError("exact VM status is ambiguous")
    return values[0]


def _status(vm_name: str, run_command: CommandRunner) -> str:
    result = run_command([UTMCTL, "status", vm_name])
    if result.returncode != 0:
        raise TargetVMError("exact VM status command failed")
    return parse_utm_status(result.stdout)


def ensure_exact_vm_started(
    vm_name: str,
    *,
    run_command: CommandRunner = _run_command,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Start only an explicitly named stopped VM and verify three fresh reads.

    Returns ``True`` when this call issued the one permitted start command and
    ``False`` when the target was already running.
    """
    if not NAME_RE.fullmatch(vm_name):
        raise TargetVMError("exact VM name must contain four lowercase letters")
    current = _status(vm_name, run_command)
    started_here = False
    if current == "stopped":
        result = run_command([UTMCTL, "start", vm_name])
        if result.returncode != 0:
            raise TargetVMError("exact VM start command failed")
        started_here = True
    elif current not in {"started", "running"}:
        raise TargetVMError(f"exact VM is not safely startable: {current}")
    reads: list[str] = []
    for index in range(3):
        if index:
            sleep_fn(3.0)
        reads.append(_status(vm_name, run_command))
    if any(value not in {"started", "running"} for value in reads):
        raise TargetVMError("exact VM running-state readback failed")
    return started_here


def _arp_ips_for_mac(output: str, wanted_mac: str) -> set[str]:
    matches: set[str] = set()
    for ip_text, mac_text in re.findall(
        r"\(([^)]+)\)\s+at\s+([0-9A-Fa-f:.-]+)", output
    ):
        try:
            padded = ":".join(part.zfill(2) for part in mac_text.split(":"))
            ip_value = str(ipaddress.IPv4Address(ip_text))
            if normalize_guest_mac(padded) == wanted_mac:
                matches.add(ip_value)
        except (InventoryError, ValueError):
            continue
    return matches


def resolve_exact_vm_ip(
    target: BoundVM,
    *,
    run_command: CommandRunner = _run_command,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    """Resolve one IP jointly identified by exact ``utmctl`` name and MAC."""
    for delay in (0.0, 5.0, 10.0):
        if delay:
            sleep_fn(delay)
        ip_result = run_command([UTMCTL, "ip-address", target.vm_name])
        utm_ips = {
            str(ipaddress.IPv4Address(value))
            for value in IP_RE.findall(ip_result.stdout if ip_result.returncode == 0 else "")
        }
        for candidate in sorted(utm_ips):
            subprocess.run(
                ["/sbin/ping", "-c", "1", "-W", "1000", candidate],
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
        arp_result = run_command(["/usr/sbin/arp", "-an"])
        arp_ips = _arp_ips_for_mac(arp_result.stdout, target.mac_address)
        matches = sorted(utm_ips & arp_ips) if utm_ips else sorted(arp_ips)
        if len(matches) == 1:
            return matches[0]
    raise TargetVMError("exact VM IP could not be uniquely matched by name and MAC")
