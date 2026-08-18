#!/usr/bin/env python3
"""Generate and verify the shared UTM-ENV environment file."""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_SOURCE_ROOT))

from services.project_paths import SHARED_DIR
from scripts.notion_api import api_from_env


DEFAULT_OUTPUT_DIR = SHARED_DIR / "Fire_One_en1.3"


FIELDS = (
    "APP_ID",
    "CONTACT_PHONE",
    "CONTACT_EMAIL",
    "VM_NAME",
    "CONTACT_FIRST_NAME",
    "CONTACT_LAST_NAME",
    "COPYRIGHT",
    "BUNDLE_ID",
    "PRIMARY_CATEGORY",
    "DESCRIPTION",
    "KEYWORDS",
    "TOP_LEVEL_DOMAIN",
    "SUPPORT_URL",
    "PRIVACY_POLICY_URL",
    "PRIVACY_CHOICES_URL",
)

CATEGORY_VALUES = {
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
    "Photo & Video": "PHOTO_AND_VIDEO",
    "生活": "LIFESTYLE",
    "体育": "SPORTS",
    "天气": "WEATHER",
    "贴纸": "STICKERS",
    "图书": "BOOKS",
    "图形和设计": "GRAPHICS_AND_DESIGN",
    "图形与设计": "GRAPHICS_AND_DESIGN",
    "Graphics & Design": "GRAPHICS_AND_DESIGN",
    "效率": "PRODUCTIVITY",
    "新闻": "NEWS",
    "医疗": "MEDICAL",
    "音乐": "MUSIC",
    "游戏": "GAMES",
    "娱乐": "ENTERTAINMENT",
}

KEYWORD_LABELS = ("关键词: ", "关键词:", "关键词：")


def _field(lines: list[str], label: str) -> str:
    matches = [line[len(label):].strip() for line in lines if line.startswith(label)]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one Notion field: {label}")
    return matches[0]


def _field_with_labels(lines: list[str], labels: tuple[str, ...], display_label: str) -> str:
    matches = []
    for line in lines:
        matching_labels = [label for label in labels if line.startswith(label)]
        if matching_labels:
            label = max(matching_labels, key=len)
            matches.append(line[len(label):].strip())
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one Notion field: {display_label}")
    return matches[0]


def parse_notion_sections(account_text: str, application_text: str, page_title: str) -> dict[str, str]:
    title_match = re.fullmatch(r"(.+)-([a-z]{4})", page_title)
    if not title_match:
        raise ValueError("Notion page title must end with a four-letter VM name")
    title_app_name, vm_name = title_match.groups()

    account_lines = account_text.splitlines()
    application_lines = application_text.splitlines()
    username = _field(account_lines, "用户名：")
    name_parts = username.split(maxsplit=1)
    if len(name_parts) != 2:
        raise ValueError("用户名： must contain first and last names")

    app_name = re.split(r"[（(]", _field(application_lines, "应用名: "), maxsplit=1)[0].rstrip()
    if not app_name:
        raise ValueError("应用名: must contain a name before any remark")
    if app_name != title_app_name:
        raise ValueError("Notion page title does not match 应用名: ")

    description_indexes = [
        index for index, line in enumerate(application_lines) if line.startswith("应用描述：")
    ]
    keyword_indexes = [
        index for index, line in enumerate(application_lines)
        if any(line.startswith(label) for label in KEYWORD_LABELS)
    ]
    if len(description_indexes) != 1:
        raise ValueError("Expected exactly one Notion field: 应用描述：")
    if len(keyword_indexes) != 1:
        raise ValueError("Expected exactly one Notion field: 关键词: ")
    description_index = description_indexes[0]
    keyword_index = keyword_indexes[0]
    if description_index >= keyword_index:
        raise ValueError("应用描述： must appear before 关键词: ")
    description = "\n".join([
        application_lines[description_index][len("应用描述："):].strip(),
        *application_lines[description_index + 1:keyword_index],
    ]).strip()

    return {
        "APP_ID": _field(account_lines, "APP_ID："),
        "CONTACT_PHONE": _field(account_lines, "电话："),
        "CONTACT_EMAIL": _field(account_lines, "邮箱："),
        "VM_NAME": vm_name,
        "CONTACT_FIRST_NAME": name_parts[0],
        "CONTACT_LAST_NAME": name_parts[1],
        "COPYRIGHT": app_name,
        "BUNDLE_ID": _field(application_lines, "正式包名: "),
        "PRIMARY_CATEGORY": _field(application_lines, "应用类型："),
        "DESCRIPTION": description,
        "KEYWORDS": _field_with_labels(application_lines, KEYWORD_LABELS, "关键词: /关键词："),
        "TOP_LEVEL_DOMAIN": _field(application_lines, "顶级域名: "),
        "SUPPORT_URL": _field(application_lines, "支持链接: "),
        "PRIVACY_POLICY_URL": _field(application_lines, "隐私协议: "),
        "PRIVACY_CHOICES_URL": _field(application_lines, "用户协议: "),
    }


def validate(data: dict[str, Any]) -> dict[str, str]:
    unexpected = sorted(set(data) - set(FIELDS))
    if unexpected:
        raise ValueError(f"Unexpected fields: {', '.join(unexpected)}")

    missing = [key for key in FIELDS if not isinstance(data.get(key), str) or not data[key].strip()]
    if missing:
        raise ValueError(f"Missing fields: {', '.join(missing)}")

    values = {key: data[key].strip() for key in FIELDS}
    if not values["APP_ID"].isdigit():
        raise ValueError("APP_ID must contain digits only")
    if not re.fullmatch(r"[a-z]{4}", values["VM_NAME"]):
        raise ValueError("VM_NAME must be four lowercase letters")
    if not values["CONTACT_PHONE"].startswith("+"):
        raise ValueError("CONTACT_PHONE must keep its + prefix")
    if "@" not in values["CONTACT_EMAIL"]:
        raise ValueError("CONTACT_EMAIL is invalid")
    category = values["PRIMARY_CATEGORY"]
    if category in CATEGORY_VALUES:
        values["PRIMARY_CATEGORY"] = CATEGORY_VALUES[category]
    elif category not in CATEGORY_VALUES.values():
        raise ValueError("PRIMARY_CATEGORY is unsupported")
    if any("\n" in value or "\r" in value for key, value in values.items() if key != "DESCRIPTION"):
        raise ValueError("Only DESCRIPTION may contain real line breaks")
    if "://" in values["TOP_LEVEL_DOMAIN"] or "/" in values["TOP_LEVEL_DOMAIN"]:
        raise ValueError("TOP_LEVEL_DOMAIN must contain only the domain")
    for key in ("SUPPORT_URL", "PRIVACY_POLICY_URL", "PRIVACY_CHOICES_URL"):
        if not values[key].startswith("https://"):
            raise ValueError(f"{key} must start with https://")
    return values


def render_env(data: dict[str, Any]) -> str:
    value = validate(data)
    description = value["DESCRIPTION"].replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\n")
    return f"""# App_ID
APP_ID={value['APP_ID']}

# 电话
CONTACT_PHONE={value['CONTACT_PHONE']}

# 邮箱
CONTACT_EMAIL={value['CONTACT_EMAIL']}

# 虚拟机名字
VM_NAME={value['VM_NAME']}

# 用户名前面的名称
CONTACT_FIRST_NAME={value['CONTACT_FIRST_NAME']}

# 用户名后面的名称
CONTACT_LAST_NAME={value['CONTACT_LAST_NAME']}

# 应用名
COPYRIGHT={value['COPYRIGHT']}

# 正式包名
BUNDLE_ID={value['BUNDLE_ID']}

# 应用类型
PRIMARY_CATEGORY={value['PRIMARY_CATEGORY']}

# App 描述空行需要改为\\n隔开
DESCRIPTION={description}

# 关键词
KEYWORDS={value['KEYWORDS']}

# 生产环境服务器地址（后面加顶级域名）
PROD_SERVER_URL=https://apple-callback.{value['TOP_LEVEL_DOMAIN']}

# 支持链接
SUPPORT_URL={value['SUPPORT_URL']}

# 隐私协议
PRIVACY_POLICY_URL={value['PRIVACY_POLICY_URL']}

# 用户协议
PRIVACY_CHOICES_URL={value['PRIVACY_CHOICES_URL']}

# 发布方式
RELEASE_OPTION=manual
CDP_ENDPOINT=http://127.0.0.1:9222
"""


def _fsync_dir(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_replace_bytes(path: Path, payload: bytes, mode: int) -> None:
    temp_fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(temp_fd, mode)
        with os.fdopen(temp_fd, "wb") as handle:
            temp_fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_dir(path.parent)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        temp_path.unlink(missing_ok=True)


def write_env_with_state(data: dict[str, Any], out_dir: Path) -> tuple[Path, str]:
    payload = render_env(data).encode("utf-8")
    out_dir = out_dir.expanduser()
    if not out_dir.is_absolute():
        raise ValueError("Output directory must be an absolute path")
    if not out_dir.exists():
        raise ValueError("Output directory must already exist")
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise ValueError("Output directory must be an existing real directory")

    output = out_dir / ".env"
    if output.is_symlink():
        raise ValueError("Refusing to replace a symlinked .env")
    if output.exists() and not output.is_file():
        raise ValueError("Existing .env must be a regular file")

    existed = output.exists()
    before = output.read_bytes() if existed else None
    before_mode = stat.S_IMODE(output.stat().st_mode) if existed else None
    if before == payload and before_mode == 0o600:
        return output, "unchanged"

    replaced = False
    try:
        _atomic_replace_bytes(output, payload, 0o600)
        replaced = True
        if output.is_symlink() or not output.is_file():
            raise RuntimeError(".env readback target is not a regular file")
        if output.read_bytes() != payload:
            raise RuntimeError(".env readback content mismatch")
        if stat.S_IMODE(output.stat().st_mode) != 0o600:
            raise RuntimeError(".env readback mode mismatch")
    except Exception as exc:
        if replaced:
            try:
                if existed:
                    assert before is not None and before_mode is not None
                    _atomic_replace_bytes(output, before, before_mode)
                    if output.read_bytes() != before:
                        raise RuntimeError("rollback content mismatch")
                    if stat.S_IMODE(output.stat().st_mode) != before_mode:
                        raise RuntimeError("rollback mode mismatch")
                else:
                    output.unlink(missing_ok=True)
                    _fsync_dir(out_dir)
            except Exception as rollback_exc:
                raise RuntimeError(
                    f".env write failed and rollback could not be verified: {rollback_exc}"
                ) from exc
        raise
    return output, "changed"


def write_env(data: dict[str, Any], out_dir: Path) -> Path:
    output, _ = write_env_with_state(data, out_dir)
    return output


def generate_env_from_notion(
    parent_title: str,
    page_title: str,
    out_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    api = api_from_env()
    api.verify_parent(parent_title)
    data = parse_notion_sections(
        api.read_section(page_title, "账号信息"),
        api.read_section(page_title, "应用信息"),
        page_title,
    )
    output, write_state = write_env_with_state(data, out_dir)
    content = output.read_bytes()
    return {
        "path": str(output),
        "bytes": len(content),
        "lines": len(content.decode("utf-8").splitlines()),
        "sha256": hashlib.sha256(content).hexdigest(),
        "ENV_WRITE": write_state,
        "ENV_READBACK": "exact",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the shared Fire_One environment file from Notion")
    parser.add_argument("--parent-title", required=True)
    parser.add_argument("--page-title", required=True)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    result = generate_env_from_notion(args.parent_title, args.page_title, args.out_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
