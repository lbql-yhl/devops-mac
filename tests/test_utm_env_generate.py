#!/usr/bin/env python3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.utm_env_generate import (
    DEFAULT_OUTPUT_DIR,
    FIELDS,
    parse_notion_sections,
    write_env,
)


ACCOUNT_SECTION = """用户名：Ada Lovelace
邮箱：person@example.com
电话：+15551234567
APP_ID：1234567890
初始密码：must-not-leak
代理：must-not-leak
"""

APPLICATION_SECTION = """应用名: Example
顶级域名: example.com
正式包名: com.example.app
隐私协议: https://example.com/privacy
用户协议: https://example.com/terms
支持链接: https://example.com/support
应用类型：GRAPHICS_AND_DESIGN
应用描述：First paragraph.

Second paragraph.
关键词: photo,design
"""


def main() -> None:
    assert DEFAULT_OUTPUT_DIR.name == "Fire_One_en1.3"

    categories = {
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
        "效率": "PRODUCTIVITY",
        "新闻": "NEWS",
        "医疗": "MEDICAL",
        "音乐": "MUSIC",
        "游戏": "GAMES",
        "娱乐": "ENTERTAINMENT",
    }
    data = {
        "APP_ID": "1234567890",
        "CONTACT_PHONE": "+15551234567",
        "CONTACT_EMAIL": "person@example.com",
        "VM_NAME": "abcd",
        "CONTACT_FIRST_NAME": "Ada",
        "CONTACT_LAST_NAME": "Lovelace",
        "COPYRIGHT": "Example",
        "BUNDLE_ID": "com.example.app",
        "PRIMARY_CATEGORY": "Graphics & Design",
        "DESCRIPTION": "First paragraph.\n\nSecond paragraph.",
        "KEYWORDS": "photo,design",
        "TOP_LEVEL_DOMAIN": "example.com",
        "SUPPORT_URL": "https://example.com/support",
        "PRIVACY_POLICY_URL": "https://example.com/privacy",
        "PRIVACY_CHOICES_URL": "https://example.com/terms",
    }

    parsed = parse_notion_sections(ACCOUNT_SECTION, APPLICATION_SECTION, "Example-abcd")
    assert set(parsed) == set(FIELDS)
    assert parsed == dict(data, PRIMARY_CATEGORY="GRAPHICS_AND_DESIGN")
    assert parsed["DESCRIPTION"] == "First paragraph.\n\nSecond paragraph."

    fullwidth_keyword_label = parse_notion_sections(
        ACCOUNT_SECTION,
        APPLICATION_SECTION.replace("关键词: ", "关键词："),
        "Example-abcd",
    )
    assert fullwidth_keyword_label["KEYWORDS"] == "photo,design"

    compact_keyword_label = parse_notion_sections(
        ACCOUNT_SECTION,
        APPLICATION_SECTION.replace("关键词: ", "关键词:"),
        "Example-abcd",
    )
    assert compact_keyword_label["KEYWORDS"] == "photo,design"

    annotated_application = APPLICATION_SECTION.replace(
        "应用名: Example", "应用名: Example（web A）(IP更新)"
    )
    annotated = parse_notion_sections(
        ACCOUNT_SECTION, annotated_application, "Example-abcd"
    )
    assert annotated["COPYRIGHT"] == "Example"

    with tempfile.TemporaryDirectory() as tmp:
        try:
            output = write_env(data, Path(tmp))
        except KeyError as exc:
            raise AssertionError("Graphics & Design must map to GRAPHICS_AND_DESIGN") from exc
        text = output.read_text(encoding="utf-8")

        assert output.name == ".env"
        assert output.stat().st_mode & 0o777 == 0o600
        assert "APP_ID=1234567890" in text
        assert "COPYRIGHT=Example" in text
        assert "PRIMARY_CATEGORY=GRAPHICS_AND_DESIGN" in text
        assert "DESCRIPTION=First paragraph.\\n\\nSecond paragraph." in text
        assert "PROD_SERVER_URL=https://apple-callback.example.com" in text
        assert "RELEASE_OPTION=manual" in text
        assert "CDP_ENDPOINT=http://127.0.0.1:9222" in text
        assert "must-not-leak" not in text

        changed = dict(data, VM_NAME="wxyz", COPYRIGHT="Changed")
        changed_output = write_env(changed, Path(tmp))
        changed_text = changed_output.read_text(encoding="utf-8")
        assert changed_output.name == ".env"
        assert "VM_NAME=wxyz" in changed_text
        assert "COPYRIGHT=Changed" in changed_text

        for category, expected in categories.items():
            try:
                category_output = write_env(dict(data, PRIMARY_CATEGORY=category), Path(tmp))
            except KeyError as exc:
                raise AssertionError(f"{category} must map to {expected}") from exc
            assert f"PRIMARY_CATEGORY={expected}" in category_output.read_text(encoding="utf-8")

        for expected in set(categories.values()):
            category_output = write_env(dict(data, PRIMARY_CATEGORY=expected), Path(tmp))
            assert f"PRIMARY_CATEGORY={expected}" in category_output.read_text(encoding="utf-8")

        try:
            write_env(dict(data, PASSWORD="must-not-be-accepted"), Path(tmp))
        except ValueError as exc:
            assert "Unexpected fields" in str(exc)
        else:
            raise AssertionError("Unexpected fields must be rejected")

        missing = Path(tmp) / "missing"
        try:
            write_env(data, missing)
        except ValueError as exc:
            assert "already exist" in str(exc)
        else:
            raise AssertionError("UTM-ENV must not create a missing output directory")

    duplicate_account = ACCOUNT_SECTION + "邮箱：duplicate@example.com\n"
    try:
        parse_notion_sections(duplicate_account, APPLICATION_SECTION, "Example-abcd")
    except ValueError as exc:
        assert "邮箱：" in str(exc)
    else:
        raise AssertionError("Duplicate Notion fields must be rejected")

    try:
        parse_notion_sections(ACCOUNT_SECTION, APPLICATION_SECTION, "Other-abcd")
    except ValueError as exc:
        assert "page title" in str(exc).lower()
    else:
        raise AssertionError("The Notion page title must match 应用名 and VM name")


if __name__ == "__main__":
    main()
