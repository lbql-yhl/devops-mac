#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/utm-business/SKILL.md"


def main() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    guide = (ROOT / "docs/utm-business.md").read_text(encoding="utf-8")
    combined = "\n".join(
        (
            skill,
            guide,
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
            (ROOT / "docs/utm-feishu-bot.md").read_text(encoding="utf-8"),
            (ROOT / "docs/feishu-host-routing.md").read_text(encoding="utf-8"),
        )
    )

    for value in (
        "blank 才写、equal 双回读跳过、conflict 不覆盖",
        "首次、5 秒、10 秒三轮",
        "verify-parent",
        "utm-business-bank-info-missing",
        "绝不从 Feishu/runtime/聊天回退银行号",
        "Agreement/Submit/Add/Done 结果不明",
        "绝不第二次点击",
        "UTM_BUSINESS=verified",
    ):
        assert value in skill, value

    for value in (
        "首次、5 秒、10 秒三轮",
        "Notion API",
        "one-shot ledger",
        "UTM_BUSINESS=verified",
    ):
        assert value in guide, value

    for stale in (
        "utm-20-bank-info-missing",
        "UTM_20_RECOVERY_FIRST=verified",
        "从 Feishu/runtime/旧运行/对话/记忆回退银行号",
    ):
        assert stale not in combined, stale

    print("UTM_BUSINESS_RECOVERY_FIRST=verified")


if __name__ == "__main__":
    main()
