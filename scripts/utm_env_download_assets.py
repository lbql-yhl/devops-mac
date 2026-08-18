#!/usr/bin/env python3
"""Download the three UTM-ENV application assets through Feishu OpenAPI."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Protocol


API_ROOT = "https://open.feishu.cn/open-apis"
DEFAULT_BASE_URL = (
    "https://qv0zc1dq6qy.feishu.cn/base/LNFdbb2cxaauuvsVGuGcebMhnxm"
    "?table=tblcENvSwMO6lhSH&view=vewfxNVEGj"
)
NAME_FIELD = "应用名"
REQUIRED_FIELDS = ("研发截图", "金币表格文件", "金币商店截图")
CODE_URL_FIELD = "代码 URL"
IMAGE_FIELDS = {"金币商店截图"}
IMAGE_EXTENSIONS = {".gif", ".heic", ".jpeg", ".jpg", ".png", ".webp"}
DEVELOPMENT_EXTENSIONS = IMAGE_EXTENSIONS | {".zip"}
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 500


@dataclass(frozen=True)
class BaseLocation:
    app_token: str
    table_id: str
    view_id: str


@dataclass(frozen=True)
class AssetPlan:
    field_name: str
    file_token: str
    source_name: str
    target_name: str


class AssetClient(Protocol):
    def list_records(self, location: BaseLocation) -> list[dict[str, Any]]: ...

    def download_media(self, file_token: str) -> bytes: ...


class GitHubClientProtocol(Protocol):
    def retrieve_repository(
        self, repo_url: str, destination: Path
    ) -> tuple[str, str]: ...


def parse_base_url(value: str) -> BaseLocation:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc.endswith("feishu.cn"):
        raise ValueError("base URL must be an HTTPS feishu.cn URL")
    match = re.fullmatch(r"/base/([A-Za-z0-9_-]+)", parsed.path.rstrip("/"))
    if match is None:
        raise ValueError("base URL does not contain a valid Base app token")
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
    return BaseLocation(
        app_token=match.group(1),
        table_id=_one_query_value(query, "table"),
        view_id=_one_query_value(query, "view"),
    )


def _one_query_value(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name, [])
    if len(values) != 1 or not values[0]:
        raise ValueError(f"base URL must contain exactly one {name} query value")
    return values[0]


def _text_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "name", "value"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    if isinstance(value, list):
        values = [item for item in (_text_value(item) for item in value) if item is not None]
        if len(values) == 1:
            return values[0]
    return None


def select_exact_record(
    records: Iterable[dict[str, Any]], app_name: str
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for record in records:
        fields = record.get("fields")
        if isinstance(fields, dict) and _text_value(fields.get(NAME_FIELD)) == app_name:
            matches.append(record)
    if len(matches) != 1:
        raise ValueError(
            f"application name must match one unique record; found {len(matches)}"
        )
    return matches[0]


def parse_github_repo_url(value: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("code URL must be a canonical HTTPS GitHub repository URL")
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:[.]git)?/?", parsed.path)
    if match is None:
        raise ValueError("code URL must identify one GitHub repository root")
    owner, repo = match.groups()
    if owner in {".", ".."} or repo in {".", ".."}:
        raise ValueError("code URL contains an unsafe repository name")
    return owner, repo


def _link_value(value: Any, field_name: str) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        link = value.get("link")
        if isinstance(link, str) and link:
            return link
    if isinstance(value, list) and len(value) == 1:
        return _link_value(value[0], field_name)
    raise ValueError(f"{field_name} must contain one non-empty URL")


def _safe_app_name(app_name: str) -> str:
    if (
        not app_name
        or app_name in {".", ".."}
        or Path(app_name).name != app_name
        or "\x00" in app_name
    ):
        raise ValueError("application name is not safe for use as a filename")
    return app_name


def _one_attachment(fields: dict[str, Any], field_name: str) -> dict[str, Any]:
    raw = fields.get(field_name)
    items = raw if isinstance(raw, list) else ([] if raw in (None, "", {}) else [raw])
    items = [item for item in items if item not in (None, "", {})]
    if len(items) != 1:
        raise ValueError(
            f"{field_name} must contain exactly one attachment; found {len(items)}"
        )
    item = items[0]
    if not isinstance(item, dict):
        raise ValueError(f"{field_name} attachment has an invalid shape")
    return item


def _attachment_plan(
    fields: dict[str, Any], field_name: str, app_name: str
) -> AssetPlan:
    item = _one_attachment(fields, field_name)
    token = item.get("file_token") or item.get("token")
    source_name = item.get("name")
    if not isinstance(token, str) or not token:
        raise ValueError(f"{field_name} attachment has no file token")
    if (
        not isinstance(source_name, str)
        or not source_name
        or Path(source_name).name != source_name
    ):
        raise ValueError(f"{field_name} attachment has no safe filename")
    suffix = Path(source_name).suffix.lower()
    if not suffix or not re.fullmatch(r"\.[a-z0-9]{1,16}", suffix):
        raise ValueError(f"{field_name} attachment has an unsafe extension")
    if field_name == "金币表格文件" and suffix != ".xlsx":
        raise ValueError("金币表格文件 must be an XLSX attachment")
    if field_name in IMAGE_FIELDS and suffix not in IMAGE_EXTENSIONS:
        raise ValueError(f"{field_name} must be a supported image attachment")
    if field_name == "研发截图" and suffix not in DEVELOPMENT_EXTENSIONS:
        raise ValueError("研发截图 must be a ZIP or supported image attachment")
    target_suffix = ".png" if field_name == "金币商店截图" else suffix
    return AssetPlan(
        field_name=field_name,
        file_token=token,
        source_name=source_name,
        target_name=f"{app_name}{target_suffix}",
    )


def plan_assets(record: dict[str, Any], app_name: str) -> list[AssetPlan]:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        raise ValueError("matching Base record has no fields")
    plans = [_attachment_plan(fields, field_name, app_name) for field_name in REQUIRED_FIELDS]
    target_names = [plan.target_name.casefold() for plan in plans]
    if len(target_names) != len(set(target_names)):
        raise ValueError("target filename collision after renaming attachments")
    return plans


def _load_env_file(env_file: Path) -> dict[str, str]:
    if not env_file.is_file():
        raise ValueError("credential env file does not exist")
    values: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_credentials(env_file: Path) -> tuple[str, str]:
    values = _load_env_file(env_file)
    app_id = os.environ.get("FEISHU_APP_ID") or values.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET") or values.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise ValueError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
    return app_id, app_secret


def _request(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    json_body: dict[str, Any] | None = None,
    label: str,
) -> bytes:
    headers = {"User-Agent": "UTM-ENV-Feishu-OpenAPI/1.0"}
    data = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                raise ValueError(f"{label} exceeds the download size limit")
            body = response.read(MAX_DOWNLOAD_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{label} failed with HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"{label} failed with network error {type(exc.reason).__name__}"
        ) from None
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"{label} exceeds the download size limit")
    return body


def _json_request(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    json_body: dict[str, Any] | None = None,
    label: str,
) -> dict[str, Any]:
    body = _request(
        url, method=method, token=token, json_body=json_body, label=label
    )
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError(f"{label} returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} returned an invalid response")
    if payload.get("code", 0) != 0:
        raise RuntimeError(f"{label} returned Feishu error code {payload.get('code')}")
    return payload


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str) -> None:
        payload = _json_request(
            f"{API_ROOT}/auth/v3/tenant_access_token/internal",
            method="POST",
            json_body={"app_id": app_id, "app_secret": app_secret},
            label="tenant token request",
        )
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("tenant token response did not contain a token")
        self._token = token
        self._media_download_urls: dict[str, str] = {}

    def list_records(self, location: BaseLocation) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            query = {"view_id": location.view_id, "page_size": "500"}
            if page_token is not None:
                query["page_token"] = page_token
            url = (
                f"{API_ROOT}/bitable/v1/apps/{location.app_token}/tables/"
                f"{location.table_id}/records?{urllib.parse.urlencode(query)}"
            )
            payload = _json_request(
                url, token=self._token, label="Base record request"
            )
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise RuntimeError("Base record response has an invalid shape")
            page_records = [item for item in data["items"] if isinstance(item, dict)]
            records.extend(page_records)
            for record in page_records:
                fields = record.get("fields")
                if not isinstance(fields, dict):
                    continue
                for value in fields.values():
                    items = value if isinstance(value, list) else [value]
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        file_token = item.get("file_token") or item.get("token")
                        download_url = item.get("url")
                        if isinstance(file_token, str) and file_token and isinstance(download_url, str):
                            parsed = urllib.parse.urlparse(download_url)
                            expected_path = f"/open-apis/drive/v1/medias/{urllib.parse.quote(file_token, safe='')}/download"
                            if (
                                parsed.scheme == "https"
                                and parsed.netloc == "open.feishu.cn"
                                and parsed.path == expected_path
                            ):
                                self._media_download_urls[file_token] = download_url
            if not data.get("has_more"):
                return records
            page_token = data.get("page_token")
            if not isinstance(page_token, str) or not page_token:
                raise RuntimeError("Base record response is missing the next page token")

    def download_media(self, file_token: str) -> bytes:
        quoted = urllib.parse.quote(file_token, safe="")
        download_url = self._media_download_urls.get(file_token)
        if download_url is None:
            download_url = f"{API_ROOT}/drive/v1/medias/{quoted}/download"
        return _request(
            download_url,
            token=self._token,
            label="attachment download",
        )


class GitHubRepositoryClient:
    def __init__(
        self,
        run_command: Any = subprocess.run,
        download_zip: Any | None = None,
    ) -> None:
        self._run_command = run_command
        self._download_zip = download_zip or self._download_repository_zip

    def retrieve_repository(
        self, repo_url: str, destination: Path
    ) -> tuple[str, str]:
        parse_github_repo_url(repo_url)
        if destination.exists():
            raise FileExistsError(f"Git repository target already exists: {destination.name}")
        environment = dict(os.environ)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        remote_result = self._run_command(
            ["/usr/bin/git", "ls-remote", "--exit-code", "--", repo_url, "HEAD"],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
            env=environment,
        )
        if remote_result.returncode != 0:
            raise RuntimeError(
                f"GitHub HEAD lookup failed with exit code {remote_result.returncode}"
            )
        remote_lines = [line.split() for line in remote_result.stdout.splitlines() if line.strip()]
        if (
            len(remote_lines) != 1
            or len(remote_lines[0]) != 2
            or remote_lines[0][1] != "HEAD"
            or re.fullmatch(r"[0-9a-f]{40}", remote_lines[0][0]) is None
        ):
            raise RuntimeError("GitHub HEAD lookup did not resolve to one commit SHA")
        commit_sha = remote_lines[0][0]

        archive = self._download_zip(repo_url, commit_sha)
        _extract_github_repository_zip(archive, destination)
        _directory_files(destination)
        return commit_sha, "github_zip"

    def _download_repository_zip(self, repo_url: str, commit_sha: str) -> bytes:
        owner, repo = parse_github_repo_url(repo_url)
        environment = dict(os.environ)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        credential = self._run_command(
            ["/usr/bin/git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
            env=environment,
        )
        if credential.returncode != 0:
            raise RuntimeError("GitHub ZIP credential lookup failed")
        values: dict[str, str] = {}
        for line in credential.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        username = values.get("username")
        password = values.get("password")
        if not username or not password:
            raise RuntimeError("GitHub ZIP credential lookup returned no saved credential")
        basic = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        url = (
            f"https://api.github.com/repos/{urllib.parse.quote(owner, safe='')}/"
            f"{urllib.parse.quote(repo, safe='')}/zipball/{commit_sha}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Basic {basic}",
                "User-Agent": "UTM-ENV-GitHub-ZIP/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                body = response.read(MAX_DOWNLOAD_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"GitHub ZIP download failed with HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"GitHub ZIP download failed with network error {type(exc.reason).__name__}"
            ) from None
        finally:
            password = None
            basic = ""
        if not body or len(body) > MAX_DOWNLOAD_BYTES:
            raise ValueError("GitHub ZIP download size is invalid")
        return body


def _extract_github_repository_zip(archive_data: bytes, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"GitHub ZIP target already exists: {destination.name}")
    try:
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            members = archive.infolist()
            if not members or len(members) > 20_000:
                raise ValueError("GitHub ZIP has an invalid member count")
            roots: set[str] = set()
            planned: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            names: set[str] = set()
            total_size = 0
            for info in members:
                pure = PurePosixPath(info.filename)
                if pure.is_absolute() or ".." in pure.parts or "\\" in info.filename:
                    raise ValueError("GitHub ZIP contains an unsafe path")
                if not pure.parts:
                    continue
                roots.add(pure.parts[0])
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError("GitHub ZIP contains an unsafe symlink")
                if info.is_dir() or len(pure.parts) == 1:
                    continue
                relative = PurePosixPath(*pure.parts[1:])
                folded = relative.as_posix().casefold()
                if folded in names:
                    raise ValueError("GitHub ZIP contains a duplicate target path")
                names.add(folded)
                total_size += info.file_size
                if total_size > MAX_DOWNLOAD_BYTES:
                    raise ValueError("GitHub ZIP exceeds the size limit")
                planned.append((info, relative))
            if len(roots) != 1 or not planned:
                raise ValueError("GitHub ZIP does not contain one repository root")

            destination.mkdir(mode=0o700)
            try:
                for info, relative in planned:
                    target = destination.joinpath(*relative.parts)
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("xb") as output:
                        remaining = info.file_size
                        while remaining:
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("GitHub ZIP member ended before its declared size")
                            output.write(chunk)
                            remaining -= len(chunk)
                        if source.read(1):
                            raise ValueError("GitHub ZIP member exceeds its declared size")
                    executable = bool((info.external_attr >> 16) & 0o111)
                    target.chmod(0o700 if executable else 0o600)
            except BaseException:
                shutil.rmtree(destination)
                raise
    except zipfile.BadZipFile:
        raise ValueError("GitHub code download is not a valid ZIP file") from None


def _detect_image_extension(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".heic"
    raise ValueError("downloaded image content does not match a supported image")


def _extract_development_archive(
    archive_data: bytes, output_root: Path, app_name: str
) -> list[Path]:
    extracted: list[Path] = []
    development_directory = output_root / app_name
    total_size = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_FILES:
                raise ValueError("development screenshot archive contains too many files")
            image_members: list[tuple[zipfile.ZipInfo, bytes, str]] = []
            for info in members:
                pure = PurePosixPath(info.filename)
                if pure.is_absolute() or ".." in pure.parts or "\\" in info.filename:
                    raise ValueError("development screenshot archive contains an unsafe path")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError("development screenshot archive contains an unsafe symlink")
                if info.is_dir() or info.filename.startswith("__MACOSX/") or pure.name.startswith("._"):
                    continue
                if info.file_size > MAX_DOWNLOAD_BYTES:
                    raise ValueError("development screenshot archive member exceeds the size limit")
                with archive.open(info) as source:
                    data = source.read(MAX_DOWNLOAD_BYTES + 1)
                if len(data) > MAX_DOWNLOAD_BYTES:
                    raise ValueError("development screenshot archive member exceeds the size limit")
                total_size += len(data)
                if total_size > MAX_DOWNLOAD_BYTES:
                    raise ValueError("development screenshot archive exceeds the size limit")
                try:
                    suffix = _detect_image_extension(data)
                except ValueError:
                    continue
                image_members.append((info, data, suffix))
    except zipfile.BadZipFile:
        raise ValueError("downloaded development screenshot is not a valid ZIP file") from None
    if not image_members:
        raise ValueError("development screenshot archive contains no supported images")
    image_members.sort(
        key=lambda item: PurePosixPath(item[0].filename).name.casefold()
    )
    development_directory.mkdir(mode=0o700)
    try:
        for index, (_, data, suffix) in enumerate(image_members, start=1):
            destination = development_directory / f"{app_name}{index}{suffix}"
            with destination.open("xb") as stream:
                stream.write(data)
            destination.chmod(0o600)
            extracted.append(destination)
    except BaseException:
        shutil.rmtree(development_directory)
        raise
    return extracted


def _directory_files(path: Path) -> list[Path]:
    if not path.is_dir() or path.is_symlink():
        raise ValueError("asset directory is not a valid non-symlink directory")
    files: list[Path] = []
    total_size = 0
    for candidate in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        if candidate.is_symlink():
            raise ValueError("GitHub code clone contains an unsafe symlink")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError("GitHub code clone contains an unsupported file type")
        files.append(candidate)
        total_size += candidate.stat().st_size
        if len(files) > 20_000 or total_size > MAX_DOWNLOAD_BYTES:
            raise ValueError("GitHub code clone exceeds the size limit")
    if not files:
        raise ValueError("asset directory contains no files")
    return files


def _validate_download(plan: AssetPlan, path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"{plan.field_name} downloaded an empty file")
    suffix = Path(plan.target_name).suffix.lower()
    if suffix == ".xlsx":
        try:
            with zipfile.ZipFile(path) as archive:
                required = {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml"}
                if not required.issubset(archive.namelist()) or archive.testzip() is not None:
                    raise ValueError("downloaded workbook is not a valid XLSX file")
        except zipfile.BadZipFile:
            raise ValueError("downloaded workbook is not a valid XLSX file") from None
    elif suffix == ".zip":
        if not zipfile.is_zipfile(path):
            raise ValueError("downloaded development screenshot is not a valid ZIP file")
    else:
        detected_suffix = _detect_image_extension(path.read_bytes())
        if plan.field_name == "金币商店截图" and detected_suffix != ".png":
            raise ValueError("金币商店截图 must contain a real PNG image")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_summary(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total_size = 0
    for candidate in _directory_files(path):
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


def download_assets(
    *,
    base_url: str,
    app_name: str,
    record_app_name: str | None = None,
    output_root: Path,
    client: AssetClient,
    github_client: GitHubClientProtocol,
) -> dict[str, Any]:
    app_name = _safe_app_name(app_name)
    record_app_name = _safe_app_name(record_app_name or app_name)
    location = parse_base_url(base_url)
    record = select_exact_record(client.list_records(location), record_app_name)
    plans = plan_assets(record, app_name)
    fields = record.get("fields")
    if not isinstance(fields, dict):
        raise ValueError("matching Base record has no fields")
    code_url = _link_value(fields.get(CODE_URL_FIELD), CODE_URL_FIELD)
    parse_github_repo_url(code_url)

    output_root = Path(output_root).expanduser().resolve()
    if output_root.exists() and (output_root.is_symlink() or not output_root.is_dir()):
        raise ValueError("output root must be a non-symlink directory")
    output_root.mkdir(parents=True, exist_ok=True)
    known_targets = [
        output_root / plan.target_name
        for plan in plans
        if plan.field_name != "研发截图"
    ]
    known_targets.append(output_root / app_name)
    known_targets.append(output_root / f"{app_name}-git")
    existing = [target.name for target in known_targets if target.exists()]
    if existing:
        raise FileExistsError(f"target already exists: {', '.join(existing)}")

    created: list[Path] = []
    try:
        final_assets: list[tuple[str, Path]] = []
        development_image_count = 0
        for plan in plans:
            data = client.download_media(plan.file_token)
            if not data or len(data) > MAX_DOWNLOAD_BYTES:
                raise ValueError(f"{plan.field_name} download size is invalid")
            suffix = Path(plan.source_name).suffix.lower()
            if plan.field_name == "研发截图":
                if suffix == ".zip":
                    development_paths = _extract_development_archive(
                        data, output_root, app_name
                    )
                else:
                    detected_suffix = _detect_image_extension(data)
                    development_directory = output_root / app_name
                    development_directory.mkdir(mode=0o700)
                    destination = development_directory / f"{app_name}1{detected_suffix}"
                    try:
                        with destination.open("xb") as stream:
                            stream.write(data)
                        destination.chmod(0o600)
                    except BaseException:
                        shutil.rmtree(development_directory)
                        raise
                    development_paths = [destination]
                created.append(output_root / app_name)
                development_image_count = len(development_paths)
                final_assets.append((plan.field_name, output_root / app_name))
                continue
            target_path = output_root / plan.target_name
            with target_path.open("xb") as stream:
                created.append(target_path)
                stream.write(data)
            target_path.chmod(0o600)
            _validate_download(plan, target_path)
            final_assets.append((plan.field_name, target_path))

        code_path = output_root / f"{app_name}-git"
        created.append(code_path)
        code_commit_sha, code_download_method = github_client.retrieve_repository(
            code_url, code_path
        )
        if code_download_method != "github_zip":
            raise ValueError("GitHub code download method is invalid")
        _directory_files(code_path)
        final_assets.append((CODE_URL_FIELD, code_path))

        expected_names = {
            "研发截图": app_name,
            "金币表格文件": f"{app_name}.xlsx",
            "金币商店截图": f"{app_name}.png",
            CODE_URL_FIELD: f"{app_name}-git",
        }
        actual_names = {field_name: path.name for field_name, path in final_assets}
        if actual_names != expected_names:
            raise ValueError("final asset filenames do not preserve the exact application name case")

        target_names = [path.name.casefold() for _, path in final_assets]
        if len(target_names) != len(set(target_names)):
            raise ValueError("target filename collision after extracting attachments")
        files: list[dict[str, Any]] = []
        for field_name, path in final_assets:
            target = output_root / path.name
            if target.is_dir():
                size, digest = _directory_summary(target)
                asset_type = "directory"
            else:
                size, digest = target.stat().st_size, _sha256(target)
                asset_type = "file"
            files.append(
                {
                    "field": field_name,
                    "name": path.name,
                    "type": asset_type,
                    "size": size,
                    "sha256": digest,
                }
            )
        return {
            "app_name": app_name,
            "record_app_name": record_app_name,
            "record_id": record.get("record_id"),
            "attachment_count": len(plans),
            "development_image_count": development_image_count,
            "code_repository_count": 1,
            "code_commit_sha": code_commit_sha,
            "code_download_method": code_download_method,
            "asset_count": len(files),
            "asset_filename_case": "exact",
            "output_root": str(output_root),
            "files": files,
        }
    except BaseException:
        for path in created:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download UTM-ENV assets from the fixed Feishu Base view."
    )
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        app_id, app_secret = load_credentials(args.env_file)
        summary = download_assets(
            base_url=args.base_url,
            app_name=args.app_name,
            output_root=args.output_root,
            client=FeishuClient(app_id, app_secret),
            github_client=GitHubRepositoryClient(),
        )
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "verified", **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
