import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SEGMENTS = (
    ("虚拟机准备", ("utm-vm-clone",)),
    (
        "环境准备",
        (
            "utm-notion",
            "utm-clash-ip",
            "utm-login",
            "utm-edit",
            "utm-key",
            "utm-apps",
            "utm-business",
        ),
    ),
    ("脚本准备、执行", ("utm-env", "utm-script", "utm-image", "utm-p8")),
    ("代码处理", ("utm-21", "utm-22", "utm-23", "utm-24")),
)

ENTRY_SCRIPTS = {
    "utm-notion": "utm_notion.py",
    "utm-clash-ip": "utm_clash_ip.py",
    "utm-login": "utm_7_login.py",
    "utm-edit": "utm_8_change_password.py",
    "utm-key": "utm_9.py",
    "utm-apps": "utm_apps.py",
    "utm-business": "utm_business.py",
    "utm-env": "utm_env.py",
    "utm-script": "utm_18.py",
    "utm-image": "utm_image.py",
    "utm-p8": "utm_p8.py",
}

DEVELOPED_SEGMENTS = (SEGMENTS[1], SEGMENTS[2])


def _skill_text(name: str) -> str:
    return (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def test_exact_four_segments_and_order_are_project_contract() -> None:
    expected = "\n".join((
        "1. 虚拟机准备：utm-vm-clone",
        "2. 环境准备：utm-notion → utm-clash-ip → utm-login → utm-edit → utm-key → utm-apps → utm-business",
        "3. 脚本准备、执行：utm-env → utm-script → utm-image → utm-p8",
        "4. 代码处理：utm-21 → utm-22 → utm-23 → utm-24",
    ))
    for path in (ROOT / "AGENTS.md", ROOT / "README.md", ROOT / "docs/project-workflow.md"):
        text = path.read_text(encoding="utf-8")
        assert expected in text, path


def test_every_mainline_skill_is_command_only_and_independently_runnable() -> None:
    forbidden = (
        "## 操作步骤",
        "## 执行流程",
        "## 自动恢复",
        "## 本技能自动恢复矩阵",
        "## 脚本合同",
        "页面锚点",
        "DOM",
        "Accessibility",
        "Computer Use",
        "helper",
    )
    for segment, skills in DEVELOPED_SEGMENTS:
        for name in skills:
            text = _skill_text(name)
            assert f"所属阶段：{segment}" in text, name
            assert "本技能可人工单独指定执行" in text, name
            assert "只运行以上唯一宿主入口" in text, name
            assert "不读取或解释脚本内容" in text, name
            assert all(token not in text for token in forbidden), name
            assert len(text.splitlines()) <= 45, name


def test_every_skill_uses_one_existing_host_entry_script() -> None:
    scripts_dir = ROOT / "scripts"
    for name, script_name in ENTRY_SCRIPTS.items():
        text = _skill_text(name)
        absolute = str(scripts_dir / script_name)
        assert (scripts_dir / script_name).is_file(), name
        assert absolute in text, name
        referenced_entries = set(
            re.findall(
                re.escape(str(scripts_dir)) + r"/[A-Za-z0-9_.-]+\.(?:py|mjs|sh)",
                text,
            )
        )
        assert referenced_entries == {absolute}, (name, referenced_entries)


def test_segment_handoffs_stop_at_each_segment_boundary() -> None:
    for _, skills in DEVELOPED_SEGMENTS:
        for index, name in enumerate(skills):
            text = _skill_text(name)
            if index + 1 < len(skills):
                assert f"成功后交接 `{skills[index + 1]}`" in text, name
            else:
                assert "成功后结束本段" in text, name


def test_code_processing_segment_remains_out_of_scope() -> None:
    project_contract = "4. 代码处理：utm-21 → utm-22 → utm-23 → utm-24"
    for path in (ROOT / "AGENTS.md", ROOT / "README.md", ROOT / "docs/project-workflow.md"):
        assert project_contract in path.read_text(encoding="utf-8"), path


def test_submission_prompt_does_not_force_all_four_segments_to_run_together() -> None:
    source = (ROOT / "services" / "feishu_bot.py").read_text(encoding="utf-8")
    assert "四段均可单独运行" in source
    assert "每个技能也可人工单独指定执行" in source
    assert "按此顺序连续执行到最终 `utm-24`" not in source
