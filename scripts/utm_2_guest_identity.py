#!/usr/bin/env python3
"""Read UTM-2 guest identity through the already verified SSH channel."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ssh_password import (  # noqa: E402
    password_environment,
    ssh_args as configured_guest_password_ssh_args,
)


MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")


class GuestIdentityError(RuntimeError):
    pass


def normalize_mac(value: str) -> str:
    compact = re.sub(r"[:-]", "", value.strip().lower())
    if not re.fullmatch(r"[0-9a-f]{12}", compact):
        raise GuestIdentityError("invalid MAC")
    return ":".join(compact[i : i + 2] for i in range(0, 12, 2))


@dataclass(frozen=True)
class GuestIdentity:
    serial_number: str
    platform_uuid: str
    mac: str


def _one(pattern: str, text: str, label: str) -> str:
    values = re.findall(pattern, text)
    if len(values) != 1 or not values[0].strip():
        raise GuestIdentityError(f"{label} count={len(values)}")
    return values[0].strip()


def parse_guest_identity(ioreg_output: str, ifconfig_output: str) -> GuestIdentity:
    serial = _one(r'"IOPlatformSerialNumber"\s*=\s*"([^"]+)"', ioreg_output, "IOPlatformSerialNumber")
    platform_uuid = _one(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', ioreg_output, "IOPlatformUUID")
    macs = [normalize_mac(value) for value in re.findall(r"(?:^|\s)ether\s+([0-9A-Fa-f:.-]{17})", ifconfig_output, re.MULTILINE)]
    if len(set(macs)) != 1:
        raise GuestIdentityError(f"guest ether count={len(set(macs))}")
    return GuestIdentity(serial, platform_uuid, macs[0])


def ssh_args(user: str, ip: str) -> list[str]:
    return configured_guest_password_ssh_args(user, ip, connect_timeout=5)


def run(args: argparse.Namespace) -> int:
    ioreg = subprocess.check_output(
        ssh_args(args.vm_user, args.vm_ip)
        + ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        text=True,
        env=password_environment(),
    )
    ifconfig = subprocess.check_output(
        ssh_args(args.vm_user, args.vm_ip) + ["ifconfig"],
        text=True,
        env=password_environment(),
    )
    identity = parse_guest_identity(ioreg, ifconfig)
    if args.config_mac and identity.mac != normalize_mac(args.config_mac):
        raise GuestIdentityError("guest MAC does not match config MAC")
    print("GUEST_IDENTIFIER_LINES=2")
    print("GUEST_MAC_MATCH=verified" if args.config_mac else "GUEST_MAC_MATCH=uncompared")
    print("UTM_2_GUEST_IDENTITY=verified")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Read guest identity for UTM-2")
    parser.add_argument("--vm-ip", required=True)
    parser.add_argument("--vm-user", default="demo")
    parser.add_argument("--config-mac", default="")
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as error:
        print(f"UTM_2_GUEST_IDENTITY=blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
