#!/usr/bin/env python3
"""Single host entry for the complete UTM-ENV workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Protocol

PROJECT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_SOURCE_ROOT))

from scripts.ssh_password import (  # noqa: E402
    password_environment,
    scp_args,
    ssh_args,
)
from scripts.clean_cli import run_clean_cli  # noqa: E402
from scripts.utm_env_download_assets import (  # noqa: E402
    DEFAULT_BASE_URL,
    FeishuClient,
    GitHubRepositoryClient,
    download_assets,
    load_credentials,
)
from scripts.utm_env_generate import generate_env_from_notion  # noqa: E402
from services.project_paths import PROJECT_ROOT, SHARED_DIR  # noqa: E402


GUEST_SHARED_ROOT = "/Volumes/My Shared Files/共享文件"
HELPER_NAMES = ("utm_env_guest_finalize.py",)


class UTMEnvError(RuntimeError):
    """Raised when a UTM-ENV invariant cannot be verified."""


def _safe_app_name(app_name: str) -> str:
    if (
        not app_name
        or app_name in {".", ".."}
        or Path(app_name).name != app_name
        or "\x00" in app_name
    ):
        raise UTMEnvError("application name is unsafe")
    return app_name


def expected_asset_names(app_name: str) -> list[str]:
    app_name = _safe_app_name(app_name)
    return [app_name, f"{app_name}.xlsx", f"{app_name}.png", f"{app_name}-git"]


def notion_page_title(app_name: str, vm_user: str) -> str:
    app_name = _safe_app_name(app_name)
    if not re.fullmatch(r"[a-z]{4}", vm_user):
        raise UTMEnvError("VM user must be four lowercase letters")
    return f"{app_name}-{vm_user}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_summary(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    files: list[Path] = []
    for candidate in sorted(
        path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()
    ):
        if candidate.is_symlink():
            raise UTMEnvError(f"asset directory contains a symlink: {path.name}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise UTMEnvError(f"asset directory contains an unsupported entry: {path.name}")
        files.append(candidate)
    if not files:
        raise UTMEnvError(f"asset directory is empty: {path.name}")
    for candidate in files:
        relative = candidate.relative_to(path).as_posix().encode("utf-8")
        file_size = candidate.stat().st_size
        size += file_size
        digest.update(b"F\0")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(str(file_size).encode("ascii"))
        digest.update(b"\0")
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return size, digest.hexdigest()


def collect_host_manifest(shared_root: Path, app_name: str) -> dict[str, dict[str, Any]]:
    shared_root = Path(shared_root).expanduser().resolve()
    if not shared_root.is_dir() or shared_root.is_symlink():
        raise UTMEnvError("host shared root must be a non-symlink directory")
    fire_one = shared_root / "Fire_One_en1.3"
    if not fire_one.is_dir() or fire_one.is_symlink():
        raise UTMEnvError("host Fire_One_en1.3 anchor is missing or unsafe")

    manifest: dict[str, dict[str, Any]] = {}
    names = expected_asset_names(app_name)
    for index, name in enumerate(names):
        path = shared_root / name
        if path.parent != shared_root or path.is_symlink():
            raise UTMEnvError(f"host asset path is unsafe: {name}")
        expected_type = "directory" if index in {0, 3} else "file"
        if expected_type == "directory":
            if not path.is_dir():
                raise UTMEnvError(f"host asset directory is missing: {name}")
            size, digest = _directory_summary(path)
        else:
            if not path.is_file():
                raise UTMEnvError(f"host asset file is missing: {name}")
            size, digest = path.stat().st_size, _sha256(path)
        manifest[name] = {"type": expected_type, "size": size, "sha256": digest}
    if list(manifest) != names:
        raise UTMEnvError("host asset filenames do not preserve exact application-name case")
    return manifest


def collect_host_env_manifest(shared_root: Path) -> dict[str, Any]:
    shared_root = Path(shared_root).expanduser().resolve()
    if not shared_root.is_dir() or shared_root.is_symlink():
        raise UTMEnvError("host shared root must be a non-symlink directory")
    fire_one = shared_root / "Fire_One_en1.3"
    env_file = fire_one / ".env"
    if (
        not fire_one.is_dir()
        or fire_one.is_symlink()
        or not env_file.is_file()
        or env_file.is_symlink()
        or env_file.stat().st_size == 0
    ):
        raise UTMEnvError("host Fire_One_en1.3/.env is missing, empty, or unsafe")
    return {
        "type": "file",
        "size": env_file.stat().st_size,
        "sha256": _sha256(env_file),
    }


class GuestTransport(Protocol):
    def probe_identity(self) -> None: ...
    def install_helpers(self) -> None: ...
    def run_guest(self, mode: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def cleanup_helpers(self) -> None: ...


def _require_copy_result(
    result: dict[str, Any], stage: str, expected_code_path: str
) -> None:
    if (
        result.get("status") != "verified"
        or result.get("asset_count") != 4
        or result.get("guest_download_asset_count") != 3
        or result.get("guest_code_path") != expected_code_path
        or result.get("guest_code_copy") != "verified_or_identical_existing"
        or result.get("host_guest_asset_sha256") != "exact"
        or result.get("fire_one_env_sha256") != "exact"
        or result.get("fire_one_env_mode") != "600"
    ):
        raise UTMEnvError(f"{stage} three-way asset verification did not pass")


def finalize_guest(
    *,
    app_name: str,
    vm_user: str,
    shared_root: Path,
    transport: GuestTransport,
    identity_probed: bool = False,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z]{4}", vm_user):
        raise UTMEnvError("VM user must be four lowercase letters")
    expected_code_path = f"/Users/{vm_user}/StudioProjects/{_safe_app_name(app_name)}"
    manifest = collect_host_manifest(shared_root, app_name)
    env_manifest = collect_host_env_manifest(shared_root)
    payload = {
        "app_name": app_name,
        "assets": manifest,
        "fire_one_env": env_manifest,
    }
    installed = False
    try:
        if not identity_probed:
            transport.probe_identity()
        installed = True
        transport.install_helpers()
        copy_result = transport.run_guest("copy", payload)
        _require_copy_result(copy_result, "copy", expected_code_path)
        verify_result = transport.run_guest("verify", payload)
        _require_copy_result(verify_result, "fresh", expected_code_path)
        return {
            "host_guest_asset_sha256": "exact",
            "fire_one_env_sha256": "exact",
            "fire_one_env_mode": "600",
            "guest_download_asset_count": 3,
            "guest_code_path": expected_code_path,
            "guest_code_copy": "verified_or_identical_existing",
            "utm_env": "verified",
        }
    finally:
        if installed:
            transport.cleanup_helpers()


class SSHGuestTransport:
    def __init__(self, vm_user: str, vm_ip: str) -> None:
        if not vm_user.isidentifier():
            raise UTMEnvError("vm-user must be a simple macOS account name")
        if not vm_ip or any(char not in "0123456789abcdefABCDEF:." for char in vm_ip):
            raise UTMEnvError("vm-ip must be a literal IPv4/IPv6 address")
        self.vm_user = vm_user
        self.vm_ip = vm_ip
        self.home = f"/Users/{vm_user}"
        self.source_paths = [PROJECT_ROOT / "scripts" / name for name in HELPER_NAMES]
        if not all(path.is_file() for path in self.source_paths):
            raise UTMEnvError("UTM-ENV guest helper sources are incomplete")
        self.source_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.source_paths}
        self.remote_paths = {
            path.name: f"{self.home}/Downloads/.{path.stem}-{self.source_hashes[path.name][:16]}{path.suffix}"
            for path in self.source_paths
        }

    def _run(
        self,
        remote_command: str,
        *,
        input_bytes: bytes | None = None,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            ssh_args(self.vm_user, self.vm_ip, connect_timeout=5) + [remote_command],
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=password_environment(),
        )
        if result.returncode != 0:
            raise UTMEnvError(f"guest SSH command failed with exit code {result.returncode}")
        return result

    def probe_identity(self) -> None:
        quoted_user = shlex.quote(self.vm_user)
        quoted_home = shlex.quote(self.home)
        quoted_shared = shlex.quote(GUEST_SHARED_ROOT)
        command = " && ".join(
            (
                f"test \"$(/usr/bin/id -un)\" = {quoted_user}",
                f"test \"$HOME\" = {quoted_home}",
                f"test -d {quoted_home}/Downloads",
                f"test ! -L {quoted_home}/Downloads",
                f"test -d {quoted_home}/Downloads/Fire_One_en1.3",
                f"test ! -L {quoted_home}/Downloads/Fire_One_en1.3",
                f"test -d {quoted_shared}",
                f"test ! -L {quoted_shared}",
                f"test -s {quoted_shared}/Fire_One_en1.3/.env",
                f"test ! -L {quoted_shared}/Fire_One_en1.3/.env",
                f"if test -e {quoted_home}/Downloads/Fire_One_en1.3/.env || test -L {quoted_home}/Downloads/Fire_One_en1.3/.env; then test -f {quoted_home}/Downloads/Fire_One_en1.3/.env && test ! -L {quoted_home}/Downloads/Fire_One_en1.3/.env; fi",
                "/usr/bin/python3 --version >/dev/null",
            )
        )
        self._run(command, timeout=12)

    def install_helpers(self) -> None:
        for source in self.source_paths:
            remote = self.remote_paths[source.name]
            digest = self.source_hashes[source.name]
            quoted_remote = shlex.quote(remote)
            probe = self._run(
                f"if test -e {quoted_remote}; then test -f {quoted_remote} && test ! -L {quoted_remote} && /usr/bin/shasum -a 256 {quoted_remote}; else /usr/bin/printf 'ABSENT\\n'; fi"
            ).stdout.decode("utf-8", errors="replace")
            if probe.strip() == "ABSENT":
                result = subprocess.run(
                    scp_args(self.vm_user, self.vm_ip, source, remote, connect_timeout=8),
                    check=False,
                    capture_output=True,
                    env=password_environment(),
                )
                if result.returncode != 0:
                    raise UTMEnvError("guest helper upload failed")
            elif not probe.split() or probe.split()[0] != digest:
                raise UTMEnvError("guest helper target conflicts with another file")
            verify = self._run(
                f"test -f {quoted_remote} && test ! -L {quoted_remote} && /bin/chmod 600 {quoted_remote} && /usr/bin/shasum -a 256 {quoted_remote}"
            ).stdout.decode("utf-8", errors="replace")
            if not verify.split() or verify.split()[0] != digest:
                raise UTMEnvError("guest helper hash verification failed")

    @staticmethod
    def _parse_result(output: bytes) -> dict[str, Any]:
        for raw_line in reversed(output.decode("utf-8", errors="replace").replace("\r", "").splitlines()):
            line = raw_line.strip()
            if line.startswith("{") and line.endswith("}"):
                value = json.loads(line)
                if isinstance(value, dict):
                    return value
        raise UTMEnvError("guest helper did not return a JSON result")

    def run_guest(self, mode: str, payload: dict[str, Any]) -> dict[str, Any]:
        if mode not in {"copy", "verify"}:
            raise UTMEnvError("invalid guest file mode")
        helper = shlex.quote(self.remote_paths["utm_env_guest_finalize.py"])
        command = (
            f"/usr/bin/python3 -B {helper} --mode {shlex.quote(mode)} "
            f"--vm-user {shlex.quote(self.vm_user)} --stdin-json"
        )
        result = self._run(
            command,
            input_bytes=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=300,
        )
        return self._parse_result(result.stdout)

    def cleanup_helpers(self) -> None:
        for source in self.source_paths:
            remote = self.remote_paths[source.name]
            digest = self.source_hashes[source.name]
            quoted_remote = shlex.quote(remote)
            self._run(
                f"if test -e {quoted_remote}; then test -f {quoted_remote} && test ! -L {quoted_remote} && test \"$(/usr/bin/shasum -a 256 {quoted_remote} | /usr/bin/awk '{{print $1}}')\" = {shlex.quote(digest)} && /bin/rm -f {quoted_remote}; fi; test ! -e {quoted_remote}"
            )


def _download_summary_matches_manifest(
    download_summary: dict[str, Any], manifest: dict[str, dict[str, Any]]
) -> bool:
    files = download_summary.get("files")
    if not isinstance(files, list) or download_summary.get("asset_filename_case") != "exact":
        return False
    actual = {
        item.get("name"): {
            "type": item.get("type"),
            "size": item.get("size"),
            "sha256": item.get("sha256"),
        }
        for item in files
        if isinstance(item, dict)
    }
    return actual == manifest


def run(args: argparse.Namespace) -> dict[str, Any]:
    env_result = generate_env_from_notion(
        args.parent_title,
        notion_page_title(args.app_name, args.vm_user),
        args.output_root / "Fire_One_en1.3",
    )
    env_result.update({
        "NOTION_PARENT": "verified",
        "NOTION_SOURCE": "api_unique_matched_and_read",
        "ENV_DATA": "validated",
        "ENV_TARGET_DIR": "verified",
        "HOST_ENV": "GENERATED_AND_VERIFIED",
    })
    transport = SSHGuestTransport(args.vm_user, args.vm_ip)
    collect_host_env_manifest(args.output_root)
    transport.probe_identity()
    app_id, app_secret = load_credentials(args.env_file)
    download_summary = download_assets(
        base_url=args.base_url,
        app_name=args.app_name,
        record_app_name=args.asset_app_name,
        output_root=args.output_root,
        client=FeishuClient(app_id, app_secret),
        github_client=GitHubRepositoryClient(),
    )
    manifest = collect_host_manifest(args.output_root, args.app_name)
    if not _download_summary_matches_manifest(download_summary, manifest):
        raise UTMEnvError("download summary does not match the independent host manifest")
    final = finalize_guest(
        app_name=args.app_name,
        vm_user=args.vm_user,
        shared_root=args.output_root,
        transport=transport,
        identity_probed=True,
    )
    return {**env_result, **download_summary, **final}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the complete UTM-ENV workflow")
    parser.add_argument("--parent-title", required=True)
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--asset-app-name")
    parser.add_argument("--vm-ip", required=True)
    parser.add_argument("--vm-user", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output-root", type=Path, default=SHARED_DIR)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    def operation() -> int:
        started_at = time.monotonic()
        result = run(args)
        print("UTM-ENV 环境文件步骤已完成")
        print("UTM-ENV 飞书资产步骤已完成")
        print("UTM-ENV 虚拟机文件步骤已完成")
        print(f"ENV_WRITE={result['ENV_WRITE']}")
        print("ENV_READBACK=exact")
        print("HOST_ENV=GENERATED_AND_VERIFIED")
        print("HOST_GUEST_ASSET_SHA256=exact")
        print("GUEST_DOWNLOAD_ASSET_COPY=three_assets_verified_or_identical_existing")
        print(f"GUEST_CODE_PATH={result['guest_code_path']}")
        print("GUEST_CODE_COPY=verified_or_identical_existing")
        print("FIRE_ONE_ENV_SHA256=exact")
        print("FIRE_ONE_ENV_MODE=600")
        print(f"UTM_ENV_ELAPSED_SECONDS={round(time.monotonic() - started_at)}")
        print("UTM_ENV=verified")
        return 0

    return run_clean_cli(
        skill_name="utm-env",
        success_marker="UTM_ENV=verified",
        operation=operation,
        preserve_error_detail=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
