#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/utm-24/SKILL.md"
DOC = ROOT / "docs/utm-24.md"


def main() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    doc = DOC.read_text(encoding="utf-8")
    active = "\n".join(
        (
            skill,
            doc,
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
            (ROOT / "docs/utm-feishu-bot.md").read_text(encoding="utf-8"),
        )
    )

    for value in (
        "紧接 `utm-23` 立即执行", "只采集 `01-media-manager.png` 和 `04-privacy-agreement.png`",
        "REVIEW_SCREENSHOT_02=verified", "REVIEW_SCREENSHOT_03=verified",
        "REVIEW_SCREENSHOT_05=verified", "REVIEW_SCREENSHOTS=verified_5",
        "Items Ready to Submit (15)", "In-App Purchases (14)",
        "record-auto-review-approval", "--iap-count 14", "--screenshot",
        "source=automatic_self_check", "operator_id=automation:self-check",
        "AUTOMATIC_REVIEW_APPROVAL=verified", "AUTOMATIC_REVIEW_SUBMIT=enabled",
        "正常运行没有等待节点", "5/10/20/40 秒",
        "REVIEW_SUBMIT_ATTEMPT_ID", "REVIEW_SUBMIT_ATTEMPT_STATUS=verified", "禁止第二次点击",
        "Waiting for Review", "15 Items Submitted",
        "developer.apple.com/contact/app-store/?topic=expedite",
        "pristine_form", "unknown_prior_send", "只点击一次 `Send`",
        "EXPEDITED_REVIEW_RESULT=verified",
        "UTM_24=verified", "最终技能", "不再调用或交接其他技能",
    ):
        assert value in skill, value

    for value in (
        "正常主线不发提审确认卡", "自动自检授权", "自动提交一次",
        "record-auto-review-approval", "REVIEW_SUBMISSION_SOURCE=automatic_self_check",
        "REVIEW_SUBMIT_ATTEMPT_ID", "最后故障卡",
    ):
        assert value in doc, value

    assert "services/feishu_bot.py notify-review \\" not in skill
    assert "--decision-kind review_submit" not in skill
    assert "wait-decision --decision-kind review_submit" not in active
    assert "notify-review-success" not in skill
    assert skill.count("saveReviewScreenshot(") == 2

    print("UTM_24_AUTOMATIC_REVIEW=verified")


if __name__ == "__main__":
    main()
