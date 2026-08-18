#!/usr/bin/env python3
"""Prepare the inherited UTM-18 guest Edge/CDP state in one host entry."""

from __future__ import annotations

import argparse
import ipaddress
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence


PROJECT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_SOURCE_ROOT))

from scripts.ssh_password import password_environment, ssh_args  # noqa: E402


UTMCTL = str(Path(shutil.which("utmctl") or "/opt/homebrew/bin/utmctl").resolve())
VM_NAME_RE = re.compile(r"[a-z]{4}")
REQUIRED_REMOTE_MARKERS = (
    "SSH_TARGET=verified",
    "EDGE_OLD_PROCESS=stopped",
    "EDGE_CDP_PROCESS=verified",
    "EDGE_CDP_PORT_9222=verified",
    "EDGE_CDP_HTTP=verified",
)

REMOTE_EDGE_SCRIPT = r'''set -euo pipefail
expected_user="$1"
expected_home="/Users/$expected_user"

if [[ "$(/usr/bin/id -un)" != "$expected_user" || "$HOME" != "$expected_home" ]]; then
  exit 90
fi
print -r -- "SSH_TARGET=verified"

if /usr/bin/pgrep -f '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge' >/dev/null 2>&1; then
  /usr/bin/pkill "Microsoft Edge"
fi
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if ! /usr/bin/pgrep -f '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge' >/dev/null 2>&1; then
    break
  fi
  /bin/sleep 1
done
if /usr/bin/pgrep -f '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge' >/dev/null 2>&1; then
  exit 91
fi
print -r -- "EDGE_OLD_PROCESS=stopped"

nohup "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/edge-debug-profile \
  --no-first-run \
  --start-maximized \
  >/dev/null 2>&1 </dev/null &

edge_pid=""
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  pids="$(/usr/bin/pgrep -f '^/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge .*--remote-debugging-port=9222' 2>/dev/null || true)"
  pid_count="$(/usr/bin/printf '%s\n' "$pids" | /usr/bin/awk 'NF { count += 1 } END { print count + 0 }')"
  if [[ "$pid_count" == "1" ]]; then
    edge_pid="$pids"
    break
  fi
  /bin/sleep 1
done
[[ -n "$edge_pid" ]] || exit 92

edge_command="$(/bin/ps -p "$edge_pid" -o command=)"
[[ "$edge_command" == *'--remote-debugging-port=9222'* ]] || exit 93
[[ "$edge_command" == *'--user-data-dir=/tmp/edge-debug-profile'* ]] || exit 93
[[ "$edge_command" == *'--no-first-run'* ]] || exit 93
[[ "$edge_command" == *'--start-maximized'* ]] || exit 93
print -r -- "EDGE_CDP_PROCESS=verified"

listener_verified=""
for attempt in {1..30}; do
  listener_pids="$(/usr/sbin/lsof -nP -iTCP:9222 -sTCP:LISTEN -t 2>/dev/null || true)"
  listener_pids="$(/usr/bin/printf '%s\n' "$listener_pids" | /usr/bin/sort -u)"
  listener_count="$(/usr/bin/printf '%s\n' "$listener_pids" | /usr/bin/awk 'NF { count += 1 } END { print count + 0 }')"
  listener_names="$(/usr/sbin/lsof -nP -a -p "$edge_pid" -iTCP:9222 -sTCP:LISTEN -F n 2>/dev/null | /usr/bin/grep '^n' || true)"
  if [[ "$listener_count" == "1" && "$listener_pids" == "$edge_pid" && "$listener_names" == 'n127.0.0.1:9222' ]]; then
    listener_verified="yes"
    break
  fi
  /bin/sleep 0.2
done
[[ "$listener_verified" == "yes" ]] || exit 94
print -r -- "EDGE_CDP_PORT_9222=verified"

websocket_url="$(
  /usr/bin/curl -fsS --max-time 5 http://127.0.0.1:9222/json/version |
    /usr/bin/plutil -extract webSocketDebuggerUrl raw -o - -
)"
[[ -n "$websocket_url" && "$websocket_url" == ws://127.0.0.1:9222/* ]] || exit 95
print -r -- "EDGE_CDP_HTTP=verified"
'''


class UTM18EdgeError(RuntimeError):
    """Raised when the inherited VM or its Edge/CDP state cannot be verified."""


def _validate_target(vm_name: str, vm_ip: str, vm_user: str) -> None:
    if not VM_NAME_RE.fullmatch(vm_name):
        raise UTM18EdgeError("VM_NAME_INVALID")
    if vm_user != vm_name:
        raise UTM18EdgeError("VM_USER_MISMATCH")
    try:
        ipaddress.IPv4Address(vm_ip)
    except ipaddress.AddressValueError as exc:
        raise UTM18EdgeError("VM_IP_INVALID") from exc


def _parse_status(output: str) -> str:
    values = [line.strip().lower() for line in output.splitlines() if line.strip()]
    if len(values) != 1:
        raise UTM18EdgeError("VM_STATUS_AMBIGUOUS")
    if values[0] not in {"started", "running"}:
        raise UTM18EdgeError(f"VM_NOT_STARTED={values[0]}")
    return values[0]


def prepare_edge(
    vm_name: str,
    vm_ip: str,
    vm_user: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    password_env: Mapping[str, str] | None = None,
    utmctl: str = UTMCTL,
) -> list[str]:
    _validate_target(vm_name, vm_ip, vm_user)
    status_result = runner(
        [utmctl, "status", vm_name],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )
    if status_result.returncode != 0:
        raise UTM18EdgeError("VM_STATUS_READ_FAILED")
    status = _parse_status(status_result.stdout)

    command = ssh_args(vm_user, vm_ip, connect_timeout=5)
    command.append(shlex.join(["/bin/zsh", "-s", "--", vm_user]))
    ssh_result = runner(
        command,
        input=REMOTE_EDGE_SCRIPT,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
        env=dict(password_env) if password_env is not None else password_environment(),
    )
    if ssh_result.returncode != 0:
        raise UTM18EdgeError(f"EDGE_PREPARE_SSH_EXIT={ssh_result.returncode}")

    output_lines = [line.strip() for line in ssh_result.stdout.splitlines() if line.strip()]
    missing = [marker for marker in REQUIRED_REMOTE_MARKERS if marker not in output_lines]
    if missing:
        raise UTM18EdgeError(f"EDGE_PREPARE_MARKERS_MISSING={','.join(missing)}")
    return [
        f"VM_STATUS={status}",
        "SSH_PASSWORD_AUTH=verified",
        *REQUIRED_REMOTE_MARKERS,
        "UTM_18_EDGE_PREPARE=verified",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the inherited VM and prepare Edge CDP for UTM-18."
    )
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-ip", required=True)
    parser.add_argument("--vm-user", required=True)
    args = parser.parse_args(argv)

    try:
        markers = prepare_edge(args.vm_name, args.vm_ip, args.vm_user)
    except (UTM18EdgeError, subprocess.TimeoutExpired) as exc:
        print(f"UTM_18_EDGE_PREPARE=blocked reason={exc}")
        return 1
    for marker in markers:
        print(marker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
