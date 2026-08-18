from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGETS = (
    "skills/_shared/AUTOMATION_CONTRACT.md",
    "skills/utm-21/SKILL.md",
    "skills/utm-22/SKILL.md",
    "skills/utm-23/SKILL.md",
    "skills/utm-24/SKILL.md",
    "skills/utm-vm-clone/SKILL.md",
    "docs/utm-21.md",
    "docs/utm-22.md",
)
HISTORICAL_FIXED_PASSWORD_SPEC = (
    ROOT / "docs/superpowers/specs/2026-08-14-utm-script-visible-terminal-design.md"
)
STALE_PASSWORD_SEMANTICS = (
    "OP-FIXED-PASSWORD-1234",
    "`1234`",
    "fixed password",
    "fixed-password",
    "固定密码",
)


def test_operational_docs_require_host_configured_guest_password() -> None:
    for relative in TARGETS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "SUBMISSION_GUEST_PASSWORD" in text, relative
        assert "OP-FIXED-PASSWORD-1234" not in text, relative
        assert "`1234`" not in text, relative

    shared = (ROOT / TARGETS[0]).read_text(encoding="utf-8")
    assert "OP-CONFIGURED-GUEST-PASSWORD" in shared
    assert "宿主 `.env`" in shared
    assert "不得回显" in shared

    for relative in TARGETS[1:6]:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "OP-CONFIGURED-GUEST-PASSWORD" in text, relative


def test_tracked_docs_and_skills_contain_no_stale_fixed_password_semantics() -> None:
    markdown_files = sorted(
        (*((ROOT / "docs").rglob("*.md")), *((ROOT / "skills").rglob("*.md")))
    )
    for path in markdown_files:
        text = path.read_text(encoding="utf-8")
        for stale in STALE_PASSWORD_SEMANTICS:
            assert stale not in text, f"{path.relative_to(ROOT)}: {stale}"

    assert not HISTORICAL_FIXED_PASSWORD_SPEC.exists()
