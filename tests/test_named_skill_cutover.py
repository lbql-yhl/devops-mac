from pathlib import Path

from scripts import preflight
from services import feishu_bot


ROOT = Path(__file__).resolve().parents[1]
ORDERED = (
    "utm-vm-clone",
    "utm-notion",
    "utm-clash-ip",
    "utm-login",
    "utm-edit",
    "utm-key",
    "utm-apps",
    "utm-business",
    "utm-env",
    "utm-script",
    "utm-image",
    "utm-p8",
    "utm-21",
    "utm-22",
    "utm-23",
    "utm-24",
)


def test_named_skills_are_the_only_mainline_names() -> None:
    assert preflight.ORDERED == ORDERED
    assert feishu_bot.SUBMISSION_SKILL_ORDER == ORDERED

    discovered = tuple(
        path.parent.name for path in sorted((ROOT / "skills").glob("*/SKILL.md"))
    )
    assert set(discovered) == set(ORDERED)

    for name in ("utm-login", "utm-edit", "utm-key", "utm-script"):
        skill = ROOT / "skills" / name / "SKILL.md"
        doc = ROOT / "docs" / f"{name}.md"
        assert skill.is_file()
        assert doc.is_file()
        assert skill.read_text(encoding="utf-8").startswith(f"---\nname: {name}\n")


def test_named_skill_handoffs_follow_the_mainline() -> None:
    login = (ROOT / "skills/utm-login/SKILL.md").read_text(encoding="utf-8")
    edit = (ROOT / "skills/utm-edit/SKILL.md").read_text(encoding="utf-8")
    key = (ROOT / "skills/utm-key/SKILL.md").read_text(encoding="utf-8")
    env = (ROOT / "skills/utm-env/SKILL.md").read_text(encoding="utf-8")
    script = (ROOT / "skills/utm-script/SKILL.md").read_text(encoding="utf-8")

    assert "utm-edit" in login
    assert "utm-key" in edit
    assert "utm-apps" in key
    assert "utm-script" in env
    assert "utm-image" in script
