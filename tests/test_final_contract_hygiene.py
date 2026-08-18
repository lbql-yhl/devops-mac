from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = tuple((ROOT / "docs").rglob("*.md"))
SEARCHABLE_CONTRACTS = DOCS + (
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "skills/utm-24/SKILL.md",
    ROOT / "skills/utm-p8/SKILL.md",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_searchable_docs_do_not_restate_superseded_final_stage_contracts() -> None:
    forbidden = (
        "成功后发送无按钮通知卡",
        "成功通知卡仅在 App Store 状态明确成功后发送一次",
        "App Store 明确显示 `Waiting for Review` 或 `15 Items Submitted` 后发送一次绿色成功通知卡",
        "当前 30 个技能",
        "登记的 30 个技能",
        "当前 30-skill",
        "current 30-skill",
        "本项目 30 技能",
    )
    hits = []
    for path in SEARCHABLE_CONTRACTS:
        text = _text(path)
        for phrase in forbidden:
            if phrase in text:
                try:
                    label = path.relative_to(ROOT)
                except ValueError:
                    label = path
                hits.append(f"{label}: {phrase}")
    assert not hits, "stale searchable contracts:\n" + "\n".join(hits)


def test_historical_execution_contracts_are_absent() -> None:
    assert not (ROOT / "docs/superpowers").exists()


def test_current_authority_and_notion_scope_are_unambiguous() -> None:
    readme = _text(ROOT / "README.md")
    agents = _text(ROOT / "AGENTS.md")
    assert "项目内 `skills/` 是唯一技能源" in readme
    assert "项目内 `skills/` 是 16 个主线技能的唯一正文源" in agents
    assert "项目采用 16 个主线技能" in readme
    assert "utm-p8" in readme
    assert (ROOT / "skills/_shared/AUTOMATION_CONTRACT.md").is_file()
