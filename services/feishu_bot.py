#!/usr/bin/env python3
"""Feishu bot entrypoint for the submission workflow.

This service intentionally uses only the Python standard library so it can run
inside the current workflow repository without dependency setup.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import string
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, parse, request

try:
    from host_config import (
        ConfigurationError,
        host_settings_snapshot,
        secure_append_text,
        secure_create_file,
        secure_runtime_file,
        secure_runtime_root,
        secure_runtime_tree,
        secure_write_text,
    )
except ModuleNotFoundError:
    from services.host_config import (
        ConfigurationError,
        host_settings_snapshot,
        secure_append_text,
        secure_create_file,
        secure_runtime_file,
        secure_runtime_root,
        secure_runtime_tree,
        secure_write_text,
    )


try:
    from feishu_gateway import (
        FeishuSendError,
        get_tenant_access_token,
        send_interactive_card,
        send_text_message,
        verify_post_message_delivery,
    )
except ModuleNotFoundError:
    from services.feishu_gateway import (
        FeishuSendError,
        get_tenant_access_token,
        send_interactive_card,
        send_text_message,
        verify_post_message_delivery,
    )


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT
VM_IMAGES_DIR = PROJECT_ROOT / "host-assets" / "vm-images"
RUNTIME_DIR = ROOT / "runtime"
RUNS_FILE = RUNTIME_DIR / "feishu-runs.json"
PROMPTS_DIR = RUNTIME_DIR / "prompts"
CODEX_APP_SESSIONS_DIR = RUNTIME_DIR / "codex-app-sessions"
PROCESSED_MESSAGES_FILE = RUNTIME_DIR / "feishu-processed-messages.json"
PROCESSED_MESSAGES_DIR = RUNTIME_DIR / "feishu-processed-messages"
CONVERSATIONS_FILE = RUNTIME_DIR / "feishu-conversations.json"
CARD_CALLBACK_LOG_FILE = RUNTIME_DIR / "feishu-card-callbacks.jsonl"


def harden_runtime_metadata() -> None:
    """Secure only Feishu-owned metadata inside the shared runtime root."""
    secure_runtime_root(ROOT)
    secure_runtime_root(RUNTIME_DIR, create=True)
    for directory in (
        PROMPTS_DIR,
        CODEX_APP_SESSIONS_DIR,
        PROCESSED_MESSAGES_DIR,
    ):
        secure_runtime_tree(directory)
    for metadata_file in (
        RUNS_FILE,
        PROCESSED_MESSAGES_FILE,
        CONVERSATIONS_FILE,
        CARD_CALLBACK_LOG_FILE,
    ):
        secure_runtime_file(metadata_file)


HEALTH_PATH = "/health"
CALLBACK_PATH = "/feishu/events"
LEGACY_CALLBACK_PATHS = {"/"}

COMMAND_PREFIXES = ("/提审", "提审")
HELP_TEXT = """提审机器人命令：
提审启动入口当前已禁用，登记消息和 /提审 开始 不会创建新运行
/提审 状态 - 查看最近一次运行
/提审 日志 - 查看最近提示文件路径
/提审 继续 - 标记为继续处理
/提审 停止 - 标记为已停止
/提审 帮助 - 查看命令"""
DECISION_TIMEOUT_SECONDS = 3600
SUBMISSION_START_ENABLED = False
SUBMISSION_SKILL_ORDER = (
    "utm-vm-clone",
    "utm-notion",
    "utm-clash-ip",
    "utm-login",
    "utm-edit",
    "utm-key",
    "utm-apps",
    "utm-business",
    "utm-env",
    "utm-script",
    "utm-image",
    "utm-p8",
    "utm-21",
    "utm-22",
    "utm-23",
    "utm-24",
)


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str
    allowed_chat_id: str
    daily_report_chat_id: str
    runner_command: str
    submission_host_machine: str
    host: str
    port: int
    send_retries: int
    send_timeout_seconds: int
    poll_chat_ids: tuple[str, ...]
    poll_interval_seconds: int
    openai_api_key: str
    openai_model: str
    assistant_provider: str
    assistant_enabled: bool
    assistant_require_mention: bool
    assistant_max_output_tokens: int
    codex_command: str
    codex_model: str
    codex_timeout_seconds: int
    verify_delivery: bool
    user_confirm_api_url: str
    user_confirm_timeout_seconds: int


def integer_setting(
    settings: Mapping[str, object],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = settings.get(key, default)
    if value == "":
        value = default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum - 1
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(
            f"invalid host setting: {key} must be an integer between "
            f"{minimum} and {maximum}"
        )
    return parsed


def load_config(
    *,
    environ: Mapping[str, str] | None = None,
    env_path: Path | None = None,
) -> Config:
    if env_path is None:
        settings_path = None if environ is not None else PROJECT_ROOT / ".env"
    else:
        settings_path = Path(env_path)
    settings = host_settings_snapshot(environ=environ, env_path=settings_path)

    def setting(name: str, default: str = "") -> str:
        value = str(settings.get(name, "")).strip()
        return value if value else default

    daily_report_chat_id = setting("FEISHU_DAILY_REPORT_CHAT_ID")
    if not daily_report_chat_id:
        raise ConfigurationError(
            "missing required host setting: FEISHU_DAILY_REPORT_CHAT_ID"
        )

    return Config(
        app_id=setting("FEISHU_APP_ID"),
        app_secret=setting("FEISHU_APP_SECRET"),
        verification_token=setting("FEISHU_VERIFICATION_TOKEN"),
        encrypt_key=setting("FEISHU_ENCRYPT_KEY"),
        allowed_chat_id=setting("FEISHU_ALLOWED_CHAT_ID"),
        daily_report_chat_id=daily_report_chat_id,
        runner_command=setting("SUBMISSION_RUNNER_COMMAND"),
        submission_host_machine=setting("SUBMISSION_HOST_MACHINE"),
        host=setting("FEISHU_BOT_HOST", "0.0.0.0"),
        port=integer_setting(
            settings,
            "FEISHU_BOT_PORT",
            default=8787,
            minimum=1,
            maximum=65535,
        ),
        send_retries=integer_setting(
            settings,
            "FEISHU_SEND_RETRIES",
            default=3,
            minimum=1,
            maximum=100,
        ),
        send_timeout_seconds=integer_setting(
            settings,
            "FEISHU_SEND_TIMEOUT_SECONDS",
            default=20,
            minimum=1,
            maximum=86400,
        ),
        poll_chat_ids=tuple(
            item.strip()
            for item in setting("FEISHU_POLL_CHAT_IDS").replace(";", ",").split(",")
            if item.strip()
        ),
        poll_interval_seconds=integer_setting(
            settings,
            "FEISHU_POLL_INTERVAL_SECONDS",
            default=15,
            minimum=1,
            maximum=86400,
        ),
        openai_api_key=setting("OPENAI_API_KEY"),
        openai_model=setting("OPENAI_MODEL", "gpt-5.6"),
        assistant_provider=setting("FEISHU_ASSISTANT_PROVIDER", "codex").lower(),
        assistant_enabled=setting("FEISHU_ASSISTANT_ENABLED", "1").lower()
        in {"1", "true", "yes"},
        assistant_require_mention=setting(
            "FEISHU_ASSISTANT_REQUIRE_MENTION", "1"
        ).lower()
        in {"1", "true", "yes"},
        assistant_max_output_tokens=integer_setting(
            settings,
            "FEISHU_ASSISTANT_MAX_OUTPUT_TOKENS",
            default=800,
            minimum=1,
            maximum=1000000,
        ),
        codex_command=setting("FEISHU_CODEX_COMMAND", "codex"),
        codex_model=setting("FEISHU_CODEX_MODEL", "gpt-5.6-sol"),
        codex_timeout_seconds=integer_setting(
            settings,
            "FEISHU_CODEX_TIMEOUT_SECONDS",
            default=180,
            minimum=1,
            maximum=86400,
        ),
        verify_delivery=setting("FEISHU_VERIFY_DELIVERY", "1").lower()
        in {"1", "true", "yes"},
        user_confirm_api_url=setting(
            "USER_CONFIRM_API_URL", "http://127.0.0.1:8000/confirm"
        ),
        user_confirm_timeout_seconds=integer_setting(
            settings,
            "USER_CONFIRM_API_TIMEOUT_SECONDS",
            default=20,
            minimum=1,
            maximum=86400,
        ),
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path: Path, value: Any) -> None:
    secure_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def redacted_submission_summary() -> str:
    return (
        "如果用户发送流程登记数据，先说明当前项目只负责接入飞书机器人、UTM 自动化入口和故障通知；"
        "自动执行范围以本项目 README、AGENTS.md 和 UTM skills 为准。"
    )


def conversation_key(chat_id: str) -> str:
    return chat_id or "unknown"


def read_conversation(chat_id: str) -> list[dict[str, str]]:
    data = read_json(CONVERSATIONS_FILE, {"conversations": {}})
    history = data.get("conversations", {}).get(conversation_key(chat_id), [])
    if not isinstance(history, list):
        return []
    return [
        {"role": str(item.get("role", "")), "content": str(item.get("content", ""))}
        for item in history
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and item.get("content")
    ][-12:]


def append_conversation(chat_id: str, role: str, content: str) -> None:
    data = read_json(CONVERSATIONS_FILE, {"conversations": {}})
    conversations = data.setdefault("conversations", {})
    key = conversation_key(chat_id)
    history = conversations.setdefault(key, [])
    history.append({"role": role, "content": content, "at": utc_now()})
    conversations[key] = history[-24:]
    write_json(CONVERSATIONS_FILE, data)


def openai_output_text(response_data: dict[str, Any]) -> str:
    if response_data.get("output_text"):
        return str(response_data["output_text"]).strip()
    chunks: list[str] = []
    for item in response_data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(str(content["text"]))
    return "\n".join(chunks).strip()


def ask_openai_assistant(config: Config, chat_id: str, user_text: str) -> str:
    if not config.openai_api_key:
        return "GPT 助手还没配置 OPENAI_API_KEY，暂时不能回答。"
    history = read_conversation(chat_id)
    input_messages = history + [{"role": "user", "content": strip_feishu_markdown(user_text)}]
    body = {
        "model": config.openai_model,
        "instructions": (
            "你是当前 mac 提审自动化项目的飞书机器人助手，使用中文简洁回复。"
            "你只解释本项目已有能力：UTM macOS VM 自动化入口、飞书长连接、自动故障重试/停止、"
            "无法自动判断时的故障卡片、运行记录和项目配置。"
            "不要套用旧导出包里的流程清单、虚拟机平台或固定宿主机路径。"
            "不要索要或复述完整密码、验证码、代理密码、证书私钥等敏感信息。"
            f"{redacted_submission_summary()}"
        ),
        "input": input_messages,
        "max_output_tokens": config.assistant_max_output_tokens,
    }
    req = request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.openai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=config.send_timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return f"GPT 助手调用失败：HTTP {exc.code} {detail}"
    except Exception as exc:  # noqa: BLE001 - returned to Feishu as an operational hint.
        return f"GPT 助手调用失败：{type(exc).__name__}: {exc}"
    text = openai_output_text(data) or "我暂时没有生成有效回复。"
    append_conversation(chat_id, "user", strip_feishu_markdown(user_text))
    append_conversation(chat_id, "assistant", text)
    return text


def ask_codex_assistant(config: Config, chat_id: str, user_text: str) -> str:
    prompt = (
        "你是当前 mac 提审自动化项目的飞书机器人助手。请用中文简洁回答用户问题。"
        "你只解释本项目已有能力：UTM macOS VM 自动化入口、飞书长连接、自动故障重试/停止、"
        "无法自动判断时的故障卡片、运行记录和项目配置。"
        "不要套用旧导出包里的流程清单、虚拟机平台或固定宿主机路径。"
        "不要索要或复述完整密码、验证码、代理密码、证书私钥。\n\n"
        f"用户消息：\n{strip_feishu_markdown(user_text)}"
    )
    output_path = RUNTIME_DIR / f"codex-assistant-{hashlib.sha256((chat_id + user_text + utc_now()).encode('utf-8')).hexdigest()[:12]}.txt"
    cmd = [
        config.codex_command,
        "-c",
        'model_reasoning_effort="low"',
        "exec",
        "--skip-git-repo-check",
        "--cd",
        str(ROOT),
        "--sandbox",
        "read-only",
        "-m",
        config.codex_model,
        "-o",
        str(output_path),
        prompt,
    ]
    try:
        subprocess.run(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.codex_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Codex 助手调用超时，请稍后重试。"
    except Exception as exc:  # noqa: BLE001 - returned to Feishu as an operational hint.
        return f"Codex 助手调用失败：{type(exc).__name__}: {exc}"
    if not output_path.exists():
        return "Codex 助手没有生成回复文件，请检查 Codex CLI 登录状态。"
    text = output_path.read_text(encoding="utf-8", errors="replace").strip()
    append_conversation(chat_id, "user", strip_feishu_markdown(user_text))
    append_conversation(chat_id, "assistant", text)
    return text or "Codex 助手没有生成有效回复。"


def ask_assistant(config: Config, chat_id: str, user_text: str) -> str:
    if config.assistant_provider == "openai":
        return ask_openai_assistant(config, chat_id, user_text)
    if config.assistant_provider == "codex":
        return ask_codex_assistant(config, chat_id, user_text)
    return f"未知助手提供方：{config.assistant_provider}"


def parse_text_content(message: dict[str, Any]) -> str:
    content = message.get("content") or ""
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return str(parsed.get("text") or "").strip()
        except json.JSONDecodeError:
            return content.strip()
    if isinstance(content, dict):
        return str(content.get("text") or "").strip()
    return ""


def normalize_command(text: str) -> tuple[str, list[str]]:
    stripped = text.strip()
    for prefix in COMMAND_PREFIXES:
        if stripped == prefix:
            return "帮助", []
        if stripped.startswith(prefix + " "):
            rest = stripped[len(prefix) :].strip()
            if not rest:
                return "帮助", []
            parts = rest.split()
            return parts[0], parts[1:]
    return "", []


def strip_feishu_markdown(text: str) -> str:
    text = re.sub(r"<at\b[^>]*>.*?</at>\s*", "", text)
    text = re.sub(r"@_user_\d+\s*", "", text)
    text = re.sub(r"(^|\s)@[^\s]+", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.strip()


def first_regex(pattern: str, text: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else ""


def parse_submission_data(text: str) -> dict[str, Any] | None:
    cleaned = strip_feishu_markdown(text)
    host_machine = first_regex(r"使用的宿主机\s*[:：]\s*(.+)", cleaned)
    app_name = first_regex(r"应用名\s*[:：]\s*(.+)", cleaned)
    proxy_line = first_regex(r"代理信息\s*[:：]\s*(.+)", cleaned)
    code_link = first_regex(r"代码链接\s*[:：]\s*(https?://\S+)", cleaned)
    aba_routing_number = first_regex(r"ABA Routing Number\s*[:：]\s*(\d+)", cleaned)
    account_number = first_regex(r"Account Number\s*[:：]\s*(\d+)", cleaned)
    if not host_machine or not app_name or not proxy_line or not code_link:
        return None

    proxy_match = re.search(
        r"(?P<host>\d{1,3}(?:\.\d{1,3}){3})[:：](?P<port>\d+)(?:[:：](?P<username>[^:\s：]+)[:：](?P<password>[^:\s：]+))?",
        proxy_line,
    )
    if not proxy_match:
        return None

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    account_index = next((i for i, line in enumerate(lines) if line.startswith("开发者账号信息")), -1)
    bank_index = next((i for i, line in enumerate(lines) if line.startswith("银行信息")), -1)
    if account_index < 0:
        return None
    account_lines = lines[account_index + 1 : bank_index if bank_index > account_index else None]
    account_text = "\n".join(account_lines)
    email = first_regex(r"邮箱\s*[:：]\s*([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", account_text) or first_regex(
        r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", account_text
    )
    sms_url = first_regex(r"短信接收链接\s*[:：]\s*(https?://\S+)", account_text)
    phone = re.sub(r"\D", "", first_regex(r"电话\s*[:：]\s*(\+?\d[\d\s-]{5,}\d)", account_text))
    password = first_regex(r"初始密码\s*[:：]\s*(.+)", account_text)
    country = first_regex(r"国家\s*[:：]\s*(.+)", account_text)
    for line in account_lines:
        if not sms_url:
            url_match = re.search(r"(https?://\S+)", line)
            if url_match:
                sms_url = url_match.group(1)
        if not phone:
            phone_match = re.search(r"(\+?\d[\d\s-]{5,}\d)", line)
            if phone_match:
                phone = re.sub(r"\D", "", phone_match.group(1))
        if re.match(r"^(电话|短信接收链接|邮箱)\s*[:：]", line):
            continue
        if "@" in line or line.startswith("http") or "应用名" in line or "代理信息" in line:
            continue
        line_value = re.sub(r"^(国家|初始密码)\s*[:：]\s*", "", line).strip()
        if not country:
            country = line_value
            continue
        if not password:
            password = line_value
    required = (
        proxy_match.group("username"),
        proxy_match.group("password"),
        country,
        email,
        password,
        phone,
        sms_url,
    )
    if not all(required):
        return None

    return {
        "host_machine": host_machine,
        "app_name": app_name,
        "proxy": {
            "host": proxy_match.group("host"),
            "port": proxy_match.group("port"),
            "username": proxy_match.group("username") or "",
            "password": proxy_match.group("password") or "",
        },
        "developer_account": {
            "country": country,
            "email": email,
            "password": password,
            "phone": phone,
            "sms_url": sms_url,
        },
        "bank_info": {
            "aba_routing_number": aba_routing_number,
            "account_number": account_number,
        },
        "code_link": code_link,
        "raw_text": text,
    }


def existing_vm_names() -> set[str]:
    names = {path.stem for path in VM_IMAGES_DIR.glob("*.utm")} if VM_IMAGES_DIR.exists() else set()
    for run in read_json(RUNS_FILE, {"runs": []}).get("runs", []):
        if run.get("vm_name"):
            names.add(str(run["vm_name"]))
    return names


def generate_vm_name() -> str:
    used = existing_vm_names()
    alphabet = string.ascii_lowercase
    for _ in range(100):
        name = "".join(secrets.choice(alphabet) for _ in range(4))
        if name not in used and name != "macos":
            return name
    raise RuntimeError("Unable to generate a unique VM name")


def build_submission_run(config: Config, text: str, chat_id: str, source: str = "feishu") -> tuple[dict[str, Any], str] | None:
    if not SUBMISSION_START_ENABLED:
        return None
    data = parse_submission_data(text)
    if not data:
        return None
    duplicate = find_duplicate_submission(chat_id, data)
    if duplicate:
        if not duplicate.get("vm_name"):
            vm_name = generate_vm_name()

            def add_vm_name(item: dict[str, Any]) -> None:
                item["vm_name"] = vm_name
                item.setdefault("events", []).append(
                    {"at": utc_now(), "status": "vm_name_generated", "note": f"Generated VM name {vm_name} for existing run"}
                )

            duplicate = mutate_run(duplicate["id"], add_vm_name) or duplicate
        if duplicate.get("status") == "queued" and not duplicate.get("codex_app_session_path"):
            def start_existing(item: dict[str, Any]) -> None:
                prepare_codex_app_session(config, item)
                item.setdefault("events", []).append(
                    {
                        "at": utc_now(),
                        "status": "codex_app_session_requested",
                        "note": f"Duplicate data from {source} created Codex App handoff for queued run",
                    }
                )

            updated = mutate_run(duplicate["id"], start_existing)
            return updated or duplicate, "started_existing"
        updated = mutate_run(
            duplicate["id"],
            lambda item: item.setdefault("events", []).append(
                {"at": utc_now(), "status": "duplicate_submission_data", "note": f"Duplicate data received from {source}"}
            ),
        )
        return updated or duplicate, "duplicate"
    run = {
        "id": f"submission-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        "app_name": data["app_name"],
        "vm_name": generate_vm_name(),
        "chat_id": chat_id,
        "raw_text": text,
        "submission_data": data,
        "status": "created",
        "source": source,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "events": [{"at": utc_now(), "status": "created", "note": f"Submission data received from {source}"}],
    }
    message = prepare_codex_app_session(config, run)
    run["updated_at"] = utc_now()
    save_run(run)
    return run, message


def find_duplicate_submission(chat_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    runs = read_json(RUNS_FILE, {"runs": []}).get("runs", [])
    proxy = data.get("proxy") or {}
    account = data.get("developer_account") or {}
    for run in reversed(runs):
        if run.get("chat_id") != chat_id:
            continue
        existing = run.get("submission_data") or {}
        existing_proxy = existing.get("proxy") or {}
        existing_account = existing.get("developer_account") or {}
        if (
            existing.get("app_name") == data.get("app_name")
            and existing_proxy.get("host") == proxy.get("host")
            and str(existing_proxy.get("port")) == str(proxy.get("port"))
            and existing_account.get("email") == account.get("email")
            and run.get("status") in {"created", "queued", "running", "resume_requested"}
        ):
            return run
    return None


def build_codex_prompt(run: dict[str, Any]) -> str:
    app_name = run["app_name"]
    vm_name = run.get("vm_name", "")
    raw_text = run["raw_text"]
    submission_data = json.dumps(run.get("submission_data", {}), ensure_ascii=False, indent=2)
    skill_order = " -> ".join(SUBMISSION_SKILL_ORDER)
    return f"""请按固定提审主线执行一次提审自动化流程。

运行 ID：{run["id"]}
应用名：{app_name}
虚拟机名称：{vm_name}
飞书来源群：{run["chat_id"]}
触发消息：{raw_text}
登记数据 JSON：
{submission_data}

执行要求：
1. 先阅读本项目 README.md、AGENTS.md 和 docs/utm-feishu-bot.md。
2. 当前运行环境是本机 macOS 上的 UTM macOS VM 项目；不要套用 Windows/VMware 路径或命令。
3. 虚拟机名称已在当前运行中生成并去重：{vm_name}。后续 Notion 登记和 `utm-vm-clone` 必须使用同一运行目标，不得按“最新”重选。
4. `utm-notion` 只执行项目绝对路径脚本；字段解析、应用类型归一化、VM 绑定、飞书单列更新和 Notion 双区块回读全部以脚本 JSON 为准。银行号码可留空；只有到 `utm-business` 银行资料步骤时两项才必填，并且只能从当前匹配 Notion 页实时读取。
5. 固定技能顺序（16 个）：{skill_order}。四段均可单独运行，每个技能也可人工单独指定执行；指定范围内保持固定顺序，不插入其他技能或按“最新”重选 run/VM。本提示是完整提审请求时执行全部四段；人工明确指定一段或一个技能时只执行指定范围。
6. 有明确恢复方法且风险可控时，自动从最小恢复点修复/重试并回填运行状态，不发卡片。
7. 无法自信判断、自动修复失败或涉及需要人工处理的风险时，先按当前技能和共享合同完成三轮“自动诊断 → 实际修复 → 独立复验”；不可逆、不能安全重复写入或外部不可修复状态做三轮独立只读复核。三轮仍无法解决后才调用 notify-fault，并传入 recovery_attempts>=3、recovery_actions、recovery_result、当前 recovery_skill 与 completed_steps，再调用 wait-decision --timeout-seconds 3600 等待卡片动作；stop 立即停止，manual_continue 立即复核现场后继续，retry_skill 立即重跑当前技能并只跳过仍通过完成检查的步骤。
8. SSH 不通时锁定同一精确 VM，重新按 MAC/ARP 获取 IP，检查 Remote Login/端口并使用宿主 `SUBMISSION_GUEST_PASSWORD` 配置恢复，每轮都独立复验身份；三轮仍失败则按当前技能的故障边界发卡。停止结果只更新原故障卡，不再发送独立停止通知。
9. 不可恢复、无授权、不可逆风险或超过重试次数时，也必须先满足三轮恢复/只读复核证据门禁；少于三轮或证据缺失时运行时拒绝 notify-fault。故障卡发送后当前执行器原地等待，等待期间不发送提醒卡；首次确认送达满 3600 秒无回复时，只向原 chat_id 发送一次无按钮超时卡片、记录 decision_timeout_stop 并停止整个流程，之后不再重发、不再轮询、不再恢复，迟到回调无效。成功通知卡无按钮、无需回复，不进入超时等待。
10. 运行状态或阻塞点需要回填到 runtime/feishu-runs.json 对应运行记录。
11. 不把 Apple ID、验证码、密码、代理完整凭证写入仓库。
"""


def save_run(run: dict[str, Any]) -> None:
    data = read_json(RUNS_FILE, {"runs": []})
    runs = data.setdefault("runs", [])
    runs.append(run)
    data["runs"] = runs[-50:]
    write_json(RUNS_FILE, data)


def find_run(run_id: str) -> dict[str, Any] | None:
    data = read_json(RUNS_FILE, {"runs": []})
    for run in data.get("runs", []):
        if run.get("id") == run_id:
            return run
    return None


def mutate_run(run_id: str, mutator: Any) -> dict[str, Any] | None:
    data = read_json(RUNS_FILE, {"runs": []})
    for run in data.get("runs", []):
        if run.get("id") == run_id:
            mutator(run)
            run["updated_at"] = utc_now()
            write_json(RUNS_FILE, data)
            return run
    return None


def update_latest_run(chat_id: str, status: str, note: str) -> dict[str, Any] | None:
    data = read_json(RUNS_FILE, {"runs": []})
    for run in reversed(data.get("runs", [])):
        if run.get("chat_id") == chat_id:
            run["status"] = status
            run["updated_at"] = utc_now()
            run.setdefault("events", []).append({"at": utc_now(), "status": status, "note": note})
            write_json(RUNS_FILE, data)
            return run
    return None


def latest_run(chat_id: str) -> dict[str, Any] | None:
    data = read_json(RUNS_FILE, {"runs": []})
    for run in reversed(data.get("runs", [])):
        if run.get("chat_id") == chat_id:
            return run
    return None


class FeishuClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._token = ""
        self._token_expires_at = 0.0

    def app_access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        if not self.config.app_id or not self.config.app_secret:
            raise RuntimeError("FEISHU_APP_ID or FEISHU_APP_SECRET is not configured")

        payload = json.dumps(
            {"app_id": self.config.app_id, "app_secret": self.config.app_secret}
        ).encode("utf-8")
        req = request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu token error: {data}")
        self._token = data["app_access_token"]
        self._token_expires_at = time.time() + int(data.get("expire", 7200))
        return self._token

    def send_text(self, chat_id: str, text: str) -> None:
        response = send_text_message(
            self.config.app_id,
            self.config.app_secret,
            chat_id,
            text,
            retries=self.config.send_retries,
            timeout=self.config.send_timeout_seconds,
        )
        if self.config.verify_delivery:
            check = verify_post_message_delivery([chat_id], response)
            if check.get("status") != "checked":
                raise RuntimeError(f"Feishu delivery check failed: {check}")

    def send_card(
        self, chat_id: str, card: dict[str, Any], *, message_uuid: str = ""
    ) -> dict[str, Any]:
        response = send_interactive_card(
            self.config.app_id,
            self.config.app_secret,
            chat_id,
            card,
            message_uuid=message_uuid,
            retries=self.config.send_retries,
            timeout=self.config.send_timeout_seconds,
        )
        if self.config.verify_delivery:
            check = verify_post_message_delivery([chat_id], response)
            if check.get("status") != "checked":
                raise RuntimeError(f"Feishu delivery check failed: {check}")
        return response

    def upload_image(self, image_path: Path) -> str:
        path = Path(image_path)
        payload = path.read_bytes()
        if not path.is_file() or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"Invalid PNG screenshot: {path}")
        boundary = f"----feishu-{uuid.uuid4().hex}"
        filename = path.name.replace('"', "")
        body = b"".join(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"image_type\"\r\n\r\nmessage\r\n".encode(),
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n".encode(),
                payload,
                f"\r\n--{boundary}--\r\n".encode(),
            )
        )
        req = request.Request(
            "https://open.feishu.cn/open-apis/im/v1/images",
            data=body,
            headers={
                "Authorization": f"Bearer {self.app_access_token()}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=self.config.send_timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        image_key = str((data.get("data") or {}).get("image_key") or "")
        if data.get("code") != 0 or not image_key:
            raise RuntimeError(f"Feishu image upload failed: {data}")
        return image_key

def verify_signature(config: Config, headers: dict[str, str], raw_body: bytes) -> bool:
    if not config.encrypt_key:
        return True
    timestamp = headers.get("x-lark-request-timestamp", "")
    nonce = headers.get("x-lark-request-nonce", "")
    signature = headers.get("x-lark-signature", "")
    body = raw_body.decode("utf-8")
    expected = hashlib.sha256((timestamp + nonce + config.encrypt_key + body).encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, signature)


def request_path(raw_path: str) -> str:
    return parse.urlsplit(raw_path).path or "/"


def is_callback_path(raw_path: str) -> bool:
    path = request_path(raw_path)
    return path == CALLBACK_PATH or path in LEGACY_CALLBACK_PATHS


def prepare_codex_app_session(config: Config, run: dict[str, Any]) -> str:
    prompt_path = PROMPTS_DIR / f"{run['id']}.md"
    secure_write_text(prompt_path, build_codex_prompt(run))
    run["prompt_path"] = str(prompt_path)
    session_path = create_codex_app_session_request(run, prompt_path)
    return (
        f"已创建运行 {run['id']}，并生成 Codex App 会话交接。\n"
        f"提示文件：{prompt_path}\n"
        f"会话交接：{session_path}\n"
        "未启动后台 runner。"
    )


def launch_runner(config: Config, run: dict[str, Any]) -> str:
    return prepare_codex_app_session(config, run)


def create_codex_app_session_request(run: dict[str, Any], prompt_path: Path) -> Path:
    CODEX_APP_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session_path = CODEX_APP_SESSIONS_DIR / f"{run['id']}.json"
    payload = {
        "run_id": run["id"],
        "app_name": run.get("app_name", ""),
        "vm_name": run.get("vm_name", ""),
        "chat_id": run.get("chat_id", ""),
        "source": run.get("source", "feishu"),
        "status": "requested",
        "created_at": utc_now(),
        "prompt_path": str(prompt_path),
        "project_root": str(ROOT),
        "instruction": (
            "Open this prompt in a Codex App session and execute the submission "
            "workflow in the foreground; do not run the background submission runner."
        ),
    }
    write_json(session_path, payload)
    run["codex_app_session_path"] = str(session_path)
    run["status"] = "codex_app_session_requested"
    run.setdefault("events", []).append(
        {
            "at": utc_now(),
            "status": "codex_app_session_requested",
            "note": "Codex App handoff created; background runner was not started",
        }
    )
    return session_path


def handle_command(config: Config, text: str, chat_id: str) -> str:
    if config.allowed_chat_id and chat_id != config.allowed_chat_id:
        return ""

    command, _args = normalize_command(text)
    if not command:
        return ""

    if command in {"帮助", "help"}:
        return HELP_TEXT

    if command == "开始":
        if not SUBMISSION_START_ENABLED:
            return ""
        maybe_data_run = build_submission_run(config, text, chat_id, source="feishu-command")
        if maybe_data_run:
            run, _message = maybe_data_run
            return f"收到，准备开始{run['app_name']}提审。"
        return ""

    if command == "状态":
        run = latest_run(chat_id)
        if not run:
            return f"还没有提审运行记录。\nchat_id：{chat_id}"
        pending = run.get("pending_decision") or {}
        pending_text = ""
        if pending.get("status") == "waiting":
            pending_text = (
                f"\n待处理故障：{pending.get('stage', '未知')}"
                f"\n请在飞书故障卡片中选择停止或人工处理后继续"
            )
        return (
            f"最近运行：{run['id']}\n"
            f"应用：{run.get('app_name')}\n"
            f"状态：{run.get('status')}\n"
            f"更新时间：{run.get('updated_at')}\n"
            f"提示文件：{run.get('prompt_path', '无')}\n"
            f"Codex App 会话交接：{run.get('codex_app_session_path', '无')}"
            f"{pending_text}"
        )

    if command == "日志":
        run = latest_run(chat_id)
        if not run:
            return "还没有日志。"
        return (
            f"运行：{run['id']}\n"
            f"提示文件：{run.get('prompt_path', '无')}\n"
            f"Codex App 会话交接：{run.get('codex_app_session_path', '无')}\n"
            f"命令日志：{run.get('runner_log_path', '未启动后台 runner')}"
        )

    if command == "继续":
        run = update_latest_run(chat_id, "resume_requested", "Resume requested from Feishu")
        if not run:
            return "没有可继续的提审运行。"
        return f"已标记继续处理：{run['id']}"

    if command == "停止":
        run = update_latest_run(chat_id, "stopped", "Stop requested from Feishu")
        if not run:
            return "没有可停止的提审运行。"
        return f"已标记停止：{run['id']}"

    return f"未知命令：{command}\n\n{HELP_TEXT}"


def handle_incoming_text(config: Config, text: str, chat_id: str, source: str = "feishu") -> str:
    if is_daily_report_chat(chat_id, config.daily_report_chat_id) or (
        config.allowed_chat_id and chat_id != config.allowed_chat_id
    ):
        return ""
    submission_data = parse_submission_data(text)
    if submission_data and not SUBMISSION_START_ENABLED:
        return ""
    if submission_data and submission_data["host_machine"] != config.submission_host_machine:
        return ""
    reply = handle_command(config, text, chat_id)
    if reply:
        return reply
    maybe_data_run = build_submission_run(config, text, chat_id, source=source)
    if maybe_data_run:
        run, _message = maybe_data_run
        return f"收到，准备开始{run['app_name']}提审。"
    if should_answer_with_assistant(config, text):
        return ask_assistant(config, chat_id, text)
    return ""


def should_answer_with_assistant(config: Config, text: str) -> bool:
    if not config.assistant_enabled:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if (
        config.assistant_require_mention
        and "@_user_" not in stripped
        and "<at " not in stripped
        and not stripped.startswith(("机器人", "GPT", "gpt"))
    ):
        return False
    return True


def append_card_callback_log(payload: dict[str, Any], result: dict[str, Any] | None = None) -> None:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    item = {
        "at": utc_now(),
        "event_type": (payload.get("header") or {}).get("event_type") if isinstance(payload.get("header"), dict) else "",
        "action": action.get("value") if isinstance(action.get("value"), (dict, str)) else "",
        "operator": event.get("operator") if isinstance(event.get("operator"), dict) else {},
        "result": result or {},
    }
    secure_append_text(
        CARD_CALLBACK_LOG_FILE,
        json.dumps(item, ensure_ascii=False) + "\n",
    )


def normalize_card_value(raw_value: Any) -> dict[str, Any]:
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str):
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def card_callback_value(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    value = normalize_card_value(action.get("value"))
    operator = event.get("operator") if isinstance(event.get("operator"), dict) else {}
    return value, str(operator.get("open_id") or operator.get("user_id") or "")


def normalize_ws_card_payload(payload: bytes | str | dict[str, Any]) -> dict[str, Any]:
    """Normalize a long-connection card callback before SDK model parsing.

    Feishu has delivered ``event.action.value`` both as an object and as a
    JSON-encoded string.  The SDK's P2 callback model only accepts a dict, so
    normalize the transport boundary first; HTTP callbacks already use the
    more permissive :func:`card_callback_value` path.
    """
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, dict):
        return {}
    normalized = json.loads(json.dumps(payload, ensure_ascii=False))
    event = normalized.get("event")
    if not isinstance(event, dict):
        return normalized
    action = event.get("action")
    if not isinstance(action, dict):
        return normalized
    value = action.get("value")
    if isinstance(value, str):
        action["value"] = normalize_card_value(value)
    return normalized


def review_submission_snapshot(
    pending: dict[str, Any], decision: str, answered_at: str, operator_id: str
) -> dict[str, Any]:
    try:
        iap_count = int(pending.get("iap_count") or 0)
    except (TypeError, ValueError):
        iap_count = 0
    return {
        "kind": "review_submit",
        "status": "approved" if decision == "submit_review" else "rejected",
        "decision": decision,
        "decision_id": str(pending.get("decision_id") or ""),
        "app_version": str(pending.get("app_version") or ""),
        "build_number": str(pending.get("build_number") or ""),
        "iap_count": iap_count,
        "evidence": str(pending.get("evidence") or ""),
        "answered_at": answered_at,
        "operator_id": operator_id,
    }


def record_automatic_review_approval(
    run_id: str,
    app_version: str,
    build_number: str,
    iap_count: int,
    evidence: str,
    screenshot_hashes: list[str],
) -> dict[str, Any] | None:
    """Persist a self-check approval without creating an interactive decision."""
    clean_hashes = [str(value).strip().lower() for value in screenshot_hashes]
    evidence_tokens = {token.strip() for token in evidence.split(";") if token.strip()}
    if (
        not app_version
        or not build_number
        or iap_count != 14
        or "REVIEW_SCREENSHOTS=verified_5" not in evidence_tokens
        or "ITEMS_READY=15" not in evidence_tokens
        or len(clean_hashes) != 5
        or any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in clean_hashes)
        or len(set(clean_hashes)) != 5
    ):
        raise ValueError("Automatic review approval evidence is incomplete")

    def apply_approval(item: dict[str, Any]) -> None:
        pending = item.get("pending_decision") or {}
        if pending.get("status") == "waiting":
            raise ValueError("Cannot approve review while an interactive decision is waiting")
        existing = item.get("review_submission_approval")
        if isinstance(existing, dict) and existing.get("status") == "rejected":
            raise ValueError("A rejected review decision cannot be overwritten automatically")
        requested_identity = {
            "kind": "review_submit",
            "status": "approved",
            "decision": "submit_review",
            "app_version": app_version,
            "build_number": build_number,
            "iap_count": iap_count,
            "evidence": evidence,
            "operator_id": "automation:self-check",
            "source": "automatic_self_check",
            "screenshot_hashes": clean_hashes,
        }
        if isinstance(existing, dict) and existing.get("status") == "approved":
            if existing.get("source") != "automatic_self_check":
                raise ValueError("An explicit review approval cannot be overwritten automatically")
            if all(existing.get(key) == value for key, value in requested_identity.items()):
                return
            if isinstance(item.get("review_submit_attempt"), dict):
                raise ValueError("Automatic review approval cannot change after a submit attempt exists")
        answered_at = utc_now()
        snapshot = {
            **requested_identity,
            "decision_id": f"auto-{uuid.uuid4().hex}",
            "answered_at": answered_at,
        }
        if not is_approved_review_submission(snapshot):
            raise ValueError("Automatic review approval snapshot is invalid")
        item["review_submission_approval"] = snapshot
        item["status"] = "review_submission_approved_automatic"
        item.setdefault("events", []).append(
            {
                "at": answered_at,
                "status": "review_submission_approved_automatic",
                "note": "五图和 15 项范围自检通过，系统自动授权单次提审",
            }
        )

    return mutate_run(run_id, apply_approval)


def record_review_submit_attempt(
    run_id: str,
    attempt_id: str,
    decision_id: str,
    app_version: str,
    build_number: str,
    items_ready: int,
    status: str,
) -> dict[str, Any] | None:
    """Persist the one review-submit click attempt and enforce monotonic transitions."""
    allowed_statuses = ("prepared", "clicking", "result_unknown", "verified")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", attempt_id or ""):
        raise ValueError("Review submit attempt ID is invalid")
    if status not in allowed_statuses:
        raise ValueError("Review submit attempt status is invalid")
    if items_ready != 15:
        raise ValueError("Review submit attempt requires exactly 15 ready items")

    def apply_attempt(item: dict[str, Any]) -> None:
        approval = item.get("review_submission_approval")
        if not is_approved_review_submission(approval):
            raise ValueError("Review submit attempt requires a complete approval")
        if (
            approval.get("decision_id") != decision_id
            or approval.get("app_version") != app_version
            or approval.get("build_number") != build_number
            or int(approval.get("iap_count") or 0) != 14
        ):
            raise ValueError("Review submit attempt does not match the approval")

        identity = {
            "kind": "review_submit",
            "attempt_id": attempt_id,
            "decision_id": decision_id,
            "app_version": app_version,
            "build_number": build_number,
            "items_ready": items_ready,
        }
        existing = item.get("review_submit_attempt")
        now = utc_now()
        if not isinstance(existing, dict):
            if status != "prepared":
                raise ValueError("Review submit attempt must start as prepared")
            item["review_submit_attempt"] = {
                **identity,
                "status": status,
                "created_at": now,
                "updated_at": now,
            }
            return

        for key, value in identity.items():
            if existing.get(key) != value:
                raise ValueError(f"Review submit attempt identity mismatch: {key}")
        current = str(existing.get("status") or "")
        if current == status:
            return
        try:
            current_index = allowed_statuses.index(current)
            next_index = allowed_statuses.index(status)
        except ValueError as exc:
            raise ValueError("Stored review submit attempt status is invalid") from exc
        if next_index != current_index + 1:
            raise ValueError(f"Review submit attempt transition refused: {current} -> {status}")
        existing["status"] = status
        existing["updated_at"] = now

    return mutate_run(run_id, apply_attempt)


def is_approved_review_submission(snapshot: Any) -> bool:
    if not isinstance(snapshot, dict):
        return False
    try:
        iap_count = int(snapshot.get("iap_count") or 0)
    except (TypeError, ValueError):
        return False
    evidence_tokens = {
        token.strip()
        for token in str(snapshot.get("evidence") or "").split(";")
        if token.strip()
    }
    source = str(snapshot.get("source") or "")
    automatic_hashes_valid = True
    if source == "automatic_self_check":
        screenshot_hashes = snapshot.get("screenshot_hashes")
        automatic_hashes_valid = bool(
            snapshot.get("operator_id") == "automation:self-check"
            and isinstance(screenshot_hashes, list)
            and len(screenshot_hashes) == 5
            and len({str(value).lower() for value in screenshot_hashes}) == 5
            and all(
                re.fullmatch(r"[0-9a-fA-F]{64}", str(value)) is not None
                for value in screenshot_hashes
            )
        )
    return bool(
        snapshot.get("kind") == "review_submit"
        and snapshot.get("status") == "approved"
        and snapshot.get("decision") == "submit_review"
        and str(snapshot.get("decision_id") or "")
        and str(snapshot.get("app_version") or "")
        and str(snapshot.get("build_number") or "")
        and iap_count == 14
        and "REVIEW_SCREENSHOTS=verified_5" in evidence_tokens
        and "ITEMS_READY=15" in evidence_tokens
        and str(snapshot.get("answered_at") or "")
        and str(snapshot.get("operator_id") or "")
        and automatic_hashes_valid
    )


def legacy_review_submission_approval(pending: Any) -> dict[str, Any]:
    if not (
        isinstance(pending, dict)
        and pending.get("kind") == "review_submit"
        and pending.get("status") == "answered"
        and pending.get("decision") == "submit_review"
    ):
        return {}
    return review_submission_snapshot(
        pending,
        "submit_review",
        str(pending.get("answered_at") or ""),
        str(pending.get("operator_id") or ""),
    )


def record_decision_by_run_id(
    run_id: str,
    decision: str,
    raw_text: str,
    operator_id: str = "",
    decision_id: str = "",
) -> dict[str, Any] | None:
    statuses = {
        "stop": "decision_stop",
        "manual_continue": "decision_manual_continue",
        "retry_skill": "decision_retry_skill",
        "do_not_submit": "decision_do_not_submit",
        "submit_review": "decision_submit_review",
        "confirm_continue": "decision_confirm_continue",
        "cancel_operation": "decision_cancel_operation",
    }
    notes = {
        "stop": "用户选择停止流程",
        "manual_continue": "已人工处理，继续流程",
        "retry_skill": "立即重试当前技能并跳过已验证成功步骤",
        "do_not_submit": "用户选择暂不提审",
        "submit_review": "已收到提审确认，立即提交",
        "confirm_continue": "用户确认继续当前操作",
        "cancel_operation": "用户取消当前操作并停止流程",
    }
    if decision not in statuses:
        raise ValueError(f"Unsupported decision: {decision}")

    def apply_decision(item: dict[str, Any]) -> None:
        pending = item.setdefault("pending_decision", {})
        fault_decision = decision in {"stop", "manual_continue", "retry_skill"}
        review_decision = decision in {"do_not_submit", "submit_review"}
        confirmation_decision = decision in {"confirm_continue", "cancel_operation"}
        expected_fault_decision_id = str(pending.get("decision_id") or "")
        if fault_decision and not (
            str(pending.get("kind") or "fault") == "fault"
            and pending.get("status") == "waiting"
            and operator_id
            and (
                decision_id == expected_fault_decision_id
            )
        ):
            raise ValueError("Fault decision is stale, incomplete, or no longer waiting")
        if review_decision and not (
            pending.get("kind") == "review_submit"
            and pending.get("status") == "waiting"
            and decision_id
            and str(pending.get("decision_id") or "") == decision_id
            and operator_id
        ):
            raise ValueError("Review decision is stale, incomplete, or no longer waiting")
        if confirmation_decision and not (
            pending.get("kind") == "confirmation"
            and pending.get("status") == "waiting"
            and decision_id
            and str(pending.get("decision_id") or "") == decision_id
            and operator_id
        ):
            raise ValueError("Confirmation is stale, incomplete, or no longer waiting")
        answered_at = utc_now()
        if review_decision:
            snapshot = review_submission_snapshot(
                pending, decision, answered_at, operator_id
            )
            if decision == "submit_review" and not is_approved_review_submission(snapshot):
                raise ValueError("Review submission approval evidence is incomplete")
            item["review_submission_approval"] = snapshot
        item["status"] = statuses[decision]
        pending["status"] = "answered"
        pending["decision"] = decision
        pending["answered_at"] = answered_at
        pending["answer_text"] = raw_text
        if operator_id:
            pending["operator_id"] = operator_id
        item.setdefault("events", []).append(
            {
                "at": utc_now(),
                "status": item["status"],
                "note": notes[decision],
            }
        )

    return mutate_run(run_id, apply_decision)


def run_host_machine(run: dict[str, Any]) -> str:
    submission_data = run.get("submission_data")
    if isinstance(submission_data, dict) and submission_data.get("host_machine"):
        return str(submission_data["host_machine"]).strip()
    return str(run.get("host_machine") or "").strip()


def is_daily_report_chat(chat_id: str, daily_report_chat_id: str) -> bool:
    """Return whether a chat is the configured report-only destination."""
    configured = str(daily_report_chat_id or "").strip()
    return bool(configured) and str(chat_id or "").strip() == configured


def local_original_chat(run: dict[str, Any], config: Config) -> str:
    """Return the immutable original chat after enforcing local run ownership."""
    local_host = str(getattr(config, "submission_host_machine", "") or "").strip()
    if not local_host or run_host_machine(run) != local_host:
        raise RuntimeError("run host is missing or does not belong to this worker")
    chat_id = str(run.get("chat_id") or "")
    if not chat_id.strip() or is_daily_report_chat(
        chat_id, getattr(config, "daily_report_chat_id", "")
    ):
        raise RuntimeError("run has no valid original workflow chat")
    return chat_id


def require_requested_original_chat(
    run: dict[str, Any], config: Config, requested_chat_id: str
) -> str:
    """Require an explicit chat argument that exactly matches the run record."""
    chat_id = local_original_chat(run, config)
    if not requested_chat_id or requested_chat_id != chat_id:
        raise RuntimeError("requested chat does not match the run's original chat")
    return chat_id


def stored_original_chat(
    run: dict[str, Any], config: Config, requested_chat_id: str = ""
) -> str:
    """Use the stored chat; reject only an explicitly supplied mismatch."""
    chat_id = local_original_chat(run, config)
    if requested_chat_id and requested_chat_id != chat_id:
        raise RuntimeError("requested chat does not match the run's original chat")
    return chat_id


def reject_nonlocal_card_callback(
    payload: dict[str, Any],
    run_id: str,
    run: dict[str, Any] | None,
    submission_host_machine: str,
    daily_report_chat_id: str,
) -> dict[str, Any] | None:
    local_host = str(submission_host_machine or "").strip()
    card_host = run_host_machine(run or {})
    original_chat = str((run or {}).get("chat_id") or "").strip()
    valid_original_chat = bool(original_chat) and not is_daily_report_chat(
        original_chat, daily_report_chat_id
    )
    if run and local_host and card_host == local_host and valid_original_chat:
        return None
    result = {"toast": {"type": "warning", "content": "非本机卡片，未执行任何操作"}}
    append_card_callback_log(
        payload,
        {
            "error": (
                "card_host_mismatch"
                if not run or not local_host or card_host != local_host
                else "card_original_chat_invalid"
            ),
            "run_id": run_id,
            "card_host_machine": card_host,
            "submission_host_machine": local_host,
            "original_chat_valid": valid_original_chat,
        },
    )
    return result


def handle_review_card_action(
    payload: dict[str, Any],
    value: dict[str, Any],
    operator_id: str,
    submission_host_machine: str,
    daily_report_chat_id: str,
) -> dict[str, Any]:
    run_id = str(value.get("run_id") or "")
    decision = str(value.get("decision") or "")
    decision_id = str(value.get("decision_id") or "")
    allowed = {"do_not_submit", "submit_review"}
    if decision not in allowed or not run_id or not decision_id:
        result = {"toast": {"type": "warning", "content": "卡片参数无效"}}
        append_card_callback_log(payload, {"error": "invalid_review_decision"})
        return result
    if not operator_id:
        result = {"toast": {"type": "warning", "content": "无法确认操作人，未执行提审"}}
        append_card_callback_log(payload, {"error": "missing_operator", "run_id": run_id})
        return result

    existing = find_run(run_id)
    rejected = reject_nonlocal_card_callback(
        payload,
        run_id,
        existing,
        submission_host_machine,
        daily_report_chat_id,
    )
    if rejected:
        return rejected
    pending = (existing or {}).get("pending_decision") or {}
    if (
        not existing
        or pending.get("kind") != "review_submit"
        or str(pending.get("decision_id") or "") != decision_id
    ):
        result = {"toast": {"type": "warning", "content": "卡片已失效或不匹配，未执行提审"}}
        append_card_callback_log(payload, {"error": "stale_review_decision", "run_id": run_id})
        return result
    if pending.get("status") != "waiting":
        if pending.get("status") == "answered" and pending.get("decision") in allowed:
            result = {
                "toast": {"type": "info", "content": "该提审确认卡已处理"},
                "card": {"type": "raw", "data": build_review_confirmation_card(existing)},
            }
            append_card_callback_log(payload, {"ok": True, "duplicate": True, "run_id": run_id})
            return result
        if pending.get("status") == "expired" and pending.get("decision") == "timeout":
            result = {
                "toast": {"type": "warning", "content": "等待回复已超过一小时，当前流程已停止"},
                "card": {"type": "raw", "data": build_decision_timeout_card(existing)},
            }
            append_card_callback_log(payload, {"error": "review_decision_timeout", "run_id": run_id})
            return result
        result = {"toast": {"type": "warning", "content": "卡片已失效或不匹配，未执行提审"}}
        append_card_callback_log(payload, {"error": "review_decision_not_waiting", "run_id": run_id})
        return result

    try:
        run = record_decision_by_run_id(
            run_id, decision, "card_button", operator_id, decision_id
        )
    except ValueError:
        result = {"toast": {"type": "warning", "content": "卡片已失效或不匹配，未执行提审"}}
        append_card_callback_log(
            payload, {"error": "stale_review_decision_during_update", "run_id": run_id}
        )
        return result
    if not run:
        result = {"toast": {"type": "warning", "content": "运行记录不存在"}}
        append_card_callback_log(payload, {"error": "run_not_found", "run_id": run_id})
        return result
    message = "已收到提审确认，正在提交" if decision == "submit_review" else "已记录暂不提审"
    result = {
        "toast": {"type": "success", "content": message},
        "card": {"type": "raw", "data": build_review_confirmation_card(run)},
    }
    append_card_callback_log(payload, {"ok": True, "run_id": run_id, "decision": decision})
    return result


def handle_confirmation_card_action(
    payload: dict[str, Any],
    value: dict[str, Any],
    operator_id: str,
    submission_host_machine: str,
    daily_report_chat_id: str,
) -> dict[str, Any]:
    run_id = str(value.get("run_id") or "")
    decision = str(value.get("decision") or "")
    decision_id = str(value.get("decision_id") or "")
    allowed = {"confirm_continue", "cancel_operation"}
    if decision not in allowed or not run_id or not decision_id or not operator_id:
        result = {"toast": {"type": "warning", "content": "确认卡参数无效"}}
        append_card_callback_log(payload, {"error": "invalid_confirmation_decision"})
        return result
    existing = find_run(run_id)
    rejected = reject_nonlocal_card_callback(
        payload,
        run_id,
        existing,
        submission_host_machine,
        daily_report_chat_id,
    )
    if rejected:
        return rejected
    pending = (existing or {}).get("pending_decision") or {}
    if (
        not existing
        or pending.get("kind") != "confirmation"
        or str(pending.get("decision_id") or "") != decision_id
    ):
        result = {"toast": {"type": "warning", "content": "确认卡已失效或不匹配"}}
        append_card_callback_log(payload, {"error": "stale_confirmation", "run_id": run_id})
        return result
    if pending.get("status") != "waiting":
        if pending.get("status") == "answered" and pending.get("decision") in allowed:
            result = {
                "toast": {"type": "info", "content": "该确认卡已处理"},
                "card": {"type": "raw", "data": build_confirmation_card(existing)},
            }
            append_card_callback_log(payload, {"ok": True, "duplicate": True, "run_id": run_id})
            return result
        if pending.get("status") == "expired" and pending.get("decision") == "timeout":
            result = {
                "toast": {"type": "warning", "content": "等待回复已超过一小时，当前流程已停止"},
                "card": {"type": "raw", "data": build_decision_timeout_card(existing)},
            }
            append_card_callback_log(payload, {"error": "confirmation_timeout", "run_id": run_id})
            return result
        result = {"toast": {"type": "warning", "content": "确认卡已失效或不匹配"}}
        append_card_callback_log(payload, {"error": "confirmation_not_waiting", "run_id": run_id})
        return result
    try:
        run = record_decision_by_run_id(
            run_id, decision, "card_button", operator_id, decision_id
        )
    except ValueError:
        result = {"toast": {"type": "warning", "content": "确认卡已失效或不匹配"}}
        append_card_callback_log(payload, {"error": "stale_confirmation_update", "run_id": run_id})
        return result
    if not run:
        result = {"toast": {"type": "warning", "content": "运行记录不存在"}}
        append_card_callback_log(payload, {"error": "run_not_found", "run_id": run_id})
        return result
    message = "已确认，正在继续流程" if decision == "confirm_continue" else "已取消，流程停止"
    result = {
        "toast": {"type": "success", "content": message},
        "card": {"type": "raw", "data": build_confirmation_card(run)},
    }
    append_card_callback_log(payload, {"ok": True, "run_id": run_id, "decision": decision})
    return result


def handle_card_action(
    payload: dict[str, Any],
    submission_host_machine: str,
    daily_report_chat_id: str,
) -> dict[str, Any]:
    value, operator_id = card_callback_value(payload)
    action = str(value.get("action") or "")
    if action == "submission_review_decision":
        return handle_review_card_action(
            payload,
            value,
            operator_id,
            submission_host_machine,
            daily_report_chat_id,
        )
    if action == "submission_confirmation_decision":
        return handle_confirmation_card_action(
            payload,
            value,
            operator_id,
            submission_host_machine,
            daily_report_chat_id,
        )
    if action != "submission_fault_decision":
        result = {"toast": {"type": "warning", "content": "未识别的卡片动作"}}
        append_card_callback_log(payload, {"ignored": action or "unknown"})
        return result

    run_id = str(value.get("run_id") or "")
    decision = str(value.get("decision") or "")
    decision_id = str(value.get("decision_id") or "")
    allowed = {"stop", "manual_continue", "retry_skill"}
    if decision not in allowed or not run_id:
        result = {"toast": {"type": "warning", "content": "卡片参数无效"}}
        append_card_callback_log(payload, {"error": "invalid_decision", "value": value})
        return result
    if not operator_id:
        result = {"toast": {"type": "warning", "content": "无法确认操作人，未执行任何操作"}}
        append_card_callback_log(payload, {"error": "missing_operator", "run_id": run_id})
        return result

    existing = find_run(run_id)
    rejected = reject_nonlocal_card_callback(
        payload,
        run_id,
        existing,
        submission_host_machine,
        daily_report_chat_id,
    )
    if rejected:
        return rejected
    pending = (existing or {}).get("pending_decision") or {}
    if str(pending.get("kind") or "fault") != "fault":
        result = {"toast": {"type": "warning", "content": "卡片参数无效"}}
        append_card_callback_log(payload, {"error": "invalid_decision_kind", "run_id": run_id})
        return result
    expected_decision_id = str(pending.get("decision_id") or "")
    if decision_id != expected_decision_id:
        result = {"toast": {"type": "warning", "content": "故障卡已失效或不匹配"}}
        append_card_callback_log(payload, {"error": "stale_fault_decision", "run_id": run_id})
        return result

    if pending.get("status") != "waiting":
        if existing and pending.get("status") == "answered" and pending.get("decision") in allowed:
            result_text = {
                "stop": "已选择：停止流程",
                "manual_continue": "已选择：已人工处理，继续流程",
                "retry_skill": "已选择：重试技能，跳过已处理成功的步骤",
            }[pending["decision"]]
            result = {
                "toast": {"type": "info", "content": f"该确认卡片已处理：{result_text}"},
                "card": {"type": "raw", "data": build_fault_decision_card(existing)},
            }
            append_card_callback_log(payload, {"ok": True, "duplicate": True, "run_id": run_id})
            return result
        if existing and pending.get("status") == "expired" and pending.get("decision") == "timeout":
            result = {
                "toast": {"type": "warning", "content": "等待回复已超过一小时，当前流程已停止"},
                "card": {"type": "raw", "data": build_decision_timeout_card(existing)},
            }
            append_card_callback_log(payload, {"error": "decision_timeout", "run_id": run_id})
            return result
        result = {"toast": {"type": "warning", "content": "运行记录不存在"}}
        append_card_callback_log(payload, {"error": "decision_not_waiting", "run_id": run_id})
        return result

    try:
        run = record_decision_by_run_id(
            run_id, decision, "card_button", operator_id, decision_id
        )
    except ValueError:
        result = {"toast": {"type": "warning", "content": "故障卡已失效或不匹配"}}
        append_card_callback_log(
            payload, {"error": "stale_fault_decision_during_update", "run_id": run_id}
        )
        return result
    if not run:
        result = {"toast": {"type": "warning", "content": "运行记录不存在"}}
        append_card_callback_log(payload, {"error": "run_not_found", "run_id": run_id})
        return result

    message = {
        "stop": "已记录停止流程",
        "manual_continue": "已收到人工处理结果，正在继续流程",
        "retry_skill": "已收到重试指令，正在重试当前技能",
    }[decision]
    result = {
        "toast": {"type": "success", "content": message},
        "card": {"type": "raw", "data": build_fault_decision_card(run)},
    }
    append_card_callback_log(payload, {"ok": True, "run_id": run_id, "decision": decision})
    return result


def handle_ws_card_action(
    data: Any,
    submission_host_machine: str,
    daily_report_chat_id: str,
) -> Any:
    event = getattr(data, "event", None)
    action = getattr(event, "action", None)
    operator = getattr(event, "operator", None)
    value = getattr(action, "value", None) if action is not None else None
    payload = {
        "header": {"event_type": "card.action.trigger"},
        "event": {
            "operator": {
                "open_id": getattr(operator, "open_id", "") if operator is not None else "",
                "user_id": getattr(operator, "user_id", "") if operator is not None else "",
            },
            "action": {"value": value if isinstance(value, (dict, str)) else {}},
        },
    }
    result = handle_card_action(
        payload, submission_host_machine, daily_report_chat_id
    )
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

        return P2CardActionTriggerResponse(result)
    except Exception:  # noqa: BLE001 - keep callback response usable across SDK versions.
        return result


def patch_lark_ws_card_dispatch() -> None:
    import base64 as _base64
    import http as _http
    import time as _time

    import lark_oapi.ws.client as ws_client

    async def _handle_data_frame(self: Any, frame: Any) -> None:
        headers = frame.headers
        message_id = ws_client._get_by_key(headers, ws_client.HEADER_MESSAGE_ID)
        total = ws_client._get_by_key(headers, ws_client.HEADER_SUM)
        sequence = ws_client._get_by_key(headers, ws_client.HEADER_SEQ)
        message_type = ws_client.MessageType(ws_client._get_by_key(headers, ws_client.HEADER_TYPE))
        payload = frame.payload
        if int(total) > 1:
            payload = self._combine(message_id, int(total), int(sequence), payload)
            if payload is None:
                return

        response = ws_client.Response(code=_http.HTTPStatus.OK)
        try:
            started = int(round(_time.time() * 1000))
            if message_type not in {ws_client.MessageType.EVENT, ws_client.MessageType.CARD}:
                return
            if message_type == ws_client.MessageType.CARD:
                # Bypass the SDK's strict Dict parser for the one callback
                # shape that Feishu has historically returned as a string.
                # The normalized bytes still flow through the registered
                # callback processor, preserving its response type and
                # verification behavior.
                normalized = normalize_ws_card_payload(payload)
                if normalized:
                    payload = ws_client.JSON.marshal(normalized).encode(ws_client.UTF_8)
            result = self._event_handler._do_without_validation(payload)
            header = headers.add()
            header.key = ws_client.HEADER_BIZ_RT
            header.value = str(int(round(_time.time() * 1000)) - started)
            if result is not None:
                response.data = _base64.b64encode(ws_client.JSON.marshal(result).encode(ws_client.UTF_8))
        except Exception as exc:  # noqa: BLE001 - mirror SDK behavior.
            ws_client.logger.error(self._fmt_log("handle message failed: {}", exc))
            response = ws_client.Response(code=_http.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = ws_client.JSON.marshal(response).encode(ws_client.UTF_8)
        await self._write_message(frame.SerializeToString())

    ws_client.Client._handle_data_frame = _handle_data_frame


def handle_ws_message_receive(data: Any, config: Config) -> None:
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    message_id = str(getattr(message, "message_id", "") or "")
    chat_id = str(getattr(message, "chat_id", "") or "")
    content = getattr(message, "content", "") or ""
    text = parse_text_content({"content": content})

    if message_id and not claim_message(message_id):
        safe_print(f"ws duplicate skipped message_id={message_id}")
        return

    def send_reply() -> None:
        try:
            reply = handle_incoming_text(config, text, chat_id, source="feishu-ws")
            if reply and chat_id:
                FeishuClient(config).send_text(chat_id, reply)
        except Exception as exc:  # noqa: BLE001 - release lets polling recover the message.
            if message_id:
                release_message(message_id)
            safe_print(f"[ws_reply_error] {type(exc).__name__}: {exc}", file=sys.stderr)

    safe_print(f"ws message received chat_id={chat_id} text={strip_feishu_markdown(text)[:80]}")
    threading.Thread(target=send_reply, daemon=True).start()


def configured_ws_message_handler(config: Config):
    """Bind one startup configuration snapshot to WS message callbacks."""

    def handle(data: Any) -> None:
        handle_ws_message_receive(data, config)

    return handle


def configured_ws_card_handler(config: Config):
    """Bind the same startup configuration snapshot to WS card callbacks."""

    def handle(data: Any) -> Any:
        return handle_ws_card_action(
            data,
            config.submission_host_machine,
            config.daily_report_chat_id,
        )

    return handle


class FeishuHandler(BaseHTTPRequestHandler):
    config: Config
    client: FeishuClient

    def _json_response(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = request_path(self.path)
        if path == HEALTH_PATH:
            self._json_response(200, {"ok": True, "time": utc_now()})
            return
        if path == CALLBACK_PATH:
            self._json_response(200, {"ok": True, "endpoint": CALLBACK_PATH.lstrip("/"), "time": utc_now()})
            return
        if path in LEGACY_CALLBACK_PATHS:
            self._json_response(
                200,
                {
                    "ok": True,
                    "endpoint": CALLBACK_PATH.lstrip("/"),
                    "legacy_path": path,
                    "callback_path": CALLBACK_PATH,
                    "time": utc_now(),
                },
            )
            return
        self._json_response(404, {"ok": False, "error": "not_found", "callback_path": CALLBACK_PATH})

    def do_HEAD(self) -> None:
        if request_path(self.path) in {HEALTH_PATH, CALLBACK_PATH, *LEGACY_CALLBACK_PATHS}:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if not is_callback_path(self.path):
            self._json_response(404, {"ok": False, "error": "not_found", "callback_path": CALLBACK_PATH})
            return

        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        headers = {key.lower(): value for key, value in self.headers.items()}
        if not verify_signature(self.config, headers, raw_body):
            self._json_response(401, {"ok": False, "error": "invalid_signature"})
            return

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self._json_response(400, {"ok": False, "error": "invalid_json"})
            return

        if payload.get("type") == "url_verification":
            if self.config.verification_token and payload.get("token") != self.config.verification_token:
                self._json_response(401, {"ok": False, "error": "invalid_verification_token"})
                return
            self._json_response(200, {"challenge": payload.get("challenge", "")})
            return

        if payload.get("encrypt"):
            self._json_response(
                400,
                {
                    "ok": False,
                    "error": "encrypted_callbacks_not_enabled",
                    "hint": "Leave Encrypt Key empty in Feishu event subscription for this service.",
                },
            )
            return

        event = payload.get("event", {})
        header = payload.get("header", {})
        event_type = header.get("event_type") or payload.get("event_type") or payload.get("type")
        if event_type in {"card.action.trigger", "card.action.trigger_v1"}:
            self._json_response(
                200,
                handle_card_action(
                    payload,
                    self.config.submission_host_machine,
                    self.config.daily_report_chat_id,
                ),
            )
            return
        if event_type != "im.message.receive_v1":
            self._json_response(200, {"ok": True, "ignored": event_type or "unknown"})
            return

        message = event.get("message", {})
        chat_id = message.get("chat_id", "")
        text = parse_text_content(message)
        reply = handle_incoming_text(self.config, text, chat_id, source="feishu-webhook")

        if reply and chat_id:
            threading.Thread(target=self._send_reply, args=(chat_id, reply), daemon=True).start()

        self._json_response(200, {"ok": True})

    def _send_reply(self, chat_id: str, reply: str) -> None:
        try:
            self.client.send_text(chat_id, reply)
        except Exception as exc:  # noqa: BLE001 - background thread must not crash server
            safe_print(f"[send_reply_error] {exc}", file=sys.stderr)

    def log_message(self, fmt: str, *args: Any) -> None:
        safe_print(f"[{datetime.now().isoformat(timespec='seconds')}] {self.address_string()} {fmt % args}")


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def configured_http_handler(config: Config) -> type[FeishuHandler]:
    """Bind validated call-time configuration to an HTTP handler class."""

    class ConfiguredFeishuHandler(FeishuHandler):
        pass

    ConfiguredFeishuHandler.config = config
    ConfiguredFeishuHandler.client = FeishuClient(config)
    return ConfiguredFeishuHandler


def validate_fault_recovery_evidence(args: Any) -> tuple[bool, str]:
    """Require proof that automatic recovery preceded a fault card."""
    try:
        attempts = int(getattr(args, "recovery_attempts", 0) or 0)
    except (TypeError, ValueError):
        return False, "recovery attempts must be an integer"
    actions = str(getattr(args, "recovery_actions", "") or "").strip()
    result = str(getattr(args, "recovery_result", "") or "").strip().lower()
    unrepairable = bool(getattr(args, "unrepairable", False))
    if attempts < 0:
        return False, "recovery attempts cannot be negative"
    if not actions or actions.startswith("<") or actions in {"none", "未填写"}:
        return False, "recovery actions are required"
    if result not in {"exhausted", "unrepairable"}:
        return False, "recovery result must be exhausted or unrepairable"
    if attempts < 3:
        return False, "fault cards require at least three recovery or read-only verification rounds"
    if result == "exhausted" and unrepairable:
        return False, "exhausted recovery cannot use the unrepairable flag"
    if result == "unrepairable" and not unrepairable:
        return False, "unrepairable recovery requires the explicit flag"
    return True, "verified"


def create_or_update_fault(
    run_id: str,
    chat_id: str,
    stage: str,
    fault: str,
    suggested_action: str,
    failure_action: str = "",
    retry_count: int = 0,
    completed_steps: str = "",
    evidence: str = "",
    recovery_skill: str = "",
    host_machine: str = "",
    recovery_attempts: int = 0,
    recovery_actions: str = "",
    recovery_result: str = "",
    unrepairable: bool = False,
) -> dict[str, Any]:
    if not run_id:
        run_id = f"submission-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run = find_run(run_id)
    existing = (run or {}).get("pending_decision") or {}
    same_waiting_fault = (
        existing.get("kind") == "fault"
        and existing.get("status") == "waiting"
        and existing.get("stage") == stage
        and existing.get("fault") == fault
        and existing.get("recovery_skill") == recovery_skill
    )
    if same_waiting_fault:
        legacy_delivered = bool(
            existing.get("first_notified_at") or existing.get("last_message_id")
        )
        needs_message_uuid = not existing.get("message_uuid")
        needs_decision_id = not existing.get("decision_id") and not legacy_delivered
        if not needs_message_uuid and not needs_decision_id:
            return run

        def add_fault_ids(item: dict[str, Any]) -> None:
            current = item.setdefault("pending_decision", {})
            if not current.get("message_uuid"):
                current["message_uuid"] = uuid.uuid4().hex
            if (
                not current.get("decision_id")
                and not current.get("first_notified_at")
                and not current.get("last_message_id")
            ):
                current["decision_id"] = uuid.uuid4().hex

        updated = mutate_run(run_id, add_fault_ids)
        if updated is None:
            raise RuntimeError(f"Unable to update fault identifiers: {run_id}")
        return updated
    if existing.get("status") == "waiting":
        raise RuntimeError("Another decision card is already waiting")
    pending = {
        "kind": "fault",
        "status": "waiting",
        "decision_id": uuid.uuid4().hex,
        "stage": stage,
        "fault": fault,
        "suggested_action": suggested_action,
        "failure_action": failure_action,
        "retry_count": retry_count,
        "completed_steps": completed_steps,
        "evidence": evidence,
        "recovery_skill": recovery_skill,
        "recovery_attempts": recovery_attempts,
        "recovery_actions": recovery_actions,
        "recovery_result": recovery_result,
        "unrepairable": unrepairable,
        "requested_at": utc_now(),
        "decision": "",
        "last_notified_at": "",
        "message_uuid": uuid.uuid4().hex,
    }
    if not run:
        run = {
            "id": run_id,
            "app_name": "未知应用",
            "host_machine": host_machine.strip(),
            "chat_id": chat_id,
            "raw_text": "",
            "status": "waiting_user_decision",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "events": [],
        }
        save_run(run)

    def apply_fault(item: dict[str, Any]) -> None:
        item["status"] = "waiting_user_decision"
        item["pending_decision"] = pending
        item.setdefault("events", []).append(
            {"at": utc_now(), "status": "waiting_user_decision", "note": f"{stage}: {fault}"}
        )

    updated = mutate_run(run_id, apply_fault)
    if updated is None:
        raise RuntimeError(f"Unable to create or update run: {run_id}")
    return updated


def create_review_confirmation(
    run_id: str,
    app_version: str,
    build_number: str,
    iap_count: int,
    evidence: str,
    image_keys: list[str],
) -> dict[str, Any]:
    decision_id = uuid.uuid4().hex
    pending = {
        "kind": "review_submit",
        "status": "waiting",
        "decision": "",
        "decision_id": decision_id,
        "app_version": app_version,
        "build_number": build_number,
        "iap_count": iap_count,
        "evidence": evidence,
        "image_keys": image_keys,
        "requested_at": utc_now(),
        "last_notified_at": "",
        "message_uuid": uuid.uuid4().hex,
    }

    def apply_confirmation(item: dict[str, Any]) -> None:
        item["status"] = "waiting_review_submission_confirmation"
        item["pending_decision"] = pending
        item.setdefault("events", []).append(
            {
                "at": utc_now(),
                "status": "waiting_review_submission_confirmation",
                "note": "等待最终提审确认",
            }
        )

    updated = mutate_run(run_id, apply_confirmation)
    if updated is None:
        raise RuntimeError(f"Unable to update run: {run_id}")
    return updated


def create_or_update_confirmation(
    run_id: str,
    stage: str,
    current_skill: str,
    question: str,
    action_summary: str,
    evidence: str,
) -> dict[str, Any]:
    stage = str(stage or "").strip()
    current_skill = str(current_skill or "").strip()
    question = str(question or "").strip()
    action_summary = str(action_summary or "").strip()
    evidence = str(evidence or "").strip()
    required = {
        "stage": stage,
        "current_skill": current_skill,
        "question": question,
        "action_summary": action_summary,
        "evidence": evidence,
    }
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if missing:
        raise RuntimeError(
            "Confirmation requires non-empty " + ", ".join(missing)
        )
    run = find_run(run_id)
    if not run:
        raise RuntimeError(f"Confirmation requires an existing run: {run_id}")
    existing = run.get("pending_decision") or {}
    same_waiting_confirmation = (
        existing.get("kind") == "confirmation"
        and existing.get("status") == "waiting"
        and existing.get("stage") == stage
        and existing.get("current_skill") == current_skill
        and existing.get("question") == question
        and existing.get("action_summary") == action_summary
        and existing.get("evidence") == evidence
    )
    if same_waiting_confirmation:
        if existing.get("message_uuid"):
            return run

        def add_message_uuid(item: dict[str, Any]) -> None:
            item.setdefault("pending_decision", {})["message_uuid"] = uuid.uuid4().hex

        updated = mutate_run(run_id, add_message_uuid)
        if updated is None:
            raise RuntimeError(f"Unable to update confirmation message uuid: {run_id}")
        return updated
    if existing.get("status") == "waiting":
        raise RuntimeError("Another decision card is already waiting")
    pending = {
        "kind": "confirmation",
        "status": "waiting",
        "decision": "",
        "decision_id": uuid.uuid4().hex,
        "stage": stage,
        "current_skill": current_skill,
        "question": question,
        "action_summary": action_summary,
        "evidence": evidence,
        "requested_at": utc_now(),
        "last_notified_at": "",
        "message_uuid": uuid.uuid4().hex,
    }

    def apply_confirmation(item: dict[str, Any]) -> None:
        item["status"] = "waiting_user_confirmation"
        item["pending_decision"] = pending
        item.setdefault("events", []).append(
            {
                "at": utc_now(),
                "status": "waiting_user_confirmation",
                "note": f"等待用户确认：{stage}",
            }
        )

    updated = mutate_run(run_id, apply_confirmation)
    if updated is None:
        raise RuntimeError(f"Unable to update run: {run_id}")
    return updated


def build_decision_timeout_card(run: dict[str, Any]) -> dict[str, Any]:
    pending = run.get("pending_decision") or {}
    kind = str(pending.get("kind") or "fault")
    card_type = {"review_submit": "提审确认卡", "confirmation": "用户确认卡"}.get(kind, "异常故障卡")
    current_skill = pending.get("current_skill") or pending.get("recovery_skill") or ("utm-24" if kind == "review_submit" else "未知")
    timeout_seconds = int(pending.get("timeout_seconds") or DECISION_TIMEOUT_SECONDS)
    lines = [
        f"**宿主机**：{run_host_machine(run) or '未知'}",
        f"**运行**：{run['id']}",
        f"**当前技能**：{current_skill}",
        f"**卡片类型**：{card_type}",
        f"**首次确认送达**：{pending.get('first_notified_at', '未知')}",
        f"**等待上限**：{timeout_seconds} 秒",
        f"**超时时间**：{pending.get('expired_at', '未知')}",
        "**处理结果**：用户在一小时内未回复，当前 run 已停止，后续技能不再执行。",
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": "等待回复超时，流程已停止"},
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
            ]
        },
    }


def build_fault_decision_card(run: dict[str, Any]) -> dict[str, Any]:
    pending = run.get("pending_decision") or {}
    if pending.get("status") == "expired" and pending.get("decision") == "timeout":
        return build_decision_timeout_card(run)
    decision = str(pending.get("decision") or "")
    resolved = pending.get("status") == "answered" and decision in {
        "stop",
        "manual_continue",
        "retry_skill",
    }
    lines = [
        f"**宿主机**：{run_host_machine(run) or '未知'}",
        f"**运行**：{run['id']}",
        f"**当前技能**：{pending.get('recovery_skill', '未填写')}",
        f"**故障阶段**：{pending.get('stage', '未知')}",
        f"**故障原因**：{pending.get('fault', '未填写')}",
        f"**已完成步骤**：{pending.get('completed_steps', '') or '无'}",
        f"**当前证据**：{pending.get('evidence', '未填写')}",
        f"**已重试次数**：{pending.get('retry_count', 0)}",
        f"**自动恢复次数**：{pending.get('recovery_attempts', 0)}",
        f"**自动恢复动作**：{pending.get('recovery_actions', '未填写')}",
        f"**自动恢复结果**：{pending.get('recovery_result', '未填写')}",
        f"**建议动作**：{pending.get('suggested_action', '未填写')}",
    ]
    if resolved:
        result_text = {
            "stop": "已选择：停止流程",
            "manual_continue": "已选择：已人工处理，继续流程",
            "retry_skill": "已选择：重试技能，跳过已处理成功的步骤",
        }[decision]
        lines.append(f"**处理结果**：{result_text}")
        elements = [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": result_text},
                "type": "default",
                "disabled": True,
            },
        ]
    else:
        base_value = {
            "action": "submission_fault_decision",
            "run_id": run["id"],
        }
        if pending.get("decision_id"):
            base_value["decision_id"] = pending["decision_id"]
        elements = [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "停止流程"},
                "type": "danger",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {**base_value, "decision": "stop"},
                    }
                ],
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "已人工处理，继续流程"},
                "type": "default",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {**base_value, "decision": "manual_continue"},
                    }
                ],
            },
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": "重试技能，跳过已处理成功的步骤",
                },
                "type": "primary",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {**base_value, "decision": "retry_skill"},
                    }
                ],
            },
        ]
    resolved_title = {
        "stop": "提审流程已停止",
        "manual_continue": "已收到处理结果，正在继续流程",
        "retry_skill": "已收到重试指令，正在重试技能",
    }.get(decision, "提审流程处理结果")
    return {
        # JSON 2.0 is required for card.action.trigger over the Feishu
        # long connection. The legacy action container emits the v1 callback.
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green" if resolved else "orange",
            "title": {
                "tag": "plain_text",
                "content": resolved_title if resolved else "提审流程发生故障",
            },
        },
        "body": {"elements": elements},
    }


def build_confirmation_card(run: dict[str, Any]) -> dict[str, Any]:
    pending = run.get("pending_decision") or {}
    if pending.get("status") == "expired" and pending.get("decision") == "timeout":
        return build_decision_timeout_card(run)
    decision = str(pending.get("decision") or "")
    resolved = pending.get("status") == "answered" and decision in {
        "confirm_continue",
        "cancel_operation",
    }
    lines = [
        f"**宿主机**：{run_host_machine(run) or '未知'}",
        f"**运行**：{run['id']}",
        f"**当前技能**：{pending.get('current_skill', '未知')}",
        f"**确认阶段**：{pending.get('stage', '未知')}",
        f"**确认问题**：{pending.get('question', '未填写')}",
        f"**确认后动作**：{pending.get('action_summary', '未填写')}",
        f"**当前证据**：{pending.get('evidence', '未填写')}",
    ]
    if resolved:
        result_text = "已选择：确认并继续" if decision == "confirm_continue" else "已选择：取消并停止"
        lines.append(f"**处理结果**：{result_text}")
        elements = [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": result_text},
                "type": "default",
                "disabled": True,
            },
        ]
    else:
        base_value = {
            "action": "submission_confirmation_decision",
            "decision_id": pending.get("decision_id", ""),
            "run_id": run["id"],
        }
        elements = [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "取消并停止"},
                "type": "danger",
                "behaviors": [
                    {"type": "callback", "value": {**base_value, "decision": "cancel_operation"}}
                ],
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "确认并继续"},
                "type": "primary",
                "behaviors": [
                    {"type": "callback", "value": {**base_value, "decision": "confirm_continue"}}
                ],
            },
        ]
    title = {
        "confirm_continue": "已确认，正在继续流程",
        "cancel_operation": "已取消，流程停止",
    }.get(decision, "流程需要用户确认")
    template = "green" if decision == "confirm_continue" else "red" if resolved else "orange"
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {"elements": elements},
    }


def build_review_confirmation_card(run: dict[str, Any]) -> dict[str, Any]:
    pending = run.get("pending_decision") or {}
    if pending.get("status") == "expired" and pending.get("decision") == "timeout":
        return build_decision_timeout_card(run)
    decision = str(pending.get("decision") or "")
    status = str(pending.get("status") or "waiting")
    resolved = status == "answered" and decision in {"do_not_submit", "submit_review"}
    raw_image_keys = pending.get("image_keys")
    image_keys = (
        [str(key).strip() for key in raw_image_keys if str(key).strip()]
        if isinstance(raw_image_keys, list)
        else []
    )
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{run.get('app_name', '未知应用')}{pending.get('app_version', '未知')} 准备提审**",
            },
        },
        *[
            {
                "tag": "img",
                "img_key": image_key,
                "preview": True,
                "transparent": False,
                "scale_type": "fit_horizontal",
                "alt": {"tag": "plain_text", "content": f"提审截图 {index}"},
            }
            for index, image_key in enumerate(image_keys[:5], 1)
        ],
        {"tag": "div", "text": {"tag": "lark_md", "content": "**确认问题**：是否现在提审？"}},
    ]
    if resolved:
        result_text = "已选择：是，现在提审" if decision == "submit_review" else "已选择：否，暂不提审"
        elements.extend(
            [
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**处理结果**：{result_text}"}},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": result_text},
                    "type": "default",
                    "disabled": True,
                },
            ]
        )
    else:
        base_value = {
            "action": "submission_review_decision",
            "decision_id": pending.get("decision_id", ""),
            "run_id": run["id"],
        }
        elements.extend(
            [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "否，暂不提审"},
                    "type": "danger",
                    "behaviors": [
                        {"type": "callback", "value": {**base_value, "decision": "do_not_submit"}}
                    ],
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "是，现在提审"},
                    "type": "primary",
                    "behaviors": [
                        {"type": "callback", "value": {**base_value, "decision": "submit_review"}}
                    ],
                },
            ]
        )
    resolved_title = "已收到提审确认，正在提交" if decision == "submit_review" else "提审确认已收到"
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green" if resolved else "orange",
            "title": {
                "tag": "plain_text",
                "content": resolved_title if resolved else "是否现在提审？",
            },
        },
        "body": {"elements": elements},
    }


def decision_card_for_run(run: dict[str, Any]) -> dict[str, Any]:
    pending = run.get("pending_decision") or {}
    if pending.get("status") == "expired" and pending.get("decision") == "timeout":
        return build_decision_timeout_card(run)
    if pending.get("kind") == "review_submit":
        return build_review_confirmation_card(run)
    if pending.get("kind") == "confirmation":
        return build_confirmation_card(run)
    return build_fault_decision_card(run)


def message_id_from_response(response: dict[str, Any] | None) -> str:
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        return str(data.get("message_id") or data.get("messageId") or "")
    return ""


def record_fault_notification(run_id: str, message_id: str) -> dict[str, Any] | None:
    if not message_id:
        raise ValueError("Fault card delivery requires a message_id")

    def apply_notification(item: dict[str, Any]) -> None:
        pending = item.setdefault("pending_decision", {})
        notified_at = utc_now()
        if not pending.get("first_notified_at"):
            pending["first_notified_at"] = notified_at
        pending["last_notified_at"] = notified_at
        if message_id:
            pending["last_message_id"] = message_id

    return mutate_run(run_id, apply_notification)


def user_confirmation_api_decision(
    run: dict[str, Any], action_summary: str, config: Config
) -> tuple[str, str]:
    url = str(getattr(config, "user_confirm_api_url", "") or "").strip()
    if not url:
        return "", ""
    payload = {
        "宿主机名": run_host_machine(run),
        "应用名": str(run.get("app_name") or "").strip(),
        "要确认的动作": str(action_summary or "").strip(),
    }
    if not all(payload.values()):
        return "", ""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        timeout = int(getattr(config, "user_confirm_timeout_seconds", 20) or 20)
        completed = subprocess.run(
            [
                "curl",
                "-sS",
                "--fail-with-body",
                "-X",
                "POST",
                url,
                "-H",
                "Content-Type: application/json",
                "--data-binary",
                "@-",
            ],
            input=body,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode("utf-8", errors="replace").strip())
        response = json.loads(completed.stdout.decode("utf-8"))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        safe_print(f"[user_confirm_api_error] {exc}", file=sys.stderr)
        return "", ""

    raw_text = json.dumps(response, ensure_ascii=False, sort_keys=True)
    status = str(response.get("status") or "").lower()
    if (
        response.get("approved") is True
        or response.get("confirmed") is True
        or response.get("can_operate") is True
        or status in {"approved", "confirmed", "confirm_continue"}
    ):
        return "confirm_continue", raw_text
    if (
        response.get("approved") is False
        or response.get("can_operate") is False
        or status in {"denied", "rejected", "cancelled", "cancel_operation"}
    ):
        return "cancel_operation", raw_text
    return "", raw_text


def notify_fault(args: argparse.Namespace, config: Config) -> int:
    if not args.run_id:
        print("notify-fault requires an existing --run-id", file=sys.stderr)
        return 2
    if not str(args.recovery_skill or "").strip():
        print("notify-fault requires --recovery-skill for immediate retry", file=sys.stderr)
        return 2
    recovery_ok, recovery_reason = validate_fault_recovery_evidence(args)
    if not recovery_ok:
        print(
            f"notify-fault refused before automatic recovery evidence: {recovery_reason}",
            file=sys.stderr,
        )
        return 2
    existing_run = find_run(args.run_id)
    if not existing_run:
        print("notify-fault requires an existing run", file=sys.stderr)
        return 2
    try:
        chat_id = require_requested_original_chat(
            existing_run, config, str(args.chat_id or "")
        )
    except RuntimeError:
        print("notify-fault refused by run host/original-chat boundary", file=sys.stderr)
        return 2
    run = create_or_update_fault(
        run_id=args.run_id,
        chat_id=chat_id,
        stage=args.stage,
        fault=args.fault,
        suggested_action=args.suggested_action,
        failure_action=args.failure_action,
        retry_count=args.retry_count,
        completed_steps=args.completed_steps,
        evidence=args.evidence,
        recovery_skill=args.recovery_skill,
        host_machine=str(getattr(config, "submission_host_machine", "") or ""),
        recovery_attempts=int(getattr(args, "recovery_attempts", 0) or 0),
        recovery_actions=str(getattr(args, "recovery_actions", "") or ""),
        recovery_result=str(getattr(args, "recovery_result", "") or ""),
        unrepairable=bool(getattr(args, "unrepairable", False)),
    )
    pending = run.get("pending_decision") or {}
    if pending.get("last_message_id") and (
        pending.get("first_notified_at") or pending.get("last_notified_at")
    ):
        print(run["id"])
        return 0
    try:
        response = FeishuClient(config).send_card(
            chat_id,
            build_fault_decision_card(run),
            message_uuid=str(pending.get("message_uuid") or ""),
        )
        message_id = message_id_from_response(response)
        if not message_id:
            print("notify-fault sent but delivery message_id is missing", file=sys.stderr)
            return 2
        record_fault_notification(run["id"], message_id)
    except FeishuSendError:
        raise
    print(run["id"])
    return 0


def notify_confirmation(args: argparse.Namespace, config: Config) -> int:
    if not args.run_id:
        print("notify-confirmation requires an existing --run-id", file=sys.stderr)
        return 2
    current_skill = str(getattr(args, "current_skill", "") or "").strip()
    stage = str(getattr(args, "stage", "") or "").strip()
    question = str(getattr(args, "confirmation_question", "") or "").strip()
    action_summary = str(getattr(args, "confirmation_action", "") or "").strip()
    evidence = str(getattr(args, "evidence", "") or "").strip()
    if not stage or not current_skill or not question or not action_summary or not evidence:
        print(
            "notify-confirmation requires --stage, --current-skill, --confirmation-question, --confirmation-action, and --evidence",
            file=sys.stderr,
        )
        return 2
    existing_run = find_run(args.run_id)
    if not existing_run:
        print("notify-confirmation requires an existing run", file=sys.stderr)
        return 2
    try:
        require_requested_original_chat(existing_run, config, str(args.chat_id or ""))
        run = create_or_update_confirmation(
            args.run_id,
            stage,
            current_skill,
            question,
            action_summary,
            evidence,
        )
        pending = run.get("pending_decision") or {}
        api_decision, api_response = user_confirmation_api_decision(
            run, action_summary, config
        )
        if api_decision in {"confirm_continue", "cancel_operation"}:
            decided = record_decision_by_run_id(
                args.run_id,
                api_decision,
                api_response or "user_confirmation_api",
                "user-api",
                str(pending.get("decision_id") or ""),
            )
            if not decided:
                raise RuntimeError("Unable to record user API confirmation")
            print(decided["id"])
            return 0
        delivered = ensure_decision_card_delivered(run, config)
    except RuntimeError as exc:
        print(f"notify-confirmation refused: {exc}", file=sys.stderr)
        return 2
    print(delivered["id"])
    return 0


def record_review_notification(run_id: str, message_id: str) -> dict[str, Any] | None:
    if not message_id:
        raise ValueError("Review card delivery requires a message_id")

    def apply_notification(item: dict[str, Any]) -> None:
        pending = item.setdefault("pending_decision", {})
        notified_at = utc_now()
        if not pending.get("first_notified_at"):
            pending["first_notified_at"] = notified_at
        pending["last_notified_at"] = notified_at
        if message_id:
            pending["last_message_id"] = message_id

    return mutate_run(run_id, apply_notification)


def ensure_decision_card_delivered(run: dict[str, Any], config: Config) -> dict[str, Any]:
    pending = run.get("pending_decision") or {}
    if pending.get("status") != "waiting":
        return run
    if pending.get("first_notified_at"):
        return run
    try:
        chat_id = local_original_chat(run, config)
    except RuntimeError as exc:
        raise RuntimeError("interactive card refused by run host/original-chat boundary") from exc
    if pending.get("last_notified_at") and pending.get("last_message_id"):
        def migrate_first_delivery(item: dict[str, Any]) -> None:
            current = item.setdefault("pending_decision", {})
            if not current.get("first_notified_at"):
                current["first_notified_at"] = current["last_notified_at"]

        migrated = mutate_run(str(run.get("id") or ""), migrate_first_delivery)
        if migrated is None:
            raise RuntimeError("Unable to migrate the card delivery timestamp")
        return migrated

    if not pending.get("message_uuid"):
        def add_message_uuid(item: dict[str, Any]) -> None:
            current = item.setdefault("pending_decision", {})
            if not current.get("message_uuid"):
                current["message_uuid"] = uuid.uuid4().hex

        updated = mutate_run(str(run.get("id") or ""), add_message_uuid)
        if updated is None:
            raise RuntimeError("Unable to persist the interactive card message uuid")
        run = updated
        pending = run.get("pending_decision") or {}
    response = FeishuClient(config).send_card(
        chat_id,
        decision_card_for_run(run),
        message_uuid=str(pending.get("message_uuid") or ""),
    )
    message_id = message_id_from_response(response)
    if not message_id:
        raise RuntimeError("Interactive card delivery is missing message_id")
    if pending.get("kind") == "review_submit":
        delivered = record_review_notification(str(run.get("id") or ""), message_id)
    elif str(pending.get("kind") or "fault") in {"fault", "confirmation"}:
        delivered = record_fault_notification(str(run.get("id") or ""), message_id)
    else:
        raise RuntimeError("Unsupported pending decision kind")
    if delivered is None:
        raise RuntimeError("Unable to record interactive card delivery")
    return delivered


def notify_review(args: argparse.Namespace, config: Config) -> int:
    if not args.run_id:
        print("notify-review requires --run-id", file=sys.stderr)
        return 2
    run = find_run(args.run_id)
    if not run:
        print("notify-review requires an existing run", file=sys.stderr)
        return 2
    try:
        chat_id = stored_original_chat(
            run, config, str(getattr(args, "chat_id", "") or "")
        )
    except RuntimeError:
        print("notify-review refused by run host/original-chat boundary", file=sys.stderr)
        return 2
    pending = run.get("pending_decision") or {}
    if pending.get("status") == "waiting":
        if (
            pending.get("kind") == "review_submit"
            and not pending.get("first_notified_at")
            and not pending.get("last_notified_at")
            and str(pending.get("app_version") or "") == str(args.app_version or "")
            and str(pending.get("build_number") or "") == str(args.build_number or "")
            and int(pending.get("iap_count") or 0) == int(args.iap_count or 0)
            and str(pending.get("evidence") or "") == str(args.evidence or "")
            and len(pending.get("image_keys") or []) == 5
        ):
            try:
                ensure_decision_card_delivered(run, config)
            except RuntimeError as exc:
                print(f"notify-review delivery retry failed: {exc}", file=sys.stderr)
                return 2
            print(run["id"])
            return 0
        print("notify-review refused: another decision is still waiting", file=sys.stderr)
        return 2
    if not args.app_version or not args.build_number or args.iap_count != 14:
        print("notify-review requires --app-version, --build-number, and --iap-count 14", file=sys.stderr)
        return 2
    evidence = str(args.evidence or "")
    if "REVIEW_SCREENSHOTS=verified_5" not in evidence or "ITEMS_READY=15" not in evidence:
        print("notify-review evidence is incomplete", file=sys.stderr)
        return 2
    screenshots = [Path(value) for value in (args.screenshot or [])]
    if len(screenshots) != 5:
        print("notify-review requires exactly five --screenshot files", file=sys.stderr)
        return 2
    for path in screenshots:
        if not path.is_file() or path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            print(f"notify-review requires a valid PNG: {path}", file=sys.stderr)
            return 2

    client = FeishuClient(config)
    image_keys = [client.upload_image(path) for path in screenshots]
    updated = create_review_confirmation(
        run_id=run["id"],
        app_version=args.app_version,
        build_number=args.build_number,
        iap_count=args.iap_count,
        evidence=evidence,
        image_keys=image_keys,
    )
    try:
        ensure_decision_card_delivered(updated, config)
    except RuntimeError as exc:
        print(f"notify-review delivery failed: {exc}", file=sys.stderr)
        return 2
    print(run["id"])
    return 0


def record_auto_review_approval_cli(args: argparse.Namespace, config: Config) -> int:
    if not args.run_id:
        print("record-auto-review-approval requires --run-id", file=sys.stderr)
        return 2
    run = find_run(args.run_id)
    if not run:
        print("record-auto-review-approval requires an existing run", file=sys.stderr)
        return 2
    try:
        stored_original_chat(run, config, str(getattr(args, "chat_id", "") or ""))
    except RuntimeError:
        print("record-auto-review-approval refused by run host/original-chat boundary", file=sys.stderr)
        return 2
    screenshots = [Path(value) for value in (args.screenshot or [])]
    if len(screenshots) != 5:
        print("record-auto-review-approval requires exactly five screenshots", file=sys.stderr)
        return 2
    hashes: list[str] = []
    for path in screenshots:
        if not path.is_file() or path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            print(f"record-auto-review-approval requires a valid PNG: {path}", file=sys.stderr)
            return 2
        hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
    try:
        updated = record_automatic_review_approval(
            run_id=run["id"],
            app_version=str(args.app_version or ""),
            build_number=str(args.build_number or ""),
            iap_count=int(args.iap_count or 0),
            evidence=str(args.evidence or ""),
            screenshot_hashes=hashes,
        )
    except ValueError as exc:
        print(f"record-auto-review-approval refused: {exc}", file=sys.stderr)
        return 2
    if updated is None or not is_approved_review_submission(
        updated.get("review_submission_approval")
    ):
        print("record-auto-review-approval could not persist approval", file=sys.stderr)
        return 2
    print(run["id"])
    return 0


def record_review_submit_attempt_cli(args: argparse.Namespace, config: Config) -> int:
    run = find_run(str(args.run_id or ""))
    if not run:
        print("record-review-submit-attempt requires an existing run", file=sys.stderr)
        return 2
    try:
        stored_original_chat(run, config, str(getattr(args, "chat_id", "") or ""))
        updated = record_review_submit_attempt(
            run_id=run["id"],
            attempt_id=str(args.attempt_id or ""),
            decision_id=str(args.decision_id or ""),
            app_version=str(args.app_version or ""),
            build_number=str(args.build_number or ""),
            items_ready=int(args.items_ready or 0),
            status=str(args.attempt_status or ""),
        )
    except (RuntimeError, ValueError) as exc:
        print(f"record-review-submit-attempt refused: {exc}", file=sys.stderr)
        return 2
    attempt = (updated or {}).get("review_submit_attempt") or {}
    if (
        attempt.get("attempt_id") != args.attempt_id
        or attempt.get("decision_id") != args.decision_id
        or attempt.get("status") != args.attempt_status
    ):
        print("record-review-submit-attempt could not persist exact state", file=sys.stderr)
        return 2
    print(run["id"])
    return 0


def stop_run_after_decision_timeout(
    run_id: str, config: Config, timeout_seconds: int
) -> dict[str, Any]:
    send_timeout = False

    def apply_timeout(item: dict[str, Any]) -> None:
        nonlocal send_timeout
        pending = item.setdefault("pending_decision", {})
        if pending.get("status") == "answered":
            return
        if pending.get("status") != "expired" or pending.get("decision") != "timeout":
            expired_at = utc_now()
            pending["status"] = "expired"
            pending["decision"] = "timeout"
            pending["expired_at"] = expired_at
            pending["timeout_seconds"] = timeout_seconds
            item["status"] = "decision_timeout_stop"
            item.setdefault("events", []).append(
                {
                    "at": expired_at,
                    "status": "decision_timeout_stop",
                    "note": "等待用户回复超时，已停止整个流程",
                }
            )
        if not pending.get("timeout_notification_attempted_at"):
            pending["timeout_notification_attempted_at"] = utc_now()
            pending["timeout_message_uuid"] = str(
                pending.get("timeout_message_uuid") or uuid.uuid4().hex
            )
            send_timeout = True

    updated = mutate_run(run_id, apply_timeout)
    if updated is None:
        raise RuntimeError(f"Unable to stop timed-out run: {run_id}")
    pending = updated.get("pending_decision") or {}
    if pending.get("status") != "expired" or pending.get("decision") != "timeout":
        return updated
    if not send_timeout:
        return updated

    chat_id = str(updated.get("chat_id") or "")
    try:
        if not chat_id or is_daily_report_chat(
            chat_id, getattr(config, "daily_report_chat_id", "")
        ):
            raise RuntimeError("Timed-out run has no valid original chat_id")
        response = FeishuClient(config).send_card(
            chat_id,
            build_decision_timeout_card(updated),
            message_uuid=str(pending.get("timeout_message_uuid") or ""),
        )
        message_id = message_id_from_response(response)
        if not message_id:
            raise RuntimeError("Timeout card sent without a message_id")
    except Exception as exc:  # noqa: BLE001 - timeout is terminal even when notification fails.
        failure_type = type(exc).__name__

        def apply_timeout_failure(item: dict[str, Any]) -> None:
            current = item.setdefault("pending_decision", {})
            failed_at = utc_now()
            current["timeout_notification_failed_at"] = failed_at
            item.setdefault("events", []).append(
                {
                    "at": failed_at,
                    "status": "decision_timeout_notification_failed",
                    "note": f"等待回复超时卡片发送失败，流程保持停止：{failure_type}",
                }
            )

        failed = mutate_run(run_id, apply_timeout_failure)
        if failed is None:
            raise RuntimeError(f"Unable to record timeout card failure: {run_id}") from exc
        return failed

    def apply_timeout_notification(item: dict[str, Any]) -> None:
        current = item.setdefault("pending_decision", {})
        if current.get("timeout_notified_at"):
            return
        notified_at = utc_now()
        current["timeout_notified_at"] = notified_at
        current["timeout_message_id"] = message_id
        item.setdefault("events", []).append(
            {
                "at": notified_at,
                "status": "decision_timeout_notified",
                "note": "等待回复超时卡片已发送",
            }
        )

    persisted = mutate_run(run_id, apply_timeout_notification)
    if persisted is None:
        raise RuntimeError(f"Unable to record timeout card: {run_id}")
    return persisted


def decision_deadline(
    pending: dict[str, Any], timeout_seconds: int, fallback_deadline: float
) -> float:
    if timeout_seconds <= 0:
        return fallback_deadline
    waiting_started_at = str(
        pending.get("first_notified_at") or pending.get("requested_at") or ""
    )
    if not waiting_started_at:
        return fallback_deadline
    try:
        requested_deadline = (
            datetime.fromisoformat(waiting_started_at).timestamp() + timeout_seconds
        )
        # A persisted timestamp can be ahead of the local clock after clock correction.
        # Never let that extend this executor's configured waiting window.
        return min(requested_deadline, fallback_deadline)
    except ValueError:
        return fallback_deadline


def wait_decision(args: argparse.Namespace, config: Config) -> int:
    expected_kind = getattr(args, "decision_kind", "") or "fault"
    timeout_seconds = max(0, int(args.timeout_seconds))
    fallback_deadline: float | None = None
    while True:
        run = find_run(args.run_id)
        pending = (run or {}).get("pending_decision") or {}
        actual_kind = str(pending.get("kind") or "fault")
        if actual_kind != expected_kind:
            print(f"{expected_kind}-decision-required", file=sys.stderr)
            return 4
        if pending.get("status") == "answered":
            decision = str(pending.get("decision") or "")
            if decision in {
                "manual_continue",
                "retry_skill",
                "submit_review",
                "confirm_continue",
            }:
                print(decision)
                return 0
            if decision in {"stop", "do_not_submit", "cancel_operation"}:
                print(decision)
                return 2
        if pending.get("status") == "expired":
            stop_run_after_decision_timeout(args.run_id, config, timeout_seconds)
            print("timeout", file=sys.stderr)
            return 3
        if run and pending.get("status") == "waiting" and not pending.get("first_notified_at"):
            ensure_decision_card_delivered(run, config)
            fallback_deadline = None
            continue
        if fallback_deadline is None:
            fallback_deadline = time.time() + timeout_seconds
        if time.time() >= decision_deadline(pending, timeout_seconds, fallback_deadline):
            break
        time.sleep(args.poll_seconds)
    stop_run_after_decision_timeout(args.run_id, config, timeout_seconds)
    print("timeout", file=sys.stderr)
    return 3


def processed_message_path(message_id: str) -> Path:
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    return PROCESSED_MESSAGES_DIR / digest


def claim_message(message_id: str) -> bool:
    if not message_id:
        return False
    try:
        secure_create_file(processed_message_path(message_id))
    except FileExistsError:
        return False
    # ponytail: One marker per message is sufficient here; move to SQLite only if chat volume becomes high.
    return True


def release_message(message_id: str) -> None:
    if message_id:
        processed_message_path(message_id).unlink(missing_ok=True)


def parse_history_text(item: dict[str, Any]) -> str:
    content = ((item.get("body") or {}).get("content") or "").strip()
    if not content or content == "This message was recalled":
        return ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(parsed, dict):
        return str(parsed.get("text") or "").strip()
    return content


def fetch_chat_messages(config: Config, chat_id: str, page_size: int = 20) -> list[dict[str, Any]]:
    token = get_tenant_access_token(
        config.app_id,
        config.app_secret,
        retries=config.send_retries,
        timeout=config.send_timeout_seconds,
    )
    query = parse.urlencode(
        {
            "container_id_type": "chat",
            "container_id": chat_id,
            "page_size": str(page_size),
            "sort_type": "ByCreateTimeDesc",
        }
    )
    req = request.Request(
        f"https://open.feishu.cn/open-apis/im/v1/messages?{query}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with request.urlopen(req, timeout=config.send_timeout_seconds) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu message history error: {data}")
    return list(data.get("data", {}).get("items", []))


def process_polled_message(config: Config, chat_id: str, item: dict[str, Any]) -> str:
    if (item.get("sender") or {}).get("sender_type") == "app":
        return ""
    text = parse_history_text(item)
    if not text:
        return ""
    reply = handle_incoming_text(config, text, chat_id, source="feishu-poll")
    if not reply:
        return ""
    FeishuClient(config).send_text(chat_id, reply)
    return reply


def poll_once(config: Config, chat_ids: tuple[str, ...] | list[str]) -> int:
    handled = 0
    for chat_id in chat_ids:
        items = fetch_chat_messages(config, chat_id)
        for item in reversed(items):
            message_id = str(item.get("message_id") or "")
            if not message_id or not claim_message(message_id):
                continue
            try:
                reply = process_polled_message(config, chat_id, item)
            except Exception:
                release_message(message_id)
                raise
            if reply:
                handled += 1
    return handled


def poll_messages(args: argparse.Namespace, config: Config) -> int:
    chat_ids = tuple(args.poll_chat_id) or config.poll_chat_ids
    if not chat_ids:
        print("poll requires --chat-id or FEISHU_POLL_CHAT_IDS", file=sys.stderr)
        return 2
    if args.once:
        print(f"handled={poll_once(config, chat_ids)}")
        return 0
    safe_print(f"Feishu poller watching {', '.join(chat_ids)} every {args.poll_interval_seconds}s")
    while True:
        try:
            handled = poll_once(config, chat_ids)
            if handled:
                safe_print(f"poll handled={handled}")
        except Exception as exc:  # noqa: BLE001 - poller should keep running.
            safe_print(f"[poll_error] {type(exc).__name__}: {exc}", file=sys.stderr)
        time.sleep(args.poll_interval_seconds)


def serve(args: argparse.Namespace, config: Config) -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    server = ReusableThreadingHTTPServer(
        (args.host, args.port), configured_http_handler(config)
    )
    safe_print(f"Feishu bot listening on http://{args.host}:{args.port}/feishu/events")
    safe_print("Health check: /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        safe_print("\nStopping Feishu bot")
    finally:
        server.server_close()
    return 0


def serve_ws(_args: argparse.Namespace, config: Config) -> int:
    if not config.app_id or not config.app_secret:
        print("ws requires FEISHU_APP_ID and FEISHU_APP_SECRET", file=sys.stderr)
        return 2
    try:
        import lark_oapi as lark
        from lark_oapi import EventDispatcherHandler, ws
    except ModuleNotFoundError:
        print("ws requires lark-oapi. Install with: python -m pip install lark-oapi", file=sys.stderr)
        return 2

    patch_lark_ws_card_dispatch()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    handler = (
        EventDispatcherHandler.builder(config.encrypt_key, config.verification_token)
        .register_p2_im_message_receive_v1(configured_ws_message_handler(config))
        .register_p2_card_action_trigger(configured_ws_card_handler(config))
        .build()
    )
    safe_print("Feishu bot websocket listening for im.message.receive_v1 and card.action.trigger")
    client = ws.Client(
        config.app_id,
        config.app_secret,
        log_level=lark.LogLevel.INFO,
        event_handler=handler,
    )
    client.start()
    return 0


def safe_print(*args: Any, file: Any = sys.stdout) -> None:
    try:
        print(*args, file=file, flush=True)
    except (BrokenPipeError, OSError):
        pass


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Run the Feishu submission bot.")
    parser.add_argument(
        "mode",
        nargs="?",
        default="serve",
        choices=(
            "serve",
            "ws",
            "poll",
            "notify-fault",
            "notify-confirmation",
            "notify-review",
            "record-auto-review-approval",
            "record-review-submit-attempt",
            "wait-decision",
        ),
        help="serve starts the HTTP bot; ws starts Feishu SDK long-connection callbacks; poll reads Feishu history as a control fallback; notify-fault sends a last-resort fault card; notify-confirmation asks the user API first and falls back to a user-decision card only when needed; notify-review keeps the optional legacy review card; record-auto-review-approval persists the unattended self-check authorization; record-review-submit-attempt persists the single submit click state; wait-decision waits for the selected decision.",
    )
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--run-id", default=os.getenv("SUBMISSION_RUN_ID", ""))
    parser.add_argument("--chat-id", default=os.getenv("SUBMISSION_CHAT_ID", ""))
    parser.add_argument("--stage", default="未知阶段")
    parser.add_argument("--fault", default="未填写故障")
    parser.add_argument("--suggested-action", default="请判断停止流程或人工处理后继续")
    parser.add_argument("--failure-action", default="未填写失败动作")
    parser.add_argument("--retry-count", type=int, default=0)
    parser.add_argument("--completed-steps", default="未填写已完成步骤")
    parser.add_argument("--evidence", default="未填写当前证据")
    parser.add_argument("--recovery-skill", default="")
    parser.add_argument("--recovery-attempts", type=int, default=0)
    parser.add_argument("--recovery-actions", default="")
    parser.add_argument(
        "--recovery-result", choices=("", "exhausted", "unrepairable"), default=""
    )
    parser.add_argument("--unrepairable", action="store_true")
    parser.add_argument(
        "--decision-kind", choices=("fault", "confirmation", "review_submit"), default=""
    )
    parser.add_argument("--current-skill", default="")
    parser.add_argument("--confirmation-question", default="")
    parser.add_argument("--confirmation-action", default="")
    parser.add_argument("--app-version", default="")
    parser.add_argument("--build-number", default="")
    parser.add_argument("--iap-count", type=int, default=0)
    parser.add_argument("--screenshot", action="append", default=[])
    parser.add_argument("--attempt-id", default="")
    parser.add_argument("--decision-id", default="")
    parser.add_argument("--items-ready", type=int, default=0)
    parser.add_argument(
        "--attempt-status",
        choices=("", "prepared", "clicking", "result_unknown", "verified"),
        default="",
    )
    parser.add_argument("--timeout-seconds", type=int, default=DECISION_TIMEOUT_SECONDS)
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
    )
    parser.add_argument("--poll-chat-id", action="append", default=[])
    args = parser.parse_args()
    harden_runtime_metadata()
    config = load_config()
    if args.host is None:
        args.host = config.host
    if args.port is None:
        args.port = config.port
    if args.poll_interval_seconds is None:
        args.poll_interval_seconds = config.poll_interval_seconds

    if args.mode == "notify-fault":
        return notify_fault(args, config)
    if args.mode == "notify-confirmation":
        return notify_confirmation(args, config)
    if args.mode == "notify-review":
        return notify_review(args, config)
    if args.mode == "record-auto-review-approval":
        return record_auto_review_approval_cli(args, config)
    if args.mode == "record-review-submit-attempt":
        return record_review_submit_attempt_cli(args, config)
    if args.mode == "wait-decision":
        return wait_decision(args, config)
    if args.mode == "poll":
        return poll_messages(args, config)
    if args.mode == "ws":
        return serve_ws(args, config)
    return serve(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
