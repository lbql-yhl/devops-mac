from __future__ import annotations

import importlib
import inspect
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, ModuleType, SimpleNamespace

import pytest

from services.host_config import ConfigurationError


FIXTURE_REPORT_CHAT = "report-v2"
FIXTURE_EVENT_CHAT = "chat-v1"


@pytest.fixture
def feishu_bot() -> ModuleType:
    return importlib.import_module("services.feishu_bot")


def test_import_does_not_read_or_require_host_dotenv(
) -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from pathlib import Path
import services.host_config as host_config

original_read_text = Path.read_text
def reject_dotenv_read(path, *args, **kwargs):
    if path.name == '.env':
        raise AssertionError('module import must not read the host .env')
    return original_read_text(path, *args, **kwargs)

Path.read_text = reject_dotenv_read
host_config._dotenv_setting = lambda *_args, **_kwargs: (_ for _ in ()).throw(
    AssertionError('module import must not resolve settings from the host .env')
)
import services.feishu_bot as module
assert 'config' not in module.FeishuHandler.__dict__
assert 'client' not in module.FeishuHandler.__dict__
print('IMPORT_OK')
""",
        ],
        cwd=root,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "IMPORT_OK"


@pytest.mark.parametrize("configured", ({}, {"FEISHU_DAILY_REPORT_CHAT_ID": "   "}))
def test_load_config_requires_daily_report_chat_without_revealing_values(
    feishu_bot: ModuleType,
    configured: dict[str, str],
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        feishu_bot.load_config(environ=configured)

    assert str(captured.value) == (
        "missing required host setting: FEISHU_DAILY_REPORT_CHAT_ID"
    )
    assert all(value not in str(captured.value) for value in configured.values())


def test_load_config_reads_without_mutating_process_environment(
    feishu_bot: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "FEISHU_DAILY_REPORT_CHAT_ID=configured-report-chat\n"
        "SUBMISSION_HOST_MACHINE=Configured Host\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("FEISHU_DAILY_REPORT_CHAT_ID", raising=False)
    monkeypatch.delenv("SUBMISSION_HOST_MACHINE", raising=False)
    before = dict(os.environ)

    config = feishu_bot.load_config(env_path=env_path)

    assert config.daily_report_chat_id == "configured-report-chat"
    assert config.submission_host_machine == "Configured Host"
    assert dict(os.environ) == before


def test_load_config_uses_one_immutable_snapshot_without_cross_version_splicing(
    feishu_bot: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = iter(
        (
            MappingProxyType(
                {
                    "FEISHU_DAILY_REPORT_CHAT_ID": "report-v1",
                    "SUBMISSION_HOST_MACHINE": "host-v1",
                }
            ),
            MappingProxyType(
                {
                    "FEISHU_DAILY_REPORT_CHAT_ID": "report-v2",
                    "SUBMISSION_HOST_MACHINE": "host-v2",
                }
            ),
        )
    )
    calls = 0

    def next_snapshot(**_kwargs: object):
        nonlocal calls
        calls += 1
        return next(snapshots)

    monkeypatch.setattr(feishu_bot, "host_settings_snapshot", next_snapshot)

    config = feishu_bot.load_config(environ={})

    assert calls == 1
    assert config.daily_report_chat_id == "report-v1"
    assert config.submission_host_machine == "host-v1"


@pytest.mark.parametrize(
    "key",
    (
        "FEISHU_BOT_PORT",
        "FEISHU_SEND_RETRIES",
        "FEISHU_SEND_TIMEOUT_SECONDS",
        "FEISHU_POLL_INTERVAL_SECONDS",
        "FEISHU_ASSISTANT_MAX_OUTPUT_TOKENS",
        "FEISHU_CODEX_TIMEOUT_SECONDS",
        "USER_CONFIRM_API_TIMEOUT_SECONDS",
    ),
)
def test_numeric_config_errors_name_only_the_key_and_range(
    feishu_bot: ModuleType,
    key: str,
) -> None:
    sentinel = f"sensitive-invalid-value-for-{key.lower()}"

    with pytest.raises(ConfigurationError) as captured:
        feishu_bot.load_config(
            environ={
                "FEISHU_DAILY_REPORT_CHAT_ID": "report-chat",
                key: sentinel,
            }
        )

    message = str(captured.value)
    assert key in message
    assert "between" in message
    assert sentinel not in message


def test_integer_parser_redacts_type_errors(feishu_bot: ModuleType) -> None:
    with pytest.raises(ConfigurationError) as captured:
        feishu_bot.integer_setting(
            {"NUMERIC_KEY": object()},
            "NUMERIC_KEY",
            default=1,
            minimum=1,
            maximum=10,
        )

    assert str(captured.value) == (
        "invalid host setting: NUMERIC_KEY must be an integer between 1 and 10"
    )


@pytest.mark.parametrize("invalid_value", ("0", "sensitive-not-an-integer"))
def test_send_retries_rejects_zero_or_invalid_without_network_or_value_leak(
    feishu_bot: ModuleType,
    invalid_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        feishu_bot.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid FEISHU_SEND_RETRIES must fail before network"
        ),
    )

    with pytest.raises(ConfigurationError) as captured:
        feishu_bot.load_config(
            environ={
                "FEISHU_DAILY_REPORT_CHAT_ID": "report-chat",
                "FEISHU_SEND_RETRIES": invalid_value,
            }
        )

    assert str(captured.value) == (
        "invalid host setting: FEISHU_SEND_RETRIES must be an integer between 1 and 100"
    )


def test_main_help_parses_before_loading_host_configuration(
    feishu_bot: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        feishu_bot,
        "load_config",
        lambda **_kwargs: pytest.fail("--help must not load host configuration"),
    )
    monkeypatch.setattr(sys, "argv", ["feishu_bot", "--help"])

    with pytest.raises(SystemExit) as captured:
        feishu_bot.main()

    assert captured.value.code == 0
    assert "Run the Feishu submission bot" in capsys.readouterr().out


def test_serve_ws_registers_message_and_card_handlers_with_same_config_snapshot(
    feishu_bot: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered: dict[str, object] = {}
    observed: dict[str, object] = {}
    config_v1 = feishu_bot.load_config(
        environ={
            "FEISHU_APP_ID": "app-v1",
            "FEISHU_APP_SECRET": "secret-v1",
            "FEISHU_DAILY_REPORT_CHAT_ID": "report-v1",
            "SUBMISSION_HOST_MACHINE": "host-v1",
        }
    )
    config_v2 = replace(
        config_v1,
        daily_report_chat_id=FIXTURE_REPORT_CHAT,
        submission_host_machine="host-v2",
    )

    class ImmediateThread:
        def __init__(self, target, **_kwargs: object) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    class FakeBuilder:
        def register_p2_im_message_receive_v1(self, callback):
            registered["message"] = callback
            return self

        def register_p2_card_action_trigger(self, callback):
            registered["card"] = callback
            return self

        def build(self) -> object:
            return object()

    class FakeEventDispatcherHandler:
        @staticmethod
        def builder(_encrypt_key: str, _verification_token: str) -> FakeBuilder:
            return FakeBuilder()

    class FakeWSClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

    fake_lark = ModuleType("lark_oapi")
    fake_lark.EventDispatcherHandler = FakeEventDispatcherHandler
    fake_lark.LogLevel = SimpleNamespace(INFO="INFO")
    fake_lark.ws = SimpleNamespace(Client=FakeWSClient)
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_lark)
    monkeypatch.setattr(feishu_bot, "patch_lark_ws_card_dispatch", lambda: None)
    monkeypatch.setattr(feishu_bot, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(feishu_bot, "claim_message", lambda _message_id: True)
    monkeypatch.setattr(feishu_bot.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(feishu_bot, "load_config", lambda: config_v2)

    def incoming(config, *_args: object, **_kwargs: object) -> str:
        observed["message_config"] = config
        return ""

    def card(_data: object, host: str, report_chat: str) -> dict[str, bool]:
        observed["card_route"] = (host, report_chat)
        return {"ok": True}

    monkeypatch.setattr(feishu_bot, "handle_incoming_text", incoming)
    monkeypatch.setattr(feishu_bot, "handle_ws_card_action", card)

    assert feishu_bot.serve_ws(SimpleNamespace(), config_v1) == 0

    message_event = SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id="message-v1",
                chat_id=FIXTURE_EVENT_CHAT,
                content='{"text":"hello"}',
            )
        )
    )
    registered["message"](message_event)  # type: ignore[operator]
    assert registered["card"](object()) == {"ok": True}  # type: ignore[operator]

    assert observed["message_config"] is config_v1
    assert observed["card_route"] == ("host-v1", "report-v1")


def test_serve_registers_http_handler_with_startup_config_snapshot(
    feishu_bot: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "FEISHU_DAILY_REPORT_CHAT_ID=report-v1\n"
        "SUBMISSION_HOST_MACHINE=host-v1\n",
        encoding="utf-8",
    )
    config_v1 = feishu_bot.load_config(environ={}, env_path=env_path)
    env_path.write_text(
        "FEISHU_DAILY_REPORT_CHAT_ID=report-v2\n"
        "SUBMISSION_HOST_MACHINE=host-v2\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class FakeServer:
        def __init__(self, address: tuple[str, int], handler: type) -> None:
            captured["address"] = address
            captured["handler"] = handler

        def serve_forever(self) -> None:
            pass

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(feishu_bot, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(feishu_bot, "ReusableThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(
        feishu_bot,
        "load_config",
        lambda **_kwargs: pytest.fail("serve must not reload configuration"),
    )

    args = SimpleNamespace(host="127.0.0.1", port=18787)
    assert feishu_bot.serve(args, config_v1) == 0

    handler = captured["handler"]
    assert handler.config is config_v1  # type: ignore[union-attr]
    assert handler.client.config is config_v1  # type: ignore[union-attr]
    assert handler.config.daily_report_chat_id == "report-v1"  # type: ignore[union-attr]
    assert handler.config.submission_host_machine == "host-v1"  # type: ignore[union-attr]


def test_daily_report_chat_is_loaded_and_used_by_routing_guards(
    feishu_bot: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        feishu_bot.load_config(
            environ={"FEISHU_DAILY_REPORT_CHAT_ID": "daily-report-chat"}
        ),
        allowed_chat_id="",
        assistant_enabled=False,
        submission_host_machine="Local Host",
    )

    assert config.daily_report_chat_id == "daily-report-chat"
    assert (
        feishu_bot.handle_incoming_text(
            config,
            "/提审 帮助",
            "daily-report-chat",
            source="test",
        )
        == ""
    )
    assert "提审机器人命令" in feishu_bot.handle_incoming_text(
        config,
        "/提审 帮助",
        "workflow-chat",
        source="test",
    )
    with pytest.raises(RuntimeError, match="original workflow chat"):
        feishu_bot.local_original_chat(
            {
                "chat_id": "daily-report-chat",
                "submission_data": {"host_machine": "Local Host"},
            },
            config,
        )


def test_card_entrypoints_require_daily_report_destination(
    feishu_bot: ModuleType,
) -> None:
    for function in (
        feishu_bot.reject_nonlocal_card_callback,
        feishu_bot.handle_review_card_action,
        feishu_bot.handle_confirmation_card_action,
        feishu_bot.handle_card_action,
        feishu_bot.handle_ws_card_action,
    ):
        parameter = inspect.signature(function).parameters["daily_report_chat_id"]
        assert parameter.default is inspect.Parameter.empty


def test_source_has_no_fixed_daily_report_destination(
    feishu_bot: ModuleType,
) -> None:
    assert not hasattr(feishu_bot, "DAILY_REPORT_CHAT_ID")
