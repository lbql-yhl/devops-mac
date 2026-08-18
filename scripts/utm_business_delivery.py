#!/usr/bin/env python3
"""Stage the fixed UTM Business Playwright files for shared delivery."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import uuid
from pathlib import Path

from scripts.ssh_password import password_environment, scp_args, ssh_args
from services.project_paths import PROJECT_ROOT

BUSINESS_GUEST_FILES = (
    "utm_business_one.mjs",
)


class BusinessGuestDeliveryError(RuntimeError):
    """Raised when the Business Playwright files are unsafe or incomplete."""


def stage_business_guest_files(shared_dir: Path) -> dict[str, str]:
    source_dir = PROJECT_ROOT / "scripts"
    target_dir = shared_dir / "AppleAccountScriptsBackup"
    if target_dir.exists() and (not target_dir.is_dir() or target_dir.is_symlink()):
        raise BusinessGuestDeliveryError("BUSINESS_SHARED_TARGET_UNSAFE")
    target_dir.mkdir(mode=0o755, exist_ok=True)
    expected: dict[str, str] = {}
    for name in BUSINESS_GUEST_FILES:
        source = source_dir / name
        if not source.is_file() or source.is_symlink() or source.stat().st_size == 0:
            raise BusinessGuestDeliveryError(f"BUSINESS_GUEST_SOURCE_INVALID={name}")
        syntax = subprocess.run(
            ["node", "--check", str(source)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if syntax.returncode != 0:
            raise BusinessGuestDeliveryError(f"BUSINESS_GUEST_SOURCE_INVALID={name}")
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        expected[name] = digest
        target = target_dir / name
        if target.exists() or target.is_symlink():
            if not target.is_file() or target.is_symlink():
                raise BusinessGuestDeliveryError(f"BUSINESS_SHARED_FILE_UNSAFE={name}")
            if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                continue
        temporary = target_dir / f".{name}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_bytes(payload)
            temporary.chmod(0o644)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    for name, digest in expected.items():
        target = target_dir / name
        if (
            not target.is_file()
            or target.is_symlink()
            or hashlib.sha256(target.read_bytes()).hexdigest() != digest
        ):
            raise BusinessGuestDeliveryError(
                f"BUSINESS_SHARED_READBACK_MISMATCH={name}"
            )
    return expected


def sync_business_guest_files(
    shared_dir: Path, *, vm_user: str, vm_ip: str
) -> dict[str, str]:
    staged = stage_business_guest_files(shared_dir)
    source_dir = Path(shared_dir) / "AppleAccountScriptsBackup"
    guest_dir = f"/Users/{vm_user}/Downloads/AppleAccountScriptsBackup"
    prepare = "\n".join(
        (
            "set -euo pipefail",
            f"test \"$(/usr/bin/id -un)\" = {shlex.quote(vm_user)}",
            f"/bin/mkdir -p {shlex.quote(guest_dir)}",
        )
    )
    prepared = subprocess.run(
        ssh_args(vm_user, vm_ip, connect_timeout=8, tty=False)
        + [shlex.join(("/bin/zsh", "-lc", prepare))],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env=password_environment(),
        cwd=PROJECT_ROOT,
    )
    if prepared.returncode != 0:
        raise BusinessGuestDeliveryError("UTM_BUSINESS_GUEST_PREPARE_FAILED")

    for filename in BUSINESS_GUEST_FILES:
        copied = subprocess.run(
            scp_args(
                vm_user,
                vm_ip,
                source_dir / filename,
                f"{guest_dir}/{filename}",
                connect_timeout=8,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            env=password_environment(),
            cwd=PROJECT_ROOT,
        )
        if copied.returncode != 0:
            raise BusinessGuestDeliveryError("UTM_BUSINESS_GUEST_COPY_FAILED")

    verify_lines = ["set -euo pipefail"]
    for filename, digest in staged.items():
        target = f"{guest_dir}/{filename}"
        verify_lines.extend(
            (
                f"test -f {shlex.quote(target)}",
                f"test ! -L {shlex.quote(target)}",
                f"test \"$(/usr/bin/shasum -a 256 {shlex.quote(target)} | /usr/bin/awk '{{print $1}}')\" = {shlex.quote(digest)}",
                f"node --check {shlex.quote(target)}",
            )
        )
    verified = subprocess.run(
        ssh_args(vm_user, vm_ip, connect_timeout=8, tty=False)
        + [shlex.join(("/bin/zsh", "-lic", "\n".join(verify_lines)))],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env=password_environment(),
        cwd=PROJECT_ROOT,
    )
    if verified.returncode != 0:
        raise BusinessGuestDeliveryError("UTM_BUSINESS_GUEST_READBACK_FAILED")
    return staged
