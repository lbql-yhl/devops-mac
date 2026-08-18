#!/usr/bin/env python3
"""Stage the fixed UTM-IMAGE guest script for clone-time shared copy."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import uuid
from pathlib import Path

from scripts.ssh_password import password_environment, scp_args, ssh_args
from services.project_paths import PROJECT_ROOT


GUEST_FILE = "utm_19_one.mjs"


class UTMImageGuestDeliveryError(RuntimeError):
    """Raised when the guest script cannot be staged safely."""


def stage_utm_image_guest_file(shared_dir: Path) -> dict[str, str]:
    source = PROJECT_ROOT / "skills" / "utm-image" / "scripts" / GUEST_FILE
    if not source.is_file() or source.is_symlink() or source.stat().st_size == 0:
        raise UTMImageGuestDeliveryError("UTM_IMAGE_GUEST_SOURCE_INVALID")
    syntax = subprocess.run(
        ["/usr/bin/env", "node", "--check", str(source)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if syntax.returncode != 0:
        raise UTMImageGuestDeliveryError("UTM_IMAGE_GUEST_SOURCE_SYNTAX_INVALID")
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    target_dir = Path(shared_dir) / "AppleAccountScriptsBackup"
    if target_dir.exists() and (target_dir.is_symlink() or not target_dir.is_dir()):
        raise UTMImageGuestDeliveryError("UTM_IMAGE_SHARED_TARGET_UNSAFE")
    target_dir.mkdir(mode=0o755, exist_ok=True)
    target = target_dir / GUEST_FILE
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise UTMImageGuestDeliveryError("UTM_IMAGE_SHARED_FILE_UNSAFE")
        if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            return {GUEST_FILE: digest}
    temporary = target_dir / f".{GUEST_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_bytes(payload)
        temporary.chmod(0o644)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    if (
        not target.is_file()
        or target.is_symlink()
        or hashlib.sha256(target.read_bytes()).hexdigest() != digest
    ):
        raise UTMImageGuestDeliveryError("UTM_IMAGE_SHARED_READBACK_MISMATCH")
    return {GUEST_FILE: digest}


def sync_utm_image_guest_file(
    shared_dir: Path, *, vm_user: str, vm_ip: str
) -> dict[str, str]:
    staged = stage_utm_image_guest_file(shared_dir)
    source = Path(shared_dir) / "AppleAccountScriptsBackup" / GUEST_FILE
    guest_dir = f"/Users/{vm_user}/Downloads/AppleAccountScriptsBackup"
    target = f"{guest_dir}/{GUEST_FILE}"
    prepare = subprocess.run(
        ssh_args(vm_user, vm_ip, connect_timeout=8, tty=False)
        + [
            shlex.join(
                (
                    "/bin/zsh",
                    "-lc",
                    "\n".join(
                        (
                            "set -euo pipefail",
                            f"test \"$(/usr/bin/id -un)\" = {shlex.quote(vm_user)}",
                            f"/bin/mkdir -p {shlex.quote(guest_dir)}",
                        )
                    ),
                )
            )
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env=password_environment(),
        cwd=PROJECT_ROOT,
    )
    if prepare.returncode != 0:
        raise UTMImageGuestDeliveryError("UTM_IMAGE_GUEST_PREPARE_FAILED")

    copied = subprocess.run(
        scp_args(vm_user, vm_ip, source, target, connect_timeout=8),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env=password_environment(),
        cwd=PROJECT_ROOT,
    )
    if copied.returncode != 0:
        raise UTMImageGuestDeliveryError("UTM_IMAGE_GUEST_COPY_FAILED")

    digest = staged[GUEST_FILE]
    verify = "\n".join(
        (
            "set -euo pipefail",
            f"test -f {shlex.quote(target)}",
            f"test ! -L {shlex.quote(target)}",
            f"test \"$(/usr/bin/shasum -a 256 {shlex.quote(target)} | /usr/bin/awk '{{print $1}}')\" = {shlex.quote(digest)}",
            f"node --check {shlex.quote(target)}",
        )
    )
    verified = subprocess.run(
        ssh_args(vm_user, vm_ip, connect_timeout=8, tty=False)
        + [shlex.join(("/bin/zsh", "-lic", verify))],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env=password_environment(),
        cwd=PROJECT_ROOT,
    )
    if verified.returncode != 0:
        raise UTMImageGuestDeliveryError("UTM_IMAGE_GUEST_READBACK_FAILED")
    return staged
