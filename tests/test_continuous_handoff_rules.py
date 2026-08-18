#!/usr/bin/env python3
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills"
ORDERED = (
    "utm-vm-clone", "utm-notion", "utm-clash-ip", "utm-login", "utm-edit", "utm-key", "utm-apps", "utm-business",
    "utm-env", "utm-script", "utm-image", "utm-p8", "utm-21", "utm-22",
    "utm-23", "utm-24",
)
MARKERS = (
    "UTM_CLONE_AND_INITIALIZE=verified", '"status": "verified"', "UTM_CLASH_IP=verified", "UTM_7=verified", "UTM_8=verified",
    "UTM_9=verified", "UTM_APPS=verified", "UTM_BUSINESS=verified",
    "UTM_ENV=verified", "UTM_18=verified", "UTM_19=verified", "UTM_P8=verified",
    "UTM_21=verified", "UTM_22=verified", "UTM_23=verified", "UTM_24=verified",
)


def read_skill(name: str) -> str:
    return (SKILL_ROOT / name / "SKILL.md").read_text(encoding="utf-8")


def main() -> None:
    assert len(ORDERED) == len(MARKERS) == 16
    assert len(set(ORDERED)) == 16

    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    current_line = next(
        line for line in agents.splitlines()
        if line.startswith("- Current skills in exact order:")
    )
    assert tuple(re.findall(r"`([^`]+)`", current_line)) == ORDERED

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_order = tuple(
        match.group(1)
        for match in re.finditer(r"(?m)^\d+\. `([^`]+)`：", readme)
    )
    assert readme_order == ORDERED

    bot_doc = (ROOT / "docs/utm-feishu-bot.md").read_text(encoding="utf-8")
    bot_doc_order = tuple(
        match.group(1)
        for match in re.finditer(r"(?m)^→ ((?:notion|utm|files)[a-z0-9-]*)$", bot_doc)
    )
    assert bot_doc_order == ORDERED

    stale_values = ("rhnz", "Xrimo", "Jan Haren", "NXTKR5YYHJ", "1.0 Ready for Review")
    for name, marker in zip(ORDERED, MARKERS):
        text = read_skill(name)
        assert marker in text, f"{name}: missing {marker}"
        for stale in stale_values:
            assert stale not in text, f"{name}: stale {stale}"

    assert "`utm-notion`" in read_skill("utm-vm-clone")
    assert "`utm-clash-ip`" in read_skill("utm-notion")
    assert "最终技能" in read_skill("utm-24")
    assert "不再调用或交接其他技能" in read_skill("utm-24")

    success_owners = tuple(
        name for name in ORDERED
        if "feishu_bot.py notify-review-success" in read_skill(name)
    )
    automatic_review_owners = tuple(
        name for name in ORDERED
        if "record-auto-review-approval" in read_skill(name)
    )
    assert success_owners == ()
    assert automatic_review_owners == ("utm-24",)

    print("CONTINUOUS_HANDOFF_RULES=verified")


if __name__ == "__main__":
    main()
