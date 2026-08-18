#!/usr/bin/env python3
"""Guest-only Apple Account password-change entry.

The new password is accepted only through stdin JSON, retained in process
memory, and delegated to the same Accessibility implementation used by UTM-7.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time


PASSWORD_VARIABLE = "APPLE_ACCOUNT_NEW_PASSWORD"


def stop_previous_change_processes() -> None:
    """End only older instances of this exact guest helper."""
    script_path = os.path.realpath(__file__)
    current_pid = os.getpid()
    try:
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return

    previous_pids: list[int] = []
    for line in output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        if pid != current_pid and script_path in fields[1]:
            previous_pids.append(pid)

    for pid in previous_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue

    deadline = time.monotonic() + 2.0
    remaining = set(previous_pids)
    while remaining and time.monotonic() < deadline:
        for pid in tuple(remaining):
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                remaining.remove(pid)
        if remaining:
            time.sleep(0.1)

    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Change the signed-in Apple Account password")
    parser.add_argument(
        "--stdin-json",
        action="store_true",
        help="read the new password from stdin JSON",
    )
    args = parser.parse_args(argv)
    if not args.stdin_json:
        print("--stdin-json is required", file=sys.stderr)
        return 6

    try:
        payload = json.load(sys.stdin)
    except (ValueError, TypeError) as error:
        print(f"stdin JSON is invalid: {error}", file=sys.stderr)
        return 6
    if not isinstance(payload, dict) or set(payload) != {PASSWORD_VARIABLE}:
        print("stdin JSON must contain only the new-password field", file=sys.stderr)
        return 6
    new_password = payload.get(PASSWORD_VARIABLE)
    if not isinstance(new_password, str) or not new_password:
        print("stdin JSON new-password field is empty", file=sys.stderr)
        return 6

    stop_previous_change_processes()

    os.environ[PASSWORD_VARIABLE] = new_password
    new_password = ""
    payload = {}
    try:
        import find_system_settings_general as implementation

        result = int(
            implementation.main(
                ["--change-password", "--new-password-var", PASSWORD_VARIABLE]
            )
        )
        if result == 0:
            print("PASSWORD_CHANGE_NAVIGATION=verified")
            print("PASSWORD_CHANGE=verified")
        return result
    finally:
        os.environ.pop(PASSWORD_VARIABLE, None)


if __name__ == "__main__":
    raise SystemExit(main())
