from pathlib import Path

from scripts import preflight


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ORDER = (
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


def test_business_replaces_utm_14_and_utm_20_in_the_mainline() -> None:
    assert preflight.ORDERED == EXPECTED_ORDER
    assert len(preflight.ORDERED) == 16
    assert (ROOT / "skills/utm-business/SKILL.md").is_file()
    assert (ROOT / "docs/utm-business.md").is_file()
    for removed in ("utm-14", "utm-20"):
        assert not (ROOT / "skills" / removed).exists()
        assert not (ROOT / "docs" / f"{removed}.md").exists()


def test_business_handoffs_are_continuous() -> None:
    apps = (ROOT / "skills/utm-apps/SKILL.md").read_text(encoding="utf-8")
    business = (ROOT / "skills/utm-business/SKILL.md").read_text(encoding="utf-8")
    screenshot = (ROOT / "skills/utm-image/SKILL.md").read_text(encoding="utf-8")
    assert "utm-business" in apps
    assert "utm-env" in business
    assert "UTM_BUSINESS=verified" in business
    assert "utm-p8" in screenshot


def test_old_business_skill_names_are_installer_removals() -> None:
    installer = (ROOT / "scripts/install_project_skills.sh").read_text(encoding="utf-8")
    skills_block = installer.split("skills=(", 1)[1].split(")", 1)[0]
    removed_block = installer.split("removed_skills=(", 1)[1].split(")", 1)[0]
    assert "utm-business" in skills_block
    assert "utm-14" not in skills_block
    assert "utm-20" not in skills_block
    assert "utm-14" in removed_block
    assert "utm-20" in removed_block
    assert installer.count("PROJECT_SKILLS_INSTALLED=16") == 2


def test_clone_preloads_the_complete_business_playwright_script_set() -> None:
    from scripts import utm_post_clone

    assert utm_post_clone.BUSINESS_GUEST_FILES == (
        "utm_business_one.mjs",
    )
