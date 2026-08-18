from __future__ import annotations

import socket
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import feishu_bot, feishu_supervisor
from services.host_config import ConfigurationError


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def configure_bot_paths(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    runtime = root / "runtime"
    monkeypatch.setattr(feishu_bot, "ROOT", root)
    monkeypatch.setattr(feishu_bot, "PROJECT_ROOT", root)
    monkeypatch.setattr(feishu_bot, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(feishu_bot, "RUNS_FILE", runtime / "feishu-runs.json")
    monkeypatch.setattr(feishu_bot, "PROMPTS_DIR", runtime / "prompts")
    monkeypatch.setattr(
        feishu_bot, "CODEX_APP_SESSIONS_DIR", runtime / "codex-app-sessions"
    )
    monkeypatch.setattr(
        feishu_bot,
        "PROCESSED_MESSAGES_FILE",
        runtime / "feishu-processed-messages.json",
    )
    monkeypatch.setattr(
        feishu_bot,
        "PROCESSED_MESSAGES_DIR",
        runtime / "feishu-processed-messages",
    )
    monkeypatch.setattr(
        feishu_bot, "CONVERSATIONS_FILE", runtime / "feishu-conversations.json"
    )
    monkeypatch.setattr(
        feishu_bot, "CARD_CALLBACK_LOG_FILE", runtime / "feishu-card-callbacks.jsonl"
    )
    return runtime


def configure_supervisor_paths(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    runtime = root / "runtime"
    monkeypatch.setattr(feishu_supervisor, "ROOT", root)
    monkeypatch.setattr(feishu_supervisor, "RUNTIME_DIR", runtime)
    names = {
        "SUPERVISOR_LOG": "feishu_supervisor.log",
        "PUBLIC_URL_FILE": "public_url.txt",
        "CALLBACK_URL_FILE": "feishu_callback_url.txt",
        "BOT_PID_FILE": "feishu_bot.pid",
        "WS_PID_FILE": "feishu_ws.pid",
        "POLLER_PID_FILE": "feishu_poller.pid",
        "CLOUDFLARED_PID_FILE": "cloudflared.pid",
    }
    for constant, name in names.items():
        monkeypatch.setattr(feishu_supervisor, constant, runtime / name)
    return runtime


def test_bot_main_orders_umask_and_hardening_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    config = SimpleNamespace(host="127.0.0.1", port=8787, poll_interval_seconds=1)
    monkeypatch.setattr(sys, "argv", ["feishu_bot", "poll", "--once"])
    monkeypatch.setattr(feishu_bot.os, "umask", lambda value: calls.append(("umask", value)))
    monkeypatch.setattr(feishu_bot, "harden_runtime_metadata", lambda: calls.append("harden"))
    monkeypatch.setattr(
        feishu_bot, "load_config", lambda: calls.append("load_config") or config
    )
    monkeypatch.setattr(
        feishu_bot, "poll_messages", lambda *_args: calls.append("dispatch") or 17
    )

    assert feishu_bot.main() == 17
    assert calls == [("umask", 0o077), "harden", "load_config", "dispatch"]


def test_supervisor_main_orders_umask_and_hardening_before_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class FakeSupervisor:
        def __init__(self) -> None:
            calls.append("construct")

        def run(self) -> int:
            calls.append("run")
            return 23

    monkeypatch.setattr(
        feishu_supervisor.os,
        "umask",
        lambda value: calls.append(("umask", value)),
    )
    monkeypatch.setattr(
        feishu_supervisor, "harden_runtime_metadata", lambda: calls.append("harden")
    )
    monkeypatch.setattr(
        feishu_supervisor, "load_dotenv", lambda _path: calls.append("load_dotenv")
    )
    monkeypatch.setattr(feishu_supervisor, "Supervisor", FakeSupervisor)

    assert feishu_supervisor.main() == 23
    assert calls == [
        ("umask", 0o077),
        "harden",
        "load_dotenv",
        "construct",
        "run",
    ]


def test_bot_hardening_preserves_foreign_helper_and_unix_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir(mode=0o755)
    runtime = configure_bot_paths(monkeypatch, root)
    runtime.mkdir(mode=0o777)
    prompt = runtime / "prompts" / "run.md"
    prompt.parent.mkdir(mode=0o777)
    prompt.write_text("prompt", encoding="utf-8")
    prompt.chmod(0o666)
    helper = runtime / "askpass"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o700)
    socket_path = runtime / "askpass.sock"
    listener = socket.socket(socket.AF_UNIX)
    monkeypatch.chdir(runtime)
    listener.bind(socket_path.name)
    socket_path.chmod(0o700)
    helper_before = helper.lstat()
    socket_before = socket_path.lstat()
    try:
        feishu_bot.harden_runtime_metadata()
    finally:
        listener.close()

    assert mode(root) == 0o700
    assert mode(runtime) == 0o700
    assert mode(prompt.parent) == 0o700
    assert mode(prompt) == 0o600
    assert helper.read_text(encoding="utf-8") == "#!/bin/sh\n"
    assert (helper.lstat().st_dev, helper.lstat().st_ino, mode(helper)) == (
        helper_before.st_dev,
        helper_before.st_ino,
        0o700,
    )
    assert (socket_path.lstat().st_dev, socket_path.lstat().st_ino, mode(socket_path)) == (
        socket_before.st_dev,
        socket_before.st_ino,
        0o700,
    )


def test_bot_metadata_writes_are_user_only_and_do_not_follow_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir(mode=0o700)
    runtime = configure_bot_paths(monkeypatch, root)
    runtime.mkdir(mode=0o700)
    feishu_bot.harden_runtime_metadata()

    feishu_bot.write_json(feishu_bot.RUNS_FILE, {"runs": []})
    feishu_bot.append_card_callback_log({"event": {}})
    assert feishu_bot.claim_message("message-1") is True
    assert mode(feishu_bot.RUNS_FILE) == 0o600
    assert mode(feishu_bot.CARD_CALLBACK_LOG_FILE) == 0o600
    assert mode(feishu_bot.processed_message_path("message-1")) == 0o600

    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    outside.chmod(0o644)
    feishu_bot.RUNS_FILE.unlink()
    feishu_bot.RUNS_FILE.symlink_to(outside)
    with pytest.raises(ConfigurationError, match="unsafe runtime file"):
        feishu_bot.write_json(feishu_bot.RUNS_FILE, {"runs": ["replacement"]})
    assert outside.read_text(encoding="utf-8") == "outside"
    assert mode(outside) == 0o644

    callback_outside = tmp_path / "outside.log"
    callback_outside.write_text("outside-log\n", encoding="utf-8")
    feishu_bot.CARD_CALLBACK_LOG_FILE.unlink()
    feishu_bot.CARD_CALLBACK_LOG_FILE.symlink_to(callback_outside)
    with pytest.raises(ConfigurationError, match="unsafe runtime file"):
        feishu_bot.append_card_callback_log({"event": {}})
    assert callback_outside.read_text(encoding="utf-8") == "outside-log\n"


def test_supervisor_metadata_writes_are_user_only_and_do_not_follow_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir(mode=0o700)
    runtime = configure_supervisor_paths(monkeypatch, root)
    runtime.mkdir(mode=0o700)
    feishu_supervisor.harden_runtime_metadata()
    supervisor = feishu_supervisor.Supervisor()
    supervisor.set_public_url("https://first.trycloudflare.com")
    assert mode(feishu_supervisor.PUBLIC_URL_FILE) == 0o600
    assert mode(feishu_supervisor.CALLBACK_URL_FILE) == 0o600
    assert mode(feishu_supervisor.SUPERVISOR_LOG) == 0o600

    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    feishu_supervisor.PUBLIC_URL_FILE.unlink()
    feishu_supervisor.PUBLIC_URL_FILE.symlink_to(outside)
    with pytest.raises(ConfigurationError, match="unsafe runtime file"):
        supervisor.set_public_url("https://second.trycloudflare.com")
    assert outside.read_text(encoding="utf-8") == "outside\n"
