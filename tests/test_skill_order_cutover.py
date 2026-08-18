from dataclasses import replace
from pathlib import Path

import services.feishu_bot as feishu_bot
from scripts import preflight


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ALLOWED_CHAT = "oc_allowed"
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
STANDALONE = ()
REMOVED = (
    "notion-utm", "notion-utm-1", "utm-clone-macos", "utm-1", "utm-2", "utm-3", "vm-down", "utm-4",
    "utm-5", "files", "utm-clash", "utm-6", "utm-10", "utm-11", "utm-12", "utm-13", "utm-14", "utm-15", "utm-16", "utm-17", "utm-19", "utm-20",
)


def test_proxy_cutover_starts_with_clone_and_has_no_legacy_proxy_stages() -> None:
    assert preflight.ORDERED == ORDERED
    assert preflight.STANDALONE == STANDALONE
    assert feishu_bot.SUBMISSION_SKILL_ORDER == ORDERED

    discovered = tuple(
        path.parent.name for path in sorted((ROOT / "skills").glob("*/SKILL.md"))
    )
    assert set(discovered) == set(ORDERED) | set(STANDALONE)
    for name in REMOVED:
        assert not (ROOT / "skills" / name).exists()
        assert not (ROOT / "docs" / f"{name}.md").exists()

    installer = (ROOT / "scripts/install_project_skills.sh").read_text(encoding="utf-8")
    skills_block = installer.split("skills=(", 1)[1].split(")", 1)[0]
    removed_block = installer.split("removed_skills=(", 1)[1].split(")", 1)[0]
    assert "utm-apps" in skills_block
    assert "utm-10" not in skills_block
    assert "utm-11" not in skills_block
    assert "utm-12" not in skills_block
    assert "utm-13" not in skills_block
    for old_name in ("utm-10", "utm-11", "utm-12", "utm-13", "utm-14", "utm-17", "utm-19", "utm-20"):
        assert old_name in removed_block
    assert installer.count("PROJECT_SKILLS_INSTALLED=16") == 2
    assert installer.count("PROJECT_STANDALONE_SKILLS_INSTALLED=0") == 2
    assert "standalone_skills=()" in installer.replace("\n", "")


def test_feishu_submission_start_entry_is_disabled(tmp_path, monkeypatch) -> None:
    assert feishu_bot.SUBMISSION_START_ENABLED is False
    monkeypatch.setattr(feishu_bot, "RUNS_FILE", tmp_path / "runs.json")
    monkeypatch.setattr(feishu_bot, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(
        feishu_bot, "CODEX_APP_SESSIONS_DIR", tmp_path / "codex-app-sessions"
    )
    config = replace(
        feishu_bot.load_config(
            environ={"FEISHU_DAILY_REPORT_CHAT_ID": "test-report-chat"}
        ),
        allowed_chat_id=FIXTURE_ALLOWED_CHAT,
        submission_host_machine="海淋",
        assistant_enabled=False,
    )
    registration = """使用的宿主机：海淋
应用名：SampleApp
代理信息：192.0.2.10:7612:user:pass
代码链接：https://example.com/repo.git
开发者账号信息：
美国
developer@example.com
InitialPass123!
15550101234 https://example.com/sms
"""
    assert feishu_bot.handle_incoming_text(
        config, registration, FIXTURE_ALLOWED_CHAT, source="test"
    ) == ""
    assert not feishu_bot.RUNS_FILE.exists()
    assert not feishu_bot.PROMPTS_DIR.exists()
    assert not feishu_bot.CODEX_APP_SESSIONS_DIR.exists()
