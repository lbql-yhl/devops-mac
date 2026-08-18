#!/usr/bin/env python3
from pathlib import Path

from skill_contract_assertions import assert_shared_contract_reference


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills"
ORDERED = (
    "utm-vm-clone", "utm-notion", "utm-clash-ip", "utm-login", "utm-edit",
    "utm-key", "utm-apps", "utm-business",
    "utm-env", "utm-script", "utm-image", "utm-p8", "utm-21", "utm-22", "utm-23", "utm-24",
)
STANDALONE = ()
GUI_SKILLS = {
    "utm-apps",
    "utm-script", "utm-image", "utm-22", "utm-23", "utm-24",
}
SHARED_RECOVERY_SKILLS = {
    "utm-clash-ip", "utm-login", "utm-edit", "utm-key", "utm-apps", "utm-business",
    "utm-env", "utm-script", "utm-image", "utm-21",
    "utm-p8", "utm-22", "utm-23", "utm-24",
}
INLINE_FAULT_COMMAND_SKILLS = SHARED_RECOVERY_SKILLS - {"utm-login"}


def read_skill(name: str) -> str:
    return (SKILL_ROOT / name / "SKILL.md").read_text(encoding="utf-8")


def require_all(text: str, values: tuple[str, ...], source: str) -> None:
    for value in values:
        assert value in text, f"{source}: missing {value}"


def main() -> None:
    assert SKILL_ROOT.is_dir(), "project must own the canonical skill source"
    discovered = tuple(
        sorted(path.parent.name for path in SKILL_ROOT.glob("*/SKILL.md"))
    )
    assert discovered == tuple(sorted((*ORDERED, *STANDALONE))), discovered
    assert len(ORDERED) == len(set(ORDERED)) == 16
    assert len(STANDALONE) == len(set(STANDALONE)) == 0
    assert set(ORDERED).isdisjoint(STANDALONE)

    shared_path = SKILL_ROOT / "_shared" / "AUTOMATION_CONTRACT.md"
    shared = shared_path.read_text(encoding="utf-8")
    require_all(
        shared,
        (
            "自动诊断 → 自动修复 → 自动复验 → 最后才发故障卡",
            "每次 GUI 动作后的固定闭环",
            "窗口尺寸、焦点、菜单或页面布局变化",
            "可逆误点恢复",
            "不可逆动作的两阶段门禁",
            "AUTO_RECOVERY_ATTEMPTS",
            "AUTO_RECOVERY_ACTIONS",
            "AUTO_RECOVERY_RESULT",
            "--recovery-attempts",
            "--recovery-actions",
            "--recovery-result",
            "--unrepairable",
            "OP-NATIVE-PASTE",
            "OP-BROWSER-URL-NO-SCHEME",
            "OP-APPLE-PHONE-OTP",
            "OP-CONFIGURED-GUEST-PASSWORD",
            "OP-USER-CONFIRMATION",
            "scripts/shared_operations.py browser-url",
        ),
        "shared contract",
    )

    clone = read_skill("utm-vm-clone")
    require_all(
        clone,
        (
            "$PROJECT_ROOT/scripts/utm_clone_and_initialize.py",
            "UTM_CLONE_AND_INITIALIZE=verified",
            "DATABASE_AVAILABLE=1",
            "VM_SHUTDOWN=verified",
        ),
        "utm-vm-clone",
    )

    for name in ORDERED:
        text = read_skill(name)
        if name == "utm-vm-clone":
            continue
        if name == "utm-notion":
            require_all(
                text,
                (
                    "$PROJECT_ROOT/scripts/utm_notion.py",
                    '"status": "verified"',
                    '"handoff": "utm-clash-ip"',
                ),
                name,
            )
            continue
        if name == "utm-apps":
            require_all(
                text,
                (
                    "$PROJECT_ROOT/scripts/utm_apps.py",
                    "操作者只运行以上唯一宿主入口",
                    "UTM_APPS=verified",
                    "成功后交接 `utm-business`",
                ),
                name,
            )
            continue
        if name == "utm-business":
            assert_shared_contract_reference(
                SKILL_ROOT / name / "SKILL.md", text, shared_path
            )
            require_all(
                text,
                (
                    "OP-FULL-ENTRY-REPLAY",
                    "$PROJECT_ROOT/scripts/utm_business.py",
                    "UTM_BUSINESS=verified",
                    "成功后交接 `utm-env`",
                ),
                name,
            )
            continue
        assert_shared_contract_reference(
            SKILL_ROOT / name / "SKILL.md", text, shared_path
        )
        require_all(
            text,
            (
                "## 本技能自动恢复矩阵",
                "三轮",
                "`--recovery-result unrepairable`",
                "`stop`",
                "`manual_continue`",
                "`retry_skill`",
            ),
            name,
        )
        assert "异常必须先向当前 run 原" not in text, name
        assert name in SHARED_RECOVERY_SKILLS
        if name in INLINE_FAULT_COMMAND_SKILLS:
            require_all(
                text,
                (
                    "notify-fault",
                    "wait-decision",
                    "--recovery-attempts",
                    "--recovery-actions",
                    "--recovery-result",
                    "<actual-count-at-least-3>",
                ),
                name,
            )
            assert text.count("services/feishu_bot.py notify-fault") == 1, name
        else:
            assert "具体发送和等待命令只使用共享合同" in text, name
            assert "notify-fault" not in text, name
        for fault_first in (
            "Immediately send the fault card",
            "任一异常都必须进入统一故障卡流程",
            "发生阻断时立即暂停后续副作用，以对应",
            "immediately use the global fault-card flow",
            "进入统一故障卡流程",
            "uses the existing fault-card path",
            "使用当前 run 的故障卡流程",
            "走当前 run 的故障卡流程",
            "进入 `utm-24` 三按钮故障卡",
            "停在故障卡人工检查循环",
        ):
            assert fault_first not in text, f"{name}: fault-first wording {fault_first}"
        if name in GUI_SKILLS:
            require_all(
                text,
                ("误点", "GUI_RECOVERY=verified", "最新截图", "至少 3 秒"),
                name,
            )

    utm_19 = read_skill("utm-image")
    require_all(
        utm_19,
        (
            "当前数字 App ID",
            "上传前记录",
            "已有截图数量",
            "剩余容量",
            "SCREENSHOT_PREUPLOAD_CLASSIFICATION=empty|complete",
            "Cancel",
        ),
        "utm-image",
    )
    assert "partial_upload" not in utm_19
    assert "不核对详情 URL" not in utm_19

    utm_22 = read_skill("utm-22")
    require_all(
        utm_22,
        (
            "UPLOAD_ATTEMPT_ID",
            "有界只读轮询",
            "结果不明",
            "先查询同一版本和构建号",
            "XCODE_GUI_RECOVERY=verified",
        ),
        "utm-22",
    )

    utm_23 = read_skill("utm-23")
    require_all(
        utm_23,
        (
            "ADD_BUILD_VISIBILITY_POLL=exhausted",
            "不得重复上传",
            "删除前证据快照",
            "删除确认弹窗",
            "确定性恢复到第一个未完成步骤",
        ),
        "utm-23",
    )
    assert "upload-existing" not in utm_23

    utm_24 = read_skill("utm-24")
    require_all(
        utm_24,
        (
            "record-auto-review-approval",
            "AUTOMATIC_REVIEW_APPROVAL=verified",
            "AUTOMATIC_REVIEW_SUBMIT=enabled",
            "系统自检授权",
        ),
        "utm-24",
    )
    assert "notify-review" not in utm_24
    assert "--decision-kind review_submit" not in utm_24
    assert "提审确认卡" not in utm_24

    utm_p8 = read_skill("utm-p8")
    require_all(
        utm_p8,
        (
            "scripts/utm_p8.py",
            "prod.yml",
            "NOTION_ROLLBACK=verified",
            "before",
        ),
        "utm-p8",
    )

    utm_7 = read_skill("utm-login")
    require_all(
        utm_7,
        (
            "scripts/utm_7_login.py",
            "--stdin-json",
            "Accessibility API",
            "不使用视觉",
            "APPLE_ACCOUNT=verified",
            "UTM_7=verified",
            "首次确认后自动关闭 System Settings",
            "第二次确认成功后保留 System Settings 打开",
        ),
        "utm-login",
    )

    utm_9 = read_skill("utm-key")
    require_all(
        utm_9,
        (
            "$PROJECT_ROOT/scripts/utm_9.py",
            "固定密码 SSH",
            "不使用视觉",
            "/usr/bin/certtool",
            "CSR_PRIVATE_KEY=verified",
            "CSR_DISK=verified",
        ),
        "utm-key",
    )
    for stale in ("Computer Use", 'open -a "Keychain Access"', "BatchMode=yes"):
        assert stale not in utm_9, stale
    assert "Computer Use" in utm_7 and "禁止" in utm_7

    for required in (
        ROOT / "scripts" / "install_project_skills.sh",
        ROOT / "scripts" / "notion_api.py",
        ROOT / "scripts" / "notion_utm_prepare.py",
        ROOT / "scripts" / "preflight.py",
        ROOT / "scripts" / "shared_operations.py",
        ROOT / "scripts" / "utm_env_generate.py",
        ROOT / "scripts" / "utm_21_clone.py",
        ROOT / "scripts" / "utm_22_distribute.mjs",
        ROOT / "services" / "feishu_bot.py",
        ROOT / "services" / "feishu_gateway.py",
        ROOT / "services" / "feishu_supervisor.py",
        ROOT / "services" / "project_paths.py",
        ROOT / "services" / "submission_runner.py",
        ROOT / ".env.example",
        ROOT / "shared-files" / "README.md",
        ROOT / "shared-files" / "apple-store-bm" / "README.md",
        ROOT / "shared-files" / "apple-store-bm" / "apple_store_tools",
        ROOT / "shared-files" / "apple-store-bm" / "config" / "prod.example.yml",
    ):
        assert required.is_file(), required

    shared_files = ROOT / "shared-files"
    forbidden_shared_paths = (
        shared_files / ".env",
        shared_files / "socks5.yml",
        shared_files / ("Fire_One_en" + "1.2"),
        shared_files / "apple-store-bm" / "config" / "prod.yml",
        shared_files / "tools" / "flutter",
    )
    for forbidden in forbidden_shared_paths:
        assert not forbidden.exists(), forbidden
    assert not tuple(shared_files.rglob("*.p8")), "shared source must not contain P8 keys"
    assert all(path.stat().st_size < 100 * 1024 * 1024 for path in shared_files.rglob("*") if path.is_file())
    assert (shared_files / "apple-store-bm" / "apple_store_tools").stat().st_mode & 0o100

    shared_readme = (shared_files / "README.md").read_text(encoding="utf-8")
    assert "Fire_One_en1.3" in shared_readme
    assert ("Fire_One_en" + "1.2") not in shared_readme
    assert not (ROOT / "docs" / "superpowers").exists(), "historical execution contracts must be removed"
    assert not (ROOT / "runtime" / "utm_22_game_center_rebuild.zsh").exists(), "stale executable"

    clash_ip = read_skill("utm-clash-ip")
    require_all(
        clash_ip,
        ("代理ip:", "代理端口:", "代理用户名：", "代理用户密码：", "SOCKS5_SHARED_READBACK=exact"),
        "utm-clash-ip",
    )

    generator = (ROOT / "scripts" / "utm_env_generate.py").read_text(encoding="utf-8")
    assert "/Users/" not in generator
    assert "SHARED_DIR" in generator

    runner = (ROOT / "services" / "submission_runner.py").read_text(encoding="utf-8")
    assert 'os.getenv("FEISHU_CODEX_MODEL", "gpt-5.6-sol")' in runner

    installer = (ROOT / "scripts" / "install_project_skills.sh").read_text(encoding="utf-8")
    require_all(
        installer,
        (
            "validate_all_sources",
            "rollback_install",
            "unsafe install root",
            "PROJECT_SKILLS_INSTALLED=16",
            "PROJECT_STANDALONE_SKILLS_INSTALLED=0",
            "PROJECT_SHARED_CONTRACT=linked",
        ),
        "installer",
    )

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SUBMISSION_HOST_MACHINE=\n" in env_example
    require_all(env_example, ("CODEUP_USERNAME=", "CODEUP_PASSWORD="), "env example")

    portable_sources = (
        ROOT / ".env.example",
        ROOT / "services" / "project_paths.py",
    )
    for source in portable_sources:
        text = source.read_text(encoding="utf-8")
        assert "/Volumes/AutoA" not in text, f"{source.name}: copied-host VM path"

    active_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md")))
    )
    for path in (ROOT / "README.md", ROOT / "AGENTS.md"):
        text = path.read_text(encoding="utf-8")
        require_all(text, ("skills/<skill>/SKILL.md", "Codex 记忆"), path.name)
    assert "/Volumes/AutoA" not in active_text
    assert "computer-use/1.0." not in active_text
    for stale_fault_first in (
        "unknown categories stop the workflow",
        "after 5 failures call `notify-fault`",
        "uses the abnormal fault-card path",
        "handled only through the fault card",
        "A known business failure also sends a new fault card",
        "未知分类立即阻断",
        "未知协议/安全异常才发故障卡",
    ):
        assert stale_fault_first not in active_text, stale_fault_first

    print("UNATTENDED_PORTABLE_SKILL_CONTRACT=verified")


if __name__ == "__main__":
    main()
