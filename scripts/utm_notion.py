#!/usr/bin/env python3
"""Register one Feishu submission in the matching Notion host page."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import string
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.notion_api import NotionAPI
from scripts.clean_cli import run_clean_cli
from scripts.vm_inventory import (
    InventoryError,
    claim_available_vm,
    claim_exact_available_vm,
    get_record,
    mark_used,
    rebind_application_name,
    scan_inventory,
)
from services.feishu_gateway import get_tenant_access_token
from services.host_config import (  # noqa: E402
    ConfigurationError,
    host_settings_snapshot,
)
from services.utm_notion_source import (
    SOURCE_ENV_KEYS,
    configured_feishu_source as configured_utm_notion_source,
    resolve_feishu_source as resolve_utm_notion_source,
)


DEFAULT_DATABASE = PROJECT_ROOT / "runtime" / "vm-inventory.sqlite3"
UTM_NOTION_FEISHU_ENV_KEYS = SOURCE_ENV_KEYS
API_BASE = "https://open.feishu.cn/open-apis"

ACCOUNT_LABELS = (
    "用户名：",
    "邮箱：",
    "初始密码：",
    "修改后的密码：",
    "电话：",
    "电话短信接收平台：",
    "生日（格式年/月/日）：",
    "team ID:",
    "APP_ID：",
    "Renewal date：",
    "代理ip:",
    "代理端口:",
    "代理用户名：",
    "代理用户密码：",
    "代码链接：",
    "ABA Routing Number：",
    "Account Number：",
)

APPLICATION_LABELS = (
    "应用名: ",
    "团队: ",
    "顶级域名: ",
    "正式包名: ",
    "正式域名: ",
    "隐私协议: ",
    "用户协议: ",
    "支持链接: ",
    "应用类型：",
    "应用描述：",
    "关键词:",
)
APPLICATION_MULTILINE_LABELS = {"正式域名: ", "应用描述："}

CATEGORY_MAP = {
    "报刊杂志": "MAGAZINES_AND_NEWSPAPERS",
    "财务": "FINANCE",
    "参考资料": "REFERENCE",
    "导航": "NAVIGATION",
    "工具": "UTILITIES",
    "购物": "SHOPPING",
    "健康健美": "HEALTH_AND_FITNESS",
    "教育": "EDUCATION",
    "旅游": "TRAVEL",
    "美食佳饮": "FOOD_AND_DRINK",
    "软件开发工具": "DEVELOPER_TOOLS",
    "商务": "BUSINESS",
    "社交": "SOCIAL_NETWORKING",
    "摄影与录像": "PHOTO_AND_VIDEO",
    "生活": "LIFESTYLE",
    "体育": "SPORTS",
    "天气": "WEATHER",
    "贴纸": "STICKERS",
    "图书": "BOOKS",
    "图形与设计": "GRAPHICS_AND_DESIGN",
    "效率": "PRODUCTIVITY",
    "新闻": "NEWS",
    "医疗": "MEDICAL",
    "音乐": "MUSIC",
    "游戏": "GAMES",
    "娱乐": "ENTERTAINMENT",
}
VALID_CATEGORIES = frozenset(CATEGORY_MAP.values())
CATEGORY_ALIASES = {
    "生活方式": "LIFESTYLE",
}
EMAIL_SUFFIX_PATTERN = (
    r"(?:co\.uk|com\.cn|com|net|org|edu|gov|io|co|cn|de|uk|jp|fr|me|app|dev)"
)
GENERATED_PASSWORD_LENGTH = 16
TEST_MISSING_VALUE = "test"


@dataclass(frozen=True)
class UtmNotionRuntimeConfig:
    feishu_app_id: str
    feishu_app_secret: str
    configured_source: tuple[str, str, str, str]
    notion_token: str
    notion_root_page_id: str
    notion_template_title: str
    host_title: str
    images_dir: Path


def _required_runtime_setting(settings: Mapping[str, str], key: str) -> str:
    value = str(settings.get(key, "")).strip()
    if not value:
        raise ConfigurationError(f"missing required host setting: {key}")
    return value


def load_runtime_config(
    *,
    environ: Mapping[str, str] | None = None,
    env_path: Path | None = None,
) -> UtmNotionRuntimeConfig:
    if env_path is None:
        settings_path = None if environ is not None else PROJECT_ROOT / ".env"
    else:
        settings_path = Path(env_path)
    settings = host_settings_snapshot(
        environ=environ,
        env_path=settings_path,
        file_override_keys=UTM_NOTION_FEISHU_ENV_KEYS,
    )
    feishu_app_id = _required_runtime_setting(settings, "FEISHU_APP_ID")
    feishu_app_secret = _required_runtime_setting(settings, "FEISHU_APP_SECRET")
    notion_token = _required_runtime_setting(settings, "NOTION_TOKEN")
    notion_root_page_id = _required_runtime_setting(settings, "NOTION_ROOT_PAGE_ID")
    host_title = _required_runtime_setting(settings, "SUBMISSION_HOST_MACHINE")
    images_dir_value = _required_runtime_setting(
        settings, "SUBMISSION_VM_IMAGES_DIR"
    )
    images_dir = Path(images_dir_value).expanduser()
    if not images_dir.is_absolute():
        raise ConfigurationError(
            "invalid host setting: SUBMISSION_VM_IMAGES_DIR must be an absolute path"
        )
    try:
        configured_source = configured_utm_notion_source(settings)
    except RuntimeError as error:
        raise ConfigurationError(str(error)) from error
    return UtmNotionRuntimeConfig(
        feishu_app_id=feishu_app_id,
        feishu_app_secret=feishu_app_secret,
        configured_source=configured_source,
        notion_token=notion_token,
        notion_root_page_id=notion_root_page_id,
        notion_template_title=str(
            settings.get("NOTION_TEMPLATE_TITLE", "模板") or "模板"
        ).strip(),
        host_title=host_title,
        images_dir=images_dir,
    )


def notion_api_from_config(config: UtmNotionRuntimeConfig) -> NotionAPI:
    return NotionAPI(
        config.notion_token,
        config.notion_root_page_id,
        config.notion_template_title,
    )


def parse_feishu_wiki_url(value: str) -> tuple[str, str, str]:
    parsed = urllib.parse.urlsplit(str(value).strip())
    hostname = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    table_values = query.get("table", [])
    view_values = query.get("view", [])
    valid_host = hostname == "feishu.cn" or hostname.endswith(".feishu.cn")
    if (
        parsed.scheme != "https"
        or not valid_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or len(path_parts) != 2
        or path_parts[0] != "wiki"
        or not re.fullmatch(r"[A-Za-z0-9]+", path_parts[1])
        or len(table_values) != 1
        or len(view_values) != 1
        or not re.fullmatch(r"tbl[A-Za-z0-9]+", table_values[0])
        or not re.fullmatch(r"vew[A-Za-z0-9]+", view_values[0])
    ):
        raise RuntimeError("Feishu Wiki URL must identify one exact Base table and view")
    return path_parts[1], table_values[0], view_values[0]


def resolve_bitable_app_token(token: str, wiki_node_token: str) -> str:
    url = API_BASE + "/wiki/v2/spaces/get_node?" + urllib.parse.urlencode(
        {"token": wiki_node_token}
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
            f"Feishu Wiki API HTTP {error.code}: code={result.get('code', 'unknown')} "
            f"msg={result.get('msg', 'request failed')}"
        ) from None
    if result.get("code") != 0:
        raise RuntimeError(
            f"Feishu Wiki API failed: code={result.get('code')} msg={result.get('msg')}"
        )
    node = dict((result.get("data") or {}).get("node") or {})
    app_token = str(node.get("obj_token") or "").strip()
    if node.get("obj_type") != "bitable" or not re.fullmatch(
        r"[A-Za-z0-9]+", app_token
    ):
        raise RuntimeError("Feishu Wiki node does not resolve to one Base")
    return app_token


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text.strip()
        return ""
    if isinstance(value, list):
        return "".join(_cell_text(item) for item in value).strip()
    return ""


def _required_text(
    fields: dict[str, object],
    name: str,
    *,
    fill_missing_with_test: bool = False,
) -> str:
    value = _cell_text(fields.get(name))
    if not value:
        if fill_missing_with_test:
            return TEST_MISSING_VALUE
        raise RuntimeError(f"Feishu field is empty: {name}")
    return value


def _http_url(
    fields: dict[str, object],
    name: str,
    *,
    required: bool = True,
    fill_missing_with_test: bool = False,
) -> str:
    cell = fields.get(name)
    value = ""
    if isinstance(cell, dict) and isinstance(cell.get("link"), str):
        value = str(cell["link"]).strip()
    if not value:
        value = _cell_text(cell)
    if not value and fill_missing_with_test:
        return TEST_MISSING_VALUE
    if not value and not required:
        return ""
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"Feishu field is not a complete HTTP(S) URL: {name}")
    return value


def _normalize_us_phone(value: str) -> str:
    digits = "".join(character for character in str(value) if character.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if 11 <= len(digits) <= 15:
        return "+" + digits
    raise RuntimeError("Feishu 账号信息 phone must contain 10 US digits")


def parse_account_info(cell: object) -> dict[str, str]:
    text = _cell_text(cell)
    cell_link = str(cell.get("link") or "").strip() if isinstance(cell, dict) else ""

    compact_prefix = (
        rf"(?P<phone>\+\d{{10,11}})-{{4}}"
        rf"(?P<sms_url>https?://[A-Z0-9.-]+/smsrecord\?token=[A-Za-z0-9]{{34}})"
        rf"(?P<email>[A-Z0-9._%+-]+@[A-Z0-9.-]+?\.{EMAIL_SUFFIX_PATTERN})"
    )
    compact_account = re.fullmatch(
        compact_prefix
        + rf"(?P<password>[A-Za-z]+\d+[!#$%&*._-]?)"
        + rf"(?P<country>[A-Za-z][A-Za-z-]*)\s+.+",
        text,
        flags=re.IGNORECASE,
    ) or re.fullmatch(
        compact_prefix + rf"(?P<password>[^\s+]+)\s+.+",
        text,
        flags=re.IGNORECASE,
    )
    if compact_account and text.count("@") == 1:
        initial_password = compact_account.group("password")
        if not re.search(r"[A-Za-z]", initial_password) or not re.search(
            r"\d", initial_password
        ):
            raise RuntimeError(
                "Feishu 账号信息 initial password must contain letters and digits"
            )
        return {
            "email": compact_account.group("email"),
            "initial_password": initial_password,
            "phone": _normalize_us_phone(compact_account.group("phone")),
            "sms_url": compact_account.group("sms_url"),
        }

    url_matches = list(re.finditer(r"https?://\S+", text, flags=re.IGNORECASE))
    if len(url_matches) > 1:
        raise RuntimeError("Feishu 账号信息 must contain at most one HTTP(S) URL")
    if url_matches:
        url_match = url_matches[0]
        sms_url = url_match.group(0)
        identity_text = (
            text[: url_match.start()].rstrip() + text[url_match.end() :].lstrip()
        ).strip()
    else:
        sms_url = cell_link
        identity_text = text
    parsed_sms_url = urllib.parse.urlsplit(sms_url)
    if not parsed_sms_url.netloc:
        raise RuntimeError("Feishu 账号信息 must contain an inline or cell-link HTTP(S) URL")
    space_separated = re.fullmatch(
        r"(?P<email>[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63})\s+"
        r"(?P<password>\S+)\s+"
        r"(?P<phone>\+?\d{10,15})",
        identity_text,
        flags=re.IGNORECASE,
    )
    if space_separated and identity_text.count("@") == 1:
        return {
            "email": space_separated.group("email"),
            "initial_password": space_separated.group("password"),
            "phone": _normalize_us_phone(space_separated.group("phone")),
            "sms_url": sms_url,
        }
    labelled_pattern = re.compile(
        rf".*?账号\s*[：:]\s*"
        rf"(?P<email>[A-Z0-9._%+-]+@[A-Z0-9.-]+?\.{EMAIL_SUFFIX_PATTERN})\s*"
        rf"密码\s*[：:]\s*(?P<password>[A-Za-z0-9]+)\s*"
        rf"接码\s*[：:]\s*(?P<phone>\+?\d{{7,15}})",
        flags=re.IGNORECASE,
    )
    labelled_matches = list(labelled_pattern.finditer(identity_text))
    if len(labelled_matches) == 1:
        labelled = labelled_matches[0]
        if labelled.end() != len(identity_text):
            raise RuntimeError("Feishu 账号信息 labelled fields must consume the full identity")
        initial_password = labelled.group("password")
        if not re.search(r"[A-Za-z]", initial_password) or not re.search(r"\d", initial_password):
            raise RuntimeError("Feishu 账号信息 initial password must contain letters and digits")
        return {
            "email": labelled.group("email"),
            "initial_password": initial_password,
            "phone": _normalize_us_phone(labelled.group("phone")),
            "sms_url": sms_url,
        }

    provider_first_match = re.fullmatch(
        rf"(?P<provider>[A-Za-z0-9]+)\s+.+?"
        rf"(?P<email>[A-Z0-9._%+-]+@[A-Z0-9.-]+?\.{EMAIL_SUFFIX_PATTERN})"
        rf"(?P<password>[^\s+]+)"
        rf"(?P<phone>\+\d{{10,11}})-{{4}}",
        identity_text,
        flags=re.IGNORECASE,
    )
    if provider_first_match and identity_text.count("@") == 1:
        initial_password = provider_first_match.group("password")
        if not re.search(r"[A-Za-z]", initial_password) or not re.search(
            r"\d", initial_password
        ):
            raise RuntimeError(
                "Feishu 账号信息 initial password must contain letters and digits"
            )
        return {
            "email": provider_first_match.group("email"),
            "initial_password": initial_password,
            "phone": _normalize_us_phone(provider_first_match.group("phone")),
            "sms_url": sms_url,
        }

    plain_email_matches = list(
        re.finditer(
            rf"[A-Z0-9._%+-]+@[A-Z0-9.-]+?\.{EMAIL_SUFFIX_PATTERN}",
            identity_text,
            flags=re.IGNORECASE,
        )
    )
    if len(plain_email_matches) == 1 and identity_text.count("@") == 1:
        plain_email = plain_email_matches[0]
        tail = identity_text[plain_email.end() :]
        candidates: list[tuple[str, str]] = []
        for phone_length in (10, 11):
            if len(tail) <= phone_length:
                continue
            initial_password = tail[:-phone_length]
            phone = tail[-phone_length:]
            if (
                phone.isdigit()
                and (phone_length == 10 or phone.startswith("1"))
                and re.fullmatch(r"[A-Za-z0-9]+", initial_password)
                and re.search(r"[A-Za-z]", initial_password)
                and re.search(r"\d", initial_password)
            ):
                candidates.append((initial_password, phone))
        if len(candidates) == 1:
            initial_password, phone = candidates[0]
            return {
                "email": plain_email.group(0),
                "initial_password": initial_password,
                "phone": _normalize_us_phone(phone),
                "sms_url": sms_url,
            }

    email_match = re.match(
        rf"^(?P<email>[A-Z0-9._%+-]+@[A-Z0-9.-]+?\.{EMAIL_SUFFIX_PATTERN})(?P<tail>.+)$",
        identity_text,
        flags=re.IGNORECASE,
    )
    if not email_match:
        raise RuntimeError("Feishu 账号信息 email could not be uniquely parsed")
    email = email_match.group("email")
    tail = email_match.group("tail")
    phone_matches = list(
        re.finditer(r"(?<!\d)(?P<phone>\+?\d{7,15})(?!\d)", tail)
    )
    if len(phone_matches) != 1:
        raise RuntimeError("Feishu 账号信息 must contain exactly one phone")
    phone_match = phone_matches[0]
    tail_without_phone = (
        tail[: phone_match.start()].rstrip() + tail[phone_match.end() :].lstrip()
    ).strip()
    password_match = re.match(
        r"(?P<password>[A-Za-z0-9]+)(?P<country>[^A-Za-z0-9].*)$",
        tail_without_phone,
    )
    if not password_match:
        raise RuntimeError("Feishu 账号信息 initial password could not be uniquely parsed")
    initial_password = password_match.group("password")
    if not re.search(r"[A-Za-z]", initial_password) or not re.search(r"\d", initial_password):
        raise RuntimeError("Feishu 账号信息 initial password must contain letters and digits")

    return {
        "email": email,
        "initial_password": initial_password,
        "phone": _normalize_us_phone(phone_match.group("phone")),
        "sms_url": sms_url,
    }


def password_meets_requirements(password: str) -> bool:
    value = str(password)
    return (
        len(value) >= 8
        and value.isascii()
        and value.isalnum()
        and any(character.islower() for character in value)
        and any(character.isupper() for character in value)
        and any(character.isdigit() for character in value)
    )


def generate_random_password() -> str:
    characters = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
    ]
    alphabet = string.ascii_letters + string.digits
    characters.extend(
        secrets.choice(alphabet)
        for _ in range(GENERATED_PASSWORD_LENGTH - len(characters))
    )
    for index in range(len(characters) - 1, 0, -1):
        swap_index = secrets.randbelow(index + 1)
        characters[index], characters[swap_index] = characters[swap_index], characters[index]
    password = "".join(characters)
    if not password_meets_requirements(password):
        raise RuntimeError("Generated password did not meet the required policy")
    return password


def _template_field_value(template: str, label: str) -> str:
    matches = [line[len(label) :] for line in template.splitlines() if line.startswith(label)]
    if len(matches) != 1:
        raise RuntimeError(f"Notion template label count must be one: {label}")
    return matches[0].strip()


def ensure_modified_password(template: str, replacements: dict[str, str]) -> bool:
    label = "修改后的密码："
    if _template_field_value(template, label):
        return False
    replacements[label] = generate_random_password()
    return True


def _proxy_parts(fields: dict[str, object]) -> tuple[str, str, str, str]:
    source = _required_text(fields, "代理信息")
    parts = source.split(":", 3)
    if len(parts) != 4 or any(not part.strip() for part in parts):
        raise RuntimeError("Feishu 代理信息 must contain IP, port, username and password")
    host, port, username, password = (part.strip() for part in parts)
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise RuntimeError("Feishu proxy port is invalid")
    return host, port, username, password


def normalize_category(source: str) -> str:
    value = str(source).strip().replace("和", "与")
    if value in VALID_CATEGORIES:
        return value
    if value in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[value]
    if value in CATEGORY_MAP:
        return CATEGORY_MAP[value]
    prefix, separator, suffix = value.rpartition("-")
    prefix = prefix.strip() if separator else ""
    suffix = suffix.strip() if separator else ""
    if suffix in VALID_CATEGORIES:
        return suffix
    if prefix in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[prefix]
    if prefix in CATEGORY_MAP:
        return CATEGORY_MAP[prefix]
    if prefix in VALID_CATEGORIES:
        return prefix
    raise RuntimeError("Feishu application category is unsupported")


def build_registration_fields(
    fields: dict[str, object],
    *,
    fill_missing_with_test: bool = False,
) -> tuple[dict[str, str], dict[str, str]]:
    app_name = _required_text(fields, "应用名")
    if fill_missing_with_test and not _cell_text(fields.get("账号信息")):
        account_info = {
            "email": TEST_MISSING_VALUE,
            "initial_password": TEST_MISSING_VALUE,
            "phone": TEST_MISSING_VALUE,
            "sms_url": TEST_MISSING_VALUE,
        }
    else:
        account_info = parse_account_info(fields.get("账号信息"))
    if fill_missing_with_test and not _cell_text(fields.get("代理信息")):
        proxy_ip = proxy_port = proxy_username = proxy_password = TEST_MISSING_VALUE
    else:
        proxy_ip, proxy_port, proxy_username, proxy_password = _proxy_parts(fields)
    code_url = _http_url(
        fields,
        "代码 URL",
        fill_missing_with_test=fill_missing_with_test,
    )

    category_source = _required_text(
        fields,
        "应用类型",
        fill_missing_with_test=fill_missing_with_test,
    )
    category = (
        TEST_MISSING_VALUE
        if category_source == TEST_MISSING_VALUE and fill_missing_with_test
        else normalize_category(category_source)
    )

    def text(name: str) -> str:
        return _required_text(
            fields,
            name,
            fill_missing_with_test=fill_missing_with_test,
        )

    top_level_domain = _cell_text(fields.get("顶级域名"))
    if not top_level_domain:
        top_level_domain = text("待购买域名")
    formal_domains = _cell_text(fields.get("正式域名(app/h5/im/log)"))
    if not formal_domains:
        formal_domains = (
            TEST_MISSING_VALUE
            if fill_missing_with_test or top_level_domain == TEST_MISSING_VALUE
            else "\n".join(
                f"{prefix}.{top_level_domain}"
                for prefix in ("app", "h5", "im", "log")
            )
        )

    account = {
        "邮箱：": account_info["email"],
        "初始密码：": account_info["initial_password"],
        "电话：": account_info["phone"],
        "电话短信接收平台：": account_info["sms_url"],
        "代理ip:": proxy_ip,
        "代理端口:": proxy_port,
        "代理用户名：": proxy_username,
        "代理用户密码：": proxy_password,
        "代码链接：": code_url,
    }
    routing_number = _cell_text(fields.get("ABA Routing Number"))
    account_number = _cell_text(fields.get("Account Number"))
    if routing_number:
        account["ABA Routing Number："] = routing_number
    if account_number:
        account["Account Number："] = account_number
    application = {
        "应用名: ": app_name,
        "团队: ": "灵光",
        "顶级域名: ": top_level_domain,
        "正式包名: ": text("正式包名"),
        "正式域名: ": formal_domains,
        "隐私协议: ": _http_url(
            fields,
            "隐私协议",
            fill_missing_with_test=fill_missing_with_test,
        ),
        "用户协议: ": _http_url(
            fields,
            "用户协议",
            fill_missing_with_test=fill_missing_with_test,
        ),
        "支持链接: ": _http_url(
            fields,
            "支持协议",
            fill_missing_with_test=fill_missing_with_test,
        ),
        "应用类型：": category,
        "应用描述：": text("应用描述"),
        "关键词:": text("关键词"),
    }
    return account, application


def fill_template_fields(
    template: str,
    labels: Iterable[str],
    replacements: dict[str, str],
    *,
    multiline_labels: set[str] | None = None,
) -> str:
    ordered_labels = tuple(labels)
    multiline = multiline_labels or set()
    lines = template.split("\n")
    indexes: dict[str, int] = {}
    for label in ordered_labels:
        matches = [index for index, line in enumerate(lines) if line.startswith(label)]
        if len(matches) != 1:
            raise RuntimeError(f"Notion template label count must be one: {label}")
        indexes[label] = matches[0]
    positions = [indexes[label] for label in ordered_labels]
    if positions != sorted(positions):
        raise RuntimeError("Notion template labels are out of order")
    unknown = set(replacements) - set(ordered_labels)
    if unknown:
        raise RuntimeError("Notion replacements contain unknown labels")

    for label in reversed(ordered_labels):
        if label not in replacements:
            continue
        value = str(replacements[label])
        if "\r" in value:
            raise RuntimeError("Notion field values must not contain carriage returns")
        start = indexes[label]
        if label in multiline:
            position = ordered_labels.index(label)
            end = indexes[ordered_labels[position + 1]] if position + 1 < len(ordered_labels) else len(lines)
            lines[start:end] = [label + part for part in value.split("\n", 1)] if "\n" not in value else [label + value.split("\n", 1)[0], *value.split("\n")[1:]]
        else:
            if "\n" in value:
                raise RuntimeError(f"Notion field must remain one line: {label}")
            lines[start] = label + value
    return "\n".join(lines)


def _stable_field_value(value: object) -> object:
    if isinstance(value, list):
        return [_stable_field_value(item) for item in value]
    if isinstance(value, dict):
        volatile = {"url", "tmp_url"} if "file_token" in value else set()
        return {
            key: _stable_field_value(item)
            for key, item in value.items()
            if key not in volatile
        }
    return value


def _fields_digest(fields: dict[str, object], *, omit: set[str] | None = None) -> str:
    filtered = {
        key: _stable_field_value(value)
        for key, value in fields.items()
        if key not in (omit or set())
    }
    payload = json.dumps(filtered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FeishuBitableClient:
    def __init__(self, token: str, app_token: str, table_id: str, view_id: str) -> None:
        if not all((token, app_token, table_id, view_id)):
            raise RuntimeError("Feishu Base client configuration is incomplete")
        self.token = token
        self.app_token = app_token
        self.table_id = table_id
        self.view_id = view_id

    @property
    def _records_path(self) -> str:
        return f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        url = API_BASE + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
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
                f"Feishu API HTTP {error.code}: code={result.get('code', 'unknown')} "
                f"msg={result.get('msg', 'request failed')}"
            ) from None
        if result.get("code") != 0:
            raise RuntimeError(
                f"Feishu API failed: code={result.get('code')} msg={result.get('msg')}"
            )
        return dict(result.get("data") or {})

    def list_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            query: dict[str, object] = {"view_id": self.view_id, "page_size": 500}
            if page_token:
                query["page_token"] = page_token
            data = self._request("GET", self._records_path, query=query)
            records.extend(data.get("items") or [])
            if not data.get("has_more"):
                return records
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise RuntimeError("Feishu pagination is missing page_token")

    def find_exact_record(
        self,
        *,
        run_id: str = "",
        app_name: str = "",
    ) -> dict[str, Any]:
        run_id = str(run_id).strip()
        app_name = str(app_name).strip()
        if not run_id and not app_name:
            raise RuntimeError("run_id or app_name is required")
        matches = []
        for record in self.list_records():
            fields = dict(record.get("fields") or {})
            if run_id and _cell_text(fields.get("Run ID")) != run_id:
                continue
            if app_name and _cell_text(fields.get("应用名")) != app_name:
                continue
            matches.append(record)
        if len(matches) != 1:
            raise RuntimeError(f"Feishu exact record match count must be one, got {len(matches)}")
        return matches[0]

    def get_record(self, record_id: str) -> dict[str, Any]:
        data = self._request("GET", f"{self._records_path}/{record_id}")
        record = data.get("record")
        if not isinstance(record, dict):
            raise RuntimeError("Feishu record readback is missing")
        return record

    def preflight_vm_write(self, record: dict[str, Any]) -> None:
        record_id = str(record.get("record_id") or "")
        if not record_id:
            raise RuntimeError("Feishu record_id is missing")
        before = self.get_record(record_id)
        before_fields = dict(before.get("fields") or {})
        current = _cell_text(before_fields.get("虚拟机"))
        if current:
            record["fields"] = before_fields
            return
        before_digest = _fields_digest(before_fields, omit={"虚拟机"})
        try:
            self._request(
                "PUT",
                f"{self._records_path}/{record_id}",
                payload={"fields": {"虚拟机": None}},
            )
        except RuntimeError as error:
            if "code=91403" in str(error):
                raise RuntimeError(
                    "Feishu app identity lacks record-write permission: code=91403"
                ) from None
            raise
        after = self.get_record(record_id)
        after_fields = dict(after.get("fields") or {})
        if _cell_text(after_fields.get("虚拟机")) != "":
            raise RuntimeError("Feishu VM write preflight readback mismatch")
        if _fields_digest(after_fields, omit={"虚拟机"}) != before_digest:
            raise RuntimeError("Feishu VM write preflight changed another field")
        record["fields"] = after_fields

    def ensure_vm(self, record: dict[str, Any], vm_name: str) -> bool:
        if not re.fullmatch(r"[a-z]{4}", vm_name):
            raise RuntimeError("VM name must be four lowercase letters")
        record_id = str(record.get("record_id") or "")
        if not record_id:
            raise RuntimeError("Feishu record_id is missing")
        before = self.get_record(record_id)
        before_fields = dict(before.get("fields") or {})
        current = _cell_text(before_fields.get("虚拟机"))
        if current and current != vm_name:
            raise RuntimeError(f"Feishu VM column already contains a different value: {current}")
        if current == vm_name:
            record["fields"] = before_fields
            return False
        before_digest = _fields_digest(before_fields, omit={"虚拟机"})
        changed = not current
        self._request(
            "PUT",
            f"{self._records_path}/{record_id}",
            payload={"fields": {"虚拟机": vm_name}},
        )
        after = self.get_record(record_id)
        after_fields = dict(after.get("fields") or {})
        if _cell_text(after_fields.get("虚拟机")) != vm_name:
            raise RuntimeError("Feishu VM column readback mismatch")
        if _fields_digest(after_fields, omit={"虚拟机"}) != before_digest:
            raise RuntimeError("Feishu update changed a field outside the VM column")
        record["fields"] = after_fields
        return changed


def _bind_vm(
    fields: dict[str, object],
    app_name: str,
    database: Path,
    images_dir: Path,
    *,
    expected_vm_name: str = "",
    rename_from: str = "",
) -> str:
    if expected_vm_name and not re.fullmatch(r"[a-z]{4}", expected_vm_name):
        raise InventoryError("expected VM name must be four lowercase letters")
    current = _cell_text(fields.get("虚拟机"))
    if not current:
        scan_inventory(database, images_dir)
        if expected_vm_name:
            return claim_exact_available_vm(database, expected_vm_name, app_name)
        return claim_available_vm(database, app_name)
    if not re.fullmatch(r"[a-z]{4}", current):
        raise RuntimeError("Feishu VM value must be four lowercase letters")
    if expected_vm_name and current != expected_vm_name:
        raise InventoryError(
            f"Feishu VM does not match the inherited clone: {current}"
        )
    record = get_record(database, current)
    if record.get("status") != "complete" or int(record.get("directory_present") or 0) != 1:
        raise InventoryError(f"Feishu VM is not a complete present inventory row: {current}")
    bound = record.get("application_name")
    if bound not in (None, app_name):
        if not rename_from:
            raise InventoryError(f"Feishu VM is already bound to another application: {current}")
        rebind_application_name(database, current, rename_from, app_name)
        bound = app_name
    if bound is None:
        if int(record.get("available") or 0) != 1:
            raise InventoryError(f"unbound Feishu VM is not available: {current}")
        mark_used(database, current, app_name)
    return current


def execute(
    *,
    run_id: str = "",
    app_name: str = "",
    vm_name: str = "",
    database: Path = DEFAULT_DATABASE,
    fill_missing_with_test: bool = False,
    rename_from: str = "",
) -> dict[str, object]:
    explicit_vm_name = bool(vm_name)
    runtime_config = load_runtime_config(env_path=PROJECT_ROOT / ".env")
    token = get_tenant_access_token(
        runtime_config.feishu_app_id,
        runtime_config.feishu_app_secret,
    )
    app_token, table_id, view_id = resolve_utm_notion_source(
        token, runtime_config.configured_source
    )
    client = FeishuBitableClient(
        token,
        app_token,
        table_id,
        view_id,
    )
    record = client.find_exact_record(run_id=run_id, app_name=app_name)
    fields = dict(record.get("fields") or {})
    account_replacements, application_replacements = build_registration_fields(
        fields,
        fill_missing_with_test=fill_missing_with_test,
    )
    exact_app_name = application_replacements["应用名: "]
    if not explicit_vm_name:
        client.preflight_vm_write(record)
    notion = notion_api_from_config(runtime_config)
    notion.verify_parent(runtime_config.host_title)
    vm_name = _bind_vm(
        fields,
        exact_app_name,
        database.resolve(),
        runtime_config.images_dir,
        expected_vm_name=vm_name,
        rename_from=rename_from,
    )
    feishu_changed = (
        False if explicit_vm_name else client.ensure_vm(record, vm_name)
    )
    page_title = f"{exact_app_name}-{vm_name}"
    page_id, page_created = notion.create_registration_copy(page_title)

    notion.verify_parent(runtime_config.host_title)
    account_before = notion.read_section(page_title, "账号信息")
    application_before = notion.read_section(page_title, "应用信息")
    modified_password_generated = ensure_modified_password(
        account_before, account_replacements
    )
    account_desired = fill_template_fields(
        account_before,
        ACCOUNT_LABELS,
        account_replacements,
    )
    application_desired = fill_template_fields(
        application_before,
        APPLICATION_LABELS,
        application_replacements,
        multiline_labels=APPLICATION_MULTILINE_LABELS,
    )

    notion.verify_parent(runtime_config.host_title)
    changed = notion.write_registration_sections(
        page_title,
        {"账号信息": account_desired, "应用信息": application_desired},
    )
    notion.verify_parent(runtime_config.host_title)
    account_after = notion.read_section(page_title, "账号信息")
    application_after = notion.read_section(page_title, "应用信息")
    if account_after != account_desired or application_after != application_desired:
        raise RuntimeError("Notion independent registration readback mismatch")

    return {
        "action": "utm-notion",
        "record_id": str(record.get("record_id") or ""),
        "page_id": page_id,
        "page_title": page_title,
        "vm_name": vm_name,
        "feishu_vm_changed": feishu_changed,
        "notion_page_created": page_created,
        "account_changed": changed["账号信息"],
        "application_changed": changed["应用信息"],
        "modified_password_generated": modified_password_generated,
        "test_filled_labels": sorted(
            label
            for label, value in {**account_replacements, **application_replacements}.items()
            if fill_missing_with_test and value == TEST_MISSING_VALUE
        ),
        "account_bytes": len(account_after.encode("utf-8")),
        "account_sha256": hashlib.sha256(account_after.encode("utf-8")).hexdigest(),
        "application_bytes": len(application_after.encode("utf-8")),
        "application_sha256": hashlib.sha256(application_after.encode("utf-8")).hexdigest(),
        "status": "verified",
        "handoff": "utm-clash-ip",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-id", default="")
    target.add_argument("--app-name", default="")
    parser.add_argument("--vm-name", default="")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--fill-missing-with-test", action="store_true")
    parser.add_argument("--rename-from", default="")
    args = parser.parse_args()
    return run_clean_cli(
        skill_name="utm-notion",
        success_marker="status=verified",
        operation=lambda: (
            execute(
                run_id=args.run_id,
                app_name=args.app_name,
                vm_name=args.vm_name,
                database=args.database,
                fill_missing_with_test=args.fill_missing_with_test,
                rename_from=args.rename_from,
            ),
            0,
        )[1],
        preserve_error_detail=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
