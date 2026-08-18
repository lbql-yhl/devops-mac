from pathlib import Path

from scripts import preflight


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "utm-vm-clone" / "SKILL.md"


def test_utm_vm_clone_skill_documents_the_single_entry_and_ten_step_recovery() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert "name: utm-vm-clone" in text
    assert "Use when" in text
    assert (
        "/Users/example/.venvs/py312-common/bin/python3 "
        "$PROJECT_ROOT/scripts/utm_clone_and_initialize.py"
    ) in text
    assert "UTM_CLONE_AND_INITIALIZE=verified" in text
    for step in range(1, 11):
        assert f"scripts/utm_vm_clone_step_{step:02d}_" in text
    assert "scripts/utm_vm_user_dependencies_guest.py" not in text
    assert "utm_apps_guest" not in text
    assert "失败只按 0/5/10 秒重跑当前步骤" in text
    assert "已验证步骤" in text
    assert "首次桌面引导" in text
    assert "check_host_vm_dependencies.py" in text
    assert "26" in text
    assert "不得读取、搜索、解释或审查脚本正文" in text
    assert "收到开始、继续、重新执行或指定步骤后，立即运行对应命令" in text
    assert "不得自行运行正文未列出的诊断命令" in text
    assert "不得显示内部检查、重试和步骤过程" in text
    assert "已绑定其他应用的陈旧活动指针" in text
    assert "总入口自动清除" in text
    assert "UTM CLI 必须唯一" in text
    assert "与 Registry" in text
    assert "最多 30 秒" in text
    assert "Registry 域可用时" in text
    assert "复制前检查" in text
    assert "5 GiB" in text
    assert "直接 plist" in text
    assert "先启动 UTM 应用" in text
    assert "宿主 AX" in text
    assert "`starting`" in text
    assert "库详情窗口" in text
    assert "SSH stdout 与 stderr" in text
    assert "MiniBuddy" in text
    assert "`pgrep -f`" in text
    assert not (ROOT / "docs" / "utm-vm-clone.md").exists()
    assert not (ROOT / "docs" / "utm-post-clone.md").exists()

    assert len(text.splitlines()) <= 70


def test_utm_vm_clone_starts_the_16_skill_mainline_without_utm_a() -> None:
    result = preflight.check(project_only=True)

    assert len(preflight.ORDERED) == 16
    assert preflight.ORDERED[0] == "utm-vm-clone"
    assert preflight.STANDALONE == ()
    assert result["skills_count"] == 16
    assert result["standalone_skills_count"] == 0
    assert result["skill_set_exact"] is True
    assert result["ok"] is True

    installer = (ROOT / "scripts" / "install_project_skills.sh").read_text(
        encoding="utf-8"
    )
    assert (
        'install_entries=("${skills[@]}" "${standalone_skills[@]}" "_shared")'
        in installer
    )
    assert installer.count("PROJECT_SKILLS_INSTALLED=16") == 2
    assert installer.count("PROJECT_STANDALONE_SKILLS_INSTALLED=0") == 2
    assert "PROJECT_SKILLS_INSTALLED=25" not in installer
