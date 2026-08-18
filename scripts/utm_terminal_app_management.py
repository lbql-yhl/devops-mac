#!/usr/bin/env python3
"""Enable and reread the existing Terminal App Management switch."""

from __future__ import annotations

import argparse
import ipaddress
import re
import shlex
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ssh_password import password_environment, ssh_args  # noqa: E402
from services.host_config import guest_password  # noqa: E402


DEEP_LINK = (
    "x-apple.systempreferences:"
    "com.apple.settings.PrivacySecurity.extension?Privacy_AppBundles"
)
APP_MANAGEMENT_MARKER = "APP_MANAGEMENT_PAGE=verified"
TERMINAL_MANAGEMENT_MARKER = "TERMINAL_APP_MANAGEMENT=verified"
GUEST_SCRIPT = PROJECT_ROOT / "scripts" / "utm_terminal_app_management_guest.py"
SAFE_REASON = re.compile(r"^[A-Za-z0-9_ .,:+\-/()=]{1,240}$")


class TerminalAppManagementError(RuntimeError):
    """Raised when the exact clone-time permission state cannot be verified."""


class TerminalPermissionTransport:
    def __init__(self, vm_name: str, vm_ip: str) -> None:
        if not re.fullmatch(r"[a-z]{4}", vm_name):
            raise TerminalAppManagementError("vm-name is invalid")
        try:
            self.vm_ip = str(ipaddress.ip_address(vm_ip))
        except ValueError as error:
            raise TerminalAppManagementError("vm-ip is invalid") from error
        if GUEST_SCRIPT.is_symlink() or not GUEST_SCRIPT.is_file():
            raise TerminalAppManagementError("App Management AX program is unavailable")
        self.vm_name = vm_name
        self.home = f"/Users/{vm_name}"

    def _run(
        self,
        remote_command: str,
        *,
        timeout: int = 120,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ssh_args(self.vm_name, self.vm_ip, connect_timeout=5)
                + [remote_command],
                input=input_text,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
                env=password_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TerminalAppManagementError(
                "guest App Management command did not finish"
            ) from error
        if result.returncode != 0:
            safe_lines = [
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip().startswith("APP_MANAGEMENT_ERROR=")
                and SAFE_REASON.fullmatch(line.strip())
            ]
            if len(safe_lines) == 1:
                raise TerminalAppManagementError(safe_lines[0].split("=", 1)[1])
            details = [
                line.strip()
                for stream in (result.stdout, result.stderr)
                for line in stream.splitlines()
                if SAFE_REASON.fullmatch(line.strip())
            ]
            if details:
                raise TerminalAppManagementError(" / ".join(details)[:240])
            raise TerminalAppManagementError(
                f"guest App Management command failed with exit code {result.returncode}"
            )
        return result

    def probe_identity(self) -> None:
        quoted_user = shlex.quote(self.vm_name)
        quoted_home = shlex.quote(self.home)
        self._run(
            " && ".join(
                (
                    f'test "$(/usr/bin/id -un)" = {quoted_user}',
                    f'test "$HOME" = {quoted_home}',
                    f'test "$(/usr/bin/stat -f %Su /dev/console)" = {quoted_user}',
                    "/usr/bin/python3 --version >/dev/null",
                )
            ),
            timeout=12,
        )

    def open_app_management_settings(self) -> None:
        command = (
            f"/usr/bin/open {shlex.quote(DEEP_LINK)} && "
            "/usr/bin/printf 'SETTINGS_OPENED=1\\n'"
        )
        output = self._run(command, timeout=12).stdout
        if output.splitlines().count("SETTINGS_OPENED=1") != 1:
            raise TerminalAppManagementError("App Management settings did not open")

    def run_ax(self) -> None:
        quoted_user = shlex.quote(self.vm_name)
        remote_command = " && ".join(
            (
                'target_user="$(/usr/bin/stat -f %Su /dev/console)"',
                f'test "$target_user" = {quoted_user}',
                "IFS= read -r SUBMISSION_GUEST_PASSWORD",
                "export SUBMISSION_GUEST_PASSWORD",
                f"exec /usr/bin/python3 -B - --vm-user {quoted_user}",
            )
        )
        output = self._run(
            remote_command,
            input_text=(
                guest_password()
                + "\n"
                + GUEST_SCRIPT.read_text(encoding="utf-8")
            ),
            timeout=120,
        ).stdout
        if output.splitlines().count(TERMINAL_MANAGEMENT_MARKER) != 1:
            raise TerminalAppManagementError(
                "Terminal App Management readback was not verified"
            )


def run(vm_name: str, vm_ip: str) -> None:
    transport = TerminalPermissionTransport(vm_name, vm_ip)
    transport.probe_identity()
    transport.open_app_management_settings()
    transport.run_ax()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enable existing Terminal App Management for one UTM guest"
    )
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-ip", required=True)
    args = parser.parse_args()
    try:
        run(args.vm_name, args.vm_ip)
    except Exception as error:
        reason = str(error)
        if not SAFE_REASON.fullmatch(reason):
            reason = "App Management verification failed"
        print(f"执行报错：utm-vm-clone-step-09；{reason}")
        return 1
    print(APP_MANAGEMENT_MARKER)
    print(TERMINAL_MANAGEMENT_MARKER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
