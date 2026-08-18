"""Validate and resolve the Feishu Base source used by ``utm_notion``."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping


API_BASE = "https://open.feishu.cn/open-apis"
SOURCE_ENV_KEYS = frozenset(
    {
        "UTM_NOTION_FEISHU_WIKI_URL",
        "UTM_NOTION_FEISHU_APP_TOKEN",
        "UTM_NOTION_FEISHU_TABLE_ID",
        "UTM_NOTION_FEISHU_VIEW_ID",
    }
)
DIRECT_IDENTIFIER_PATTERNS = {
    "UTM_NOTION_FEISHU_APP_TOKEN": re.compile(r"[A-Za-z0-9]+"),
    "UTM_NOTION_FEISHU_TABLE_ID": re.compile(r"tbl[A-Za-z0-9]+"),
    "UTM_NOTION_FEISHU_VIEW_ID": re.compile(r"vew[A-Za-z0-9]+"),
}


def validate_direct_identifier(key: str, value: str) -> str:
    """Validate one direct Base identifier without echoing its value."""
    pattern = DIRECT_IDENTIFIER_PATTERNS[key]
    if pattern.fullmatch(value) is None:
        raise RuntimeError(f"invalid UTM_NOTION_FEISHU direct identifier: {key}")
    return value


def configured_feishu_source(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str, str, str]:
    values = os.environ if environ is None else environ
    wiki_url = str(values.get("UTM_NOTION_FEISHU_WIKI_URL", "")).strip()
    direct_keys = (
        "UTM_NOTION_FEISHU_APP_TOKEN",
        "UTM_NOTION_FEISHU_TABLE_ID",
        "UTM_NOTION_FEISHU_VIEW_ID",
    )
    direct_values = tuple(str(values.get(key, "")).strip() for key in direct_keys)
    if wiki_url and any(direct_values):
        raise RuntimeError(
            "mixed UTM_NOTION_FEISHU source categories: "
            "UTM_NOTION_FEISHU_WIKI_URL and direct Base identifiers"
        )
    if wiki_url:
        parsed = urllib.parse.urlsplit(wiki_url)
        hostname = (parsed.hostname or "").lower()
        path_parts = [part for part in parsed.path.split("/") if part]
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        table_values = query.get("table", [])
        view_values = query.get("view", [])
        if (
            parsed.scheme != "https"
            or not (hostname == "feishu.cn" or hostname.endswith(".feishu.cn"))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or len(path_parts) != 2
            or path_parts[0] != "wiki"
            or not re.fullmatch(r"[A-Za-z0-9]+", path_parts[1])
            or len(table_values) != 1
            or len(view_values) != 1
        ):
            raise RuntimeError(
                "invalid UTM_NOTION_FEISHU_WIKI_URL source category"
            )
        try:
            table_id = validate_direct_identifier(
                "UTM_NOTION_FEISHU_TABLE_ID", table_values[0]
            )
            view_id = validate_direct_identifier(
                "UTM_NOTION_FEISHU_VIEW_ID", view_values[0]
            )
        except RuntimeError as error:
            raise RuntimeError(
                "invalid UTM_NOTION_FEISHU_WIKI_URL source category"
            ) from error
        return "wiki", path_parts[1], table_id, view_id
    if all(direct_values):
        validated = tuple(
            validate_direct_identifier(key, value)
            for key, value in zip(direct_keys, direct_values)
        )
        return "direct", *validated
    if any(direct_values):
        missing = ", ".join(
            key for key, value in zip(direct_keys, direct_values) if not value
        )
        raise RuntimeError(
            "incomplete UTM_NOTION_FEISHU direct Base identifiers; missing keys: "
            + missing
        )
    raise RuntimeError(
        "missing UTM_NOTION_FEISHU source category: "
        "UTM_NOTION_FEISHU_WIKI_URL or complete direct Base identifiers"
    )


def resolve_feishu_source(
    token: str,
    configured_source: tuple[str, str, str, str],
) -> tuple[str, str, str]:
    category, source_token, table_id, view_id = configured_source
    if category == "direct":
        return source_token, table_id, view_id
    if category != "wiki":
        raise RuntimeError("invalid UTM_NOTION_FEISHU source category")
    url = API_BASE + "/wiki/v2/spaces/get_node?" + urllib.parse.urlencode(
        {"token": source_token}
    )
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            result = json.load(error)
        except Exception:
            result = {}
        raise RuntimeError(
            f"Feishu Wiki API HTTP {error.code}: "
            f"code={result.get('code', 'unknown')} category=wiki"
        ) from None
    if result.get("code") != 0:
        raise RuntimeError(
            f"Feishu Wiki API failed: code={result.get('code')} category=wiki"
        )
    node = dict((result.get("data") or {}).get("node") or {})
    app_token = str(node.get("obj_token") or "").strip()
    if node.get("obj_type") != "bitable":
        raise RuntimeError("Feishu Wiki source category is not a Base")
    try:
        validated_app_token = validate_direct_identifier(
            "UTM_NOTION_FEISHU_APP_TOKEN", app_token
        )
    except RuntimeError as error:
        raise RuntimeError("Feishu Wiki source category is not a Base") from error
    return validated_app_token, table_id, view_id
