#!/usr/bin/env python3
import re
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.feishu_bot as feishu_bot
from services.feishu_bot import parse_submission_data
from scripts.notion_utm_prepare import format_account_block, validate_account_block


FIXTURE_ALLOWED_CHAT = "oc_allowed"


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    daily_docs = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in ("AGENTS.md", "README.md", "docs/utm-feishu-bot.md")
    )
    assert "日报必须先从本地项目证据生成预览" in daily_docs
    assert "等待用户明确确认同一份文本后" in daily_docs
    assert "故障卡、成功卡" in daily_docs
    assert "不使用 Git 状态或提交历史" in daily_docs

    report_only_chat = "-".join(("daily", "report", "chat"))
    config = replace(
        feishu_bot.load_config(
            environ={"FEISHU_DAILY_REPORT_CHAT_ID": report_only_chat}
        ),
        allowed_chat_id=FIXTURE_ALLOWED_CHAT,
        daily_report_chat_id=report_only_chat,
    )
    assert (
        feishu_bot.handle_incoming_text(
            config,
            "@GaleOpsBrain 状态",
            config.daily_report_chat_id,
            source="test",
        )
        == ""
    )
    assert feishu_bot.handle_incoming_text(config, "", "oc_unauthorized", source="test") == ""
    assert feishu_bot.handle_command(config, "", "oc_unauthorized") == ""

    text = """@GaleOpsBrain
使用的宿主机：海淋
应用名：SampleApp
代理信息：[192.0.2.10:7612](http://192.0.2.10:7612/):proxyuser:proxypass
代码链接：https://example.com/ios/sample-app.git
开发者账号信息：
示例国家
developer@example.com
InitialPass123!
5550101234 [https://example.com/sms?token=test](https://example.com/sms?token=test)
银行信息：
ABA Routing Number：000000000
Account Number：000000000000
"""
    data = parse_submission_data(text)
    assert data is not None
    assert data["host_machine"] == "海淋"
    assert data["app_name"] == "SampleApp"
    assert data["proxy"] == {
        "host": "192.0.2.10",
        "port": "7612",
        "username": "proxyuser",
        "password": "proxypass",
    }
    assert data["developer_account"]["country"] == "示例国家"
    assert data["developer_account"]["email"] == "developer@example.com"
    assert data["developer_account"]["password"] == "InitialPass123!"
    assert data["developer_account"]["phone"] == "5550101234"
    assert data["developer_account"]["sms_url"] == "https://example.com/sms?token=test"
    assert data["bank_info"] == {
        "aba_routing_number": "000000000",
        "account_number": "000000000000",
    }
    assert data["code_link"] == "https://example.com/ios/sample-app.git"

    labeled_text = """使用的宿主机：海淋
应用名：LabelApp
代理信息：198.51.100.20:6967:proxyuser:proxypass
代码链接：https://example.com/ios/label-app.git
开发者账号信息：
国家：英国
电话：18457393658
短信接收链接：https://example.com/sms-record?token=sample@example.com
邮箱：label@example.com
初始密码：InitialPass123!
"""
    labeled_data = parse_submission_data(labeled_text)
    assert labeled_data is not None
    assert labeled_data["developer_account"]["country"] == "英国"
    assert labeled_data["developer_account"]["email"] == "label@example.com"
    assert labeled_data["developer_account"]["password"] == "InitialPass123!"
    assert labeled_data["developer_account"]["phone"] == "18457393658"
    assert labeled_data["developer_account"]["sms_url"] == "https://example.com/sms-record?token=sample@example.com"
    assert labeled_data["bank_info"] == {"aba_routing_number": "", "account_number": ""}

    with tempfile.TemporaryDirectory() as tmp:
        old_images_dir = feishu_bot.VM_IMAGES_DIR
        old_runs_file = feishu_bot.RUNS_FILE
        old_prompts_dir = feishu_bot.PROMPTS_DIR
        old_codex_app_sessions_dir = feishu_bot.CODEX_APP_SESSIONS_DIR
        tmp_path = Path(tmp)
        feishu_bot.VM_IMAGES_DIR = tmp_path / "images"
        feishu_bot.VM_IMAGES_DIR.mkdir()
        feishu_bot.RUNS_FILE = tmp_path / "runs.json"
        feishu_bot.PROMPTS_DIR = tmp_path / "prompts"
        feishu_bot.CODEX_APP_SESSIONS_DIR = tmp_path / "codex-app-sessions"
        config = replace(
            config,
            allowed_chat_id=FIXTURE_ALLOWED_CHAT,
            runner_command="python3 services/submission_runner.py",
            assistant_enabled=False,
            submission_host_machine="海淋",
        )
        try:
            blank_config = replace(config, submission_host_machine="")
            assert feishu_bot.handle_incoming_text(
                blank_config, text, FIXTURE_ALLOWED_CHAT, source="test"
            ) == ""
            foreign_text = text.replace("使用的宿主机：海淋", "使用的宿主机：dev1", 1)
            assert feishu_bot.handle_incoming_text(
                config, foreign_text, FIXTURE_ALLOWED_CHAT, source="test"
            ) == ""
            assert feishu_bot.handle_incoming_text(
                config, "/提审 开始 SampleApp", FIXTURE_ALLOWED_CHAT, source="test"
            ) == ""
            assert not feishu_bot.RUNS_FILE.exists()
            assert not feishu_bot.PROMPTS_DIR.exists()

            assert feishu_bot.handle_incoming_text(
                config,
                text.split("银行信息：", 1)[0],
                FIXTURE_ALLOWED_CHAT,
                source="test",
            ) == ""
            assert not feishu_bot.RUNS_FILE.exists()
            assert not feishu_bot.PROMPTS_DIR.exists()
            assert not feishu_bot.CODEX_APP_SESSIONS_DIR.exists()

            with mock.patch.object(feishu_bot, "SUBMISSION_START_ENABLED", True), mock.patch.object(
                feishu_bot.subprocess, "Popen",
                side_effect=AssertionError("Feishu intake must not start a background runner"),
            ):
                assert (
                    feishu_bot.handle_incoming_text(
                        config,
                        text.split("银行信息：", 1)[0],
                        FIXTURE_ALLOWED_CHAT,
                        source="test",
                    )
                    == "收到，准备开始SampleApp提审。"
                )
            assert feishu_bot.RUNS_FILE.exists()
            prompt_paths = list(feishu_bot.PROMPTS_DIR.glob("*.md"))
            assert len(prompt_paths) == 1
            session_paths = list(feishu_bot.CODEX_APP_SESSIONS_DIR.glob("*.json"))
            assert len(session_paths) == 1
            runs = feishu_bot.read_json(feishu_bot.RUNS_FILE, {"runs": []})["runs"]
            assert len(runs) == 1
            assert runs[0]["status"] == "codex_app_session_requested"
            assert runs[0]["codex_app_session_path"] == str(session_paths[0])
            assert "runner_log_path" not in runs[0]
            session = feishu_bot.read_json(session_paths[0], {})
            assert session["status"] == "requested"
            assert session["run_id"] == runs[0]["id"]
            assert session["prompt_path"] == str(prompt_paths[0])
            assert "do not run the background submission runner" in session["instruction"]
            prompt = prompt_paths[0].read_text(encoding="utf-8")
            agents = (root / "AGENTS.md").read_text(encoding="utf-8")
            current_line = next(
                line for line in agents.splitlines()
                if line.startswith("- Current skills in exact order:")
            )
            current_order = tuple(re.findall(r"`([^`]+)`", current_line))
            assert len(current_order) == 16
            assert feishu_bot.SUBMISSION_SKILL_ORDER == current_order
            assert f"固定技能顺序（16 个）：{' -> '.join(current_order)}。" in prompt
            for required in (
                "银行信息区块可整体省略",
                "utm-business-bank-info-missing",
                "重新执行 `verify-parent` 和两次 `read-field --copy`",
                "Feishu/runtime/旧运行/对话/记忆",
            ):
                assert required in prompt, required
        finally:
            feishu_bot.VM_IMAGES_DIR = old_images_dir
            feishu_bot.RUNS_FILE = old_runs_file
            feishu_bot.PROMPTS_DIR = old_prompts_dir
            feishu_bot.CODEX_APP_SESSIONS_DIR = old_codex_app_sessions_dir

    account_block = format_account_block(data)
    assert account_block.splitlines() == [
        "用户名：",
        "",
        "邮箱：developer@example.com",
        "",
        "初始密码：InitialPass123!",
        "",
        "修改后的密码：",
        "",
        "电话：+15550101234",
        "",
        "电话短信接收平台：https://example.com/sms?token=test",
        "",
        "生日：",
        "",
        "team ID:",
        "",
        "APP_ID：",
        "",
        "Renewal date：",
        "",
        "代理ip:192.0.2.10",
        "",
        "代理端口:7612",
        "",
        "代理用户名：proxyuser",
        "",
        "代理用户密码：proxypass",
        "",
        "代码链接：https://example.com/ios/sample-app.git",
        "",
        "ABA Routing Number：000000000",
        "",
        "Account Number：000000000000",
    ]

    data = parse_submission_data(text.replace("使用的宿主机", "使用的虚拟机"))
    assert data is None

    optional_bank_cases = (
        (
            text.replace("ABA Routing Number：000000000", "ABA Routing Number：").replace(
                "Account Number：000000000000", "Account Number："
            ),
            {"aba_routing_number": "", "account_number": ""},
        ),
        (
            text.replace("ABA Routing Number：000000000", "ABA Routing Number："),
            {"aba_routing_number": "", "account_number": "000000000000"},
        ),
        (
            text.replace("Account Number：000000000000", "Account Number："),
            {"aba_routing_number": "000000000", "account_number": ""},
        ),
        (
            text.split("银行信息：", 1)[0],
            {"aba_routing_number": "", "account_number": ""},
        ),
    )
    optional_bank_data = [parse_submission_data(case) for case, _ in optional_bank_cases]
    assert all(item is not None for item in optional_bank_data)
    for item, (_, expected_bank_info) in zip(optional_bank_data, optional_bank_cases):
        assert item["bank_info"] == expected_bank_info
        optional_account_block = format_account_block(item)
        validate_account_block(optional_account_block)
        assert (
            f"ABA Routing Number：{expected_bank_info['aba_routing_number']}"
            in optional_account_block.splitlines()
        )
        assert (
            f"Account Number：{expected_bank_info['account_number']}"
            in optional_account_block.splitlines()
        )

    invalid_block = account_block.replace("用户名：", "旧用户名字段：", 1)
    try:
        validate_account_block(invalid_block)
    except RuntimeError:
        pass
    else:
        raise AssertionError("validator accepted a stale account label")

    with tempfile.TemporaryDirectory() as tmp:
        old_images_dir = feishu_bot.VM_IMAGES_DIR
        old_runs_file = feishu_bot.RUNS_FILE
        tmp_path = Path(tmp)
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        (images_dir / "abcd.utm").mkdir()
        runs_file = tmp_path / "runs.json"
        runs_file.write_text('{"runs":[{"vm_name":"efgh"}]}', encoding="utf-8")
        feishu_bot.VM_IMAGES_DIR = images_dir
        feishu_bot.RUNS_FILE = runs_file
        try:
            vm_name = feishu_bot.generate_vm_name()
        finally:
            feishu_bot.VM_IMAGES_DIR = old_images_dir
            feishu_bot.RUNS_FILE = old_runs_file
        assert len(vm_name) == 4
        assert vm_name.islower()
        assert vm_name.isalpha()
        assert vm_name not in {"abcd", "efgh", "macos"}


if __name__ == "__main__":
    main()
