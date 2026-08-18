#!/usr/bin/env python3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / "skills"

EXPECTED_LABELS = {
    "utm-notion": ("账号字段", "应用字段"),
    "utm-login": ("邮箱：", "修改后的密码：", "初始密码：", "电话：", "电话短信接收平台：", "用户名：", "生日（格式年/月/日）："),
    "utm-edit": ("修改后的密码：",),
    "utm-key": ("邮箱：", "用户名："),
    "utm-apps": ("邮箱：", "修改后的密码：", "初始密码：", "电话：", "电话短信接收平台：", "team ID:", "Renewal date：", "APP_ID：", "应用名: ", "正式包名: "),
    "utm-business": ("生日（格式年/月/日）：", "电话：", "电话短信接收平台：", "商务", "ABA Routing Number：", "Account Number：", "应用名: ", "正式包名: "),
    "utm-env": (
        "用户名：", "邮箱：", "电话：", "APP_ID：",
        "应用名: ", "顶级域名: ", "正式包名: ", "隐私协议: ",
        "用户协议: ", "支持链接: ", "应用类型：", "应用描述：", "关键词: ",
    ),
    "utm-script": ("邮箱：", "修改后的密码：", "初始密码：", "电话：", "电话短信接收平台："),
    "utm-image": ("APP_ID：",),
    "utm-p8": ("更新信息", "退款回调及p8", "issuer id: ", "key id:", "p8文件内容："),
    "utm-21": ("代码链接：", "APP_ID：", "正式包名: "),
    "utm-24": ("隐私协议: ",),
}

REQUIRED_API_ONLY_GUARDS = {
    "utm-24": (
        "Notion 只通过项目 `scripts/notion_api.py` 读取；不得用宿主 Chrome、Notion 插件、"
        "CUA、坐标或浏览器剪贴板读取 Notion。"
    ),
    "utm-p8": (
        "Notion 只通过项目 `scripts/notion_api.py` 读写；不得用宿主 Chrome、Notion 插件、"
        "CUA、坐标或浏览器剪贴板读写 Notion。"
    ),
}

FORBIDDEN_UI_READS = {
    "utm-login": ("local Chrome Notion session",),
    "utm-key": ("宿主机 Google Chrome",),
    "utm-env": ("browser.user.openTabs()", "[data-block-id]", "--json"),
    "utm-21": ("用 Chrome 插件", "已打开的宿主 Chrome"),
}

DOC_EXPECTATIONS = {
    "docs/utm-notion.md": ("scripts/utm_notion.py", "--app-name", "应用类型", "银行空值仍保留模板"),
    "docs/utm-key.md": ("scripts/notion_api.py", "verify-parent", "邮箱："),
    "docs/utm-apps.md": ("scripts/notion_api.py", "Notion API", "应用"),
    "docs/utm-business.md": ("scripts/notion_api.py", "Notion API", "生日（格式年/月/日）：", "ABA Routing Number："),
    "docs/utm-env.md": (
        "scripts/notion_api.py", "verify-parent", "scripts/utm_env_generate.py",
        "应用名: ", "关键词: ",
    ),
    "docs/utm-script.md": ("scripts/notion_api.py", "verify-parent", "utm-apps"),
    "docs/utm-image.md": (
        "scripts/notion_api.py", "verify-parent", "APP_ID：",
        "scripts/utm_image_source.py", "美女截图 链接", "研发截图", "应用名",
    ),
    "docs/utm-21.md": ("scripts/notion_api.py", "verify-parent", "正式包名: "),
    "docs/utm-24.md": ("scripts/notion_api.py", "verify-parent", "隐私协议: "),
    "docs/utm-p8.md": (
        "scripts/notion_api.py", "verify-parent", "write-toggle-code", "read-toggle-code", "退款回调及p8"
    ),
}


def main() -> None:
    notion_utm = (SKILL_ROOT / "utm-notion" / "SKILL.md").read_text(encoding="utf-8")
    assert "scripts/utm_notion.py --run-id '<run-id>'" in notion_utm
    assert "scripts/utm_notion.py --app-name '<app-name>'" in notion_utm
    assert "空值保留模板标签" in notion_utm

    for skill_name, labels in EXPECTED_LABELS.items():
        text = (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
        label_source = text
        if skill_name == "utm-notion":
            assert "scripts/utm_notion.py" in text
        elif skill_name == "utm-edit":
            assert "scripts/notion_api.py" in text, skill_name
            assert "verify-parent" not in text, skill_name
        elif skill_name == "utm-apps":
            label_source = (PROJECT_ROOT / "scripts" / "utm_apps.py").read_text(
                encoding="utf-8"
            )
            assert "scripts.notion_api" in label_source, skill_name
            assert "verify_parent" in label_source, skill_name
        else:
            assert "scripts/notion_api.py" in text, skill_name
            assert "verify-parent" in text, skill_name
        for label in labels:
            assert label in label_source, f"{skill_name}: {label!r}"
        for stale in FORBIDDEN_UI_READS.get(skill_name, ()):
            assert stale not in text, f"{skill_name}: {stale}"
        guard = REQUIRED_API_ONLY_GUARDS.get(skill_name)
        if guard:
            assert guard in text, f"{skill_name}: missing API-only guard"

    for relative_path, expected in DOC_EXPECTATIONS.items():
        text = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        for value in expected:
            assert value in text, f"{relative_path}: {value!r}"

    utm_image = (SKILL_ROOT / "utm-image" / "SKILL.md").read_text(encoding="utf-8")
    for required in (
        "scripts/notion_api.py", "verify-parent", "APP_ID：",
        "scripts/utm_image_source.py", "美女截图 链接", "研发截图", "应用名", "飞书 OpenAPI",
    ):
        assert required in utm_image, required
    for forbidden in ("截图链接: ",):
        assert forbidden not in utm_image, forbidden

    utm_env = (SKILL_ROOT / "utm-env" / "SKILL.md").read_text(encoding="utf-8")
    for required in ("scripts/utm_env_download_assets.py", "研发截图", "金币表格文件", "金币商店截图", "代码 URL"):
        assert required in utm_env, required
    for forbidden in ("研发金币图链接：", "金币表格: "):
        assert forbidden not in utm_env, forbidden

    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "existing guest Edge plus already-open host Chrome workflow" not in agents
    assert "using native clipboard and the current right-click `Paste` menu" not in agents
    assert "字段级写入并独立回读 Notion `APP_ID：`" in agents
    api_rule = next(line for line in agents.splitlines() if line.startswith("- Notion API 硬规则："))
    for skill_name in EXPECTED_LABELS:
        assert f"`{skill_name}`" in api_rule, skill_name

    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    bot_doc = (PROJECT_ROOT / "docs/utm-feishu-bot.md").read_text(encoding="utf-8")
    for value in ("utm-login", "utm-key", "utm-apps", "utm-business", "utm-env", "utm-script", "utm-image", "utm-21"):
        assert value in readme
        assert value in bot_doc
    assert "通过 Notion API" in readme
    assert "scripts/notion_api.py" in bot_doc
    utm_24_lines = [
        line for line in readme.splitlines()
        if line.startswith("→ utm-24：") or line.startswith("15. `utm-24`：")
    ]
    assert len(utm_24_lines) == 2
    assert any("Notion API" in line for line in utm_24_lines)
    assert any("record-auto-review-approval" in line for line in utm_24_lines)
    utm_p8_lines = [
        line for line in readme.splitlines()
        if line.startswith("→ utm-p8：") or line.startswith("12. `utm-p8`：")
    ]
    assert len(utm_p8_lines) == 2
    assert all("Notion API" in line for line in utm_p8_lines)

    print("NOTION_READ_SKILLS=verified")


if __name__ == "__main__":
    main()
