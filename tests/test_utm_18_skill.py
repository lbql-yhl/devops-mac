#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/utm-script/SKILL.md"
DOC = ROOT / "docs/utm-script.md"
UTM19_SKILL = ROOT / "skills/utm-image/SKILL.md"
COMPLETE_ENTRY = "$PROJECT_ROOT/scripts/utm_18.py"


def main() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    checklist = DOC.read_text(encoding="utf-8")

    for label, text in (("skill", skill), ("doc", checklist)):
        assert COMPLETE_ENTRY in text, label
        assert text.count(COMPLETE_ENTRY) == 1, label
        for required in (
            "唯一完整入口",
            "scripts/utm_18_edge.py",
            "scripts/utm_18_ssh.py",
            "scripts/utm_18_attempt.py",
            "APPLE_DEVELOPER_ACCOUNT=verified",
            "/bin/zsh -lic",
            "CDP_ENDPOINT=http://127.0.0.1:9222",
            "npm run fill:description",
            "UTM_18_ATTEMPT_ID",
            "UTM_18_LOG_PATH=precommitted",
            "增强版内购创建完成！",
            "统计信息: 共处理 N 个产品",
            "N>=1",
            "exited_zero",
            "resident_after_summary",
            "SSH_EXIT=255",
            "❌ 创建产品",
            "error calling the App Store Connect API",
            "status 500",
            "UNEXPECTED_ERROR",
            "FILL_DESCRIPTION=blocked_business_error",
            "UTM_18=verified",
            "utm-image",
        ):
            assert required in text, f"{label}: {required}"
        for forbidden in (
            "open -a Terminal",
            "open -na Terminal",
            "Command+N",
            "sky.click",
            "SUBMISSION_SSH_PRIVATE_KEY",
            "BatchMode=yes",
            "ssh -i",
            "📊 统计信息: 共处理 14 个产品",
        ):
            assert forbidden not in text, f"{label}: {forbidden}"

    assert "python3 \"$PROJECT_ROOT/scripts/utm_18_edge.py\"" not in skill
    assert "python3 \"$PROJECT_ROOT/scripts/utm_18_ssh.py\"" not in skill
    assert "正常成功路径不暂停、不询问用户" in skill

    synchronized = {
        "README.md": ROOT / "README.md",
        "AGENTS.md": ROOT / "AGENTS.md",
        "docs/utm-feishu-bot.md": ROOT / "docs" / "utm-feishu-bot.md",
    }
    for label, path in synchronized.items():
        content = path.read_text(encoding="utf-8")
        assert COMPLETE_ENTRY in content, label
        assert "唯一完整入口" in content, label
        assert "resident_after_summary" in content, label
        assert "FILL_DESCRIPTION=blocked_business_error" in content, label

    utm19 = UTM19_SKILL.read_text(encoding="utf-8")
    utm19_doc = (ROOT / "docs" / "utm-image.md").read_text(encoding="utf-8")
    for stale in (
        "保留 `utm-script` 的 guest Terminal",
        "保留 guest Terminal",
        "不操作保留的 guest Terminal",
    ):
        assert stale not in utm19, stale
        assert stale not in utm19_doc, stale


if __name__ == "__main__":
    main()
