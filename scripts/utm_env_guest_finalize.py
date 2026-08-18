#!/usr/bin/env python3
"""Guest-only UTM-ENV asset copy and verification helper."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


GUEST_SHARED_ROOT = Path("/Volumes/My Shared Files/共享文件")


class GuestFinalizeError(RuntimeError):
    """Raised when a guest file invariant cannot be verified."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_summary(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total_size = 0
    files: list[Path] = []
    for candidate in sorted(
        path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()
    ):
        if candidate.is_symlink():
            raise GuestFinalizeError(f"asset directory contains a symlink: {path.name}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise GuestFinalizeError(
                f"asset directory contains an unsupported entry: {path.name}"
            )
        files.append(candidate)
    if not files:
        raise GuestFinalizeError(f"asset directory is empty: {path.name}")
    for candidate in files:
        relative = candidate.relative_to(path).as_posix().encode("utf-8")
        size = candidate.stat().st_size
        total_size += size
        digest.update(b"F\0")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return total_size, digest.hexdigest()


def _summary(path: Path, expected_type: str) -> dict[str, Any]:
    if path.is_symlink():
        raise GuestFinalizeError(f"asset is an unsafe symlink: {path.name}")
    if expected_type == "directory":
        if not path.is_dir():
            raise GuestFinalizeError(f"asset directory is missing: {path.name}")
        size, digest = _directory_summary(path)
    elif expected_type == "file":
        if not path.is_file():
            raise GuestFinalizeError(f"asset file is missing: {path.name}")
        size, digest = path.stat().st_size, _sha256(path)
    else:
        raise GuestFinalizeError("asset manifest contains an invalid type")
    return {"type": expected_type, "size": size, "sha256": digest}


def _validate_payload(
    payload: dict[str, Any],
) -> tuple[str, dict[str, dict[str, Any]], dict[str, Any]]:
    app_name = payload.get("app_name")
    assets = payload.get("assets")
    fire_one_env = payload.get("fire_one_env")
    if (
        not isinstance(app_name, str)
        or not app_name
        or Path(app_name).name != app_name
        or not isinstance(assets, dict)
        or not isinstance(fire_one_env, dict)
    ):
        raise GuestFinalizeError("asset payload is invalid")
    expected_names = [
        app_name,
        f"{app_name}.xlsx",
        f"{app_name}.png",
        f"{app_name}-git",
    ]
    if list(assets) != expected_names:
        raise GuestFinalizeError(
            "asset payload does not preserve exact application-name case"
        )
    normalized: dict[str, dict[str, Any]] = {}
    for name in expected_names:
        evidence = assets.get(name)
        if not isinstance(evidence, dict):
            raise GuestFinalizeError("asset payload evidence is invalid")
        expected_type = "directory" if name in {app_name, f"{app_name}-git"} else "file"
        if (
            evidence.get("type") != expected_type
            or not isinstance(evidence.get("size"), int)
            or not isinstance(evidence.get("sha256"), str)
            or len(evidence["sha256"]) != 64
        ):
            raise GuestFinalizeError(f"asset payload evidence is invalid: {name}")
        normalized[name] = {
            "type": expected_type,
            "size": evidence["size"],
            "sha256": evidence["sha256"],
        }
    if (
        fire_one_env.get("type") != "file"
        or not isinstance(fire_one_env.get("size"), int)
        or fire_one_env["size"] <= 0
        or not isinstance(fire_one_env.get("sha256"), str)
        or len(fire_one_env["sha256"]) != 64
    ):
        raise GuestFinalizeError("Fire_One_en1.3/.env evidence is invalid")
    return app_name, normalized, {
        "type": "file",
        "size": fire_one_env["size"],
        "sha256": fire_one_env["sha256"],
    }


def _verify_identity(vm_user: str) -> tuple[Path, Path, Path]:
    if os.getuid() == 0:
        raise GuestFinalizeError("guest file helper must not run as root")
    current_user = subprocess.run(
        ["/usr/bin/id", "-un"], check=True, capture_output=True, text=True
    ).stdout.strip()
    home = Path.home().resolve()
    if current_user != vm_user or home != Path(f"/Users/{vm_user}"):
        raise GuestFinalizeError("guest SSH identity does not match the exact VM user")
    downloads = home / "Downloads"
    fire_one = downloads / "Fire_One_en1.3"
    if not downloads.is_dir() or downloads.is_symlink():
        raise GuestFinalizeError("guest Downloads is missing or unsafe")
    if not fire_one.is_dir() or fire_one.is_symlink():
        raise GuestFinalizeError("guest Fire_One_en1.3 anchor is missing or unsafe")
    if not GUEST_SHARED_ROOT.is_dir() or GUEST_SHARED_ROOT.is_symlink():
        raise GuestFinalizeError("guest shared mount is missing or unsafe")
    return home, downloads, fire_one


def _prepare_studio_projects(home: Path, mode: str) -> Path:
    studio_projects = home / "StudioProjects"
    if studio_projects.parent != home or studio_projects.is_symlink():
        raise GuestFinalizeError("guest StudioProjects path is unsafe")
    if studio_projects.exists():
        if not studio_projects.is_dir():
            raise GuestFinalizeError("guest StudioProjects path is unsafe")
        return studio_projects
    if mode == "verify":
        raise GuestFinalizeError("guest StudioProjects is missing during verification")
    try:
        studio_projects.mkdir(mode=0o755)
    except OSError as error:
        raise GuestFinalizeError("guest StudioProjects creation failed") from error
    if not studio_projects.is_dir() or studio_projects.is_symlink():
        raise GuestFinalizeError("guest StudioProjects creation verification failed")
    return studio_projects


def run_assets(mode: str, vm_user: str, payload: dict[str, Any]) -> dict[str, Any]:
    if mode not in {"copy", "verify"}:
        raise GuestFinalizeError("invalid asset mode")
    app_name, assets, env_evidence = _validate_payload(payload)
    home, downloads, fire_one = _verify_identity(vm_user)
    if downloads.parent != home or fire_one.parent != downloads:
        raise GuestFinalizeError("Fire_One_en1.3 is not directly below Downloads")
    studio_projects = _prepare_studio_projects(home, mode)
    code_source_name = f"{app_name}-git"
    code_target = studio_projects / app_name

    source_env = GUEST_SHARED_ROOT / "Fire_One_en1.3" / ".env"
    target_env = fire_one / ".env"
    if (
        source_env.parent != GUEST_SHARED_ROOT / "Fire_One_en1.3"
        or target_env.parent != fire_one
    ):
        raise GuestFinalizeError("Fire_One_en1.3/.env parent path is invalid")
    if _summary(source_env, "file") != env_evidence:
        raise GuestFinalizeError(
            "guest shared Fire_One_en1.3/.env differs from host evidence"
        )
    if target_env.is_symlink():
        raise GuestFinalizeError("guest Fire_One_en1.3/.env is an unsafe symlink")
    if mode == "copy":
        result = subprocess.run(
            ["/bin/cp", "-p", str(source_env), str(target_env)],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise GuestFinalizeError("guest Fire_One_en1.3/.env refresh failed")
        target_env.chmod(0o600)
    if _summary(target_env, "file") != env_evidence:
        raise GuestFinalizeError("guest Fire_One_en1.3/.env hash verification failed")
    if target_env.stat().st_mode & 0o777 != 0o600:
        raise GuestFinalizeError("guest Fire_One_en1.3/.env mode is not 600")

    for name, expected in assets.items():
        source = GUEST_SHARED_ROOT / name
        target = code_target if name == code_source_name else downloads / name
        expected_parent = studio_projects if name == code_source_name else downloads
        if source.parent != GUEST_SHARED_ROOT or target.parent != expected_parent:
            raise GuestFinalizeError("asset parent path is invalid")
        if _summary(source, expected["type"]) != expected:
            raise GuestFinalizeError(
                f"guest shared asset differs from the host manifest: {name}"
            )
        if target.exists() or target.is_symlink():
            if _summary(target, expected["type"]) != expected:
                target_kind = (
                    "StudioProjects target" if name == code_source_name else "Downloads target"
                )
                raise GuestFinalizeError(
                    f"guest {target_kind} conflicts with shared source: {name}"
                )
            continue
        if mode == "verify":
            target_kind = (
                "StudioProjects target" if name == code_source_name else "Downloads target"
            )
            raise GuestFinalizeError(
                f"guest {target_kind} is missing during verification: {name}"
            )
        result = subprocess.run(
            [
                "/bin/cp",
                "-pR" if expected["type"] == "directory" else "-p",
                str(source),
                str(target),
            ],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0 or _summary(target, expected["type"]) != expected:
            raise GuestFinalizeError(f"guest asset copy verification failed: {name}")

    for name, expected in assets.items():
        if _summary(GUEST_SHARED_ROOT / name, expected["type"]) != expected:
            raise GuestFinalizeError(f"fresh shared-source verification failed: {name}")
        target = code_target if name == code_source_name else downloads / name
        if _summary(target, expected["type"]) != expected:
            target_kind = "StudioProjects" if name == code_source_name else "Downloads"
            raise GuestFinalizeError(f"fresh {target_kind} verification failed: {name}")
    if _summary(source_env, "file") != env_evidence:
        raise GuestFinalizeError("fresh shared Fire_One_en1.3/.env verification failed")
    if _summary(target_env, "file") != env_evidence:
        raise GuestFinalizeError("fresh Downloads Fire_One_en1.3/.env verification failed")
    return {
        "status": "verified",
        "asset_count": 4,
        "host_guest_asset_sha256": "exact",
        "fire_one_env_sha256": "exact",
        "fire_one_env_mode": "600",
        "guest_download_asset_count": 3,
        "guest_code_path": str(code_target),
        "guest_code_copy": "verified_or_identical_existing",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="UTM-ENV guest file helper")
    parser.add_argument("--mode", required=True, choices=("copy", "verify"))
    parser.add_argument("--vm-user", required=True)
    parser.add_argument("--stdin-json", action="store_true")
    args = parser.parse_args()
    try:
        if not args.stdin_json:
            raise GuestFinalizeError("asset modes require --stdin-json")
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise GuestFinalizeError("stdin payload must be a JSON object")
        result = run_assets(args.mode, args.vm_user, payload)
    except Exception as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
