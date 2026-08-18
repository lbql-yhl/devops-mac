#!/usr/bin/env python3
"""Standalone, nonvisual UTM macOS clone workflow.

The command deliberately has no Feishu/run/name inputs. It allocates a unique
four-letter name in the local SQLite inventory, clones the configured template,
refreshes the UTM backend identity, and verifies the package and registration.
It never starts the cloned guest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.project_paths import PROJECT_ROOT, VM_IMAGES_DIR, VM_TEMPLATE  # noqa: E402
from scripts.vm_inventory import (  # noqa: E402
    InventoryError,
    list_records,
    register_clone,
    reserve_vm_name,
    update_status,
)


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
WAIT_SECONDS = 3.0
REGISTRATION_ATTEMPTS = 11
STORAGE_RESERVE_BYTES = 5 * 1024 * 1024 * 1024


class CloneError(RuntimeError):
    """Raised when cloning or independent verification cannot continue."""


def normalize_mac(value: str) -> str:
    compact = re.sub(r"[:-]", "", str(value).strip().lower())
    if not re.fullmatch(r"[0-9a-f]{12}", compact):
        raise CloneError("invalid MAC address")
    return ":".join(compact[i : i + 2] for i in range(0, 12, 2))


def is_local_unicast_mac(value: str) -> bool:
    try:
        mac = normalize_mac(value)
    except CloneError:
        return False
    first = int(mac[:2], 16)
    return bool(first & 0x02) and not bool(first & 0x01)


def generate_local_mac(used: set[str]) -> str:
    normalized = {normalize_mac(item) for item in used}
    for _ in range(512):
        raw = bytearray(secrets.token_bytes(6))
        raw[0] = (raw[0] & 0xFC) | 0x02
        value = ":".join(f"{part:02x}" for part in raw)
        if value not in normalized and is_local_unicast_mac(value):
            return value
    raise CloneError("unable to generate unique local MAC")


def new_machine_identifier() -> bytes:
    ecid = secrets.randbits(64) or 1
    return plistlib.dumps({"ECID": ecid}, fmt=plistlib.FMT_BINARY, sort_keys=False)


def _plist_format(data: bytes) -> int:
    return plistlib.FMT_BINARY if data.startswith(b"bplist00") else plistlib.FMT_XML


def rewrite_config_bytes(
    source_bytes: bytes,
    vm_name: str,
    config_uuid: str,
    mac_address: str,
    machine_identifier: bytes,
) -> bytes:
    if not re.fullmatch(r"[a-z]{4}", vm_name):
        raise CloneError("VM name must be four lowercase letters")
    if not UUID_RE.fullmatch(config_uuid):
        raise CloneError("config UUID is invalid")
    mac_address = normalize_mac(mac_address)
    if not is_local_unicast_mac(mac_address):
        raise CloneError("MAC must be locally administered unicast")
    if not isinstance(machine_identifier, bytes) or not machine_identifier.startswith(b"bplist00"):
        raise CloneError("MachineIdentifier must be a binary plist")
    data = plistlib.loads(source_bytes)
    if not isinstance(data, dict):
        raise CloneError("config plist root must be a dictionary")
    information = data.get("Information")
    network = data.get("Network")
    system = data.get("System")
    if not isinstance(information, dict) or not isinstance(network, list) or len(network) != 1:
        raise CloneError("config Information/Network shape is invalid")
    if not isinstance(network[0], dict) or not isinstance(system, dict):
        raise CloneError("config Network/System shape is invalid")
    mac_platform = system.get("MacPlatform")
    if not isinstance(mac_platform, dict) or not isinstance(mac_platform.get("HardwareModel"), bytes):
        raise CloneError("config HardwareModel is missing")
    data = dict(data)
    data["Information"] = dict(information)
    data["Information"]["Name"] = vm_name
    data["Information"]["UUID"] = config_uuid.upper()
    data["Network"] = [dict(network[0])]
    data["Network"][0]["MacAddress"] = mac_address
    data["System"] = dict(system)
    data["System"]["MacPlatform"] = dict(mac_platform)
    data["System"]["MacPlatform"]["MachineIdentifier"] = machine_identifier
    return plistlib.dumps(data, fmt=_plist_format(source_bytes), sort_keys=False)


def read_identity(config_path: Path) -> dict[str, Any]:
    data = plistlib.loads(config_path.read_bytes())
    try:
        information = data["Information"]
        network = data["Network"]
        platform = data["System"]["MacPlatform"]
        config_uuid = str(information["UUID"]).upper()
        mac = normalize_mac(str(network[0]["MacAddress"]))
        machine_identifier = platform["MachineIdentifier"]
        hardware_model = platform["HardwareModel"]
    except (KeyError, IndexError, TypeError, ValueError, CloneError) as error:
        raise CloneError(f"invalid config identity: {error}") from error
    if not UUID_RE.fullmatch(config_uuid) or not is_local_unicast_mac(mac):
        raise CloneError("config identity is invalid")
    if not isinstance(machine_identifier, bytes) or not isinstance(hardware_model, bytes):
        raise CloneError("config MachineIdentifier/HardwareModel must be bytes")
    try:
        parsed_machine = plistlib.loads(machine_identifier)
        ecid = parsed_machine["ECID"]
    except (ValueError, KeyError, TypeError) as error:
        raise CloneError(f"MachineIdentifier is not a valid binary plist: {error}") from error
    if not isinstance(ecid, int) or ecid == 0:
        raise CloneError("MachineIdentifier ECID must be a nonzero integer")
    return {
        "uuid": config_uuid,
        "mac": mac,
        "machine_identifier": machine_identifier,
        "machine_identifier_sha256": hashlib.sha256(machine_identifier).hexdigest(),
        "hardware_model": hardware_model,
        "ecid": ecid,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_manifest(root: Path, excluded: set[str]) -> dict[str, dict[str, Any]]:
    root = root.resolve()
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        # exFAT/SMB may materialize macOS resource/finder metadata as
        # AppleDouble sidecars (``._foo``).  They are filesystem metadata,
        # not part of the logical UTM bundle, and writing the clone marker can
        # create a sidecar that does not exist in the source template.
        if any(part.startswith("._") for part in relative_path.parts):
            continue
        relative = relative_path.as_posix()
        if relative in excluded or any(relative.startswith(f"{item}/") for item in excluded):
            continue
        mode = path.lstat().st_mode & 0o777
        if path.is_symlink():
            result[relative] = {"type": "symlink", "mode": mode, "target": os.readlink(path)}
        elif path.is_dir():
            result[relative] = {"type": "directory", "mode": mode}
        elif path.is_file():
            result[relative] = {
                "type": "file",
                "mode": mode,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        else:
            raise CloneError(f"unsupported package entry: {relative}")
    return result


def manifest_sha256(manifest: dict[str, dict[str, Any]]) -> str:
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def compare_manifests(source: Path, destination: Path, excluded: set[str]) -> list[str]:
    left = package_manifest(source, excluded)
    right = package_manifest(destination, excluded)
    mismatches = []
    for relative in sorted(set(left) | set(right)):
        if left.get(relative) != right.get(relative):
            mismatches.append(relative)
    return mismatches


def parse_utmctl_list(output: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 3 or not UUID_RE.fullmatch(fields[0]):
            continue
        rows.append({"uuid": fields[0].upper(), "status": fields[1].lower(), "name": fields[2]})
    return rows


def run_command(command: list[str]) -> str:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "output", "") or str(error)
        raise CloneError(f"command failed: {' '.join(command)}: {detail.strip()}") from error


def target_status_rows(uuid_value: str, name: str) -> list[dict[str, str]]:
    return [row for row in parse_utmctl_list(run_command(["utmctl", "list"])) if row["uuid"] == uuid_value and row["name"] == name]


def read_registry() -> dict[str, Any]:
    try:
        raw = subprocess.check_output(["defaults", "export", "com.utmapp.UTM", "-"], stderr=subprocess.STDOUT)
        value = plistlib.loads(raw)
    except (OSError, subprocess.CalledProcessError, plistlib.InvalidFileException) as error:
        detail = getattr(error, "output", b"") or str(error)
        raise CloneError(f"UTM Registry read failed: {detail}") from error
    if not isinstance(value, dict):
        raise CloneError("UTM Registry root is not a dictionary")
    return value


def registry_matches(registry: dict[str, Any], uuid_value: str, name: str, bundle: Path) -> list[dict[str, str]]:
    entries = registry.get("Registry", registry)
    if not isinstance(entries, dict):
        return []
    entry = entries.get(uuid_value)
    if not isinstance(entry, dict):
        return []
    package = entry.get("Package")
    path = package.get("Path") if isinstance(package, dict) else None
    entry_name = str(entry.get("Name") or "")
    if entry_name == name and str(path or "") == str(bundle):
        return [{"uuid": uuid_value, "name": name, "path": str(bundle)}]
    return []


def wait_for_registration(
    uuid_value: str, name: str, bundle: Path
) -> tuple[list[dict[str, str]], list[dict[str, str]], bool]:
    rows: list[dict[str, str]] = []
    matches: list[dict[str, str]] = []
    registry_available = False
    for attempt in range(REGISTRATION_ATTEMPTS):
        rows = target_status_rows(uuid_value, name)
        registry = read_registry()
        registry_available = bool(registry)
        matches = registry_matches(registry, uuid_value, name, bundle)
        if registration_verified(rows, matches, registry_available):
            return rows, matches, registry_available
        if attempt + 1 < REGISTRATION_ATTEMPTS:
            time.sleep(WAIT_SECONDS)
    return rows, matches, registry_available


def registration_verified(
    rows: list[dict[str, str]],
    matches: list[dict[str, str]],
    registry_available: bool,
) -> bool:
    cli_verified = len(rows) == 1 and rows[0]["status"] == "stopped"
    return cli_verified and (not registry_available or len(matches) == 1)


def atomic_write(path: Path, payload: bytes, mode: int | None = None) -> None:
    original_mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode if mode is not None else original_mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_marker(path: Path, marker: dict[str, Any]) -> None:
    atomic_write(path, (json.dumps(marker, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(), 0o600)


def template_name(template: Path) -> str:
    data = plistlib.loads((template / "config.plist").read_bytes())
    return str(data.get("Information", {}).get("Name") or template.stem)


def require_template_stopped(template_name_value: str) -> None:
    statuses = []
    for _ in range(2):
        output = run_command(["utmctl", "status", template_name_value])
        values = [line.strip().lower() for line in output.splitlines() if line.strip()]
        if len(values) != 1:
            raise CloneError(f"ambiguous template status: {values}")
        statuses.append(values[0])
        if len(statuses) == 1:
            time.sleep(WAIT_SECONDS)
    if statuses != ["stopped", "stopped"]:
        raise CloneError(f"template must be stopped: {statuses}")


def existing_identities(images_dir: Path) -> list[dict[str, Any]]:
    identities = []
    for bundle in sorted(images_dir.glob("*.utm")):
        if not bundle.is_dir() or not (bundle / "config.plist").is_file():
            continue
        try:
            identity = read_identity(bundle / "config.plist")
        except CloneError:
            continue
        identity["bundle"] = bundle
        identities.append(identity)
    return identities


def package_allocated_bytes(source: Path) -> int:
    return sum(
        path.stat(follow_symlinks=False).st_blocks * 512
        for path in source.rglob("*")
        if not path.is_symlink()
    )


def require_clone_capacity(source: Path, images_dir: Path) -> None:
    required = package_allocated_bytes(source) + STORAGE_RESERVE_BYTES
    available = shutil.disk_usage(images_dir).free
    if available < required:
        raise CloneError(
            f"insufficient clone storage: required={required} available={available}"
        )


def verify_identity_unique(identity: dict[str, Any], existing: Iterable[dict[str, Any]]) -> None:
    for other in existing:
        if identity["uuid"] == other["uuid"] or identity["mac"] == other["mac"] or identity["machine_identifier_sha256"] == other["machine_identifier_sha256"]:
            raise CloneError(f"clone identity collides with {other.get('bundle', '<inventory>')}")


def clone_once(database: Path) -> dict[str, str]:
    source = VM_TEMPLATE.expanduser().resolve()
    images_dir = VM_IMAGES_DIR.expanduser().resolve()
    if not source.is_dir() or source.is_symlink() or not (source / "config.plist").is_file():
        raise CloneError(f"configured template is missing: {source}")
    if not images_dir.is_dir() or images_dir.is_symlink():
        raise CloneError(f"configured image directory is invalid: {images_dir}")
    require_clone_capacity(source, images_dir)
    require_template_stopped(template_name(source))
    name = reserve_vm_name(database, images_dir)
    destination = images_dir / f"{name}.utm"
    attempt_id = str(uuid.uuid4())
    marker_path = destination / ".submission-clone.json"
    marker = {"attempt_id": attempt_id, "vm_name": name, "source": str(source), "status": "copying"}
    try:
        if destination.exists() or destination.is_symlink():
            raise CloneError(f"reserved destination already exists: {destination}")
        destination.mkdir(mode=0o700)
        write_marker(marker_path, marker)
        update_status(database, name, "copying", attempt_id=attempt_id)
        run_command(["/usr/bin/ditto", f"{source}/.", f"{destination}/"])
        destination_config = destination / "config.plist"
        if not destination_config.is_file():
            raise CloneError("copied config.plist is missing")
        source_identity = read_identity(source / "config.plist")
        historical = list_records(database)
        existing = [item for item in existing_identities(images_dir) if item.get("bundle") != destination]
        used_macs = {item["mac"] for item in existing} | {
            str(item["mac_address"]).lower()
            for item in historical
            if item.get("mac_address")
        }
        config_uuid = str(uuid.uuid4()).upper()
        new_mac = generate_local_mac(used_macs | {source_identity["mac"]})
        machine_identifier = new_machine_identifier()
        before = destination_config.read_bytes()
        rewritten = rewrite_config_bytes(before, name, config_uuid, new_mac, machine_identifier)
        atomic_write(destination_config, rewritten)
        after = read_identity(destination_config)
        if after["uuid"] != config_uuid or after["mac"] != new_mac or after["machine_identifier"] != machine_identifier:
            atomic_write(destination_config, before)
            raise CloneError("config identity readback mismatch; original bytes restored")
        if after["hardware_model"] != source_identity["hardware_model"]:
            atomic_write(destination_config, before)
            raise CloneError("HardwareModel changed; original bytes restored")
        verify_identity_unique(after, existing)
        for record in historical:
            if record.get("config_uuid") == after["uuid"] or record.get("machine_identifier_sha256") == after["machine_identifier_sha256"]:
                raise CloneError("clone identity collides with historical inventory")
        excluded = {"config.plist", ".submission-clone.json"}
        mismatches = compare_manifests(source, destination, excluded)
        if mismatches:
            raise CloneError(f"package manifest mismatch: {mismatches[:5]}")
        source_manifest_sha = manifest_sha256(package_manifest(source, excluded))
        destination_manifest_sha = manifest_sha256(package_manifest(destination, excluded))
        rows = target_status_rows(config_uuid, name)
        registry = read_registry()
        registry_available = bool(registry)
        matches = registry_matches(registry, config_uuid, name, destination)
        if not rows and not matches:
            # UTM has no CLI register subcommand; opening the exact bundle is
            # the one system registration call allowed by the skill. It does
            # not issue utmctl start and the guest remains stopped.
            subprocess.run(["open", str(destination)], check=True)
        if not registration_verified(rows, matches, registry_available):
            rows, matches, registry_available = wait_for_registration(
                config_uuid, name, destination
            )
        if not registration_verified(rows, matches, registry_available):
            raise CloneError(
                "UTM registration mismatch: "
                f"cli={rows!r} registry_available={registry_available!r} "
                f"registry={matches!r}"
            )
        config_sha = _sha256(destination_config)
        marker.update(
            {
                "status": "complete",
                "config_uuid": config_uuid,
                "mac_address": new_mac,
                "machine_identifier_sha256": after["machine_identifier_sha256"],
                "source_manifest_sha256": source_manifest_sha,
                "destination_manifest_sha256": destination_manifest_sha,
                "config_sha256": config_sha,
                "completed_at": time.time(),
            }
        )
        write_marker(marker_path, marker)
        register_clone(
            database,
            name,
            config_uuid,
            destination,
            new_mac,
            after["machine_identifier_sha256"],
            attempt_id=attempt_id,
            status="complete",
        )
        return {
            "vm_name": name,
            "attempt_id": attempt_id,
            "config_uuid": config_uuid,
            "mac": new_mac,
            "source_manifest_sha256": source_manifest_sha,
            "destination_manifest_sha256": destination_manifest_sha,
            "config_sha256": config_sha,
        }
    except Exception:
        try:
            update_status(database, name, "failed", attempt_id=attempt_id)
        except Exception:
            pass
        if marker_path.parent.exists():
            marker["status"] = "failed"
            try:
                write_marker(marker_path, marker)
            except Exception:
                pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone nonvisual UTM macOS clone")
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "runtime" / "vm-inventory.sqlite3")
    parser.add_argument("--check-only", action="store_true", help="validate configured template and database without cloning")
    args = parser.parse_args()
    try:
        if args.check_only:
            source = VM_TEMPLATE.expanduser().resolve()
            if not source.is_dir() or not (source / "config.plist").is_file():
                raise CloneError(f"configured template is missing: {source}")
            require_template_stopped(template_name(source))
            print(f"TEMPLATE={source}")
            print("TEMPLATE_STATUS=stopped")
            print("UTM_CLONE_CHECK=verified")
            return 0
        result = clone_once(args.database.expanduser().resolve())
        for key, value in result.items():
            print(f"{key.upper()}={value}")
        print("CLONE_MARKER=verified")
        print("CLONE_CONFIG_IDENTITY=verified")
        print("UTM_REGISTRATION_MATCH_COUNT=1")
        print("UTM_REGISTRATION_STATE=stopped")
        print("UTM_CLONE_MACOS=verified")
        return 0
    except (CloneError, InventoryError, OSError, subprocess.SubprocessError) as error:
        print(f"UTM_CLONE_MACOS=blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
