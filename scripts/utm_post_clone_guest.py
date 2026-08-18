#!/usr/bin/env python3
"""Guest-side operations for post-clone macOS initialization."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from services.host_config import ConfigurationError, guest_password
from scripts.ssh_password import password_environment, ssh_args as shared_ssh_args
from scripts.utm_1_ax import (
    _copy as copy_ax_value,
    _load_ax as load_utm_ax,
    ax_tree,
    normalize_mac as normalize_config_mac,
    utm_pid,
)
from scripts.utm_2_guest_identity import GuestIdentity


WAIT_SECONDS = 3.0
SSH_COMMAND_TIMEOUT_SECONDS = 45.0
SSH_PROBE_TIMEOUT_SECONDS = 12.0
DEMO_RETIREMENT_TIMEOUT_SECONDS = 180.0


class GuestAutomationError(RuntimeError):
    """Raised when a password-authenticated guest operation is not verified."""


def parse_key_values(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def ssh_args(user: str, ip: str, *, tty: bool = False) -> list[str]:
    return shared_ssh_args(user, ip, connect_timeout=5, tty=tty)


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout_seconds: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            env=password_environment(),
            check=check,
        )
    except subprocess.TimeoutExpired as error:
        raise GuestAutomationError(
            f"SSH password command timed out after {timeout_seconds:g} seconds"
        ) from error
    except (OSError, subprocess.CalledProcessError) as error:
        stdout = getattr(error, "stdout", "") or getattr(error, "output", "") or ""
        stderr = getattr(error, "stderr", "") or ""
        details = [str(value).strip() for value in (stdout, stderr) if str(value).strip()]
        detail = " | ".join(details) or str(error)
        raise GuestAutomationError(f"SSH password command failed: {detail}") from error


def ssh_capture(
    user: str,
    ip: str,
    command: list[str],
    *,
    timeout_seconds: float | None = None,
) -> str:
    return _run(
        ssh_args(user, ip) + command,
        timeout_seconds=timeout_seconds,
    ).stdout


def ssh_script(
    user: str, ip: str, script: str, *, timeout_seconds: float | None = None
) -> str:
    return _run(
        ssh_args(user, ip) + ["/bin/zsh", "-s"],
        input_text=script,
        timeout_seconds=timeout_seconds,
    ).stdout


def ssh_python(user: str, ip: str, source: str, args: list[str] | None = None) -> str:
    return _run(
        ssh_args(user, ip) + ["/usr/bin/python3", "-", *(args or [])],
        input_text=source,
    ).stdout


def bootstrap_clash_app_data(user: str, ip: str) -> None:
    """Initialize the exact fresh user's Clash sources while Aqua is active."""
    source = r'''import os, re, signal, stat, subprocess, sys, tempfile, time
from pathlib import Path

user=sys.argv[1]
console=subprocess.run(
    ['/usr/bin/stat','-f','%Su','/dev/console'],
    capture_output=True,text=True,check=True,timeout=8,
).stdout.strip()
if console != user: raise SystemExit('CLASH_BOOTSTRAP_CONSOLE=failed')
base=Path('/Users')/user/'Library/Application Support/io.github.clash-verge-rev.clash-verge-rev'
required=(base/'verge.yaml',base/'config.yaml',base/'profiles.yaml')

def safe_directory(path):
 try: info=path.lstat()
 except FileNotFoundError: return False
 return stat.S_ISDIR(info.st_mode) and not path.is_symlink() and info.st_uid==os.getuid()

def safe_file(path):
 try: info=path.lstat()
 except FileNotFoundError: return False
 return stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_uid==os.getuid()

if safe_directory(base) and all(safe_file(path) for path in required):
 print('CLASH_APP_DATA=initialized_verified'); raise SystemExit(0)
if base.exists() or any(path.exists() for path in required):
 raise SystemExit('CLASH_APP_DATA=partial_or_unsafe')
app=Path('/Applications/Clash Verge.app')
binary=app/'Contents/MacOS/clash-verge'
if not app.is_dir() or app.is_symlink() or not binary.is_file() or binary.is_symlink() or not os.access(binary,os.X_OK):
 raise SystemExit('CLASH_APP=unsafe')

def clash_pids():
 result=subprocess.run(
  ['/usr/bin/pgrep','-f',f'^{re.escape(str(binary))}$'],
  capture_output=True,text=True,timeout=8,
 )
 return [int(value) for value in result.stdout.split() if value.isdigit()]

stalled_pids=clash_pids()
for pid in stalled_pids:
 os.kill(pid, signal.SIGTERM)
if stalled_pids:
 for _ in range(5):
  time.sleep(1)
  if not clash_pids(): break
 else: raise SystemExit('CLASH_APP_BOOTSTRAP=stalled_process_would_not_exit')

result=subprocess.run(['/usr/bin/open',str(app)],capture_output=True,text=True,timeout=10)
if result.returncode != 0 and not clash_pids():
 detail=((result.stderr or '')+'\n'+(result.stdout or '')).strip().replace('\n',' | ')
 raise SystemExit(f'CLASH_APP_BOOTSTRAP=launch_failed:exit={result.returncode}:{detail or "no_detail"}')
for _ in range(20):
 time.sleep(3)
 if safe_directory(base) and all(safe_file(path) for path in required):
  print('CLASH_APP_DATA=initialized_verified'); raise SystemExit(0)

for pid in clash_pids():
 os.kill(pid, signal.SIGTERM)
for _ in range(5):
 time.sleep(1)
 if not clash_pids(): break
else: raise SystemExit('CLASH_APP_BOOTSTRAP=seed_process_would_not_exit')
if base.exists() or any(path.exists() for path in required):
 raise SystemExit('CLASH_APP_DATA=partial_or_unsafe')
base.mkdir(mode=0o700,parents=True)
os.chmod(base,0o700)
seed_payloads={
 base/'verge.yaml':b'enable_tun_mode: false\nenable_system_proxy: false\nenable_auto_launch: false\nenable_silent_start: false\nenable_dns_settings: false\nverge_mixed_port: 7897\n',
 base/'config.yaml':b'ipv6: false\nunified-delay: true\nmixed-port: 7897\nallow-lan: false\n',
 base/'profiles.yaml':b'current: null\nitems: null\n',
}
for path,payload in seed_payloads.items():
 descriptor,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=base)
 temporary=Path(name)
 try:
  os.fchmod(descriptor,0o600)
  with os.fdopen(descriptor,'wb') as handle:
   handle.write(payload); handle.flush(); os.fsync(handle.fileno())
  os.replace(temporary,path)
 finally:
  if temporary.exists(): temporary.unlink()
if not safe_directory(base) or not all(safe_file(path) for path in required):
 raise SystemExit('CLASH_APP_DATA=seed_verification_failed')
print('CLASH_APP_DATA=initialized_verified')
'''
    output = ssh_python(user, ip, source, [user])
    if output.strip() != "CLASH_APP_DATA=initialized_verified":
        raise GuestAutomationError("Clash app-data initialization was not verified")


def password_lines(count: int = 4) -> str:
    if count <= 0:
        raise ValueError("password line count must be positive")
    return (guest_password() + "\n") * count


def ssh_sudo_script(
    user: str,
    ip: str,
    script: str,
    *,
    timeout_seconds: float = SSH_COMMAND_TIMEOUT_SECONDS,
) -> str:
    remote = "sudo -S -p '__SUBMISSION_SUDO_PROMPT__' /bin/zsh -c " + shlex.quote(script)
    return _run(
        ssh_args(user, ip) + [remote],
        input_text=password_lines(),
        timeout_seconds=timeout_seconds,
    ).stdout


def ssh_sudo_script_allow_marker(
    user: str, ip: str, script: str, marker: str
) -> str:
    remote = "sudo -S -p '__SUBMISSION_SUDO_PROMPT__' /bin/zsh -c " + shlex.quote(script)
    result = _run(
        ssh_args(user, ip) + [remote],
        input_text=password_lines(),
        timeout_seconds=SSH_COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 and marker not in output:
        raise GuestAutomationError(f"remote sudo command failed: exit={result.returncode}")
    return output


def ssh_password_command(
    user: str, ip: str, command: str, *, password_inputs: int = 1
) -> tuple[int, str]:
    result = _run(
        ssh_args(user, ip) + [command],
        input_text=password_lines(password_inputs),
        timeout_seconds=SSH_COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def ensure_ssh(user: str, ip: str) -> None:
    errors: list[str] = []
    for attempt in range(3):
        try:
            identity = ssh_capture(
                user,
                ip,
                ["id", "-un"],
                timeout_seconds=SSH_PROBE_TIMEOUT_SECONDS,
            ).strip()
            check = ssh_script(
                user,
                ip,
                "printf 'user=%s\\n' \"$(id -un)\"\nid -Gn\n",
                timeout_seconds=SSH_PROBE_TIMEOUT_SECONDS,
            )
            lines = check.splitlines()
            if identity != user or not lines or lines[0].strip() != f"user={user}":
                raise GuestAutomationError("SSH_PASSWORD_IDENTITY_MISMATCH")
            if "admin" not in " ".join(lines[1:]).split():
                raise GuestAutomationError("SSH_PASSWORD_ADMIN_GROUP_MISSING")
            return
        except GuestAutomationError as error:
            errors.append(str(error))
            if attempt < 2:
                time.sleep(WAIT_SECONDS)
    raise GuestAutomationError(f"SSH_PASSWORD_AUTH=blocked attempts=3 errors={errors}")


def read_guest_identity(user: str, ip: str, expected_mac: str | None = None) -> GuestIdentity:
    ioreg = ssh_capture(user, ip, ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
    ifconfig = ssh_capture(user, ip, ["ifconfig"])
    try:
        serials = re.findall(r'"IOPlatformSerialNumber"\s*=\s*"([^"]+)"', ioreg)
        uuids = re.findall(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', ioreg)
        macs = []
        for value in re.findall(r"(?:^|\s)ether\s+([0-9A-Fa-f:.-]{11,17})", ifconfig, re.MULTILINE):
            try:
                macs.append(normalize_config_mac(value))
            except ValueError:
                continue
        if expected_mac is None:
            if len(serials) != 1 or len(uuids) != 1 or len(set(macs)) != 1:
                raise GuestAutomationError("guest identity is not unique")
            wanted = next(iter(set(macs)))
        else:
            wanted = normalize_config_mac(expected_mac)
            if len(serials) != 1 or len(uuids) != 1 or macs.count(wanted) != 1:
                raise GuestAutomationError(
                    f"guest identity counts serial={len(serials)} uuid={len(uuids)} mac={macs.count(wanted)}"
                )
        return GuestIdentity(serials[0].strip(), uuids[0].strip(), wanted)
    except Exception as error:
        if isinstance(error, GuestAutomationError):
            raise
        raise GuestAutomationError(f"guest identity read failed: {error}") from error


def verify_admin(user: str, ip: str) -> None:
    output = ssh_script(user, ip, "printf '%s\\n' \"$(id -un)\"\nid -Gn\ntest -d \"$HOME\"\n")
    lines = output.splitlines()
    if not lines or lines[0].strip() != user or "admin" not in " ".join(lines[1:]).split():
        raise GuestAutomationError("final administrator verification failed")


def settings_verified(values: dict[str, str]) -> bool:
    required = {
        "AutomaticCheckEnabled", "AutomaticDownload", "AutomaticallyInstallMacOSUpdates",
        "AutomaticallyInstallAppUpdates", "CriticalUpdateInstall", "ConfigDataInstall",
        "AutoUpdate", "sleep", "displaysleep", "disksleep", "screensaver_idle", "askForPassword",
        "screenLock",
        "LOCK_SCREEN_SHOW_CLOCK", "LOCK_SCREEN_SHOW_24_HOUR", "LOCK_SCREEN_SHOW_USER_PHOTO",
        "LOCK_SCREEN_SHOW_PASSWORD_HINTS", "LOCK_SCREEN_SHOW_MESSAGE", "LOCK_SCREEN_SHOW_POWER_BUTTONS",
    }
    disabled = {"0", "false", "no", "off"}
    return required.issubset(values) and all(
        values[key].strip().casefold() in disabled for key in required
    )


def screen_lock_status_verified(output: str) -> bool:
    return "screenLock is off" in output


def disable_screen_lock(user: str, ip: str) -> str:
    code, output = ssh_password_command(
        user,
        ip,
        "/usr/sbin/sysadminctl -screenLock off -password -",
        password_inputs=1,
    )
    if code != 0:
        raise GuestAutomationError(f"SCREEN_LOCK_DISABLE_FAILED={output.strip()}")
    time.sleep(WAIT_SECONDS)
    status = ssh_script(
        user,
        ip,
        "/usr/sbin/sysadminctl -screenLock status 2>&1\n",
        timeout_seconds=SSH_PROBE_TIMEOUT_SECONDS,
    )
    if not screen_lock_status_verified(status):
        raise GuestAutomationError(f"SCREEN_LOCK_DISABLE_NOT_VERIFIED={status.strip()}")
    return status


def guest_shutdown_script() -> str:
    return "/sbin/shutdown -h now\n"


def guest_reboot_script() -> str:
    return "/sbin/shutdown -r now\n"


def demo_retirement_script() -> str:
    """Stop demo's logged-out user domain, then delete only that account."""
    return r'''set -e
if ! /usr/bin/id demo >/dev/null 2>&1; then
  printf 'DEMO_ACCOUNT=absent\n'
  exit 0
fi
console_user=$(/usr/bin/stat -f '%Su' /dev/console)
printf 'CONSOLE_USER=%s\n' "$console_user"
if [ "$console_user" = demo ]; then
  printf 'DEMO_CONSOLE_SESSION=active\n'
  exit 2
fi
demo_uid=$(/usr/bin/id -u demo)
case "$demo_uid" in (*[!0-9]*|'') printf 'DEMO_UID=invalid\n'; exit 3;; esac
# A password SSH login can leave a Background user launchd domain containing
# distnoted/cfprefsd even though demo is not the console user. Retire only
# that exact UID domain before evaluating whether the account is still busy.
/bin/launchctl bootout "user/$demo_uid" >/dev/null 2>&1 || true
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if ! /usr/bin/pgrep -u demo >/dev/null 2>&1; then break; fi
  /bin/sleep 1
done
if /usr/bin/pgrep -u demo >/dev/null 2>&1; then
  printf 'DEMO_ACTIVE=present\n'
  /usr/bin/pgrep -u demo -alf || true
  exit 4
fi
target_user="${SUDO_USER:-}"
if [ -z "$target_user" ] || [ "$target_user" = root ] || [ "$target_user" = demo ]; then
  printf 'DEMO_DELETE_ADMIN=invalid\n'
  exit 5
fi
/usr/bin/id "$target_user" >/dev/null 2>&1
/usr/bin/id -Gn "$target_user" | /usr/bin/grep -Eq '(^|[[:space:]])admin([[:space:]]|$)'
/usr/sbin/sysadminctl -deleteUser demo -secure -adminUser "$target_user" -adminPassword -
if /usr/bin/id demo >/dev/null 2>&1; then
  printf 'DEMO_ACCOUNT=present\n'
  exit 6
fi
printf 'DEMO_ACCOUNT=absent\n'
'''


def demo_cleanup_script() -> str:
    return r'''set -e
demo_user=demo
demo_home="/Users/${demo_user}"
if /usr/bin/pgrep -u demo >/dev/null 2>&1; then
  printf 'DEMO_ACTIVE=present\n'
  exit 2
fi
if /usr/bin/id demo >/dev/null 2>&1; then
  printf 'DEMO_ID=present\n'
  exit 3
fi
if /usr/bin/dscl . -read "$demo_home" >/dev/null 2>&1; then
  printf 'DEMO_DSCL=present\n'
  exit 4
fi
clash_plist=/Library/LaunchDaemons/io.github.clash-verge-rev.clash-verge-rev.service.plist
clash_label=io.github.clash-verge-rev.clash-verge-rev.service
clash_state=/var/root/.local/state/clash-verge-service/desired-state.json
clash_runtime=/private/tmp/verge/clash-verge-service.core.json
stale_demo_pattern='^/Applications/Clash Verge[.]app/Contents/MacOS/verge-mihomo .*[/]Users/demo/'
clash_service_pattern='[/]clash-verge-service( |$)'
clash_app_pattern='^/Applications/Clash Verge[.]app/Contents/MacOS/'
live_pids() {
  pattern="$1"
  for pid in $(/usr/bin/pgrep -f "$pattern" 2>/dev/null || true); do
    state="$(/bin/ps -o state= -p "$pid" 2>/dev/null | /usr/bin/tr -d '[:space:]')"
    case "$state" in
      Z*|'') ;;
      *) printf '%s\n' "$pid" ;;
    esac
  done
}
stale_clash=1
if [ -L "$clash_state" ] || [ -L "$clash_runtime" ] || [ -L "$clash_plist" ]; then
  printf 'CLASH_STALE_STATE_UNSAFE=symlink\n'
  exit 5
fi
if [ -f "$clash_state" ] && /usr/bin/grep -Fq "$demo_home/" "$clash_state"; then
  stale_clash=1
  /usr/bin/grep -Fq '"core_should_be_running"' "$clash_state"
  /usr/bin/grep -Fq '"config_dir"' "$clash_state"
fi
if [ -n "$(live_pids "$stale_demo_pattern")" ]; then
  stale_clash=1
fi
if [ "$stale_clash" = 1 ]; then
  clash_was_loaded=0
  if [ -f "$clash_plist" ]; then
    if /bin/launchctl print "system/$clash_label" >/dev/null 2>&1; then
      clash_was_loaded=1
    fi
    /bin/launchctl disable "system/$clash_label" >/dev/null 2>&1 || true
    /bin/launchctl bootout "system/$clash_label" >/dev/null 2>&1 || true
    /bin/launchctl bootout system "$clash_plist" >/dev/null 2>&1 || true
  fi
  /bin/rm -f -- "$clash_state" "$clash_runtime"
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    /bin/rm -f -- "$clash_state" "$clash_runtime"
    stale_pids="$(live_pids "$stale_demo_pattern")"
    service_pids="$(live_pids "$clash_service_pattern")"
    app_pids="$(live_pids "$clash_app_pattern")"
    [ -z "$app_pids" ] || /bin/kill -TERM $app_pids || true
    [ -z "$stale_pids" ] || /bin/kill -TERM $stale_pids || true
    [ -z "$service_pids" ] || /bin/kill -TERM $service_pids || true
    /bin/sleep 1
    if [ -z "$(live_pids "$stale_demo_pattern")" ] && [ -z "$(live_pids "$clash_service_pattern")" ]; then
      break
    fi
  done
  stale_pids="$(live_pids "$stale_demo_pattern")"
  service_pids="$(live_pids "$clash_service_pattern")"
  app_pids="$(live_pids "$clash_app_pattern")"
  if [ -n "$stale_pids" ] || [ -n "$service_pids" ] || [ -n "$app_pids" ]; then
    [ -z "$app_pids" ] || /bin/kill -KILL $app_pids || true
    [ -z "$stale_pids" ] || /bin/kill -KILL $stale_pids || true
    [ -z "$service_pids" ] || /bin/kill -KILL $service_pids || true
    /bin/rm -f -- "$clash_state" "$clash_runtime"
    /bin/sleep 2
  fi
  if [ -n "$(live_pids "$stale_demo_pattern")" ]; then
    printf 'STALE_DEMO_PATH_PROCESS=present\n'
    exit 6
  fi
  if [ "$clash_was_loaded" = 1 ] && /usr/bin/pgrep -f '[/]clash-verge-service$' >/dev/null 2>&1; then
    printf 'CLASH_SERVICE_STOP=failed\n'
    exit 7
  fi
fi
if [ -L "$demo_home" ]; then
  printf 'DEMO_HOME_UNSAFE=present\n'
  exit 8
fi
if [ -e "$demo_home" ]; then
  /bin/rm -rf -- "$demo_home"
fi
test ! -e "$demo_home" -a ! -L "$demo_home"
if [ -f "$clash_state" ] && /usr/bin/grep -Fq "$demo_home/" "$clash_state"; then
  printf 'CLASH_STALE_STATE=present\n'
  exit 9
fi
if [ -n "$(live_pids "$stale_demo_pattern")" ]; then
  printf 'STALE_DEMO_PATH_PROCESS=present\n'
  exit 10
fi
if [ -e "$demo_home" ] || [ -L "$demo_home" ]; then
  printf 'DEMO_HOME=present\n'
  exit 11
fi
printf 'DEMO_ID=absent\n'
printf 'DEMO_DSCL=absent\n'
printf 'DEMO_STATE=absent\n'
printf 'DEMO_HOME=absent\n'
printf 'CLASH_STALE_STATE=absent\n'
printf 'STALE_DEMO_PATH_PROCESS=absent\n'
'''


def demo_cleanup_verified(output: str) -> bool:
    required = {
        "DEMO_ID=absent",
        "DEMO_DSCL=absent",
        "DEMO_STATE=absent",
        "DEMO_HOME=absent",
        "CLASH_STALE_STATE=absent",
        "STALE_DEMO_PATH_PROCESS=absent",
    }
    return required.issubset({line.strip() for line in output.splitlines()})


def residual_demo_home_cleanup_script() -> str:
    return r'''set -e
demo_user=demo
demo_home="/Users/${demo_user}"
if /usr/bin/id demo >/dev/null 2>&1 || /usr/bin/dscl . -read "$demo_home" >/dev/null 2>&1; then
  printf 'DEMO_ACCOUNT_RECORD=present\n'
  exit 2
fi
if [ -L "$demo_home" ]; then
  printf 'DEMO_HOME_UNSAFE=symlink\n'
  exit 3
fi
if [ -e "$demo_home" ]; then
  /bin/rm -rf -- "$demo_home"
fi
test ! -e "$demo_home" -a ! -L "$demo_home"
printf 'DEMO_RESIDUAL_HOME=absent\n'
'''


def desktop_state_script() -> str:
    return r'''set -e
printf 'CONSOLE_USER=%s\n' "$(/usr/bin/stat -f '%Su' /dev/console)"
if /usr/bin/pgrep -x Finder >/dev/null 2>&1; then printf 'FINDER=ready\n'; else printf 'FINDER=missing\n'; fi
current_uid=$(/usr/bin/id -u)
mini_pattern='/System/Library/CoreServices/Setup Assistant[.]app/Contents/MacOS/Setup Assistant -MiniBuddyYes'
if /usr/bin/pgrep -u "$current_uid" -f "$mini_pattern" >/dev/null 2>&1; then printf 'SETUP_ASSISTANT=present\n'; else printf 'SETUP_ASSISTANT=absent\n'; fi
'''


def setup_assistant_probe() -> str:
    return r'''set -e
current_uid=$(/usr/bin/id -u)
mini_pattern='/System/Library/CoreServices/Setup Assistant[.]app/Contents/MacOS/Setup Assistant -MiniBuddyYes'
if /usr/bin/pgrep -u "$current_uid" -f "$mini_pattern" >/dev/null 2>&1; then printf 'SETUP_ASSISTANT=present\n'; else printf 'SETUP_ASSISTANT=absent\n'; fi
if /usr/bin/pgrep -x Finder >/dev/null 2>&1; then printf 'FINDER=ready\n'; else printf 'FINDER=missing\n'; fi
'''


def complete_setup_assistant_script() -> str:
    """Complete only the current user's MiniBuddy Accessibility page."""
    return r'''set -e
current_user=$(/usr/bin/id -un)
console_user=$(/usr/bin/stat -f '%Su' /dev/console)
current_uid=$(/usr/bin/id -u)
mini_pattern='/System/Library/CoreServices/Setup Assistant[.]app/Contents/MacOS/Setup Assistant -MiniBuddyYes'
if [ "$current_user" != "$console_user" ] || [ "$current_user" = root ]; then
  printf 'MINIBUDDY_USER_MISMATCH=%s/%s\n' "$current_user" "$console_user"
  exit 2
fi
test -e /var/db/.AppleSetupDone
if /usr/bin/pgrep -u "$current_uid" -f "$mini_pattern" >/dev/null 2>&1; then
  pids=$(/usr/bin/pgrep -u "$current_uid" -f "$mini_pattern")
  count=$(printf '%s\n' "$pids" | /usr/bin/awk 'NF {n++} END {print n+0}')
  [ "$count" -eq 1 ] || { printf 'MINIBUDDY_COUNT=%s\n' "$count"; exit 3; }
  pid=$pids
  owner=$(/bin/ps -p "$pid" -o uid= | /usr/bin/xargs)
  command=$(/bin/ps -p "$pid" -o command=)
  [ "$owner" = "$current_uid" ] || { printf 'MINIBUDDY_OWNER_MISMATCH\n'; exit 4; }
  case "$command" in (*'-MiniBuddyYes'*) ;; (*) printf 'MINIBUDDY_COMMAND_MISMATCH\n'; exit 5;; esac
  /usr/bin/defaults write com.apple.SetupAssistant DidSeeAccessibility -bool true
  /usr/bin/defaults write com.apple.SetupAssistant MiniBuddyShouldLaunchToResumeSetup -bool false
  /bin/kill -TERM "$pid"
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    /usr/bin/pgrep -u "$current_uid" -f "$mini_pattern" >/dev/null 2>&1 || break
    /bin/sleep 1
  done
fi
if /usr/bin/pgrep -u "$current_uid" -f "$mini_pattern" >/dev/null 2>&1; then
  printf 'SETUP_ASSISTANT=present\n'
  exit 6
fi
did_see=$(/usr/bin/defaults read com.apple.SetupAssistant DidSeeAccessibility)
resume=$(/usr/bin/defaults read com.apple.SetupAssistant MiniBuddyShouldLaunchToResumeSetup)
[ "$did_see" = 1 ] && [ "$resume" = 0 ]
printf 'SETUP_ASSISTANT_ACTION=guest_state_completed\n'
printf 'SETUP_ASSISTANT=absent\n'
'''


def launch_finder_script() -> str:
    """Launch Finder inside the exact console user's Aqua bootstrap domain."""
    return r'''set -e
target_user="${SUDO_USER:-}"
console_user=$(/usr/bin/stat -f '%Su' /dev/console)
if [ -z "$target_user" ] || [ "$target_user" = root ] || [ "$target_user" != "$console_user" ]; then
  printf 'FINDER_USER_MISMATCH=%s/%s\n' "$target_user" "$console_user"
  exit 2
fi
uid=$(/usr/bin/id -u "$target_user")
case "$uid" in (*[!0-9]*|'') printf 'FINDER_UID=invalid\n'; exit 3;; esac
set +e
/bin/launchctl asuser "$uid" /usr/bin/sudo -u "$target_user" -H /usr/bin/open -a Finder
finder_open_exit=$?
set -e
printf 'FINDER_OPEN_EXIT=%s\n' "$finder_open_exit"
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  /usr/bin/pgrep -u "$uid" -x Finder >/dev/null 2>&1 && break
  /bin/sleep 1
done
/usr/bin/pgrep -u "$uid" -x Finder >/dev/null 2>&1 || { printf 'FINDER=missing\n'; exit 4; }
printf 'FINDER=ready\n'
'''


def setup_assistant_verified(output: str) -> bool:
    values = parse_key_values(output)
    if values.get("SETUP_ASSISTANT") != "absent" or values.get("FINDER") != "ready":
        return False
    return values.get("SETUP_ASSISTANT_ACTION") in (
        None,
        "host_input_not_now",
        "guest_state_completed",
    )


def desktop_state_verified(output: str, user: str) -> bool:
    values = parse_key_values(output)
    return (
        values.get("CONSOLE_USER") == user
        and values.get("FINDER") == "ready"
        and values.get("SETUP_ASSISTANT") == "absent"
    )


def guest_window_point(
    bounds: tuple[float, float, float, float], normalized_x: float, normalized_y: float
) -> tuple[float, float]:
    x, y, width, height = (float(value) for value in bounds)
    if width < 320 or height < 240:
        raise GuestAutomationError(f"UTM_WINDOW_BOUNDS_INVALID={bounds!r}")
    if not 0.0 < normalized_x < 1.0 or not 0.0 < normalized_y < 1.0:
        raise GuestAutomationError("UTM_WINDOW_POINT_MUST_BE_INSIDE_WINDOW")
    return (x + width * normalized_x, y + height * normalized_y)


def _ax_pair(ax: Any, value: Any, value_type: Any, first: str, second: str) -> tuple[float, float]:
    try:
        result = ax.AXValueGetValue(value, value_type, None)
    except Exception as error:
        raise GuestAutomationError("UTM_AX_GEOMETRY_UNREADABLE") from error
    raw = result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], (bool, int)):
        if not result[0]:
            raise GuestAutomationError("UTM_AX_GEOMETRY_UNREADABLE")
        raw = result[1]
    if hasattr(raw, first) and hasattr(raw, second):
        return float(getattr(raw, first)), float(getattr(raw, second))
    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        return float(raw[0]), float(raw[1])
    raise GuestAutomationError("UTM_AX_GEOMETRY_UNREADABLE")


def _window_bounds(ax: Any, window: Any) -> tuple[float, float, float, float]:
    position = copy_ax_value(ax, window, ax.kAXPositionAttribute)
    size = copy_ax_value(ax, window, ax.kAXSizeAttribute)
    if position is None or size is None:
        raise GuestAutomationError("UTM_AX_GEOMETRY_MISSING")
    x, y = _ax_pair(ax, position, ax.kAXValueCGPointType, "x", "y")
    width, height = _ax_pair(ax, size, ax.kAXValueCGSizeType, "width", "height")
    bounds = (x, y, width, height)
    guest_window_point(bounds, 0.5, 0.5)
    return bounds


def _activate_utm_via_dock() -> None:
    """Activate UTM through its semantic Dock item using a native click."""
    try:
        import Quartz
    except ImportError as error:
        raise GuestAutomationError("PyObjC Quartz is required for UTM activation") from error
    ax, _ = load_utm_ax()
    result = subprocess.run(
        ["/usr/bin/pgrep", "-x", "Dock"],
        text=True,
        capture_output=True,
        check=True,
    )
    pids = [value for value in result.stdout.splitlines() if value.isdigit()]
    if len(pids) != 1:
        raise GuestAutomationError(f"DOCK_PID_COUNT={len(pids)}")
    dock = ax.AXUIElementCreateApplication(int(pids[0]))
    candidates: list[tuple[float, float, tuple[float, float]]] = []
    for node in ax_tree(ax, dock):
        title = str(copy_ax_value(ax, node, ax.kAXTitleAttribute) or "")
        role = str(copy_ax_value(ax, node, ax.kAXRoleAttribute) or "")
        subrole = str(copy_ax_value(ax, node, ax.kAXSubroleAttribute) or "")
        if title != "UTM" or role != "AXDockItem" or subrole != "AXApplicationDockItem":
            continue
        x, y = _ax_pair(
            ax,
            copy_ax_value(ax, node, ax.kAXPositionAttribute),
            ax.kAXValueCGPointType,
            "x",
            "y",
        )
        width, height = _ax_pair(
            ax,
            copy_ax_value(ax, node, ax.kAXSizeAttribute),
            ax.kAXValueCGSizeType,
            "width",
            "height",
        )
        if width > 0 and height > 0:
            candidates.append((x, y, (x + width / 2, y + height / 2)))
    if not candidates:
        raise GuestAutomationError("DOCK_UTM_APP_ITEM_COUNT=0")
    _, _, point = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
    current = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    try:
        for event_type in (
            Quartz.kCGEventMouseMoved,
            Quartz.kCGEventLeftMouseDown,
            Quartz.kCGEventLeftMouseUp,
        ):
            event = Quartz.CGEventCreateMouseEvent(
                None, event_type, point, Quartz.kCGMouseButtonLeft
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    finally:
        restore = Quartz.CGEventCreateMouseEvent(
            None,
            Quartz.kCGEventMouseMoved,
            (float(current.x), float(current.y)),
            Quartz.kCGMouseButtonLeft,
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, restore)
    time.sleep(WAIT_SECONDS)
    pid = utm_pid(open_if_missing=False)
    app = ax.AXUIElementCreateApplication(pid)
    if copy_ax_value(ax, app, ax.kAXFrontmostAttribute) is not True:
        raise GuestAutomationError("DOCK_UTM_ACTIVATION_READBACK_FAILED")
    print("UTM_DOCK_ACTIVATION=verified")


def exact_guest_window(ax: Any, pid: int, vm_name: str) -> Any:
    """Return only the VM display window, never UTM's library/settings window."""
    app = ax.AXUIElementCreateApplication(pid)
    windows = list(copy_ax_value(ax, app, ax.kAXWindowsAttribute) or [])
    matches = [
        window
        for window in windows
        if str(copy_ax_value(ax, window, ax.kAXTitleAttribute) or "") == vm_name
    ]
    if len(matches) != 1:
        titles = [
            str(copy_ax_value(ax, window, ax.kAXTitleAttribute) or "")
            for window in windows
        ]
        raise GuestAutomationError(
            f"UTM_GUEST_WINDOW_COUNT={len(matches)} TITLES={titles}"
        )
    return matches[0]


def raise_result_acceptable(
    raise_result: int,
    *,
    frontmost: bool,
    main: bool,
    focused: bool,
    minimized: bool,
) -> bool:
    """Use current window state as authority when AXRaise is unreliable."""
    return (
        raise_result == 0
        and frontmost is True
        and main is True
        and focused is True
        and minimized is False
    )


def _focus_exact_utm(
    vm_name: str, bundle: Path
) -> tuple[int, tuple[float, float, float, float]]:
    try:
        ax, _ = load_utm_ax()
        pid = utm_pid(open_if_missing=True)
        try:
            window = exact_guest_window(ax, pid, vm_name)
        except Exception:
            subprocess.run(["/usr/bin/open", "-a", "UTM", str(bundle)], check=True)
            time.sleep(WAIT_SECONDS)
            pid = utm_pid(open_if_missing=False)
            window = exact_guest_window(ax, pid, vm_name)
        app = ax.AXUIElementCreateApplication(pid)
        if ax.AXUIElementSetAttributeValue(app, ax.kAXFrontmostAttribute, True) != ax.kAXErrorSuccess:
            raise GuestAutomationError("UTM_AX_FRONTMOST_FAILED")
        raise_result = ax.AXUIElementPerformAction(window, ax.kAXRaiseAction)
        for attribute in (ax.kAXMainAttribute, ax.kAXFocusedAttribute):
            ax.AXUIElementSetAttributeValue(window, attribute, True)
        time.sleep(WAIT_SECONDS)
        window = exact_guest_window(ax, pid, vm_name)
        frontmost = copy_ax_value(ax, app, ax.kAXFrontmostAttribute)
        main = copy_ax_value(ax, window, ax.kAXMainAttribute)
        focused = copy_ax_value(ax, window, ax.kAXFocusedAttribute)
        minimized = copy_ax_value(ax, window, ax.kAXMinimizedAttribute)
        if not raise_result_acceptable(
            raise_result,
            frontmost=frontmost is True,
            main=main is True,
            focused=focused is True,
            minimized=minimized is True,
        ):
            _activate_utm_via_dock()
            pid = utm_pid(open_if_missing=False)
            app = ax.AXUIElementCreateApplication(pid)
            window = exact_guest_window(ax, pid, vm_name)
            if ax.AXUIElementSetAttributeValue(app, ax.kAXFrontmostAttribute, True) != ax.kAXErrorSuccess:
                raise GuestAutomationError("UTM_AX_FRONTMOST_RECOVERY_FAILED")
            raise_result = ax.AXUIElementPerformAction(window, ax.kAXRaiseAction)
            for attribute in (ax.kAXMainAttribute, ax.kAXFocusedAttribute):
                ax.AXUIElementSetAttributeValue(window, attribute, True)
            time.sleep(WAIT_SECONDS)
            window = exact_guest_window(ax, pid, vm_name)
            frontmost = copy_ax_value(ax, app, ax.kAXFrontmostAttribute)
            main = copy_ax_value(ax, window, ax.kAXMainAttribute)
            focused = copy_ax_value(ax, window, ax.kAXFocusedAttribute)
            minimized = copy_ax_value(ax, window, ax.kAXMinimizedAttribute)
            if not raise_result_acceptable(
                raise_result,
                frontmost=frontmost is True,
                main=main is True,
                focused=focused is True,
                minimized=minimized is True,
            ):
                raise GuestAutomationError(
                    f"UTM_AX_FOCUS_READBACK_FAILED=raise:{raise_result} "
                    f"frontmost:{frontmost} main:{main} focused:{focused} "
                    f"minimized:{minimized} recovery:dock-native-click"
                )
        return pid, _window_bounds(ax, window)
    except GuestAutomationError:
        raise
    except Exception as error:
        raise GuestAutomationError(f"UTM_AX_TARGET_FAILED={error}") from error


def _mouse_click(pid: int, point: tuple[float, float]) -> None:
    try:
        import Quartz
    except ImportError as error:
        raise GuestAutomationError("PyObjC Quartz is required for VM input") from error
    for event_type in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(
            None, event_type, point, Quartz.kCGMouseButtonLeft
        )
        Quartz.CGEventPostToPid(pid, event)


def _key_codes(pid: int, values: Iterable[int]) -> None:
    try:
        import Quartz
    except ImportError as error:
        raise GuestAutomationError("PyObjC Quartz is required for VM input") from error
    for key_code in values:
        for pressed in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, key_code, pressed)
            Quartz.CGEventPostToPid(pid, event)
        time.sleep(KEY_EVENT_DELAY_SECONDS)


LOGIN_NAME_KEY_CODES = {
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5,
    "h": 4, "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45,
    "o": 31, "p": 35, "q": 12, "r": 15, "s": 1, "t": 17, "u": 32,
    "v": 9, "w": 13, "x": 7, "y": 16, "z": 6,
}
DIGIT_KEY_CODES = {
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21,
    "5": 23, "6": 22, "7": 26, "8": 28, "9": 25,
}
KEY_EVENT_DELAY_SECONDS = 0.03


def configured_password_key_codes() -> tuple[int, ...]:
    """Map the configured four ASCII digits without exposing their value."""
    password = guest_password()
    if re.fullmatch(r"[0-9]{4}", password) is None:
        raise ConfigurationError(
            "invalid host setting: SUBMISSION_GUEST_PASSWORD must be exactly "
            "four ASCII digits"
        )
    return tuple(DIGIT_KEY_CODES[digit] for digit in password)


def login_key_codes(user: str) -> tuple[int, ...]:
    """Type the exact four-letter VM user, then the configured password."""
    if not re.fullmatch(r"[a-z]{4}", user):
        raise GuestAutomationError("GUEST_LOGIN_USER_INVALID")
    return (
        tuple(LOGIN_NAME_KEY_CODES[character] for character in user)
        + (48,)
        + configured_password_key_codes()
        + (36,)
    )


def username_key_codes(user: str) -> tuple[int, ...]:
    """Clear stale login-window text and type the exact VM user."""
    if not re.fullmatch(r"[a-z]{4}", user):
        raise GuestAutomationError("GUEST_LOGIN_USER_INVALID")
    return (51,) * 32 + tuple(LOGIN_NAME_KEY_CODES[character] for character in user)


def password_key_codes() -> tuple[int, ...]:
    """Clear stale login-window text and submit the configured VM password."""
    return (51,) * 32 + configured_password_key_codes() + (36,)


def loginwindow_full_name_enabled(user: str, ip: str) -> bool:
    output = ssh_sudo_script(
        user,
        ip,
        "/usr/bin/defaults read /Library/Preferences/com.apple.loginwindow "
        "SHOWFULLNAME 2>/dev/null || printf '__SHOWFULLNAME_MISSING__\\n'\n",
    ).strip().casefold()
    if output in {"1", "true", "yes"}:
        return True
    if output in {"0", "false", "no", "__showfullname_missing__"}:
        return False
    raise GuestAutomationError(f"LOGINWINDOW_FULL_NAME_MODE_UNVERIFIED={output!r}")


def _capture_guest_input(pid: int, vm_name: str) -> None:
    """Enable UTM's exact Capture Input checkbox and independently read it back."""
    ax, _ = load_utm_ax()

    def exact_control() -> Any:
        window = exact_guest_window(ax, pid, vm_name)
        matches = []
        for node in ax_tree(ax, window):
            role = str(copy_ax_value(ax, node, ax.kAXRoleAttribute) or "")
            description = str(
                copy_ax_value(ax, node, ax.kAXDescriptionAttribute) or ""
            )
            if role == "AXCheckBox" and description in {"Capture Input", "捕获输入"}:
                matches.append(node)
        if len(matches) != 1:
            raise GuestAutomationError(f"UTM_CAPTURE_INPUT_COUNT={len(matches)}")
        return matches[0]

    control = exact_control()
    if copy_ax_value(ax, control, ax.kAXEnabledAttribute) is False:
        raise GuestAutomationError("UTM_CAPTURE_INPUT_DISABLED")
    value = copy_ax_value(ax, control, ax.kAXValueAttribute)
    checked = value is True or str(value).strip().casefold() in {"1", "true", "yes", "on"}
    if not checked:
        result = ax.AXUIElementPerformAction(control, ax.kAXPressAction)
        if result != ax.kAXErrorSuccess:
            raise GuestAutomationError(f"UTM_CAPTURE_INPUT_PRESS_FAILED={result}")
        time.sleep(WAIT_SECONDS)
    control = exact_control()
    value = copy_ax_value(ax, control, ax.kAXValueAttribute)
    checked = value is True or str(value).strip().casefold() in {"1", "true", "yes", "on"}
    if not checked:
        raise GuestAutomationError(f"UTM_CAPTURE_INPUT_READBACK_FAILED={value!r}")


def _focus_guest_input(
    vm_name: str, bundle: Path
) -> tuple[int, tuple[float, float, float, float]]:
    pid, bounds = _focus_exact_utm(vm_name, bundle)
    _capture_guest_input(pid, vm_name)
    return pid, bounds


def _dismiss_setup_once(vm_name: str, bundle: Path) -> None:
    pid, bounds = _focus_guest_input(vm_name, bundle)
    _mouse_click(pid, guest_window_point(bounds, 0.76, 0.80))
    time.sleep(WAIT_SECONDS)


def read_desktop_state(user: str, ip: str) -> tuple[str, dict[str, str]]:
    output = ssh_script(user, ip, desktop_state_script(), timeout_seconds=15.0)
    return output, parse_key_values(output)


def enter_guest_desktop(user: str, ip: str, vm_name: str, bundle: Path) -> dict[str, str]:
    current, values = read_desktop_state(user, ip)
    if values.get("CONSOLE_USER") != user:
        full_name_login = loginwindow_full_name_enabled(user, ip)
        pid, bounds = _focus_guest_input(vm_name, bundle)
        if full_name_login:
            _key_codes(pid, username_key_codes(user) + (36,))
            time.sleep(WAIT_SECONDS)
            _key_codes(pid, password_key_codes())
        else:
            _mouse_click(pid, guest_window_point(bounds, 0.52, 0.91))
            time.sleep(WAIT_SECONDS)
            _key_codes(pid, password_key_codes())
        time.sleep(WAIT_SECONDS)
        for attempt in range(4):
            current, values = read_desktop_state(user, ip)
            if values.get("CONSOLE_USER") == user:
                break
            if attempt < 3:
                time.sleep(WAIT_SECONDS)
        if values.get("CONSOLE_USER") != user:
            if full_name_login:
                raise GuestAutomationError(f"GUEST_LOGIN_NOT_VERIFIED={values}")
            pid, bounds = _focus_guest_input(vm_name, bundle)
            _mouse_click(pid, guest_window_point(bounds, 0.52, 0.87))
            time.sleep(WAIT_SECONDS)
            _key_codes(pid, username_key_codes(user))
            time.sleep(WAIT_SECONDS)
            _mouse_click(pid, guest_window_point(bounds, 0.52, 0.91))
            time.sleep(WAIT_SECONDS)
            _key_codes(pid, password_key_codes())
            time.sleep(WAIT_SECONDS)
            for attempt in range(4):
                current, values = read_desktop_state(user, ip)
                if values.get("CONSOLE_USER") == user:
                    break
                if attempt < 3:
                    time.sleep(WAIT_SECONDS)
            if values.get("CONSOLE_USER") != user:
                raise GuestAutomationError(f"GUEST_LOGIN_NOT_VERIFIED={values}")
    for attempt in range(4):
        current, values = read_desktop_state(user, ip)
        if desktop_state_verified(current, user):
            if not setup_assistant_verified(current):
                raise GuestAutomationError(f"SETUP_ASSISTANT_NOT_VERIFIED={values}")
            return values
        if (
            values.get("SETUP_ASSISTANT") == "absent"
            and values.get("FINDER") == "missing"
        ):
            finder = parse_key_values(
                ssh_sudo_script(user, ip, launch_finder_script())
            )
            if finder.get("FINDER") != "ready":
                raise GuestAutomationError(f"FINDER_LAUNCH_FAILED={finder}")
            current, values = read_desktop_state(user, ip)
            if not desktop_state_verified(current, user):
                if (
                    values.get("CONSOLE_USER") == user
                    and values.get("FINDER") == "ready"
                    and values.get("SETUP_ASSISTANT") == "present"
                ):
                    continue
                raise GuestAutomationError(
                    f"FINDER_AFTER_ABSENT_SETUP_NOT_VERIFIED={values}"
                )
            return values
        if values.get("SETUP_ASSISTANT") == "present":
            completed = ssh_script(
                user,
                ip,
                complete_setup_assistant_script(),
                timeout_seconds=30.0,
            )
            completed_values = parse_key_values(completed)
            if (
                completed_values.get("SETUP_ASSISTANT") != "absent"
                or completed_values.get("SETUP_ASSISTANT_ACTION")
                != "guest_state_completed"
            ):
                raise GuestAutomationError(
                    f"SETUP_ASSISTANT_COMPLETION_FAILED={completed_values}"
                )
            finder = parse_key_values(
                ssh_sudo_script(user, ip, launch_finder_script())
            )
            if finder.get("FINDER") != "ready":
                raise GuestAutomationError(f"FINDER_LAUNCH_FAILED={finder}")
            current, values = read_desktop_state(user, ip)
            if not desktop_state_verified(current, user):
                raise GuestAutomationError(f"DESKTOP_AFTER_SETUP_NOT_VERIFIED={values}")
            values["SETUP_ASSISTANT_ACTION"] = "guest_state_completed"
            return values
        elif attempt < 3:
            time.sleep(WAIT_SECONDS)
    raise GuestAutomationError(f"SETUP_ASSISTANT_NOT_VERIFIED={values}")


def settings_write_script() -> str:
    return r'''set -e
target_user="${SUDO_USER:-}"
if [ -z "$target_user" ] || [ "$target_user" = root ]; then target_user=$(/usr/bin/stat -f '%Su' /dev/console); fi
if [ -z "$target_user" ] || [ "$target_user" = root ]; then printf 'LOCK_SCREEN_USER=ambiguous\n'; exit 2; fi
user_defaults() { /usr/bin/sudo -u "$target_user" -H /usr/bin/defaults "$@"; }
for key in AutomaticCheckEnabled AutomaticDownload AutomaticallyInstallMacOSUpdates AutomaticallyInstallAppUpdates CriticalUpdateInstall ConfigDataInstall; do
  /usr/bin/defaults write /Library/Preferences/com.apple.SoftwareUpdate "$key" -bool false
done
/usr/libexec/PlistBuddy -c "Print :AutomaticCheckEnabled" /Library/Preferences/com.apple.SoftwareUpdate.plist >/dev/null 2>&1 && /usr/libexec/PlistBuddy -c "Set :AutomaticCheckEnabled false" /Library/Preferences/com.apple.SoftwareUpdate.plist || /usr/libexec/PlistBuddy -c "Add :AutomaticCheckEnabled bool false" /Library/Preferences/com.apple.SoftwareUpdate.plist
/usr/bin/defaults write /Library/Preferences/com.apple.commerce AutoUpdate -bool false
/usr/sbin/softwareupdate --schedule off >/dev/null 2>&1 || true
/usr/bin/pmset -a sleep 0 displaysleep 0 disksleep 0
/usr/bin/defaults write /Library/Preferences/com.apple.loginwindow SHOWFULLNAME -bool true
/usr/bin/defaults write /Library/Preferences/com.apple.loginwindow RetriesUntilHint -int 0
/usr/bin/defaults write /Library/Preferences/com.apple.loginwindow LoginwindowText -string ''
/usr/bin/defaults write /Library/Preferences/com.apple.loginwindow PowerOffDisabled -bool true
user_defaults write com.apple.screensaver idleTime -int 0
user_defaults -currentHost write com.apple.screensaver idleTime -int 0
user_defaults write com.apple.screensaver askForPassword -int 0
user_defaults write com.apple.screensaver askForPasswordDelay -int 0
user_defaults write com.apple.screensaver showClock -bool false
user_defaults write com.apple.menuextra.clock Show24Hour -bool false
printf 'LOCK_SCREEN_SHOW_CLOCK=0\nLOCK_SCREEN_SHOW_24_HOUR=0\nLOCK_SCREEN_SHOW_USER_PHOTO=0\n'
printf 'LOCK_SCREEN_SHOW_PASSWORD_HINTS=0\nLOCK_SCREEN_SHOW_MESSAGE=0\nLOCK_SCREEN_SHOW_POWER_BUTTONS=0\n'
printf 'SETTINGS_WRITE=complete\n'
'''


def settings_read_script() -> str:
    return r'''set -e
target_user="${SUDO_USER:-}"
if [ -z "$target_user" ] || [ "$target_user" = root ]; then target_user=$(/usr/bin/stat -f '%Su' /dev/console); fi
if [ -z "$target_user" ] || [ "$target_user" = root ]; then printf 'LOCK_SCREEN_USER=ambiguous\n'; exit 2; fi
user_defaults() { /usr/bin/sudo -u "$target_user" -H /usr/bin/defaults "$@"; }
for key in AutomaticCheckEnabled AutomaticDownload AutomaticallyInstallMacOSUpdates AutomaticallyInstallAppUpdates CriticalUpdateInstall ConfigDataInstall; do
  if [ "$key" = AutomaticCheckEnabled ]; then
    value=$(/usr/libexec/PlistBuddy -c 'Print :AutomaticCheckEnabled' /Library/Preferences/com.apple.SoftwareUpdate.plist 2>/dev/null || printf '0')
  else
    value=$(/usr/bin/defaults read /Library/Preferences/com.apple.SoftwareUpdate "$key" 2>/dev/null || printf '0')
  fi
  printf '%s=%s\n' "$key" "$value"
done
printf 'AutoUpdate=%s\n' "$(/usr/bin/defaults read /Library/Preferences/com.apple.commerce AutoUpdate)"
for key in sleep displaysleep disksleep; do
  value=$(/usr/bin/pmset -g custom | /usr/bin/awk -v wanted="$key" '$1 == wanted {print $2; exit}')
  printf '%s=%s\n' "$key" "$value"
done
printf 'screensaver_idle=%s\n' "$(user_defaults -currentHost read com.apple.screensaver idleTime 2>/dev/null || user_defaults read com.apple.screensaver idleTime)"
printf 'askForPassword=%s\n' "$(user_defaults read com.apple.screensaver askForPassword)"
screen_lock_status=$(/usr/sbin/sysadminctl -screenLock status 2>&1 || true)
case "$screen_lock_status" in
  *'screenLock is off'*) printf 'screenLock=off\n' ;;
  *) printf 'screenLock=not_off\n' ;;
esac
show_clock=$(user_defaults read com.apple.screensaver showClock)
show_24_hour=$(user_defaults read com.apple.menuextra.clock Show24Hour)
show_full_name=$(/usr/bin/defaults read /Library/Preferences/com.apple.loginwindow SHOWFULLNAME)
password_hints=$(/usr/bin/defaults read /Library/Preferences/com.apple.loginwindow RetriesUntilHint)
lock_message=$(/usr/bin/defaults read /Library/Preferences/com.apple.loginwindow LoginwindowText 2>/dev/null || printf '')
power_buttons=$(/usr/bin/defaults read /Library/Preferences/com.apple.loginwindow PowerOffDisabled)
printf 'LOCK_SCREEN_SHOW_CLOCK=%s\n' "$show_clock"
printf 'LOCK_SCREEN_SHOW_24_HOUR=%s\n' "$show_24_hour"
if [ "$show_full_name" = 1 ] || [ "$show_full_name" = true ]; then printf 'LOCK_SCREEN_SHOW_USER_PHOTO=0\n'; else printf 'LOCK_SCREEN_SHOW_USER_PHOTO=1\n'; fi
if [ "$password_hints" = 0 ]; then printf 'LOCK_SCREEN_SHOW_PASSWORD_HINTS=0\n'; else printf 'LOCK_SCREEN_SHOW_PASSWORD_HINTS=1\n'; fi
if [ -z "$lock_message" ]; then printf 'LOCK_SCREEN_SHOW_MESSAGE=0\n'; else printf 'LOCK_SCREEN_SHOW_MESSAGE=1\n'; fi
if [ "$power_buttons" = 1 ] || [ "$power_buttons" = true ]; then printf 'LOCK_SCREEN_SHOW_POWER_BUTTONS=0\n'; else printf 'LOCK_SCREEN_SHOW_POWER_BUTTONS=1\n'; fi
'''


_REMOTE_MANIFEST = r'''
import hashlib, os, subprocess, sys
from pathlib import Path
src = Path('/Volumes/My Shared Files/共享文件')
dst = Path.home() / 'Downloads'
mode = sys.argv[1]
def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''): value.update(chunk)
    return value.hexdigest()
def entries(root):
    if not root.is_dir() or root.is_symlink(): raise SystemExit('SHARED_SOURCE_INVALID')
    result = {}
    for path in sorted(root.rglob('*'), key=lambda item: str(item)):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink(): result[rel] = {'type': 'symlink', 'target': os.readlink(path)}
        elif path.is_dir(): result[rel] = {'type': 'directory'}
        elif path.is_file(): result[rel] = {'type': 'file', 'size': path.stat().st_size, 'sha256': digest(path)}
        else: raise SystemExit('SHARED_SOURCE_UNSUPPORTED')
    return result
def same(target, record):
    if not target.exists() and not target.is_symlink(): return False
    if record['type'] == 'symlink': return target.is_symlink() and os.readlink(target) == record['target']
    if record['type'] == 'directory': return target.is_dir() and not target.is_symlink()
    return target.is_file() and not target.is_symlink() and target.stat().st_size == record['size'] and digest(target) == record['sha256']
source = entries(src)
if not source: raise SystemExit('SOURCE_ENTRIES=0')
conflicts = [rel for rel, record in source.items() if ((dst / rel).exists() or (dst / rel).is_symlink()) and not same(dst / rel, record)]
if conflicts: print('DEST_CONFLICTS=%d' % len(conflicts)); raise SystemExit(2)
print('SOURCE_ENTRIES=%d' % len(source)); print('DEST_CONFLICTS=0')
if mode == 'preflight': print('missing=0'); print('mismatched=0'); print('verification=preflight'); raise SystemExit(0)
if mode == 'copy': dst.mkdir(parents=True, exist_ok=True); subprocess.run(['/usr/bin/ditto', str(src) + '/.', str(dst) + '/'], check=True)
result = entries(dst)
missing = [rel for rel in source if rel not in result]
mismatched = [rel for rel in source if rel in result and not same(dst / rel, source[rel])]
print('missing=%d' % len(missing)); print('mismatched=%d' % len(mismatched))
if missing or mismatched: raise SystemExit(3)
print('verification=passed')
'''


def copy_shared(user: str, ip: str) -> dict[str, str]:
    preflight = parse_key_values(ssh_python(user, ip, _REMOTE_MANIFEST, ["preflight"]))
    if preflight.get("DEST_CONFLICTS") != "0" or int(preflight.get("SOURCE_ENTRIES", "0")) <= 0:
        raise GuestAutomationError(f"COPY_PREFLIGHT_FAILED={preflight}")
    result = parse_key_values(ssh_python(user, ip, _REMOTE_MANIFEST, ["copy"]))
    if result.get("DEST_CONFLICTS") != "0" or result.get("missing") != "0" or result.get("mismatched") != "0" or result.get("verification") != "passed":
        raise GuestAutomationError(f"COPY_VERIFICATION_FAILED={result}")
    return result


def verify_copy(user: str, ip: str) -> dict[str, str]:
    result = parse_key_values(ssh_python(user, ip, _REMOTE_MANIFEST, ["verify"]))
    if result.get("missing") != "0" or result.get("mismatched") != "0" or result.get("verification") != "passed":
        raise GuestAutomationError(f"FINAL_COPY_VERIFICATION_FAILED={result}")
    return result
