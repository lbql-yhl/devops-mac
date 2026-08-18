from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "utm-notion" / "SKILL.md"


def test_utm_notion_skill_is_a_thin_deterministic_script_entry() -> None:
    text = SKILL.read_text(encoding="utf-8")
    run_id_command = (
        "python3 $PROJECT_ROOT/scripts/utm_notion.py "
        "--run-id '<run-id>'"
    )
    app_name_command = (
        "python3 $PROJECT_ROOT/scripts/utm_notion.py "
        "--app-name '<app-name>'"
    )

    assert "name: utm-notion" in text
    assert run_id_command in text
    assert text.count(run_id_command) == 1
    assert app_name_command in text
    assert text.count(app_name_command) == 1
    assert "--vm-name '<vm-name>'" in text
    assert "两个选择器二选一" in text
    assert "不读取或解释脚本内容" in text
    for redundant in ("Computer Use", "浏览器", "剪贴板", "视觉", "LIFESTYLE", "初始密码"):
        assert redundant not in text


def test_utm_notion_skill_keeps_missing_value_fill_explicitly_test_only() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert "--fill-missing-with-test" not in text
    assert "映射必填字段" not in text
