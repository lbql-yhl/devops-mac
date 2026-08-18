#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/utm-23/SKILL.md"
DOC = ROOT / "docs/utm-23.md"


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
        "先执行只读已准备恢复分支", "ALREADY_PREPARED_CHECK=verified",
        "部分准备先建立逐项状态账本", "DETERMINISTIC_RESUME_STEP=<n>",
        "PREPARATION_STATE=partial_recoverable", "PREPARATION_STATE=ambiguous",
        "AUTO_RECOVERY_ATTEMPTS=3", "utm-23-partial-preparation",
        "15/30/60/120 秒", "ADD_BUILD_VISIBILITY_POLL=exhausted",
        "DUPLICATE_UPLOAD=forbidden", "任何决定都不得上传",
        "Missing Compliance", "Game Center", "In-App Purchases (14)",
        "Draft Submissions (1)", "Create New Submission",
        'saveReviewScreenshot("02-iap-drafts.png")',
        'saveReviewScreenshot("03-app-information.png")',
        "删除前证据快照", "结果不明只读检查", "用 before 自动恢复错误行",
        "APP_STORE_REGULATIONS_PERMITS=empty",
        "APP_STORE_SERVER_NOTIFICATIONS=empty",
        "SUBMIT_FOR_REVIEW=not_clicked", "UTM_23=verified", "`utm-24`",
    ):
        assert value in skill, value

    for value in (
        "有序状态账本", "从第一个未完成", "15/30/60/120 秒",
        "SECOND_UPLOAD=forbidden", "before/after", "最后故障卡",
        "SUBMIT_FOR_REVIEW=not_clicked", "UTM_23=verified",
    ):
        assert value in doc, value

    for stale in (
        "upload-existing", "REUPLOAD_ATTEMPT", "REUPLOAD_SOURCE",
        "重新上传后", "部分准备或状态不明确时发送三按钮故障卡",
    ):
        assert stale not in active, stale

    print("UTM_23_DETERMINISTIC_RESUME=verified")


if __name__ == "__main__":
    main()
