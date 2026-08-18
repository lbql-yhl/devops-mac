from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.utm_18_attempt import (
    UTM18AttemptError,
    classify_attempt,
    create_attempt,
    load_single_attempt,
    mark_attempt,
    archive_retryable_attempt,
    REMOTE_PRECOMMIT,
    REMOTE_INSPECT,
    _remote_run_script,
    run_remote,
)


SUCCESS_LOG = """work started
增强版内购创建完成！
统计信息: 共处理 14 个产品
"""


def test_attempt_ledger_and_paths_are_unique_mode_600(tmp_path: Path) -> None:
    attempt = create_attempt(
        tmp_path,
        run_id="run-12345678",
        vm_name="abcd",
        vm_ip="192.168.64.20",
    )
    assert (attempt.ledger_path.stat().st_mode & 0o777) == 0o600
    assert attempt.log_path == (
        f"/Users/example/Downloads/utm-18-fill-description-{attempt.attempt_id}.log"
    )
    assert attempt.status_path == f"{attempt.log_path}.status"
    assert attempt.command_path == (
        f"/Users/example/Downloads/utm-18-fill-description-{attempt.attempt_id}.command"
    )
    saved = json.loads(attempt.ledger_path.read_text(encoding="utf-8"))
    assert saved["state"] == "prepared"
    assert load_single_attempt(tmp_path, "run-12345678") == attempt


def test_remote_fill_description_opens_visible_terminal_with_launch_timeout(
    tmp_path: Path,
) -> None:
    attempt = create_attempt(
        tmp_path,
        run_id="run-12345678",
        vm_name="abcd",
        vm_ip="192.168.64.20",
    )
    seen: dict[str, object] = {}

    def runner(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return type("Result", (), {"returncode": 0})()

    assert run_remote(attempt, runner=runner) == 0
    assert 0 < int(seen["timeout"]) <= 60
    assert "input" not in seen
    remote_command = str(seen["command"][-1])
    assert remote_command == (
        f"/usr/bin/open -a Terminal {attempt.command_path}"
    )
    assert "npm" not in remote_command


def test_remote_precommit_creates_mode_0700_visible_command(tmp_path: Path) -> None:
    attempt_id = "a" * 32
    log_path = tmp_path / "attempt.log"
    status_path = tmp_path / "attempt.status"
    command_path = tmp_path / "attempt.command"
    visible_command = "#!/bin/zsh\necho visible\n"

    subprocess.run(
        [
            "python3",
            "-B",
            "-c",
            REMOTE_PRECOMMIT,
            str(log_path),
            str(status_path),
            attempt_id,
            str(command_path),
            visible_command,
        ],
        check=True,
    )

    assert command_path.read_text(encoding="utf-8") == visible_command
    assert command_path.stat().st_mode & 0o777 == 0o700
    assert log_path.stat().st_mode & 0o777 == 0o600
    assert status_path.read_text(encoding="utf-8") == (
        f"ATTEMPT_ID={attempt_id}\nRUN_STATE=prepared\n"
    )


def test_visible_command_runs_npm_with_tee_statuses_and_leaves_login_shell(
    tmp_path: Path,
) -> None:
    attempt = create_attempt(
        tmp_path,
        run_id="run-12345678",
        vm_name="abcd",
        vm_ip="192.168.64.20",
    )

    script = _remote_run_script(attempt)

    assert "cd Downloads/Fire_One_en1.3 && npm run fill:description" in script
    assert f'/usr/bin/tee "{attempt.log_path}"' in script
    assert "RUN_STATE=launching" in script
    assert "RUN_STATE=running" in script
    assert "RUN_STATE=finished" in script
    assert "RUNNER_PID=%s" in script
    assert "REMOTE_NPM_EXIT=%s" in script
    assert "REMOTE_TEE_EXIT=%s" in script
    assert script.rstrip().endswith("exec /bin/zsh -l")


def test_multiple_ledgers_are_never_guessed_as_latest(tmp_path: Path) -> None:
    first = create_attempt(
        tmp_path,
        run_id="run-12345678",
        vm_name="abcd",
        vm_ip="192.168.64.20",
    )
    duplicate = first.ledger_path.with_name("f" * 32 + ".json")
    duplicate.write_text(first.ledger_path.read_text(encoding="utf-8"), encoding="utf-8")
    duplicate.chmod(0o600)
    with pytest.raises(UTM18AttemptError, match="ATTEMPT_LEDGER_NOT_UNIQUE"):
        load_single_attempt(tmp_path, "run-12345678")


def test_explicit_business_error_takes_precedence_over_success_summary() -> None:
    result = classify_attempt(
        "❌ 创建产品 Demo 失败\n" + SUCCESS_LOG,
        "ATTEMPT_ID=" + "a" * 32 + "\nRUN_STATE=finished\nREMOTE_NPM_EXIT=0\nREMOTE_TEE_EXIT=0\n",
        process_running=False,
        expected_attempt_id="a" * 32,
    )
    assert result.state == "blocked_business_error"
    assert result.mode == "business_error"


def test_zero_exit_and_resident_after_summary_are_both_success_modes() -> None:
    attempt_id = "a" * 32
    finished = (
        f"ATTEMPT_ID={attempt_id}\nRUN_STATE=finished\n"
        "REMOTE_NPM_EXIT=0\nREMOTE_TEE_EXIT=0\n"
    )
    exited = classify_attempt(
        SUCCESS_LOG,
        finished,
        process_running=False,
        expected_attempt_id=attempt_id,
    )
    assert exited.state == "verified"
    assert exited.mode == "exited_zero"
    assert exited.product_count == 14

    running = f"ATTEMPT_ID={attempt_id}\nRUN_STATE=running\n"
    resident = classify_attempt(
        SUCCESS_LOG,
        running,
        process_running=True,
        expected_attempt_id=attempt_id,
    )
    assert resident.state == "verified"
    assert resident.mode == "resident_after_summary"


def test_completed_summary_with_interrupted_transport_requires_stable_recheck() -> None:
    attempt_id = "a" * 32
    for status in (
        f"ATTEMPT_ID={attempt_id}\nRUN_STATE=running\n",
        (
            f"ATTEMPT_ID={attempt_id}\nRUN_STATE=finished\n"
            "REMOTE_NPM_EXIT=130\nREMOTE_TEE_EXIT=0\n"
        ),
    ):
        result = classify_attempt(
            SUCCESS_LOG,
            status,
            process_running=False,
            expected_attempt_id=attempt_id,
        )
        assert result.state == "verification_pending"
        assert result.mode == "completed_after_transport_stop"
        assert result.product_count == 14


def test_summary_must_have_one_dynamic_positive_count() -> None:
    attempt_id = "a" * 32
    status = (
        f"ATTEMPT_ID={attempt_id}\nRUN_STATE=finished\n"
        "REMOTE_NPM_EXIT=0\nREMOTE_TEE_EXIT=0\n"
    )
    for log in (
        "增强版内购创建完成！\n统计信息: 共处理 0 个产品\n",
        SUCCESS_LOG + "统计信息: 共处理 2 个产品\n",
    ):
        result = classify_attempt(
            log,
            status,
            process_running=False,
            expected_attempt_id=attempt_id,
        )
        assert result.state == "ambiguous"


def test_incomplete_result_reports_finished_exit_status_and_last_log_line() -> None:
    attempt_id = "a" * 32
    result = classify_attempt(
        "work started\n已点击inflight 页面保存按钮\n",
        (
            f"ATTEMPT_ID={attempt_id}\nRUN_STATE=finished\n"
            "REMOTE_NPM_EXIT=1\nREMOTE_TEE_EXIT=0\n"
        ),
        process_running=False,
        expected_attempt_id=attempt_id,
    )

    assert result.reason == (
        "RUN_STATE=finished;REMOTE_NPM_EXIT=1;REMOTE_TEE_EXIT=0;"
        "LAST_LOG_LINE=已点击inflight 页面保存按钮"
    )


def test_incomplete_result_includes_complete_final_error_block() -> None:
    attempt_id = "a" * 32
    result = classify_attempt(
        (
            "work started\n"
            "Error: 无法找到侧边栏入口：App Information\n"
            "Call log:\n"
            "  - waiting for locator\n"
        ),
        (
            f"ATTEMPT_ID={attempt_id}\nRUN_STATE=finished\n"
            "REMOTE_NPM_EXIT=1\nREMOTE_TEE_EXIT=0\n"
        ),
        process_running=False,
        expected_attempt_id=attempt_id,
    )

    assert result.reason.endswith(
        "ERROR_DETAIL=Error: 无法找到侧边栏入口：App Information\n"
        "Call log:\n"
        "  - waiting for locator"
    )


def test_incomplete_result_includes_complete_final_json_error_object() -> None:
    attempt_id = "a" * 32
    result = classify_attempt(
        (
            "已点击inflight 页面保存按钮\n"
            "{\n"
            '  "code": "NAVIGATION_FAILED",\n'
            '  "message": "App Information unavailable"\n'
            "}\n"
        ),
        (
            f"ATTEMPT_ID={attempt_id}\nRUN_STATE=finished\n"
            "REMOTE_NPM_EXIT=1\nREMOTE_TEE_EXIT=0\n"
        ),
        process_running=False,
        expected_attempt_id=attempt_id,
    )

    assert result.reason.endswith(
        "ERROR_DETAIL={\n"
        '  "code": "NAVIGATION_FAILED",\n'
        '  "message": "App Information unavailable"\n'
        "}"
    )


def test_mark_attempt_updates_same_ledger_without_changing_binding(tmp_path: Path) -> None:
    attempt = create_attempt(
        tmp_path,
        run_id="run-12345678",
        vm_name="abcd",
        vm_ip="192.168.64.20",
    )
    updated = mark_attempt(attempt, state="verified", mode="exited_zero")
    assert updated.state == "verified"
    assert updated.attempt_id == attempt.attempt_id
    assert updated.log_path == attempt.log_path
    assert (updated.ledger_path.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize(
    ("log", "npm_exit", "tee_exit"),
    (
        ("Error: 找不到可点击的inflight 页面保存按钮", "1", "0"),
        ("已点击inflight 页面保存按钮", "1", "141"),
        (
            "browserType.connectOverCDP: connect ECONNREFUSED 127.0.0.1:9222",
            "1",
            "0",
        ),
    ),
)
def test_retryable_finished_attempt_is_archived_outside_active_ledger_directory(
    tmp_path: Path, log: str, npm_exit: str, tee_exit: str
) -> None:
    attempt = create_attempt(
        tmp_path,
        run_id="run-12345678",
        vm_name="abcd",
        vm_ip="192.168.64.20",
    )
    attempt = mark_attempt(attempt, state="running")

    archived = archive_retryable_attempt(
        attempt,
        log=log,
        status=(
            f"ATTEMPT_ID={attempt.attempt_id}\nRUN_STATE=finished\n"
            f"REMOTE_NPM_EXIT={npm_exit}\nREMOTE_TEE_EXIT={tee_exit}\n"
        ),
        process_running=False,
    )

    assert archived.parent.name == "archive"
    assert archived.is_file()
    assert load_single_attempt(tmp_path, "run-12345678") is None


def test_remote_inspect_excludes_its_own_process_instead_of_self_matching() -> None:
    assert "pgrep" not in REMOTE_INSPECT
    assert "RUNNER_PID" in REMOTE_INSPECT
    assert '"npm run fill:description" in command' not in REMOTE_INSPECT
    assert '"src/fill-description.ts" in command' not in REMOTE_INSPECT
