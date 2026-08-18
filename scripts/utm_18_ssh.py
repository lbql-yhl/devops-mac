#!/usr/bin/env python3
"""Run one UTM-18 SSH operation through the configured-password transport."""

from __future__ import annotations

import argparse
import ipaddress
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence


PROJECT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_SOURCE_ROOT))

from scripts.ssh_password import (  # noqa: E402
    password_environment,
    ssh_args,
)


class UTM18SSHError(ValueError):
    """Raised when a UTM-18 SSH target or command is unsafe."""


def build_invocation(
    vm_name: str,
    vm_ip: str,
    remote_command: Sequence[str],
    *,
    connect_timeout: int = 5,
) -> tuple[list[str], Mapping[str, str]]:
    if not re.fullmatch(r"[a-z]{4}", vm_name):
        raise UTM18SSHError("VM_NAME_INVALID")
    try:
        ipaddress.IPv4Address(vm_ip)
    except ipaddress.AddressValueError as exc:
        raise UTM18SSHError("VM_IP_INVALID") from exc
    if not remote_command or any("\x00" in value for value in remote_command):
        raise UTM18SSHError("REMOTE_COMMAND_INVALID")
    return (
        ssh_args(vm_name, vm_ip, connect_timeout=connect_timeout)
        + [shlex.join(remote_command)],
        password_environment(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one UTM-18 command using the configured guest password."
    )
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-ip", required=True)
    parser.add_argument("--connect-timeout", type=int, default=5)
    parser.add_argument("remote_command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    remote_command = list(args.remote_command)
    if remote_command and remote_command[0] == "--":
        remote_command.pop(0)
    command, env = build_invocation(
        args.vm_name,
        args.vm_ip,
        remote_command,
        connect_timeout=args.connect_timeout,
    )
    completed = subprocess.run(command, env=env, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
