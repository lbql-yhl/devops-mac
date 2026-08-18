#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    skill = (ROOT / "skills/utm-apps/SKILL.md").read_text(encoding="utf-8")
    docs = (ROOT / "docs/utm-apps.md").read_text(encoding="utf-8")
    workflow = (ROOT / "scripts/utm_11_one.mjs").read_text(encoding="utf-8")

    for document in (skill, docs):
        for value in (
            "$PROJECT_ROOT/scripts/utm_apps.py",
            "/Users/example/Downloads/AppleAccountScriptsBackup/utm_11_one.mjs",
            "UTM_11=verified",
            "NOTION_MEMBERSHIP_FIELDS=updated",
            "UTM_APPS=verified",
        ):
            assert value in document, value
        for removed_rule in (
            "SSH stdin",
            "one-shot",
            "禁止上传",
            "覆盖 guest",
            "SowSheet-igec",
            "UUID/MAC",
        ):
            assert removed_rule not in document, removed_rule

    for script_step in (
        "https://developer.apple.com/app-store/small-business-program/",
        "Enroll now",
        'input[type="radio"]',
        'input[type="submit"]#submit',
        "05-small-business.png",
    ):
        assert script_step in workflow, script_step


if __name__ == "__main__":
    main()
