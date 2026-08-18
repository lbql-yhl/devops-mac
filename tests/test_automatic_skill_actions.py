#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
SKILL_NAMES = (
    "utm-vm-clone", "utm-notion", "utm-clash-ip", "utm-login", "utm-edit",
    "utm-key", "utm-apps", "utm-business",
    "utm-env", "utm-script", "utm-image", "utm-p8", "utm-21", "utm-22", "utm-23", "utm-24",
)


def main() -> None:
    skill_texts = {
        name: (SKILLS / name / "SKILL.md").read_text(encoding="utf-8") for name in SKILL_NAMES
    }
    active_docs = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("utm-*.md"))]
    canonical = "\n".join((*skill_texts.values(), *(path.read_text(encoding="utf-8") for path in active_docs)))

    assert "再自动点击一次并记录 `PASSWORD_CHANGE_SUBMISSION=clicked_once`" in skill_texts["utm-edit"]
    for stale in ("Personal Information", "用户名：", "生日：", "初始密码："):
        assert stale not in skill_texts["utm-edit"], stale
    assert "只允许 Submit 一次" in skill_texts["utm-apps"]
    assert "one-shot `Submit`" in skill_texts["utm-business"]
    assert "`bank_add` attempt 后只点击一次 `Add`" in skill_texts["utm-business"]
    assert "record-auto-review-approval" in skill_texts["utm-24"]
    assert "notify-review" not in skill_texts["utm-24"]
    assert "--decision-kind review_submit" not in skill_texts["utm-24"]
    assert "notify-review-success" not in skill_texts["utm-24"]
    assert "EXPEDITED_REVIEW_RESULT=verified" in skill_texts["utm-24"]
    assert "notify-review-success" not in canonical
    assert "AUTOMATIC_REVIEW_APPROVAL=verified" in skill_texts["utm-24"]
    assert "source=automatic_self_check" in skill_texts["utm-24"]
    assert "Proxies → PROXY" in skill_texts["utm-clash-ip"]
    for required in (
        "Personal Information",
        "用户名：",
        "生日（格式年/月/日）：",
        "APPLE_ACCOUNT_PROFILE=verified",
        "NOTION_USERNAME=verified",
        "NOTION_BIRTHDAY=verified",
    ):
        assert required in skill_texts["utm-login"], required

    for relative_path in ("README.md", "AGENTS.md", "docs/utm-feishu-bot.md"):
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "自动" in text, relative_path
        assert "最后故障卡" in text, relative_path

    for stale in (
        "最终 `Change` 由用户确认并点击",
        "user-click checkpoint",
        "两次最终 `Submit` 都必须分别取得用户明确授权",
        "用户明确授权后",
        "用户明确提供",
        "用户对本次证书提交的明确授权",
        "用户对本次 W-8BEN 提交的明确授权",
        "keychain-auth-prompt",
        "apple-device-2fa",
        "change-password-generate-password",
        "computer-use/1.0.",
        "REQUIRED SUB-SKILL",
        "空值时回退",
        "or current-template `截图链接: `",
    ):
        assert stale not in canonical, stale

    for name in ("utm-edit", "utm-business", "utm-env", "utm-p8", "utm-24"):
        text = skill_texts[name]
        assert "notify-fault" in text, name
        assert "wait-decision" in text, name
    assert "正常运行没有等待节点" in skill_texts["utm-24"]
    print("AUTOMATIC_SKILL_ACTIONS=verified")


if __name__ == "__main__":
    main()
