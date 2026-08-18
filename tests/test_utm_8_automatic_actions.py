#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    ROOT / "skills/utm-edit/SKILL.md",
    ROOT / "docs/utm-edit.md",
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "docs/utm-feishu-bot.md",
)


def main() -> None:
    combined = "\n".join(source.read_text(encoding="utf-8") for source in SOURCES)
    skill = (ROOT / "skills/utm-edit/SKILL.md").read_text(encoding="utf-8")
    doc = (ROOT / "docs/utm-edit.md").read_text(encoding="utf-8")
    for text in (skill, doc):
        assert "Sign-In & Security" in text
        assert "Change Password" in text
        assert "修改后的密码：" in text
        assert "scripts/utm_8_change_password.py" in text
        assert "AppleAccountScriptsBackup" in text
        assert "Accessibility" in text
        for stale in (
            "Personal Information",
            "用户名：",
            "生日：",
            "初始密码：",
            "生成一个新密码",
            "PASSWORD_CANDIDATE_ATTEMPTS",
            "screen-workflow-router",
            "screen_workflow_router",
            "三张模板",
            "视觉路由",
            "cv2",
            "pyautogui",
            "guest-local `.venv`",
        ):
            assert stale not in text, stale
    assert "自动点击一次最终 `Change`/`Continue`" in combined
    assert "UTM_8=verified" in combined
    for forbidden in (
        "最终 `Change` 由用户确认并点击",
        "不得代替用户激活",
        "用户完成该点击",
        "user-click checkpoint",
        "change-password-generate-password",
        "重填验证通过后再次自动点击一次最终 `Change`/`Continue`",
    ):
        assert forbidden not in combined, forbidden
    print("UTM_8_AUTOMATIC_ACTIONS=verified")


if __name__ == "__main__":
    main()
