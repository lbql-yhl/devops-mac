#!/usr/bin/env python3
"""Configure Clash Verge in one exact SQLite-bound UTM guest.

The runner is command-only: it verifies the exact inventory binding, starts
that same VM only when it is stopped, refreshes its IP by registered MAC, and
uses configured-password SSH.  It has no screenshot, OCR, CUA, or coordinate path.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import Any, Mapping, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOTION_API = PROJECT_ROOT / "scripts" / "notion_api.py"
PROFILE_NAME = "My-SOCKS5-Proxy"
PROFILE_UID_PREFIX = "L"
APP_DATA_RELATIVE = Path(
    "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
)
EGRESS_SOURCES: tuple[tuple[str, str], ...] = (
    ("ipify", "https://api.ipify.org"),
    ("ip_sb", "https://api.ip.sb/ip"),
    ("icanhazip", "https://icanhazip.com"),
)

sys.path.insert(0, str(PROJECT_ROOT))
from scripts.clean_cli import run_clean_cli  # noqa: E402
from scripts.notion_api import load_dotenv  # noqa: E402
from services.project_paths import VM_IMAGES_DIR  # noqa: E402
from scripts.utm_clash_ip_target import (  # noqa: E402
    TargetVMError,
    ensure_exact_vm_started,
    require_exact_bound_vm,
    resolve_exact_vm_ip,
)

INVENTORY_DATABASE = PROJECT_ROOT / "runtime" / "vm-inventory.sqlite3"


class ClashIPError(RuntimeError):
    """Raised when a target is ambiguous or an independent readback fails."""


CONFIG_FAILURE_STAGES = frozenset(
    {
        "validate_inputs",
        "render_verge",
        "render_config",
        "render_registry",
        "render_profile",
        "write_verge",
        "write_config",
        "write_registry",
        "write_profile",
        "write_headless",
        "readback_verge",
        "readback_config",
        "readback_registry",
        "readback_headless",
        "unknown",
    }
)


class GuestConfigWriteError(ClashIPError):
    """A non-sensitive guest configuration failure classification."""

    def __init__(self, stage: str, *, rollback_verified: bool) -> None:
        safe_stage = stage if stage in CONFIG_FAILURE_STAGES else "unknown"
        super().__init__(f"guest configuration failed at {safe_stage}")
        self.stage = safe_stage
        self.rollback_verified = rollback_verified


@dataclass(frozen=True)
class ProxyValues:
    host: ipaddress.IPv4Address
    port: int
    username: str
    password: str


def _yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _validate_private_text(name: str, value: str) -> str:
    if value != value.strip() or not value:
        raise ValueError(f"{name} must be non-empty and trimmed")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} contains a control character")
    return value


def render_socks5_profile(values: ProxyValues) -> str:
    """Render the immutable SOCKS5 template with only four private substitutions."""
    username = _validate_private_text("username", values.username)
    password = _validate_private_text("password", values.password)
    if not 1 <= values.port <= 65535:
        raise ValueError("port must be in 1..65535")
    host = str(values.host)
    return f'''port: 7890
socks-port: 7891
allow-lan: false
mode: rule
log-level: info

dns:
  enable: true
  listen: 0.0.0.0:53
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  nameserver:
    - 8.8.8.8
    - 1.1.1.1
  fallback:
    - https://dns.google/dns-query
    - https://cloudflare-dns.com/dns-query

proxies:
  - name: "{PROFILE_NAME}"
    type: socks5
    server: {_yaml_quote(host)}
    port: {values.port}
    username: {_yaml_quote(username)}
    password: {_yaml_quote(password)}

proxy-groups:
  - name: "PROXY"
    type: select
    proxies:
      - "{PROFILE_NAME}"

rules:
  - DOMAIN-SUFFIX,apple.com,PROXY
  - DOMAIN-SUFFIX,icloud.com,PROXY
  - DOMAIN-SUFFIX,mobileme.icloud.com,PROXY
  - DOMAIN-SUFFIX,me.com,PROXY
  - DOMAIN-SUFFIX,mzstatic.com,PROXY
  - DOMAIN-SUFFIX,itunes.apple.com,PROXY
  - DOMAIN-SUFFIX,apps.apple.com,PROXY
  - DOMAIN-SUFFIX,appstoreconnect.apple.com,PROXY
  - DOMAIN-SUFFIX,testflight.apple.com,PROXY
  - DOMAIN-SUFFIX,developer.apple.com,PROXY
  - DOMAIN-KEYWORD,apple,PROXY
  - DOMAIN-KEYWORD,icloud,PROXY
  - DOMAIN-KEYWORD,appstore,PROXY
  - GEOIP,CN,DIRECT
  - MATCH,PROXY
'''


def render_headless_runtime_profile(profile: str) -> str:
    """Add only the two fixed load-time scalars to an exact Profile."""
    if not profile or not profile.endswith("\n"):
        raise ValueError("profile must be non-empty and newline terminated")
    if re.search(r"(?m)^(?:ipv6|unified-delay):", profile):
        raise ValueError("profile already contains a headless runtime scalar")
    return "ipv6: false\nunified-delay: true\n" + profile


def patch_top_level_scalars(text: str, values: Mapping[str, bool]) -> str:
    """Replace only unique top-level YAML scalars and preserve all other text."""
    lines = text.splitlines(keepends=True)
    for key, desired in values.items():
        pattern = re.compile(rf"^{re.escape(key)}:\s*.*?(\r?\n)?$")
        indexes = [index for index, line in enumerate(lines) if pattern.match(line)]
        if not indexes:
            raise ValueError(f"missing top-level YAML key: {key}")
        if len(indexes) != 1:
            raise ValueError(f"duplicate top-level YAML key: {key}")
        newline = "\r\n" if lines[indexes[0]].endswith("\r\n") else "\n"
        lines[indexes[0]] = f"{key}: {'true' if desired else 'false'}{newline}"
    return "".join(lines)


def patch_profile_registry(text: str, *, uid: str, name: str) -> str:
    """Select one local profile and replace only its existing registry block."""
    if not re.fullmatch(r"L[a-z]{4}Socks5", uid):
        raise ValueError("profile uid is invalid")
    current_matches = list(re.finditer(r"(?m)^current:\s*.*$", text))
    if len(current_matches) != 1:
        raise ValueError("profiles registry current key must be unique")
    item_matches = list(re.finditer(r"(?m)^items:[ \t]*(.*?)[ \t]*$", text))
    if len(item_matches) != 1 or item_matches[0].group(1) not in {"", "null"}:
        raise ValueError("profiles registry items key must be unique")

    if item_matches[0].group(1) == "null":
        text = (
            text[: item_matches[0].start()]
            + "items:"
            + text[item_matches[0].end() :]
        )
        current_matches = list(re.finditer(r"(?m)^current:\s*.*$", text))

    text = (
        text[: current_matches[0].start()]
        + f"current: {uid}"
        + text[current_matches[0].end() :]
    )
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("- uid: ")]
    target_ranges: list[tuple[int, int]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        if lines[start].strip() == f"- uid: {uid}":
            target_ranges.append((start, end))
    if len(target_ranges) > 1:
        raise ValueError("duplicate local profile registry entry")
    if target_ranges:
        start, end = target_ranges[0]
        del lines[start:end]

    entry = [
        f"- uid: {uid}",
        "  type: local",
        f"  name: {name}",
        f"  file: {uid}.yaml",
        "  desc: null",
        "  updated: 0",
    ]
    while lines and not lines[-1].strip():
        lines.pop()
    lines.extend(entry)
    return "\n".join(lines) + "\n"


def _safe_private_file(path: Path) -> None:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ClashIPError(f"private file is unsafe: {path.name}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ClashIPError(f"private file mode is unsafe: {path.name}")


def _atomic_replace(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ClashIPError(f"atomic target is unsafe: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def parse_proxy_stdin(stream: TextIO) -> ProxyValues:
    """Parse the exact four-field JSON object supplied on standard input."""
    payload = stream.read(16_385)
    if not payload or len(payload.encode("utf-8")) > 16_384:
        raise ValueError("proxy stdin is empty or too large")
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError("proxy stdin must be one JSON object") from error
    required = {"host", "port", "username", "password"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("proxy stdin keys must exactly match host/port/username/password")
    host = ipaddress.IPv4Address(str(raw["host"]))
    port_text = str(raw["port"])
    if not re.fullmatch(r"[1-9][0-9]{0,4}", port_text):
        raise ValueError("proxy port must be canonical decimal")
    values = ProxyValues(
        host=host,
        port=int(port_text),
        username=_validate_private_text("username", str(raw["username"])),
        password=_validate_private_text("password", str(raw["password"])),
    )
    if not 1 <= values.port <= 65_535:
        raise ValueError("proxy port is outside 1..65535")
    return values


def validate_proxy_source_arguments(
    *,
    proxy_stdin: bool,
    parent_title: str | None,
    page_title: str | None,
) -> None:
    titles_present = bool(parent_title and parent_title.strip()) or bool(
        page_title and page_title.strip()
    )
    if proxy_stdin and titles_present:
        raise ValueError("stdin and Notion proxy sources are mutually exclusive")
    if proxy_stdin:
        return
    if not parent_title or not parent_title.strip() or not page_title or not page_title.strip():
        raise ValueError("parent-title and page-title are required for Notion proxy source")


def resolve_notion_titles(
    *,
    application_name: str,
    vm_name: str,
    parent_title: str | None,
    page_title: str | None,
    proxy_stdin: bool,
    environment: Mapping[str, str],
) -> tuple[str | None, str | None]:
    if proxy_stdin:
        return parent_title, page_title
    expected_parent = environment.get("SUBMISSION_HOST_MACHINE", "").strip()
    if not expected_parent:
        raise ClashIPError("SUBMISSION_HOST_MACHINE_REQUIRED")
    expected_page = f"{application_name.strip()}-{vm_name}"
    resolved_parent = parent_title.strip() if parent_title else expected_parent
    resolved_page = page_title.strip() if page_title else expected_page
    if resolved_parent != expected_parent:
        raise ClashIPError("NOTION_PARENT_TITLE_MISMATCH")
    if resolved_page != expected_page:
        raise ClashIPError("NOTION_PAGE_TITLE_MISMATCH")
    return resolved_parent, resolved_page


class Runner:
    def __init__(
        self,
        *,
        vm_name: str,
        application_name: str,
        vm_ip: str | None,
        parent_title: str | None,
        page_title: str | None,
        proxy_stdin: bool,
        database: Path = INVENTORY_DATABASE,
        images_dir: Path = VM_IMAGES_DIR,
    ) -> None:
        if not re.fullmatch(r"[a-z]{4}", vm_name):
            raise ValueError("vm_name must contain four lowercase letters")
        if not application_name.strip():
            raise ValueError("application_name must be non-empty")
        if vm_ip is not None and str(ipaddress.IPv4Address(vm_ip)) != vm_ip:
            raise ValueError("vm_ip must be canonical IPv4")
        validate_proxy_source_arguments(
            proxy_stdin=proxy_stdin,
            parent_title=parent_title,
            page_title=page_title,
        )
        self.vm_name = vm_name
        self.application_name = application_name.strip()
        self.vm_ip = vm_ip
        self.parent_title = parent_title
        self.page_title = page_title
        self.proxy_stdin = proxy_stdin
        self.database = Path(database)
        self.images_dir = Path(images_dir)
        self.console_state: str | None = None
        self.system_dns_fingerprint: str | None = None
        self.profile_uid = f"{PROFILE_UID_PREFIX}{vm_name}Socks5"

        sys.path.insert(0, str(PROJECT_ROOT))
        from scripts.preflight import SHARED_DIR  # imported after root is fixed

        self.shared_dir = Path(SHARED_DIR)

    def prepare_vm(self) -> None:
        target = require_exact_bound_vm(
            self.database,
            self.images_dir,
            self.vm_name,
            self.application_name,
        )
        print("VM_INVENTORY_BINDING=verified")
        started_here = ensure_exact_vm_started(self.vm_name)
        print(f"VM_POWER={'started' if started_here else 'already_running'}")
        resolved_ip = resolve_exact_vm_ip(target)
        if self.vm_ip is not None and self.vm_ip != resolved_ip:
            print("VM_IP_REFRESHED=verified")
        self.vm_ip = resolved_ip
        print("VM_IP_IDENTITY=verified")

    def _ssh_dependencies(self) -> tuple[Any, Any, Any]:
        from scripts.ssh_password import password_environment, scp_args, ssh_args

        return password_environment, scp_args, ssh_args

    def _run_ssh(
        self,
        command: list[str],
        *,
        input_text: str | None = None,
        timeout: int = 45,
    ) -> subprocess.CompletedProcess[str]:
        if self.vm_ip is None:
            raise ClashIPError("VM IP has not been prepared")
        password_environment, _, ssh_args = self._ssh_dependencies()
        return subprocess.run(
            ssh_args(self.vm_name, self.vm_ip) + command,
            env=password_environment(),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=True,
        )

    def _run_sudo(self, script: str, *, timeout: float = 45.0) -> str:
        if self.vm_ip is None:
            raise ClashIPError("VM IP has not been prepared")
        from scripts.utm_post_clone_guest import (
            GuestAutomationError,
            ssh_sudo_script,
        )

        try:
            return ssh_sudo_script(
                self.vm_name, self.vm_ip, script, timeout_seconds=timeout
            )
        except (GuestAutomationError, subprocess.TimeoutExpired) as error:
            raise ClashIPError(
                f"guest privileged command failed: {error}"
            ) from None

    def verify_identity(self) -> None:
        source = r'''import os, re, subprocess
home=os.path.expanduser('~')
expected_home=os.path.join('/Users',''' + repr(self.vm_name) + r''')
if home != expected_home: raise SystemExit('GUEST_HOME=failed')
if not re.fullmatch(r'[a-z]{4}', os.path.basename(home)): raise SystemExit('GUEST_USER=failed')
groups=subprocess.run(['/usr/bin/id','-Gn'],capture_output=True,text=True,check=True).stdout.split()
if 'admin' not in groups: raise SystemExit('GUEST_ADMIN=failed')
console=subprocess.run(['/usr/bin/stat','-f','%Su','/dev/console'],capture_output=True,text=True,check=True).stdout.strip()
if console == os.path.basename(home): print('GUEST_CONSOLE=interactive_verified')
elif console == 'root': print('GUEST_CONSOLE=headless_verified')
else: raise SystemExit('GUEST_CONSOLE=unknown')
print('GUEST_IDENTITY=verified')
'''
        output = self._run_ssh(["/usr/bin/python3", "-"], input_text=source, timeout=12).stdout
        markers = set(output.splitlines())
        if "GUEST_IDENTITY=verified" not in markers or not {
            "GUEST_CONSOLE=interactive_verified",
            "GUEST_CONSOLE=headless_verified",
        }.intersection(markers):
            raise ClashIPError("guest identity readback failed")
        console_marker = next(
            marker for marker in markers if marker.startswith("GUEST_CONSOLE=")
        )
        self.console_state = (
            "interactive" if console_marker.endswith("interactive_verified") else "headless"
        )
        print(console_marker)
        print("GUEST_IDENTITY=verified")

    def read_notion_proxy(self, private_dir: Path) -> ProxyValues:
        if self.parent_title is None or self.page_title is None:
            raise ClashIPError("Notion titles are unavailable")
        subprocess.run(
            [
                sys.executable,
                str(NOTION_API),
                "verify-parent",
                "--title",
                self.parent_title,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        labels = {
            "host": "代理ip:",
            "port": "代理端口:",
            "username": "代理用户名：",
            "password": "代理用户密码：",
        }
        for key, label in labels.items():
            target = private_dir / key
            subprocess.run(
                [
                    sys.executable,
                    str(NOTION_API),
                    "read-field",
                    "--title",
                    self.page_title,
                    "--heading",
                    "账号信息",
                    "--label",
                    label,
                    "--out",
                    str(target),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            _safe_private_file(target)

        raw = {
            key: (private_dir / key).read_text(encoding="utf-8").strip()
            for key in labels
        }
        host = ipaddress.IPv4Address(raw["host"])
        port_text = raw["port"]
        if not re.fullmatch(r"[1-9][0-9]{0,4}", port_text):
            raise ClashIPError("proxy port is not canonical decimal")
        values = ProxyValues(
            host=host,
            port=int(port_text),
            username=_validate_private_text("username", raw["username"]),
            password=_validate_private_text("password", raw["password"]),
        )
        if not 1 <= values.port <= 65535:
            raise ClashIPError("proxy port is outside 1..65535")
        print("NOTION_PROXY_FIELDS=verified")
        return values

    def read_proxy(self, private_dir: Path) -> ProxyValues:
        if not self.proxy_stdin:
            return self.read_notion_proxy(private_dir)
        if sys.stdin.isatty():
            payload = getpass.getpass(prompt="")
            values = parse_proxy_stdin(io.StringIO(payload))
        else:
            values = parse_proxy_stdin(sys.stdin)
        print("PROXY_STDIN_FIELDS=verified")
        return values

    def write_and_copy_profile(self, values: ProxyValues) -> Path:
        target = self.shared_dir / "socks5.yml"
        payload = render_socks5_profile(values).encode("utf-8")
        print("SOCKS5_TEMPLATE=exact")
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise ClashIPError("shared profile target is unsafe")
        before = target.read_bytes() if target.exists() else None
        before_mode = stat.S_IMODE(target.stat().st_mode) if before is not None else None
        try:
            _atomic_replace(target, payload)
            if target.read_bytes() != payload or stat.S_IMODE(target.stat().st_mode) != 0o600:
                raise ClashIPError("shared profile readback failed")
            print("SOCKS5_SHARED_READBACK=exact")

            password_environment, scp_args, _ = self._ssh_dependencies()
            guest_target = f"/Users/{self.vm_name}/Downloads/socks5.yml"
            subprocess.run(
                scp_args(self.vm_name, self.vm_ip, target, guest_target),
                env=password_environment(),
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            expected_hash = hashlib.sha256(payload).hexdigest()
            verify = r'''import hashlib, os, stat, sys
p=sys.argv[1]
s=os.lstat(p)
if stat.S_ISLNK(s.st_mode) or not stat.S_ISREG(s.st_mode): raise SystemExit('GUEST_PROFILE=unsafe')
if s.st_uid != os.getuid(): raise SystemExit('GUEST_PROFILE_OWNER=failed')
os.chmod(p,0o600)
s=os.lstat(p)
if stat.S_IMODE(s.st_mode) != 0o600: raise SystemExit('GUEST_PROFILE_MODE=failed')
print('GUEST_PROFILE_SHA256='+hashlib.sha256(open(p,'rb').read()).hexdigest())
'''
            output = self._run_ssh(
                ["/usr/bin/python3", "-", guest_target], input_text=verify
            ).stdout.strip()
            if output != f"GUEST_PROFILE_SHA256={expected_hash}":
                raise ClashIPError("guest profile hash readback failed")
            print("SOCKS5_GUEST_PERMISSION=verified")
            print("SOCKS5_GUEST_READBACK=exact")
            return target
        except Exception:
            if before is not None and before_mode is not None:
                _atomic_replace(target, before, before_mode)
            raise

    def _ensure_clash_app_data(self) -> None:
        if self.console_state not in {"interactive", "headless"}:
            raise ClashIPError("guest console state is unavailable")
        if self.console_state == "headless" and self._probe_clash_app_data_state() == "absent":
            raise ClashIPError("fresh headless Clash app-data requires clone initialization")
        source = r'''import os, stat, subprocess, sys, time
from pathlib import Path

user, console_state = sys.argv[1:]
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

def safe_installed_dir(path):
 try: info=path.lstat()
 except FileNotFoundError: return False
 return stat.S_ISDIR(info.st_mode) and not path.is_symlink()

def safe_installed_file(path):
 try: info=path.lstat()
 except FileNotFoundError: return False
 return stat.S_ISREG(info.st_mode) and not path.is_symlink()

if safe_directory(base) and all(safe_file(path) for path in required):
 print('CLASH_APP_DATA=existing_verified')
 raise SystemExit(0)
if base.exists() or any(path.exists() for path in required):
 raise SystemExit('CLASH_APP_DATA=partial_or_unsafe')
if console_state!='interactive':
 raise SystemExit('CLASH_APP_DATA=headless_bootstrap_forbidden')
app=Path('/Applications/Clash Verge.app')
binary=app/'Contents/MacOS/clash-verge'
if not safe_installed_dir(app) or not safe_installed_file(binary) or not os.access(binary,os.X_OK):
 raise SystemExit('CLASH_APP=unsafe')
result=subprocess.run(['/usr/bin/open','-gja',str(app)],capture_output=True,text=True,timeout=10)
if result.returncode!=0: raise SystemExit('CLASH_APP_BOOTSTRAP=launch_failed')
for _ in range(5):
 time.sleep(3)
 if safe_directory(base) and all(safe_file(path) for path in required):
  print('CLASH_APP_DATA=fresh_bootstrap_verified')
  raise SystemExit(0)
raise SystemExit('CLASH_APP_DATA=fresh_bootstrap_failed')
'''
        output = self._run_ssh(
            ["/usr/bin/python3", "-", self.vm_name, self.console_state],
            input_text=source,
            timeout=30,
        ).stdout
        markers = set(output.splitlines())
        if markers == {"CLASH_APP_DATA=existing_verified"}:
            print("CLASH_APP_DATA=existing_verified")
            return
        if markers == {"CLASH_APP_DATA=fresh_bootstrap_verified"}:
            print("CLASH_APP_DATA=fresh_bootstrap_verified")
            return
        raise ClashIPError("Clash app-data bootstrap readback failed")

    def _probe_clash_app_data_state(self) -> str:
        source = r'''import os, stat, sys
from pathlib import Path
user=sys.argv[1]
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
 print('CLASH_APP_DATA_STATE=existing')
elif not base.exists() and not any(path.exists() for path in required):
 print('CLASH_APP_DATA_STATE=absent')
else:
 print('CLASH_APP_DATA_STATE=partial')
'''
        output = self._run_ssh(
            ["/usr/bin/python3", "-", self.vm_name],
            input_text=source,
            timeout=12,
        ).stdout.strip()
        expected = {
            "CLASH_APP_DATA_STATE=existing": "existing",
            "CLASH_APP_DATA_STATE=absent": "absent",
            "CLASH_APP_DATA_STATE=partial": "partial",
        }
        if output not in expected:
            raise ClashIPError("Clash app-data state is unavailable")
        return expected[output]

    def _stop_clash(self) -> None:
        script = rf'''
set -e
target_user={self.vm_name}
app_pattern='^/Applications/Clash Verge[.]app/Contents/MacOS/'
for pid in $(/usr/bin/pgrep -u "$target_user" -f "$app_pattern" || true); do /bin/kill -TERM "$pid"; done
/bin/sleep 3
if /usr/bin/pgrep -u "$target_user" -f "$app_pattern" >/dev/null 2>&1; then printf 'CLASH_APP_STOP=failed\n'; exit 2; fi
plist='/Library/LaunchDaemons/io.github.clash-verge-rev.clash-verge-rev.service.plist'
if [ -f "$plist" ] && [ ! -L "$plist" ] && /bin/launchctl print system/io.github.clash-verge-rev.clash-verge-rev.service >/dev/null 2>&1; then
  /bin/launchctl bootout system "$plist"
fi
for pid in $(/usr/bin/pgrep -f '^/Applications/Clash Verge[.]app/Contents/MacOS/verge-mihomo( |$)' || true); do /bin/kill -TERM "$pid"; done
/bin/sleep 3
if /usr/bin/pgrep -f '^/Applications/Clash Verge[.]app/Contents/MacOS/verge-mihomo( |$)' >/dev/null 2>&1; then printf 'CLASH_CORE_STOP=failed\n'; exit 3; fi
printf 'CLASH_STOP=verified\n'
'''
        output = self._run_sudo(script, timeout=30)
        if "CLASH_STOP=verified" not in output:
            raise ClashIPError("Clash stop readback failed")

    def _write_guest_app_config(self) -> None:
        source = r'''import os, stat, sys, tempfile
from pathlib import Path

user, uid = sys.argv[1:]
base=Path('/Users')/user/'Library/Application Support/io.github.clash-verge-rev.clash-verge-rev'
download=Path('/Users')/user/'Downloads/socks5.yml'
profile_uid='L'+user+'Socks5'
if not base.is_dir() or base.is_symlink(): raise SystemExit('APP_DATA=unsafe')
if not download.is_file() or download.is_symlink(): raise SystemExit('PROFILE_SOURCE=unsafe')

def patch_scalars(text, values):
 import re
 lines=text.splitlines(keepends=True)
 for key, desired in values.items():
  pattern=re.compile(r'^'+re.escape(key)+r':\s*.*?(\r?\n)?$')
  indexes=[i for i,line in enumerate(lines) if pattern.match(line)]
  if len(indexes)!=1: raise SystemExit('CONFIG_KEY_'+key+'=nonunique')
  newline='\r\n' if lines[indexes[0]].endswith('\r\n') else '\n'
  rendered=('true' if desired else 'false') if isinstance(desired,bool) else str(desired)
  lines[indexes[0]]=key+': '+rendered+newline
 return ''.join(lines)

def patch_registry(text):
 import re
 matches=list(re.finditer(r'(?m)^current:\s*.*$',text))
 item_matches=list(re.finditer(r'(?m)^items:[ \t]*(.*?)[ \t]*$',text))
 if len(matches)!=1 or len(item_matches)!=1 or item_matches[0].group(1) not in ('','null'): raise SystemExit('PROFILE_REGISTRY=invalid')
 if item_matches[0].group(1)=='null':
  text=text[:item_matches[0].start()]+'items:'+text[item_matches[0].end():]
  matches=list(re.finditer(r'(?m)^current:\s*.*$',text))
 text=text[:matches[0].start()]+'current: '+profile_uid+text[matches[0].end():]
 lines=text.splitlines()
 starts=[i for i,line in enumerate(lines) if line.startswith('- uid: ')]
 ranges=[]
 for pos,start in enumerate(starts):
  end=starts[pos+1] if pos+1<len(starts) else len(lines)
  if lines[start].strip()=='- uid: '+profile_uid: ranges.append((start,end))
 if len(ranges)>1: raise SystemExit('PROFILE_ENTRY=duplicate')
 if ranges:
  start,end=ranges[0]; del lines[start:end]
 while lines and not lines[-1].strip(): lines.pop()
 lines.extend(['- uid: '+profile_uid,'  type: local','  name: My-SOCKS5-Proxy','  file: '+profile_uid+'.yaml','  desc: null','  updated: 0'])
 return '\n'.join(lines)+'\n'

def atomic(path, payload):
 if path.exists() and (path.is_symlink() or not path.is_file()): raise SystemExit('TARGET_'+path.name+'=unsafe')
 fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
 temp=Path(name)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'wb') as stream:
   stream.write(payload); stream.flush(); os.fsync(stream.fileno())
  os.replace(temp,path)
 finally:
  if temp.exists(): temp.unlink()

verge=base/'verge.yaml'
config=base/'config.yaml'
registry=base/'profiles.yaml'
profiles_dir=base/'profiles'
profile_target=profiles_dir/(profile_uid+'.yaml')
headless_target=base/'clash-verge-headless.yaml'
for path in (verge,config,registry):
 if not path.is_file() or path.is_symlink(): raise SystemExit('APP_FILE_'+path.name+'=unsafe')
if profiles_dir.exists() and (profiles_dir.is_symlink() or not profiles_dir.is_dir()): raise SystemExit('PROFILES_DIR=unsafe')
profiles_dir.mkdir(mode=0o700,exist_ok=True)
managed=(verge,config,registry,profile_target,headless_target)
before_payloads={}
for path in managed:
 if path.exists():
  if path.is_symlink() or not path.is_file(): raise SystemExit('MANAGED_'+path.name+'=unsafe')
  before_payloads[path]=(path.read_bytes(),stat.S_IMODE(path.stat().st_mode))
 else:
  before_payloads[path]=None

stage='render_verge'
try:
 verge_payload=patch_scalars(verge.read_text(),{
  'enable_tun_mode':True,'enable_system_proxy':False,
  'enable_auto_launch':True,'enable_silent_start':True,
  'enable_dns_settings':False,'verge_mixed_port':7897,
 }).encode()
 stage='render_config'
 config_payload=patch_scalars(config.read_text(),{'ipv6':False,'unified-delay':True}).encode()
 config_payload=patch_scalars(config_payload.decode(),{'mixed-port':7897,'allow-lan':False}).encode()
 stage='render_registry'
 registry_payload=patch_registry(registry.read_text()).encode()
 stage='render_profile'
 profile_payload=download.read_bytes()
 profile_lines=profile_payload.decode().splitlines(keepends=True)
 profile_lines=[line for line in profile_lines if not line.startswith(('port:','socks-port:'))]
 headless_payload=b'ipv6: false\nunified-delay: true\nmixed-port: 7897\n'+''.join(profile_lines).encode()
 stage='write_verge'
 atomic(verge,verge_payload)
 stage='write_config'
 atomic(config,config_payload)
 stage='write_registry'
 atomic(registry,registry_payload)
 stage='write_profile'
 atomic(profile_target,profile_payload)
 stage='write_headless'
 atomic(headless_target,headless_payload)

 stage='readback_verge'
 if patch_scalars(verge.read_text(),{
  'enable_tun_mode':True,'enable_system_proxy':False,
  'enable_auto_launch':True,'enable_silent_start':True,
  'enable_dns_settings':False,'verge_mixed_port':7897,
 }) != verge.read_text(): raise RuntimeError('VERGE_READBACK=failed')
 stage='readback_config'
 if patch_scalars(config.read_text(),{'ipv6':False,'unified-delay':True,'mixed-port':7897,'allow-lan':False}) != config.read_text(): raise RuntimeError('CONFIG_SOURCE_READBACK=failed')
 stage='readback_registry'
 if registry.read_text().count('current: '+profile_uid)!=1 or registry.read_text().count('- uid: '+profile_uid)!=1: raise RuntimeError('PROFILE_READBACK=failed')
 stage='readback_headless'
 if headless_target.read_bytes()!=headless_payload: raise RuntimeError('HEADLESS_RUNTIME_READBACK=failed')
except Exception:
 failure_stage=stage
 try:
  for path,before in before_payloads.items():
   if before is None:
    if path.exists():
     if path.is_symlink() or not path.is_file(): raise RuntimeError('rollback_unsafe')
     path.unlink()
   else:
    payload,mode=before
    atomic(path,payload)
    os.chmod(path,mode)
  for path,before in before_payloads.items():
   if before is None:
    if path.exists(): raise RuntimeError('rollback_extra_target')
   elif path.read_bytes()!=before[0] or stat.S_IMODE(path.stat().st_mode)!=before[1]:
    raise RuntimeError('rollback_mismatch')
 except Exception:
  print('CONFIG_ROLLBACK=failed')
  print('CONFIG_FAILURE_STAGE='+failure_stage,file=sys.stderr)
  raise SystemExit(92)
 print('CONFIG_ROLLBACK=verified')
 print('CONFIG_FAILURE_STAGE='+failure_stage,file=sys.stderr)
 raise SystemExit(91)
print('UI_PERSISTENT_SETTINGS=verified')
print('CONFIG_WRITE=atomic_verified')
'''
        try:
            output = self._run_ssh(
                ["/usr/bin/python3", "-", self.vm_name, self.profile_uid],
                input_text=source,
                timeout=30,
            ).stdout
        except subprocess.CalledProcessError as error:
            stdout_lines = set((error.stdout or "").splitlines())
            stderr_lines = set((error.stderr or "").splitlines())
            stages = {
                line.split("=", 1)[1]
                for line in stderr_lines
                if line.startswith("CONFIG_FAILURE_STAGE=")
                and line.split("=", 1)[1] in CONFIG_FAILURE_STAGES
            }
            stage = next(iter(stages)) if len(stages) == 1 else "unknown"
            rollback_verified = "CONFIG_ROLLBACK=verified" in stdout_lines
            print(f"CONFIG_FAILURE_STAGE={stage}")
            if rollback_verified:
                print("CONFIG_ROLLBACK=verified")
            raise GuestConfigWriteError(
                stage, rollback_verified=rollback_verified
            ) from error
        markers = set(output.splitlines())
        if not {
            "UI_PERSISTENT_SETTINGS=verified",
            "CONFIG_WRITE=atomic_verified",
        }.issubset(markers):
            raise ClashIPError("guest app configuration readback failed")
        print("UI_PERSISTENT_SETTINGS=verified")
        print("CONFIG_WRITE=atomic_verified")

    def _verify_config_recovery(self) -> None:
        source = r'''import hashlib, os, re, stat, subprocess, sys, tempfile, time
from pathlib import Path

user,console_state=sys.argv[1:]
base=Path('/Users')/user/'Library/Application Support/io.github.clash-verge-rev.clash-verge-rev'
download=Path('/Users')/user/'Downloads/socks5.yml'
profile_uid='L'+user+'Socks5'
profiles_dir=base/'profiles'
verge=base/'verge.yaml'; config=base/'config.yaml'; registry=base/'profiles.yaml'
profile_target=profiles_dir/(profile_uid+'.yaml')
headless_target=base/'clash-verge-headless.yaml'

def require_dir(path):
 info=path.lstat()
 if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or not os.access(path,os.W_OK):
  raise SystemExit('CONFIG_DIAGNOSTIC_DIRECTORY=failed')
def require_file(path):
 info=path.lstat()
 if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():
  raise SystemExit('CONFIG_DIAGNOSTIC_FILE=failed')
require_dir(base); require_dir(profiles_dir)
for path in (verge,config,registry,download): require_file(path)
for path in (profile_target,headless_target):
 if path.exists(): require_file(path)

def patch_scalars(text, values):
 lines=text.splitlines(keepends=True)
 for key,desired in values.items():
  pattern=re.compile(r'^'+re.escape(key)+r':\s*.*?(\r?\n)?$')
  indexes=[i for i,line in enumerate(lines) if pattern.match(line)]
  if len(indexes)!=1: raise SystemExit('CONFIG_DIAGNOSTIC_KEY=failed')
  newline='\r\n' if lines[indexes[0]].endswith('\r\n') else '\n'
  rendered=('true' if desired else 'false') if isinstance(desired,bool) else str(desired)
  lines[indexes[0]]=key+': '+rendered+newline
 return ''.join(lines)

def patch_registry(text):
 matches=list(re.finditer(r'(?m)^current:\s*.*$',text))
 item_matches=list(re.finditer(r'(?m)^items:[ \t]*(.*?)[ \t]*$',text))
 if len(matches)!=1 or len(item_matches)!=1 or item_matches[0].group(1) not in ('','null'):
  raise SystemExit('CONFIG_DIAGNOSTIC_REGISTRY=failed')
 if item_matches[0].group(1)=='null':
  text=text[:item_matches[0].start()]+'items:'+text[item_matches[0].end():]
  matches=list(re.finditer(r'(?m)^current:\s*.*$',text))
 text=text[:matches[0].start()]+'current: '+profile_uid+text[matches[0].end():]
 lines=text.splitlines(); starts=[i for i,line in enumerate(lines) if line.startswith('- uid: ')]
 ranges=[]
 for pos,start in enumerate(starts):
  end=starts[pos+1] if pos+1<len(starts) else len(lines)
  if lines[start].strip()=='- uid: '+profile_uid: ranges.append((start,end))
 if len(ranges)>1: raise SystemExit('CONFIG_DIAGNOSTIC_PROFILE_DUPLICATE=failed')
 if ranges:
  start,end=ranges[0]; del lines[start:end]
 while lines and not lines[-1].strip(): lines.pop()
 lines.extend(['- uid: '+profile_uid,'  type: local','  name: My-SOCKS5-Proxy','  file: '+profile_uid+'.yaml','  desc: null','  updated: 0'])
 return '\n'.join(lines)+'\n'

verge_values={'enable_tun_mode':True,'enable_system_proxy':False,'enable_auto_launch':True,'enable_silent_start':True,'enable_dns_settings':False,'verge_mixed_port':7897}
config_values={'ipv6':False,'unified-delay':True,'mixed-port':7897,'allow-lan':False}
verge_payload=patch_scalars(verge.read_text(),verge_values).encode()
config_payload=patch_scalars(config.read_text(),config_values).encode()
registry_payload=patch_registry(registry.read_text()).encode()
profile_payload=download.read_bytes()
profile_lines=[line for line in profile_payload.decode().splitlines(keepends=True) if not line.startswith(('port:','socks-port:'))]
headless_payload=b'ipv6: false\nunified-delay: true\nmixed-port: 7897\n'+''.join(profile_lines).encode()
print('CONFIG_RECOVERY_DIAGNOSTIC_1=verified')

def atomic(path,payload):
 fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
 temp=Path(name)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'wb') as stream:
   stream.write(payload); stream.flush(); os.fsync(stream.fileno())
  os.replace(temp,path)
 finally:
  if temp.exists(): temp.unlink()
with tempfile.TemporaryDirectory(prefix='utm-clash-ip-diagnostic-',dir=base) as name:
 root=Path(name); dry_profiles=root/'profiles'; dry_profiles.mkdir(mode=0o700)
 targets=(root/'verge.yaml',root/'config.yaml',root/'profiles.yaml',dry_profiles/(profile_uid+'.yaml'),root/'clash-verge-headless.yaml')
 payloads=(verge_payload,config_payload,registry_payload,profile_payload,headless_payload)
 for target,payload in zip(targets,payloads): atomic(target,payload)
 if any(target.read_bytes()!=payload or stat.S_IMODE(target.stat().st_mode)!=0o600 for target,payload in zip(targets,payloads)):
  raise SystemExit('CONFIG_DIAGNOSTIC_ATOMIC=failed')
 if targets[2].read_text().count('current: '+profile_uid)!=1 or targets[2].read_text().count('- uid: '+profile_uid)!=1:
  raise SystemExit('CONFIG_DIAGNOSTIC_DRY_REGISTRY=failed')
print('CONFIG_RECOVERY_DIAGNOSTIC_2=verified')

def state():
 hashes=tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in (verge,config,registry))
 lines=subprocess.run(['/bin/ps','-ww','-axo','user=,command='],capture_output=True,text=True,check=True).stdout.splitlines()
 apps=[line for line in lines if '/Applications/Clash Verge.app/Contents/MacOS/clash-verge' in line]
 cores=[line for line in lines if '/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo' in line]
 helper=subprocess.run(['/bin/launchctl','print','system/io.github.clash-verge-rev.clash-verge-rev.service'],capture_output=True,text=True)
 socket=Path('/tmp/verge/verge-mihomo.sock')
 socket_ok=socket.exists() and stat.S_ISSOCK(socket.lstat().st_mode)
 core_ok=(len(cores)==1 and cores[0].split(None,1)[0]=='root')
 if console_state=='interactive':
  process_ok=(len(apps)==1 and apps[0].split(None,1)[0]==user and core_ok and 'clash-verge.yaml' in cores[0] and 'clash-verge-headless.yaml' not in cores[0])
 else:
  process_ok=(not apps and core_ok and 'clash-verge-headless.yaml' in cores[0])
 helper_loaded=helper.returncode==0
 helper_ok=helper_loaded if console_state=='interactive' else not helper_loaded
 return hashes,process_ok,helper_ok,socket_ok
first=state(); time.sleep(5); second=state()
if first!=second or not all(second[1:]): raise SystemExit('CONFIG_DIAGNOSTIC_STABILITY=failed')
print('CONFIG_RECOVERY_DIAGNOSTIC_3=verified')
'''
        output = self._run_ssh(
            ["/usr/bin/python3", "-", self.vm_name, self.console_state],
            input_text=source,
            timeout=20,
        ).stdout
        markers = set(output.splitlines())
        required = {
            "CONFIG_RECOVERY_DIAGNOSTIC_1=verified",
            "CONFIG_RECOVERY_DIAGNOSTIC_2=verified",
            "CONFIG_RECOVERY_DIAGNOSTIC_3=verified",
        }
        if markers != required:
            raise ClashIPError("configuration recovery diagnostics failed")
        for marker in sorted(required):
            print(marker)

    def _start_official_clash(self) -> None:
        if self.console_state not in {"interactive", "headless"}:
            raise ClashIPError("guest console state is unavailable")
        if self.console_state == "interactive":
            helper_script = r'''
set -e
plist='/Library/LaunchDaemons/io.github.clash-verge-rev.clash-verge-rev.service.plist'
if [ ! -f "$plist" ] || [ -L "$plist" ]; then exit 2; fi
if ! /bin/launchctl print system/io.github.clash-verge-rev.clash-verge-rev.service >/dev/null 2>&1; then
  /bin/launchctl bootstrap system "$plist"
fi
/bin/sleep 3
/bin/launchctl print system/io.github.clash-verge-rev.clash-verge-rev.service >/dev/null
printf 'CLASH_HELPER=loaded_verified\n'
'''
            helper_output = self._run_sudo(helper_script, timeout=20)
            if "CLASH_HELPER=loaded_verified" not in helper_output.splitlines():
                raise ClashIPError("Clash helper bootstrap failed")
            print("CLASH_HELPER=loaded_verified")
            app_script = rf'''set -e
/usr/bin/open -gja 'Clash Verge'
for delay in 3 3 3 3 3; do
  /bin/sleep "$delay"
  if /usr/bin/pgrep -u {self.vm_name} -f '^/Applications/Clash Verge[.]app/Contents/MacOS/' >/dev/null 2>&1 \
     && /usr/bin/pgrep -f '^/Applications/Clash Verge[.]app/Contents/MacOS/verge-mihomo ' >/dev/null 2>&1 \
     && [ -S /tmp/verge/verge-mihomo.sock ]; then
    printf 'CLASH_APP=started_verified\n'
    exit 0
  fi
done
exit 3
'''
            app_output = self._run_ssh(
                ["/bin/zsh", "-s"], input_text=app_script, timeout=25
            ).stdout
            if "CLASH_APP=started_verified" not in app_output.splitlines():
                raise ClashIPError("Clash app start failed")
            print("CLASH_APP=started_verified")
        else:
            headless_script = rf'''set -e
target_user={self.vm_name}
core='/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo'
base="/Users/$target_user/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
config="$base/clash-verge-headless.yaml"
socket='/tmp/verge/verge-mihomo.sock'
plist='/Library/LaunchDaemons/io.github.clash-verge-rev.clash-verge-rev.service.plist'
if [ ! -x "$core" ] || [ ! -f "$config" ] || [ -L "$config" ]; then exit 4; fi
repaired=0
if [ -f "$plist" ] && [ ! -L "$plist" ] && /bin/launchctl print system/io.github.clash-verge-rev.clash-verge-rev.service >/dev/null 2>&1; then
  /bin/launchctl bootout system "$plist"
  repaired=1
fi
for attempt in 1 2; do
  for pid in $(/usr/bin/pgrep -f '^/Applications/Clash Verge[.]app/Contents/MacOS/verge-mihomo( |$)' || true); do
    /bin/kill -TERM "$pid"
    repaired=1
  done
  /bin/sleep 3
  if /usr/bin/pgrep -f '^/Applications/Clash Verge[.]app/Contents/MacOS/verge-mihomo( |$)' >/dev/null 2>&1; then
    repaired=1
    continue
  fi
  if [ -e "$socket" ]; then
    [ -S "$socket" ] || exit 5
    /bin/rm -f "$socket"
    repaired=1
  fi
  /usr/bin/nohup "$core" -d "$base" -f "$config" -ext-ctl-unix "$socket" </dev/null >/dev/null 2>&1 &
  /bin/sleep 5
  count=$(/usr/bin/pgrep -f '^/Applications/Clash Verge[.]app/Contents/MacOS/verge-mihomo( |$)' | /usr/bin/wc -l | /usr/bin/tr -d ' ' || true)
  if [ "$count" = '1' ] && [ -S "$socket" ]; then
    if [ "$repaired" = '1' ] || [ "$attempt" = '2' ]; then printf 'CLASH_HEADLESS_REPAIR=verified\n'; fi
    printf 'CLASH_HEADLESS_FALLBACK=verified\n'
    exit 0
  fi
  repaired=1
done
exit 6
'''
            headless_output = self._run_sudo(headless_script, timeout=20)
            if "CLASH_HEADLESS_FALLBACK=verified" not in headless_output.splitlines():
                raise ClashIPError("headless Clash fallback failed")
            if "CLASH_HEADLESS_REPAIR=verified" in headless_output.splitlines():
                print("CLASH_HEADLESS_REPAIR=verified")
            print("CLASH_HELPER=unloaded_verified")
            print("CLASH_HEADLESS_FALLBACK=verified")
        print("OFFICIAL_CLASH_LIFECYCLE=verified")

    def configure_clash(self) -> None:
        self._ensure_clash_app_data()
        self._stop_clash()
        try:
            self._write_guest_app_config()
        except GuestConfigWriteError as failure:
            if not failure.rollback_verified or failure.stage == "unknown":
                raise
            self._start_official_clash()
            print("CLASH_RECOVERY_RESTART=verified")
            self._verify_config_recovery()
            print("CONFIG_RECOVERY_DIAGNOSTICS=verified")
            self._stop_clash()
            try:
                self._write_guest_app_config()
            except GuestConfigWriteError as retry_failure:
                print("CONFIG_REPAIR_RETRY=failed")
                print("CONFIG_REPAIR_RETRY_LIMIT=exhausted")
                if retry_failure.rollback_verified:
                    self._start_official_clash()
                    print("CLASH_RECOVERY_RESTART=verified")
                raise
            print("CONFIG_REPAIR_RETRY=verified")
        self._start_official_clash()
        self.apply_runtime_state()

    def restart_clash_for_persistence_check(self) -> None:
        self._stop_clash()
        self._start_official_clash()
        self.apply_runtime_state()

    def apply_runtime_state(self) -> None:
        source = r'''import json, subprocess, time
sock='/tmp/verge/verge-mihomo.sock'
def request(endpoint, method='GET', payload=None):
 command=['/usr/bin/curl','--silent','--show-error','--fail','--unix-socket',sock,'--request',method]
 if payload is not None:
  command.extend(['--header','Content-Type: application/json','--data',json.dumps(payload,separators=(',',':'))])
 command.append('http://localhost'+endpoint)
 result=subprocess.run(command,capture_output=True,text=True,timeout=10,check=True)
 return json.loads(result.stdout) if result.stdout.strip() else None
request('/configs',method='PATCH',payload={
 'tun':{'enable':True,'dns-hijack':['any:53','tcp://any:53']},
 'ipv6':False,'unified-delay':True,'mode':'rule',
})
request('/proxies/PROXY',method='PUT',payload={'name':'My-SOCKS5-Proxy'})
for delay in (0,3,5):
 if delay: time.sleep(delay)
 config=request('/configs'); proxy=request('/proxies/PROXY')
 if (config.get('tun',{}).get('enable') is True
     and config.get('ipv6') is False
     and config.get('unified-delay') is True
     and config.get('mode')=='rule'
     and config.get('tun',{}).get('dns-hijack')==['any:53','tcp://any:53']
     and proxy.get('name')=='PROXY'
     and proxy.get('now')=='My-SOCKS5-Proxy'):
  print('RUNTIME_COMMAND_STATE=verified')
  break
else: raise SystemExit('RUNTIME_COMMAND_STATE=failed')
'''
        output = self._run_ssh(
            ["/usr/bin/python3", "-"], input_text=source, timeout=30
        ).stdout
        if output.strip() != "RUNTIME_COMMAND_STATE=verified":
            raise ClashIPError("Clash command runtime state readback failed")
        print("RUNTIME_COMMAND_STATE=verified")

    def _read_system_dns_fingerprint(self) -> str:
        source = r'''import hashlib, re, subprocess
def run(command):
 return subprocess.run(command,capture_output=True,text=True,timeout=10,check=True).stdout
route=run(['/sbin/route','-n','get','default'])
interfaces=re.findall(r'(?m)^\s*interface:\s*(\S+)\s*$',route)
if len(interfaces)!=1: raise SystemExit('DEFAULT_INTERFACE=nonunique')
blocks=re.split(r'\n\s*\n',run(['/usr/sbin/networksetup','-listallhardwareports']).strip())
services=[]
for block in blocks:
 hardware=re.search(r'(?m)^Hardware Port:\s*(.+)$',block)
 device=re.search(r'(?m)^Device:\s*(\S+)$',block)
 if hardware and device and device.group(1)==interfaces[0]: services.append(hardware.group(1).strip())
if len(services)!=1: raise SystemExit('DEFAULT_SERVICE=nonunique')
dns=run(['/usr/sbin/networksetup','-getdnsservers',services[0]])
payload=(services[0]+'\0'+dns).encode()
print('SYSTEM_DNS_FINGERPRINT='+hashlib.sha256(payload).hexdigest())
'''
        output = self._run_ssh(
            ["/usr/bin/python3", "-"], input_text=source, timeout=20
        ).stdout.strip()
        if not re.fullmatch(r"SYSTEM_DNS_FINGERPRINT=[0-9a-f]{64}", output):
            raise ClashIPError("system DNS fingerprint failed")
        return output.split("=", 1)[1]

    def capture_system_dns_fingerprint(self) -> None:
        self.system_dns_fingerprint = self._read_system_dns_fingerprint()

    def verify_system_dns_unchanged(self) -> None:
        if self.system_dns_fingerprint is None:
            raise ClashIPError("system DNS baseline is unavailable")
        if self._read_system_dns_fingerprint() != self.system_dns_fingerprint:
            raise ClashIPError("system DNS changed across Clash persistence check")
        print("SYSTEM_DNS=unchanged_verified")

    def verify_clash(self) -> None:
        if self.console_state not in {"interactive", "headless"}:
            raise ClashIPError("guest console state is unavailable")
        source = r'''import json, re, subprocess
from pathlib import Path
user,console_state=sys.argv[1:]
base=Path('/Users')/user/'Library/Application Support/io.github.clash-verge-rev.clash-verge-rev'
def one(path,key):
 if not path.is_file() or path.is_symlink(): return []
 values=[]
 for line in path.read_text().splitlines():
  match=re.match(r'^'+re.escape(key)+r':\s*(.*?)\s*$',line)
  if match: values.append(match.group(1))
 return values
required={
 'enable_tun_mode':'true','enable_system_proxy':'false',
 'enable_auto_launch':'true','enable_silent_start':'true',
 'enable_dns_settings':'false','verge_mixed_port':'7897',
}
for key,value in required.items():
 if one(base/'verge.yaml',key)!=[value]: raise SystemExit('VERGE_'+key+'=failed')
runtime_required={'ipv6':'false','unified-delay':'true','mixed-port':'7897'}
for key,value in runtime_required.items():
 if one(base/'config.yaml',key)!=[value]: raise SystemExit('CONFIG_SOURCE_'+key+'=failed')
runtime_file=base/('clash-verge-check.yaml' if console_state=='interactive' else 'clash-verge-headless.yaml')
for key,value in runtime_required.items():
 if one(runtime_file,key)!=[value]: raise SystemExit('RUNTIME_FILE_'+key+'=failed')
if console_state=='interactive':
 for key,value in runtime_required.items():
  if one(base/'clash-verge.yaml',key)!=[value]: raise SystemExit('GENERATED_FILE_'+key+'=failed')
registry=(base/'profiles.yaml').read_text()
uid='L'+user+'Socks5'
if registry.count('current: '+uid)!=1 or registry.count('- uid: '+uid)!=1: raise SystemExit('PROFILE_REGISTRY=failed')
processes=subprocess.run(['/bin/ps','-ww','-axo','user=,command='],capture_output=True,text=True,check=True).stdout.splitlines()
apps=[line for line in processes if '/Applications/Clash Verge.app/Contents/MacOS/clash-verge' in line]
cores=[line for line in processes if '/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo' in line]
if len(cores)!=1 or cores[0].split(None,1)[0]!='root' or '/Users/'+user+'/' not in cores[0] or ('/Users/'+'demo/') in cores[0]: raise SystemExit('CLASH_PROCESS=failed')
if console_state=='interactive':
 if len(apps)!=1 or apps[0].split(None,1)[0]!=user or 'clash-verge.yaml' not in cores[0] or 'clash-verge-headless.yaml' in cores[0]: raise SystemExit('CLASH_APP_PROCESS=failed')
else:
 if apps or 'clash-verge-headless.yaml' not in cores[0]: raise SystemExit('CLASH_HEADLESS_PROCESS=failed')
helper=subprocess.run(['/bin/launchctl','print','system/io.github.clash-verge-rev.clash-verge-rev.service'],capture_output=True,text=True)
helper_loaded=helper.returncode==0
helper_ok=helper_loaded if console_state=='interactive' else not helper_loaded
if not helper_ok: raise SystemExit('CLASH_HELPER=failed')
sock='/tmp/verge/verge-mihomo.sock'
def api(endpoint):
 result=subprocess.run(['/usr/bin/curl','--silent','--show-error','--fail','--unix-socket',sock,'http://localhost'+endpoint],capture_output=True,text=True,timeout=8,check=True)
 return json.loads(result.stdout)
config=api('/configs'); proxy=api('/proxies/PROXY')
if config.get('tun',{}).get('enable') is not True: raise SystemExit('RUNTIME_TUN=failed')
if config.get('ipv6') is not False: raise SystemExit('RUNTIME_IPV6=failed')
if config.get('unified-delay') is not True: raise SystemExit('RUNTIME_DELAY=failed')
if config.get('mode')!='rule': raise SystemExit('RUNTIME_MODE=failed')
if config.get('mixed-port')!=7897: raise SystemExit('RUNTIME_MIXED_PORT=failed')
if proxy.get('name')!='PROXY' or proxy.get('now')!='My-SOCKS5-Proxy': raise SystemExit('PROFILE_SELECTION=failed')
delay=api('/proxies/My-SOCKS5-Proxy/delay?timeout=5000&url=https%3A%2F%2Fwww.gstatic.com%2Fgenerate_204')
if not isinstance(delay.get('delay'),int) or delay.get('delay')<=0: raise SystemExit('PROFILE_DELAY=failed')
print('CLASH=verified\nCLASH_PROCESS=verified\nCLASH_HELPER=verified\nUI_SETTINGS=verified\nPROFILE=verified\nRUNTIME_TUN=verified')
'''.replace("from pathlib import Path\n", "from pathlib import Path\nimport sys\n")
        required_markers = {
            "CLASH=verified",
            "CLASH_PROCESS=verified",
            "CLASH_HELPER=verified",
            "UI_SETTINGS=verified",
            "PROFILE=verified",
            "RUNTIME_TUN=verified",
        }
        last_error: subprocess.CalledProcessError | None = None
        for attempt, delay in enumerate((0, 5, 15), start=1):
            if delay:
                sleep(delay)
            try:
                output = self._run_ssh(
                    ["/usr/bin/python3", "-", self.vm_name, self.console_state],
                    input_text=source,
                    timeout=30,
                ).stdout
            except subprocess.CalledProcessError as error:
                last_error = error
                continue
            markers = set(output.splitlines())
            if required_markers.issubset(markers):
                if attempt > 1:
                    print("CLASH_READBACK_RECOVERY=verified")
                break
        else:
            if last_error is not None:
                raise last_error
            raise ClashIPError("Clash controller verification failed")
        print("CLASH=verified")
        print("CLASH_PROCESS=verified")
        print("CLASH_HELPER=verified")
        print("UI_SETTINGS=verified")
        print("PROFILE=verified")
        print("RUNTIME_TUN=verified")

    def verify_egress(self) -> None:
        source = r'''import ipaddress, re, subprocess
from pathlib import Path
user=sys.argv[1]
p=Path('/Users')/user/'Downloads/socks5.yml'
values=[m.group(1).strip().strip(chr(34)).strip(chr(39)) for m in (re.match(r'^\s*server:\s*(.*?)\s*$',line) for line in p.read_text().splitlines()) if m]
if len(values)!=1: raise SystemExit('EXPECTED_PROXY_IP=nonunique')
expected=values[0]
if str(ipaddress.IPv4Address(expected))!=expected: raise SystemExit('EXPECTED_PROXY_IP=invalid')
sources=[('ipify','https://api.ipify.org'),('ip_sb','https://api.ip.sb/ip'),('icanhazip','https://icanhazip.com')]
for name,url in sources:
 try:
  result=subprocess.run(['/usr/bin/curl','--silent','--show-error','--fail','--connect-timeout','8','--max-time','15',url],capture_output=True,text=True,timeout=20)
  value=result.stdout.strip()
  if str(ipaddress.IPv4Address(value))!=value: continue
  if value!=expected: raise SystemExit('PROXY_EGRESS=mismatch')
  print('EGRESS_HTTP_SOURCE='+name+'\nPROXY_EGRESS=verified')
  break
 except (OSError,ValueError,subprocess.TimeoutExpired):
  continue
else: raise SystemExit('PROXY_EGRESS=unavailable')
'''.replace("from pathlib import Path\n", "from pathlib import Path\nimport sys\n")
        output = self._run_ssh(
            ["/usr/bin/python3", "-", self.vm_name], input_text=source, timeout=70
        ).stdout
        if "PROXY_EGRESS=verified" not in output.splitlines():
            raise ClashIPError("public egress equality failed")
        source_marker = next(
            line for line in output.splitlines() if line.startswith("EGRESS_HTTP_SOURCE=")
        )
        print(source_marker)
        print("PROXY_EGRESS=verified")

    def run(self) -> None:
        self.prepare_vm()
        with tempfile.TemporaryDirectory(prefix="utm-clash-ip-") as temporary:
            private_dir = Path(temporary)
            private_dir.chmod(0o700)
            self.verify_identity()
            values = self.read_proxy(private_dir)
            self.write_and_copy_profile(values)
            self.configure_clash()
            self.verify_clash()
            self.capture_system_dns_fingerprint()
            self.restart_clash_for_persistence_check()
            self.verify_clash()
            self.verify_system_dns_unchanged()
            print("CLASH_POST_RESTART_SETTINGS=verified")
            self.verify_egress()
        print("UTM_CLASH_IP=verified")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--application-name", required=True)
    parser.add_argument("--vm-ip")
    parser.add_argument("--parent-title")
    parser.add_argument("--page-title")
    parser.add_argument("--proxy-stdin", action="store_true")
    parser.add_argument("--database", type=Path, default=INVENTORY_DATABASE)
    parser.add_argument("--images-dir", type=Path, default=VM_IMAGES_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")
    parent_title, page_title = resolve_notion_titles(
        application_name=args.application_name,
        vm_name=args.vm_name,
        parent_title=args.parent_title,
        page_title=args.page_title,
        proxy_stdin=args.proxy_stdin,
        environment=os.environ,
    )
    runner = Runner(
        vm_name=args.vm_name,
        application_name=args.application_name,
        vm_ip=args.vm_ip,
        parent_title=parent_title,
        page_title=page_title,
        proxy_stdin=args.proxy_stdin,
        database=args.database,
        images_dir=args.images_dir,
    )
    return run_clean_cli(
        skill_name="utm-clash-ip",
        success_marker="UTM_CLASH_IP=verified",
        operation=lambda: (runner.run(), 0)[1],
        preserve_error_detail=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
