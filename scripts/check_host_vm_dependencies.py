#!/usr/bin/env python3
"""Read-only host and exact UTM guest dependency checker."""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import plistlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ssh_password import password_environment, ssh_args  # noqa: E402
from services.project_paths import SHARED_DIR  # noqa: E402


VM_NAME_RE = re.compile(r"^[a-z]{4}$")
IP_RE = re.compile(
    r"(?<![0-9])(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])"
    r"(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}(?![0-9])"
)
UUID_RE = re.compile(
    r"^[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}$"
)
MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
DATABASE = PROJECT_ROOT / "runtime" / "vm-inventory.sqlite3"
ACTIVE_CLONE_PATH = PROJECT_ROOT / "runtime" / "utm-vm-clone-active.json"
POST_CLONE_STATE_DIR = PROJECT_ROOT / "runtime" / "post-clone"
UTMCTL = shutil.which("utmctl") or "/opt/homebrew/bin/utmctl"

HOST_LABELS = (
    "宿主Python",
    "宿主Node.js",
    "宿主npm",
    "宿主Git",
    "宿主SSH",
    "宿主UTM",
    "宿主Swift",
    "宿主Pillow",
    "宿主PyObjC",
    "宿主飞书SDK",
    "宿主Playwright",
)

GUEST_LABELS = (
    "虚拟机连接",
    "虚拟机Python",
    "虚拟机Node.js",
    "虚拟机npm",
    "虚拟机Git",
    "虚拟机Microsoft Edge",
    "虚拟机Clash Verge",
    "虚拟机Xcode",
    "虚拟机PyObjC",
    "虚拟机Playwright",
    "虚拟机Fire One",
    "虚拟机证书工具",
    "虚拟机签名工具",
    "虚拟机打包工具",
    "虚拟机App Store工具",
)

GUEST_PROBES = (
    ("虚拟机Python", "command -v python3 >/dev/null 2>&1"),
    ("虚拟机Node.js", "command -v node >/dev/null 2>&1"),
    ("虚拟机npm", "command -v npm >/dev/null 2>&1"),
    ("虚拟机Git", "command -v git >/dev/null 2>&1"),
    ("虚拟机Microsoft Edge", "test -d '/Applications/Microsoft Edge.app'"),
    (
        "虚拟机Clash Verge",
        "test -x '/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo'",
    ),
    (
        "虚拟机Xcode",
        "test -d '/Applications/Xcode.app' && "
        "/usr/bin/xcodebuild -version >/dev/null 2>&1 && "
        "/usr/bin/xcrun --find xcodebuild >/dev/null 2>&1",
    ),
    (
        "虚拟机PyObjC",
        "python3 -B -c 'import ApplicationServices,AppKit,Foundation,Quartz' "
        ">/dev/null 2>&1",
    ),
    (
        "虚拟机Playwright",
        "test -f \"$HOME/Downloads/Fire_One_en1.3/node_modules/"
        "playwright-core/index.mjs\"",
    ),
    (
        "虚拟机Fire One",
        "test -f \"$HOME/Downloads/Fire_One_en1.3/package.json\" && "
        "test -f \"$HOME/Downloads/Fire_One_en1.3/src/fill-description.ts\" && "
        "test -x \"$HOME/Downloads/Fire_One_en1.3/node_modules/.bin/ts-node\"",
    ),
    (
        "虚拟机证书工具",
        "test -x /usr/bin/security && test -x /usr/bin/certtool && "
        "test -x /usr/bin/openssl",
    ),
    (
        "虚拟机签名工具",
        "test -x /usr/bin/codesign && test -x /usr/bin/dwarfdump && "
        "test -x /usr/bin/plutil",
    ),
    (
        "虚拟机打包工具",
        "test -x /usr/bin/ditto && test -x /usr/bin/unzip && "
        "test -x /usr/bin/sips",
    ),
    (
        "虚拟机App Store工具",
        "test -x \"$HOME/Downloads/apple-store-bm/apple_store_tools\"",
    ),
)


def validate_vm_name(value: str) -> str:
    vm_name = str(value).strip()
    if not VM_NAME_RE.fullmatch(vm_name):
        raise ValueError("VM name must contain exactly four lowercase letters")
    return vm_name


def render_result(label: str, installed: bool) -> str:
    return f"{label}{'已安装' if installed else '未安装'}"


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def check_host_dependencies() -> dict[str, bool]:
    command_groups = {
        "宿主Python": (sys.executable,),
        "宿主Node.js": ("node",),
        "宿主npm": ("npm",),
        "宿主Git": ("git",),
        "宿主SSH": ("ssh", "scp"),
        "宿主UTM": (UTMCTL,),
        "宿主Swift": ("swift", "swiftc"),
    }
    results = {
        label: all(
            bool(
                shutil.which(command)
                if not Path(command).is_absolute()
                else Path(command).is_file() and os.access(command, os.X_OK)
            )
            for command in commands
        )
        for label, commands in command_groups.items()
    }
    results["宿主Pillow"] = _module_available("PIL")
    results["宿主PyObjC"] = all(
        _module_available(name)
        for name in ("ApplicationServices", "AppKit", "Foundation", "Quartz")
    )
    results["宿主飞书SDK"] = _module_available("lark_oapi")
    results["宿主Playwright"] = (
        SHARED_DIR / "Fire_One_en1.3" / "node_modules" / "playwright-core" / "index.mjs"
    ).is_file()
    return {label: bool(results.get(label, False)) for label in HOST_LABELS}


def _normalize_mac(value: str) -> str:
    parts = str(value).strip().lower().replace("-", ":").split(":")
    normalized = ":".join(part.zfill(2) for part in parts)
    if not MAC_RE.fullmatch(normalized):
        raise ValueError("invalid MAC")
    return normalized


def _read_exact_inventory_record(
    vm_name: str, *, active_clone: bool = False
) -> Mapping[str, object]:
    if DATABASE.is_symlink() or not DATABASE.is_file():
        raise RuntimeError("inventory unavailable")
    database_uri = f"file:{quote(str(DATABASE.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(database_uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT vm_name, bundle_path, config_uuid, mac_address, status, "
            "directory_present, reusable, available, application_name "
            "FROM vm_inventory WHERE vm_name=?",
            (vm_name,),
        ).fetchall()
    finally:
        connection.close()
    if len(rows) != 1:
        raise RuntimeError("inventory match is not unique")
    record = dict(rows[0])
    if record.get("status") != "complete" or int(record.get("reusable") or 0) != 0:
        raise RuntimeError("inventory record is incomplete")
    if active_clone:
        if (
            int(record.get("directory_present") or 0) not in {0, 1}
            or int(record.get("available") or 0) != 0
            or record.get("application_name") is not None
        ):
            raise RuntimeError("inventory record is not the active clone")
    elif int(record.get("directory_present") or 0) != 1:
        raise RuntimeError("inventory record is incomplete")
    return record


def _verify_bundle_identity(record: Mapping[str, object]) -> str:
    bundle = Path(str(record.get("bundle_path") or "")).expanduser()
    if bundle.is_symlink() or not bundle.is_dir():
        raise RuntimeError("bundle unavailable")
    config = bundle / "config.plist"
    if config.is_symlink() or not config.is_file():
        raise RuntimeError("config unavailable")
    payload = plistlib.loads(config.read_bytes())
    config_uuid = str(payload["Information"]["UUID"]).upper()
    network = payload["Network"]
    if not isinstance(network, list) or len(network) != 1:
        raise RuntimeError("network identity is ambiguous")
    config_mac = _normalize_mac(str(network[0]["MacAddress"]))
    if (
        not UUID_RE.fullmatch(config_uuid)
        or config_uuid != str(record.get("config_uuid") or "").upper()
        or config_mac != _normalize_mac(str(record.get("mac_address") or ""))
    ):
        raise RuntimeError("bundle identity mismatch")
    return config_mac


def _read_json_object(path: Path, *, label: str) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError(f"{label} invalid") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} invalid")
    return value


def _read_active_clone_context(
    vm_name: str, record: Mapping[str, object]
) -> str:
    bundle = Path(str(record.get("bundle_path") or "")).expanduser().resolve()
    config_uuid = str(record.get("config_uuid") or "").upper()
    active = _read_json_object(ACTIVE_CLONE_PATH, label="active clone")
    expected_active = {
        "schema_version": 1,
        "vm_name": vm_name,
        "config_uuid": config_uuid,
        "bundle_path": str(bundle),
    }
    if dict(active) != expected_active:
        raise RuntimeError("active clone identity mismatch")
    state = _read_json_object(POST_CLONE_STATE_DIR / f"{vm_name}.json", label="clone state")
    if (
        state.get("vm_name") != vm_name
        or state.get("bundle") != str(bundle)
        or str(state.get("config_uuid") or "").upper() != config_uuid
    ):
        raise RuntimeError("clone state identity mismatch")
    try:
        steps = [int(value) for value in state.get("steps", [])]
    except (TypeError, ValueError) as error:
        raise RuntimeError("clone state steps invalid") from error
    if steps[:5] != [2, 3, 4, 5, 6] or steps != list(range(2, 2 + len(steps))):
        raise RuntimeError("clone state steps incomplete")
    return _verify_bundle_identity(record)


def _quiet_run(command: list[str], *, timeout: int = 12, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        **kwargs,
    )


def _arp_ips_for_mac(output: str, wanted_mac: str) -> set[str]:
    matches: set[str] = set()
    for ip_text, mac_text in re.findall(
        r"\(([^)]+)\)\s+at\s+([0-9A-Fa-f:.-]+)", output
    ):
        try:
            if _normalize_mac(mac_text) == wanted_mac:
                matches.add(str(ipaddress.IPv4Address(ip_text)))
        except ValueError:
            continue
    return matches


def _resolve_running_vm_ip(vm_name: str, registered_mac: str) -> str:
    status = _quiet_run([UTMCTL, "status", vm_name])
    states = [line.strip().lower() for line in status.stdout.splitlines() if line.strip()]
    if status.returncode != 0 or len(states) != 1 or states[0] not in {"started", "running"}:
        raise RuntimeError("VM is not running")
    result = _quiet_run([UTMCTL, "ip-address", vm_name])
    addresses = {
        str(ipaddress.IPv4Address(value))
        for value in IP_RE.findall(result.stdout if result.returncode == 0 else "")
    }
    for address in addresses:
        _quiet_run(["/sbin/ping", "-c", "1", "-W", "1000", address], timeout=3)
    arp = _quiet_run(["/usr/sbin/arp", "-an"])
    arp_matches = _arp_ips_for_mac(arp.stdout, registered_mac)
    matches = addresses & arp_matches if addresses else arp_matches
    if len(matches) != 1:
        raise RuntimeError("VM IP is not unique")
    return next(iter(matches))


def _guest_probe_script(vm_name: str) -> str:
    lines = (
        "set +e",
        f"if [[ $(/usr/bin/id -un 2>/dev/null) != {shlex.quote(vm_name)} || "
        f"$HOME != {shlex.quote(f'/Users/{vm_name}')} ]]; then exit 97; fi",
        "printf '%s\\n' '虚拟机连接=1'",
    )
    probes = tuple(
        f"if {predicate}; then printf '%s\\n' {shlex.quote(label + '=1')}; "
        f"else printf '%s\\n' {shlex.quote(label + '=0')}; fi"
        for label, predicate in GUEST_PROBES
    )
    return "\n".join((*lines, *probes))


def check_guest_dependencies(
    vm_name: str, *, active_clone: bool = False
) -> dict[str, bool] | None:
    try:
        record = _read_exact_inventory_record(vm_name, active_clone=active_clone)
        registered_mac = (
            _read_active_clone_context(vm_name, record)
            if active_clone
            else _verify_bundle_identity(record)
        )
        vm_ip = _resolve_running_vm_ip(vm_name, registered_mac)
        remote = shlex.join(("/bin/zsh", "-lc", _guest_probe_script(vm_name)))
        result = _quiet_run(
            ssh_args(vm_name, vm_ip, connect_timeout=5, tty=False) + [remote],
            timeout=30,
            stdin=subprocess.DEVNULL,
            env=password_environment(),
            cwd=PROJECT_ROOT,
        )
        if result.returncode != 0:
            return None
        parsed: dict[str, bool] = {}
        allowed = set(GUEST_LABELS)
        for line in result.stdout.splitlines():
            label, separator, value = line.strip().partition("=")
            if separator and label in allowed and value in {"0", "1"}:
                parsed[label] = value == "1"
        if parsed.get("虚拟机连接") is not True:
            return None
        return {label: bool(parsed.get(label, False)) for label in GUEST_LABELS}
    except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException, sqlite3.Error, subprocess.SubprocessError):
        return None
    except RuntimeError:
        return None


HostChecker = Callable[[], Mapping[str, bool]]
GuestChecker = Callable[[str], Mapping[str, bool] | None]


def check_dependencies(
    vm_name: str,
    *,
    host_checker: HostChecker = check_host_dependencies,
    guest_checker: GuestChecker = check_guest_dependencies,
) -> dict[str, bool]:
    exact_vm_name = validate_vm_name(vm_name)
    host = host_checker()
    guest = guest_checker(exact_vm_name)
    results = {label: bool(host.get(label, False)) for label in HOST_LABELS}
    results.update(
        {label: bool(guest.get(label, False)) if guest is not None else False for label in GUEST_LABELS}
    )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--active-clone", action="store_true")
    args = parser.parse_args(argv)
    try:
        results = check_dependencies(
            args.vm_name,
            guest_checker=lambda vm_name: check_guest_dependencies(
                vm_name, active_clone=args.active_clone
            ),
        )
    except ValueError as error:
        parser.error(str(error))
    for label, installed in results.items():
        print(render_result(label, installed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
