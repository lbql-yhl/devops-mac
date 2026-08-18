#!/usr/bin/env python3
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.feishu_bot as feishu_bot


FIXTURE_ALLOWED_CHAT = "oc_allowed"


class ImmediateThread:
    def __init__(self, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


def assistant_config():
    return replace(
        feishu_bot.load_config(
            environ={"FEISHU_DAILY_REPORT_CHAT_ID": "test-report-chat"}
        ),
        allowed_chat_id=FIXTURE_ALLOWED_CHAT,
        assistant_provider="codex",
        assistant_enabled=True,
        assistant_require_mention=True,
        codex_command="/opt/test/bin/codex",
        codex_model="gpt-5.6-sol",
    )


def test_codex_command() -> None:
    captured = []

    def fake_run(cmd, **_kwargs):
        captured.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_text("MODEL_OK", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with (
            patch.object(feishu_bot, "RUNTIME_DIR", tmp_path),
            patch.object(feishu_bot, "CONVERSATIONS_FILE", tmp_path / "conversations.json"),
            patch.object(feishu_bot.subprocess, "run", fake_run),
        ):
            assert feishu_bot.ask_codex_assistant(
                assistant_config(), FIXTURE_ALLOWED_CHAT, "你是谁"
            ) == "MODEL_OK"

    cmd = captured[0]
    assert cmd[0] == "/opt/test/bin/codex"
    assert 'model_reasoning_effort="low"' in cmd
    assert "--skip-git-repo-check" in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-5.6-sol"


def test_websocket_and_poller_reply_once() -> None:
    config = assistant_config()
    sent = []
    event = SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id="om_test",
                chat_id=FIXTURE_ALLOWED_CHAT,
                content=json.dumps({"text": "@_user_1 你是谁"}, ensure_ascii=False),
            )
        )
    )
    history_item = {
        "message_id": "om_test",
        "sender": {"sender_type": "user"},
        "body": {"content": json.dumps({"text": "@_user_1 你是谁"}, ensure_ascii=False)},
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with (
            patch.object(feishu_bot, "PROCESSED_MESSAGES_FILE", tmp_path / "processed.json"),
            patch.object(feishu_bot, "PROCESSED_MESSAGES_DIR", tmp_path / "processed", create=True),
            patch.object(feishu_bot, "load_config", return_value=config),
            patch.object(feishu_bot, "ask_assistant", return_value="测试回复"),
            patch.object(feishu_bot.FeishuClient, "send_text", lambda _self, chat_id, text: sent.append((chat_id, text))),
            patch.object(feishu_bot, "fetch_chat_messages", return_value=[history_item]),
            patch.object(feishu_bot.threading, "Thread", ImmediateThread),
        ):
            feishu_bot.handle_ws_message_receive(event, config)
            assert feishu_bot.poll_once(config, [FIXTURE_ALLOWED_CHAT]) == 0

    assert sent == [(FIXTURE_ALLOWED_CHAT, "测试回复")]


def main() -> None:
    test_codex_command()
    test_websocket_and_poller_reply_once()


if __name__ == "__main__":
    main()
