"""Contract tests for the v3.0 Agent registry and documentation."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SKILLS = [
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
]


def load_registry() -> dict[str, object]:
    return json.loads((ROOT / "config/agents.json").read_text(encoding="utf-8-sig"))


def test_project_version_is_3() -> None:
    assert (ROOT / "VERSION").read_text(encoding="utf-8-sig").strip() == "3.0"
    assert load_registry()["project_version"] == "3.0"


def test_registry_matches_exact_mainline() -> None:
    registry = load_registry()
    assert registry["mainline_order"] == EXPECTED_SKILLS
    assert "utm-clash" not in registry["mainline_order"]
    assert registry["handoff_schema"] == [
        "status",
        "run_id",
        "agent_id",
        "skill_id",
        "evidence",
        "artifacts",
        "next_step",
    ]


def test_every_registered_agent_has_prompt_file() -> None:
    agents = load_registry()["agents"]
    assert len(agents) == 12
    ids = {agent["id"] for agent in agents}
    assert len(ids) == len(agents)
    for agent in agents:
        prompt = ROOT / agent["prompt_file"]
        assert prompt.is_file(), agent["id"]
        assert prompt.read_text(encoding="utf-8-sig").strip(), agent["id"]


def test_required_specialists_are_registered() -> None:
    agents = load_registry()["agents"]
    names = {agent["id"] for agent in agents}
    assert {
        "feishu-orchestrator",
        "visual-assets",
        "app-a-code",
        "fault-recovery",
    } <= names


def test_readme_contains_v3_architecture_and_fault_loop() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8-sig")
    assert "v3.0" in readme
    assert "飞书主控 Agent" in readme
    assert "flowchart" in readme
    assert "重试 → 自愈 → 人工" in readme
    assert "utm-clash-ip" in readme
    assert "utm-clash →" not in readme
