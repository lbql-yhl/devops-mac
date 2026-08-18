#!/usr/bin/env python3
"""Safely verify or copy an already-authorized UTM shared-directory bookmark."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any


UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
DOMAIN = "com.utmapp.UTM"


def require_uuid(value: str) -> str:
    if not UUID_RE.fullmatch(value):
        raise ValueError("invalid UTM UUID")
    return value.upper()


def require_share_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("share path must be an absolute non-symlink path")
    return str(path)


def read_preferences(path: Path) -> dict[str, Any]:
    data = plistlib.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise ValueError("UTM preferences root is not a dictionary")
    return data


def write_preferences(path: Path, data: dict[str, Any]) -> None:
    encoded = plistlib.dumps(data, fmt=plistlib.FMT_BINARY, sort_keys=False)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def registry_entry(data: dict[str, Any], uuid: str) -> dict[str, Any]:
    registry = data.get("Registry")
    if not isinstance(registry, dict):
        raise ValueError("UTM Registry is missing")
    entry = registry.get(uuid)
    if not isinstance(entry, dict):
        raise ValueError("UTM Registry entry is missing")
    return entry


def matching_entries(entry: dict[str, Any], share: str) -> list[dict[str, Any]]:
    values = entry.get("SharedDirectories", [])
    if not isinstance(values, list):
        raise ValueError("SharedDirectories is not a list")
    result = []
    for value in values:
        if isinstance(value, dict) and value.get("Path") == share:
            result.append(value)
    return result


def verify_shared_directory(path: Path, target_uuid: str, share: str) -> dict[str, Any]:
    target_uuid = require_uuid(target_uuid)
    share = require_share_path(share)
    entries = matching_entries(registry_entry(read_preferences(path), target_uuid), share)
    if len(entries) != 1:
        raise ValueError(f"shared-directory count is {len(entries)}")
    entry = entries[0]
    if entry.get("ReadOnly") is not True:
        raise ValueError("shared-directory is not read-only")
    bookmark = entry.get("Bookmark")
    if not isinstance(bookmark, bytes) or not bookmark:
        raise ValueError("shared-directory bookmark is missing")
    return {"count": 1, "read_only": True, "bookmark_bytes": len(bookmark)}


def sync_shared_directory(path: Path, source_uuid: str, target_uuid: str, share: str) -> bool:
    source_uuid = require_uuid(source_uuid)
    target_uuid = require_uuid(target_uuid)
    share = require_share_path(share)
    data = read_preferences(path)
    source = registry_entry(data, source_uuid)
    target = registry_entry(data, target_uuid)
    source_matches = matching_entries(source, share)
    if len(source_matches) != 1:
        raise ValueError(f"source shared-directory count is {len(source_matches)}")
    source_entry = source_matches[0]
    if source_entry.get("ReadOnly") is not True or not isinstance(source_entry.get("Bookmark"), bytes) or not source_entry["Bookmark"]:
        raise ValueError("source shared-directory is not a usable read-only bookmark")
    target_matches = matching_entries(target, share)
    if len(target_matches) == 1:
        current = target_matches[0]
        if current.get("ReadOnly") is True and current.get("Bookmark") == source_entry["Bookmark"]:
            return False
    if len(target_matches) > 1:
        raise ValueError(f"target shared-directory count is {len(target_matches)}")
    directories = target.get("SharedDirectories", [])
    if not isinstance(directories, list):
        raise ValueError("target SharedDirectories is not a list")
    target["SharedDirectories"] = [item for item in directories if not (isinstance(item, dict) and item.get("Path") == share)]
    target["SharedDirectories"].append({"Path": share, "ReadOnly": True, "Bookmark": source_entry["Bookmark"]})
    write_preferences(path, data)
    verify_shared_directory(path, target_uuid, share)
    return True


def export_preferences(destination: Path) -> None:
    result = subprocess.run(["defaults", "export", DOMAIN, "-"], check=True, stdout=subprocess.PIPE)
    destination.write_bytes(result.stdout)


def import_preferences(source: Path) -> None:
    subprocess.run(["defaults", "import", DOMAIN, str(source)], check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("verify", "sync"))
    parser.add_argument("--target-uuid", required=True)
    parser.add_argument("--share-path", required=True)
    parser.add_argument("--source-uuid")
    parser.add_argument("--preferences-path", type=Path)
    args = parser.parse_args()
    if args.action == "sync" and not args.source_uuid:
        parser.error("sync requires --source-uuid")
    if args.preferences_path:
        path = args.preferences_path
        changed = sync_shared_directory(path, args.source_uuid, args.target_uuid, args.share_path) if args.action == "sync" else False
        evidence = verify_shared_directory(path, args.target_uuid, args.share_path)
    else:
        with tempfile.TemporaryDirectory(prefix="utm-registry-share.") as directory:
            path = Path(directory) / "preferences.plist"
            export_preferences(path)
            changed = sync_shared_directory(path, args.source_uuid, args.target_uuid, args.share_path) if args.action == "sync" else False
            evidence = verify_shared_directory(path, args.target_uuid, args.share_path)
            if changed:
                import_preferences(path)
    print(f"UTM_SHARE_CHANGED={'true' if changed else 'false'}")
    print("UTM_SHARE_READONLY=verified")
    print(f"UTM_SHARE_BOOKMARK_BYTES={evidence['bookmark_bytes']}")


if __name__ == "__main__":
    main()
