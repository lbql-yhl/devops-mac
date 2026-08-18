#!/usr/bin/env python3
"""Resolve UTM-IMAGE screenshot input from the fixed Feishu Base view."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.utm_env_download_assets import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEVELOPMENT_EXTENSIONS,
    BaseLocation,
    FeishuClient,
    load_credentials,
    parse_base_url,
    select_exact_record,
)


BEAUTY_FIELD = "美女截图 链接"
DEVELOPMENT_FIELD = "研发截图"
MANIFEST_NAME = "source.json"
MAX_STDIN_BYTES = 65_536


class ScreenshotSourceClient(Protocol):
    def list_records(self, location: BaseLocation) -> list[dict[str, Any]]: ...

    def download_media(self, file_token: str) -> bytes: ...


@dataclass(frozen=True)
class ScreenshotSelection:
    source_field: str
    kind: str
    url: str | None = None
    file_token: str | None = None
    source_name: str | None = None


def _text_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("text", "value", "link", "url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    if isinstance(value, list):
        for item in value:
            if candidate := _text_value(item):
                return candidate
    return None


def _field_is_truly_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return all(_field_is_truly_empty(item) for item in value)
    if isinstance(value, dict):
        return not value or all(_field_is_truly_empty(item) for item in value.values())
    return False


def _beauty_share_url(value: Any) -> str | None:
    if _field_is_truly_empty(value):
        return None
    if isinstance(value, (list, tuple)):
        items = [item for item in value if not _field_is_truly_empty(item)]
        if len(items) != 1:
            raise ValueError(
                f"{BEAUTY_FIELD} must contain exactly one Wenshushu link"
            )
        value = items[0]
    candidate = _text_value(value)
    if candidate is None:
        raise ValueError(f"{BEAUTY_FIELD} must contain one Wenshushu link")
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{BEAUTY_FIELD} must contain one valid Wenshushu link") from error
    hostname = (parsed.hostname or "").casefold()
    allowed_host = hostname == "c.wss.ink" or (
        hostname == "wenshushu.cn" or hostname.endswith(".wenshushu.cn")
    )
    if (
        parsed.scheme.casefold() != "https"
        or not allowed_host
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/f/")
        or not parsed.path.removeprefix("/f/").strip("/")
    ):
        raise ValueError(f"{BEAUTY_FIELD} must contain one valid Wenshushu link")
    return candidate


def _development_attachment(value: Any) -> tuple[str, str]:
    items = value if isinstance(value, list) else ([] if value in (None, "", {}) else [value])
    items = [item for item in items if item not in (None, "", {})]
    if len(items) != 1:
        raise ValueError(
            f"研发截图 must contain exactly one attachment; found {len(items)}"
        )
    item = items[0]
    if not isinstance(item, dict):
        raise ValueError("研发截图 attachment has an invalid shape")
    token = item.get("file_token") or item.get("token")
    name = item.get("name")
    if not isinstance(token, str) or not token.strip() or token != token.strip():
        raise ValueError("研发截图 attachment has no file token")
    if (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise ValueError("研发截图 attachment has no safe filename")
    suffix = Path(name).suffix.lower()
    if suffix not in DEVELOPMENT_EXTENSIONS:
        raise ValueError("研发截图 must be a ZIP or supported image attachment")
    return token, name


def select_screenshot_source(fields: dict[str, Any]) -> ScreenshotSelection:
    raw_beauty = fields.get(BEAUTY_FIELD)
    beauty = _beauty_share_url(raw_beauty)
    if beauty is not None:
        return ScreenshotSelection(
            source_field=BEAUTY_FIELD,
            kind="share_url",
            url=beauty,
        )

    token, name = _development_attachment(fields.get(DEVELOPMENT_FIELD))
    return ScreenshotSelection(
        source_field=DEVELOPMENT_FIELD,
        kind="attachment",
        file_token=token,
        source_name=name,
    )


def _secure_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(path)
    path.chmod(0o600)


def prepare_screenshot_source(
    *,
    base_url: str,
    app_name: str,
    output_dir: Path,
    client: ScreenshotSourceClient,
) -> dict[str, Any]:
    if not app_name or app_name != app_name.strip():
        raise ValueError("application name must be non-empty and already normalized")
    output_dir = Path(output_dir).resolve(strict=True)
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError("output directory must be a real directory")
    manifest_path = output_dir / MANIFEST_NAME
    if manifest_path.exists() or manifest_path.is_symlink():
        raise FileExistsError("screenshot source manifest already exists")

    location = parse_base_url(base_url)
    record = select_exact_record(client.list_records(location), app_name)
    fields = record.get("fields")
    if not isinstance(fields, dict):
        raise ValueError("matching Base record has no fields")
    selection = select_screenshot_source(fields)

    if selection.kind == "share_url":
        assert selection.url is not None
        payload = {
            "status": "verified",
            "source_field": selection.source_field,
            "kind": selection.kind,
            "url": selection.url,
            "source_identity_sha256": hashlib.sha256(
                selection.url.encode("utf-8")
            ).hexdigest(),
        }
    else:
        assert selection.file_token is not None and selection.source_name is not None
        data = client.download_media(selection.file_token)
        if not data:
            raise ValueError("研发截图 attachment download is empty")
        suffix = Path(selection.source_name).suffix.lower()
        attachment_path = output_dir / f"development-source{suffix}"
        if attachment_path.exists() or attachment_path.is_symlink():
            raise FileExistsError("development screenshot target already exists")
        descriptor = os.open(
            attachment_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
        except BaseException:
            attachment_path.unlink(missing_ok=True)
            raise
        attachment_path.chmod(0o600)
        payload = {
            "status": "verified",
            "source_field": selection.source_field,
            "kind": selection.kind,
            "path": str(attachment_path),
            "source_name": selection.source_name,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "source_identity_sha256": hashlib.sha256(
                selection.file_token.encode("utf-8")
            ).hexdigest(),
        }

    payload["app_name_sha256"] = hashlib.sha256(app_name.encode("utf-8")).hexdigest()
    _secure_json(manifest_path, payload)
    return payload


def _read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        raise ValueError("stdin JSON is too large")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("stdin must contain one JSON object")
    if set(payload) != {"app_name", "output_dir"}:
        raise ValueError("stdin must contain exactly app_name and output_dir")
    return payload


def main() -> int:
    try:
        payload = _read_stdin_json()
        app_name = payload["app_name"]
        output_dir = payload["output_dir"]
        if not isinstance(app_name, str) or not isinstance(output_dir, str):
            raise ValueError("app_name and output_dir must be strings")
        credentials = load_credentials(PROJECT_ROOT / ".env")
        result = prepare_screenshot_source(
            base_url=DEFAULT_BASE_URL,
            app_name=app_name,
            output_dir=Path(output_dir),
            client=FeishuClient(*credentials),
        )
    except Exception as exc:
        message = re.sub(r"https?://\S+", "[redacted-url]", str(exc))
        print(json.dumps({"status": "error", "error": message}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": "verified",
                "source_field": result["source_field"],
                "kind": result["kind"],
                "manifest": str(Path(payload["output_dir"]) / MANIFEST_NAME),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
