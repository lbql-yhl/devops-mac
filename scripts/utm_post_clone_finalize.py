#!/usr/bin/env python3
"""Final post-clone audit, guest shutdown, and availability registration."""

from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from scripts.utm_1_ax import normalize_mac
from scripts.utm_post_clone_guest import (
    GuestAutomationError,
    demo_cleanup_script,
    demo_cleanup_verified,
    ensure_ssh,
    enter_guest_desktop,
    guest_shutdown_script,
    parse_key_values,
    read_guest_identity,
    settings_read_script,
    settings_verified,
    ssh_script,
    ssh_sudo_script,
    verify_admin,
    verify_copy,
)
from scripts.vm_inventory import (
    confirm_bundle_present,
    get_record,
    set_available,
    update_guest_mac,
)


WAIT_SECONDS = 3.0
UTMCTL = str(Path(shutil.which("utmctl") or "/opt/homebrew/bin/utmctl").resolve())
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


class FinalizationError(RuntimeError):
    """Raised before database availability can be safely committed."""


def _run(command: list[str]) -> str:
    try:
        return subprocess.run(
            command, text=True, capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stdout", "") or getattr(error, "output", "") or str(error)
        raise FinalizationError(f"command failed: {str(detail).strip()}") from error


def status(vm_name: str) -> str:
    values = [line.strip().lower() for line in _run([UTMCTL, "status", vm_name]).splitlines() if line.strip()]
    if len(values) != 1 or values[0] not in {"started", "running", "stopped", "suspended"}:
        raise FinalizationError(f"UTM_STATUS_AMBIGUOUS={values!r}")
    return values[0]


def ensure_started(vm_name: str) -> None:
    current = status(vm_name)
    if current == "stopped":
        _run([UTMCTL, "start", vm_name])
    elif current not in {"started", "running"}:
        raise FinalizationError(f"FINAL_TARGET_NOT_STARTABLE={current}")
    reads = []
    for _ in range(3):
        reads.append(status(vm_name))
        time.sleep(WAIT_SECONDS)
    if any(value not in {"started", "running"} for value in reads):
        raise FinalizationError(f"FINAL_STARTED_READBACK_FAILED={reads}")


def ensure_stopped(vm_name: str) -> None:
    reads: list[str] = []
    for index in range(8):
        reads.append(status(vm_name))
        if len(reads) >= 2 and reads[-2:] == ["stopped", "stopped"]:
            return
        if index < 7:
            time.sleep(WAIT_SECONDS)
    raise FinalizationError(f"FINAL_STOPPED_READBACK_FAILED={reads}")


def config_identity(bundle: Path) -> tuple[str, str]:
    try:
        data = plistlib.loads((bundle / "config.plist").read_bytes())
        config_uuid = str(data["Information"]["UUID"]).upper()
        network = data["Network"]
        if not isinstance(network, list) or len(network) != 1:
            raise ValueError("Network count is not one")
        return config_uuid, normalize_mac(str(network[0]["MacAddress"]))
    except Exception as error:
        raise FinalizationError(f"FINAL_CONFIG_IDENTITY_INVALID={error}") from error


def _ip_candidates(vm_name: str) -> list[str]:
    result = subprocess.run(
        [UTMCTL, "ip-address", vm_name], text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        return []
    return sorted(set(IP_RE.findall(result.stdout)))


def ping_until_ready(ip: str) -> None:
    """Require one successful ping across the fixed 0/3/5-second rounds."""
    return_codes: list[int] = []
    for delay in (0.0, 3.0, 5.0):
        if delay:
            time.sleep(delay)
        result = subprocess.run(
            ["/sbin/ping", "-c", "1", "-W", "1000", ip],
            text=True,
            capture_output=True,
            check=False,
        )
        return_codes.append(result.returncode)
        if result.returncode == 0:
            return
    raise FinalizationError(f"PING_READINESS_FAILED={ip} RETURN_CODES={return_codes}")


def resolve_ip(vm_name: str, config_mac: str) -> str:
    wanted = normalize_mac(config_mac)
    for attempt, delay in enumerate((0.0, 2.0, 5.0)):
        if delay:
            time.sleep(delay)
        utm_ips = _ip_candidates(vm_name)
        arp_output = _run(["/usr/sbin/arp", "-an"])
        arp_ips = []
        for ip, mac in re.findall(r"\(([^)]+)\)\s+at\s+([0-9A-Fa-f:.-]+)", arp_output):
            try:
                padded = ":".join(part.zfill(2) for part in mac.split(":"))
                if IP_RE.fullmatch(ip) and normalize_mac(padded) == wanted:
                    arp_ips.append(ip)
            except ValueError:
                continue
        candidates = set(utm_ips) & set(arp_ips) if utm_ips else set(arp_ips)
        if len(candidates) == 1:
            ip = next(iter(candidates))
            ping_until_ready(ip)
            return ip
        if attempt == 2:
            raise FinalizationError(
                f"FINAL_IP_MATCH_COUNT={len(candidates)} UTM_IPS={utm_ips} ARP_IPS={arp_ips}"
            )
    raise FinalizationError("FINAL_IP_RESOLUTION_EXHAUSTED")


def finalize(
    *,
    vm_name: str,
    bundle: Path,
    expected_config_uuid: str,
    database: Path,
) -> dict[str, Any]:
    """Run the complete final gate and mark available only after shutdown."""
    bundle = Path(bundle).resolve()
    database = Path(database).resolve()
    config_uuid, config_mac = config_identity(bundle)
    if config_uuid != expected_config_uuid.upper():
        raise FinalizationError("FINAL_CONFIG_UUID_MISMATCH")
    record = get_record(database, vm_name)
    if str(record.get("bundle_path") or "") != str(bundle):
        raise FinalizationError("FINAL_DATABASE_BUNDLE_MISMATCH")
    if str(record.get("config_uuid") or "").upper() != config_uuid:
        raise FinalizationError("FINAL_DATABASE_UUID_MISMATCH")
    ensure_started(vm_name)
    ip = resolve_ip(vm_name, config_mac)
    ensure_ssh(vm_name, ip)
    desktop = enter_guest_desktop(vm_name, ip, vm_name, bundle)
    if desktop.get("FINDER") != "ready":
        raise FinalizationError(f"FINAL_GUEST_DESKTOP_NOT_READY={desktop}")
    verify_admin(vm_name, ip)
    identity = read_guest_identity(vm_name, ip, config_mac)
    if identity.mac != config_mac:
        raise FinalizationError("FINAL_GUEST_MAC_MISMATCH")
    if (
        identity.serial_number != record.get("guest_serial_number")
        or identity.platform_uuid.upper()
        != str(record.get("guest_platform_uuid") or "").upper()
    ):
        raise FinalizationError("FINAL_GUEST_SERIAL_OR_UUID_MISMATCH")
    cleanup = ssh_sudo_script(vm_name, ip, demo_cleanup_script())
    if not demo_cleanup_verified(cleanup):
        raise FinalizationError("FINAL_DEMO_CLEANUP_NOT_VERIFIED")
    demo_state = ssh_script(
        vm_name,
        ip,
        "if /usr/bin/id demo >/dev/null 2>&1 || /usr/bin/dscl . -read /Users/demo >/dev/null 2>&1 || [ -e /Users/demo ] || [ -L /Users/demo ]; then echo DEMO_PRESENT; exit 1; else echo DEMO_ABSENT; fi\n",
    )
    if "DEMO_ABSENT" not in demo_state:
        raise FinalizationError("FINAL_DEMO_STATE_NOT_ABSENT")
    settings = parse_key_values(ssh_sudo_script(vm_name, ip, settings_read_script()))
    if not settings_verified(settings):
        raise FinalizationError(f"FINAL_SYSTEM_SETTINGS_FAILED={settings}")
    ssh_script(
        vm_name,
        ip,
        "test -d '/Volumes/My Shared Files/共享文件' -a ! -L '/Volumes/My Shared Files/共享文件'\n",
    )
    verify_copy(vm_name, ip)
    try:
        ssh_sudo_script(vm_name, ip, guest_shutdown_script())
    except GuestAutomationError:
        # SSH normally closes before the shutdown command returns.  Only the
        # independent UTM state read below is authoritative.
        pass
    ensure_stopped(vm_name)
    # No inventory mutation is allowed until the guest-side audit has passed
    # and two independent UTM reads have proved the VM is stopped.
    update_guest_mac(database, vm_name, identity.mac)
    confirm_bundle_present(database, vm_name, bundle)
    set_available(database, vm_name, True)
    final_record = get_record(database, vm_name)
    if (
        int(final_record.get("available") or 0) != 1
        or int(final_record.get("directory_present") or 0) != 1
        or final_record.get("status") != "complete"
    ):
        raise FinalizationError("FINAL_DATABASE_AVAILABLE_READBACK_FAILED")
    return {
        "ip": ip,
        "desktop": "verified",
        "shutdown": "verified",
        "guest_serial": identity.serial_number,
        "guest_uuid": identity.platform_uuid,
        "guest_mac": identity.mac,
        "settings": "verified",
        "sharing": "verified",
        "available": "1",
    }
