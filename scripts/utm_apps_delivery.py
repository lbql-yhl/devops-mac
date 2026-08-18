#!/usr/bin/env python3
"""Stage the UTM Apps Playwright guest scripts for clone-time shared copy."""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scripts.ssh_password import password_environment, scp_args, ssh_args  # noqa: E402
from services.project_paths import PROJECT_ROOT  # noqa: E402
from services.project_paths import SHARED_DIR  # noqa: E402


PLAYWRIGHT_GUEST_FILES = (
    "session.mjs",
    "utm_10_login.mjs",
    "utm_11_one.mjs",
    "utm_12_one.mjs",
    "utm_13_one.mjs",
)
PLAYWRIGHT_LINK = Path("node_modules/playwright-core")
PLAYWRIGHT_LINK_TARGET = Path("../../Fire_One_en1.3/node_modules/playwright-core")


class UTMAppsPlaywrightDeliveryError(RuntimeError):
    """Raised when a Playwright guest script cannot be staged safely."""


def stage_utm_apps_playwright_files(shared_dir: Path) -> dict[str, str]:
    target_dir = Path(shared_dir) / "AppleAccountScriptsBackup"
    if target_dir.exists() and (target_dir.is_symlink() or not target_dir.is_dir()):
        raise UTMAppsPlaywrightDeliveryError("UTM_APPS_SHARED_TARGET_UNSAFE")
    target_dir.mkdir(mode=0o755, exist_ok=True)

    staged: dict[str, str] = {}
    for filename in PLAYWRIGHT_GUEST_FILES:
        source = PROJECT_ROOT / "scripts" / filename
        if not source.is_file() or source.is_symlink() or source.stat().st_size == 0:
            raise UTMAppsPlaywrightDeliveryError("UTM_APPS_GUEST_SOURCE_INVALID")
        syntax = subprocess.run(
            ["/usr/bin/env", "node", "--check", str(source)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if syntax.returncode != 0:
            raise UTMAppsPlaywrightDeliveryError(
                "UTM_APPS_GUEST_SOURCE_SYNTAX_INVALID"
            )

        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        target = target_dir / filename
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise UTMAppsPlaywrightDeliveryError("UTM_APPS_SHARED_FILE_UNSAFE")
            if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                staged[filename] = digest
                continue

        temporary = target_dir / f".{filename}.{uuid.uuid4().hex}.tmp"
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
            raise UTMAppsPlaywrightDeliveryError(
                "UTM_APPS_SHARED_READBACK_MISMATCH"
            )
        staged[filename] = digest
    link = target_dir / PLAYWRIGHT_LINK
    link.parent.mkdir(mode=0o755, exist_ok=True)
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or Path(os.readlink(link)) != PLAYWRIGHT_LINK_TARGET:
            raise UTMAppsPlaywrightDeliveryError("UTM_APPS_PLAYWRIGHT_LINK_CONFLICT")
    else:
        link.symlink_to(PLAYWRIGHT_LINK_TARGET)
    staged[str(PLAYWRIGHT_LINK)] = str(PLAYWRIGHT_LINK_TARGET)
    return staged


def sync_utm_apps_playwright_files(
    shared_dir: Path, *, vm_user: str, vm_ip: str
) -> dict[str, str]:
    staged = stage_utm_apps_playwright_files(shared_dir)
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
        raise UTMAppsPlaywrightDeliveryError("UTM_APPS_GUEST_PREPARE_FAILED")

    for filename in PLAYWRIGHT_GUEST_FILES:
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
            raise UTMAppsPlaywrightDeliveryError("UTM_APPS_GUEST_COPY_FAILED")

    verify_lines = ["set -euo pipefail"]
    for filename in PLAYWRIGHT_GUEST_FILES:
        expected = staged[filename]
        target = f"{guest_dir}/{filename}"
        verify_lines.extend(
            (
                f"test -f {shlex.quote(target)}",
                f"test ! -L {shlex.quote(target)}",
                f"test \"$(/usr/bin/shasum -a 256 {shlex.quote(target)} | /usr/bin/awk '{{print $1}}')\" = {shlex.quote(expected)}",
                f"node --check {shlex.quote(target)}",
            )
        )
    playwright = (
        f"/Users/{vm_user}/Downloads/Fire_One_en1.3/"
        "node_modules/playwright-core/index.mjs"
    )
    verify_lines.append(f"test -f {shlex.quote(playwright)}")
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
        raise UTMAppsPlaywrightDeliveryError("UTM_APPS_GUEST_READBACK_FAILED")
    return staged


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync UTM Apps Playwright files")
    parser.add_argument("--vm-user", required=True)
    parser.add_argument("--vm-ip", required=True)
    args = parser.parse_args()
    try:
        sync_utm_apps_playwright_files(
            SHARED_DIR, vm_user=args.vm_user, vm_ip=args.vm_ip
        )
    except Exception as error:
        print(f"UTM_APPS_SYNC=blocked reason={type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
