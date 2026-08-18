from pathlib import Path
import os


ROOT = Path(__file__).resolve().parents[1]

SKILLS = (
    ("utm-notion", "utm_notion.py", "utm-clash-ip", "status=verified"),
    ("utm-clash-ip", "utm_clash_ip.py", "utm-login", "UTM_CLASH_IP=verified"),
    ("utm-login", "utm_7_login.py", "utm-edit", "UTM_7=verified"),
    ("utm-edit", "utm_8_change_password.py", "utm-key", "UTM_8=verified"),
    ("utm-key", "utm_9.py", "utm-apps", "UTM_9=verified"),
    ("utm-apps", "utm_apps.py", "utm-business", "UTM_APPS=verified"),
    ("utm-business", "utm_business.py", "utm-env", "UTM_BUSINESS=verified"),
    ("utm-env", "utm_env.py", "utm-script", "UTM_ENV=verified"),
    ("utm-script", "utm_18.py", "utm-image", "UTM_18=verified"),
    ("utm-image", "utm_image.py", "utm-p8", "UTM_19=verified"),
    ("utm-p8", "utm_p8.py", "utm-21", "UTM_P8=verified"),
)


def test_each_skill_declares_one_host_entry_and_the_next_handoff() -> None:
    for skill, script, next_skill, _marker in SKILLS:
        text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        absolute_entry = str(ROOT / "scripts" / script)
        assert absolute_entry in text, f"{skill}: unique host entry missing"
        if skill == "utm-apps":
            assert text.count(absolute_entry) == 2
            assert "按 run-id 运行：" in text
            assert "按页面运行：" in text
            continue
        assert "操作者只运行以上唯一宿主入口" in text, skill
        if skill in {"utm-vm-clone", "utm-business", "utm-p8"}:
            assert "成功后结束本段" in text, skill
            assert f"交接 `{next_skill}`" in text, skill
        else:
            assert f"成功后交接 `{next_skill}`" in text, skill


def test_each_skill_and_doc_publish_the_same_quiet_terminal_contract() -> None:
    for skill, _script, _next_skill, marker in SKILLS:
        paths = (
            ROOT / "skills" / skill / "SKILL.md",
            ROOT / "docs" / f"{skill}.md",
        )
        for path in paths:
            assert path.is_file(), path
            text = path.read_text(encoding="utf-8")
            if skill == "utm-apps" and path.name == "SKILL.md":
                continue
            assert f"`开始执行：{skill}`" in text, path
            assert f"`执行成功：{skill}；{marker}`" in text, path
            assert f"`执行报错：{skill}；<非敏感原因>`" in text, path
            assert "只显示" in text, path


def test_all_twelve_host_entries_use_the_shared_quiet_output_wrapper() -> None:
    for skill, script, _next_skill, marker in SKILLS:
        source = (ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "run_clean_cli" in source, script
        assert f'skill_name="{skill}"' in source, script
        assert marker in source, script
        assert "traceback.print_exc" not in source, script


def test_host_entries_have_no_legacy_user_facing_output_strings() -> None:
    forbidden = ("开始 UTM", "运行报错：", "UTM_P8_FAILED=")
    for _skill, script, _next_skill, _marker in SKILLS:
        source = (ROOT / "scripts" / script).read_text(encoding="utf-8")
        for value in forbidden:
            assert value not in source, f"{script}: legacy output remains: {value}"


def test_quiet_wrapper_suppresses_internal_python_and_child_fd_output(capfd) -> None:
    from scripts.clean_cli import run_clean_cli

    def operation() -> int:
        print("internal python chatter")
        os.write(1, b"internal child stdout\n")
        os.write(2, b"internal child stderr\n")
        print("UTM_TEST=verified")
        return 0

    assert run_clean_cli(
        skill_name="utm-test",
        success_marker="UTM_TEST=verified",
        operation=operation,
    ) == 0
    captured = capfd.readouterr()
    assert captured.out.splitlines() == [
        "开始执行：utm-test",
        "执行成功：utm-test；UTM_TEST=verified",
    ]
    assert captured.err == ""


def test_quiet_wrapper_allows_only_explicit_step_progress(capfd) -> None:
    from scripts.clean_cli import emit_progress, run_clean_cli

    def operation() -> int:
        emit_progress("步骤2已操作")
        print("internal detail")
        return 0

    assert run_clean_cli(
        skill_name="utm-test",
        success_marker="UTM_TEST=verified",
        operation=operation,
    ) == 0
    captured = capfd.readouterr()
    assert captured.out.splitlines() == [
        "开始执行：utm-test",
        "步骤2已操作",
        "执行成功：utm-test；UTM_TEST=verified",
    ]


def test_quiet_wrapper_reports_one_safe_error_without_traceback(capfd) -> None:
    from scripts.clean_cli import run_clean_cli

    def operation() -> int:
        print("secret internal detail")
        raise RuntimeError("unsafe free-form detail")

    assert run_clean_cli(
        skill_name="utm-test",
        success_marker="UTM_TEST=verified",
        operation=operation,
    ) == 1
    captured = capfd.readouterr()
    assert captured.out.splitlines() == ["开始执行：utm-test"]
    assert captured.err.splitlines() == ["执行报错：utm-test；RuntimeError"]
    assert "Traceback" not in captured.out + captured.err
    assert "unsafe free-form detail" not in captured.out + captured.err


def test_quiet_wrapper_can_preserve_full_error_for_non_sensitive_workflow(capfd) -> None:
    from scripts.clean_cli import run_clean_cli

    def operation() -> int:
        raise RuntimeError("SSH password command failed: connection refused")

    assert run_clean_cli(
        skill_name="utm-vm-clone-step-02",
        success_marker="STEP_02_ACCOUNT=verified",
        operation=operation,
        preserve_error_detail=True,
    ) == 1
    captured = capfd.readouterr()
    assert captured.out.splitlines() == ["开始执行：utm-vm-clone-step-02"]
    assert captured.err.splitlines() == [
        "执行报错：utm-vm-clone-step-02；SSH password command failed: connection refused"
    ]


def test_quiet_wrapper_preserves_full_blocked_output_for_non_sensitive_workflow(capfd) -> None:
    from scripts.clean_cli import run_clean_cli

    def operation() -> int:
        print(
            "UTM_CLONE=blocked: SSH password command failed: connection refused",
            file=os.sys.stderr,
        )
        return 1

    assert run_clean_cli(
        skill_name="utm-vm-clone",
        success_marker="UTM_CLONE=verified",
        operation=operation,
        preserve_error_detail=True,
    ) == 1
    captured = capfd.readouterr()
    assert captured.err.splitlines() == [
        "执行报错：utm-vm-clone；SSH password command failed: connection refused"
    ]


def test_quiet_wrapper_preserves_only_safe_internal_error_classification(capfd) -> None:
    from scripts.clean_cli import run_clean_cli

    def operation() -> int:
        print("secret diagnostic body")
        print("UTM_TEST=blocked:PAGE_TIMEOUT", file=os.sys.stderr)
        return 3

    assert run_clean_cli(
        skill_name="utm-test",
        success_marker="UTM_TEST=verified",
        operation=operation,
    ) == 1
    captured = capfd.readouterr()
    assert captured.out.splitlines() == ["开始执行：utm-test"]
    assert captured.err.splitlines() == ["执行报错：utm-test；PAGE_TIMEOUT"]
    assert "secret diagnostic body" not in captured.out + captured.err


def test_quiet_wrapper_preserves_safe_error_with_optional_space(capfd) -> None:
    from scripts.clean_cli import run_clean_cli

    def operation() -> int:
        print("UTM_TEST=blocked: PAGE_TIMEOUT", file=os.sys.stderr)
        return 3

    assert run_clean_cli(
        skill_name="utm-test",
        success_marker="UTM_TEST=verified",
        operation=operation,
    ) == 1
    captured = capfd.readouterr()
    assert captured.out.splitlines() == ["开始执行：utm-test"]
    assert captured.err.splitlines() == ["执行报错：utm-test；PAGE_TIMEOUT"]
