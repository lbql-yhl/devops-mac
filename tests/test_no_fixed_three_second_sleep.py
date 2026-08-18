from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXED_THREE_SECOND_SLEEP = re.compile(
    r"(?:\btime\.)?sleep\(3(?:\.0)?\)|\bsleeper\(3(?:\.0)?\)"
)


def test_project_scripts_have_no_fixed_three_second_sleep() -> None:
    violations: list[str] = []
    for path in (
        PROJECT_ROOT / "scripts" / "utm_apps.py",
        PROJECT_ROOT / "scripts" / "apple_web_workflow.py",
        PROJECT_ROOT / "scripts" / "edge_accessibility.py",
    ):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FIXED_THREE_SECOND_SLEEP.search(line):
                violations.append(f"{path.relative_to(PROJECT_ROOT)}:{line_number}")
    assert violations == []


def test_utm_apps_contracts_require_immediate_action_not_fixed_waits() -> None:
    for relative_path in (
        "AGENTS.md",
        "skills/_shared/AUTOMATION_CONTRACT.md",
        "skills/utm-apps/SKILL.md",
        "docs/utm-apps.md",
    ):
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert "等待至少 3 秒" not in source, relative_path
        assert "至少等待 3 秒" not in source, relative_path
        assert "等待固定 3 秒" not in source, relative_path
        assert "等待 3 秒" not in source, relative_path
