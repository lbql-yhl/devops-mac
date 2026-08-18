#!/usr/bin/env python3
"""Shared Feishu OpenAPI sender for the submission workflow."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
RETRYABLE_CODES = {99991400, 99991401, 99991402, 99991403}


class FeishuSendError(RuntimeError):
    """Raised when Feishu send fails after retries."""

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def network_precheck(host: str = "open.feishu.cn", port: int = 443, timeout: float = 5.0) -> dict[str, Any]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "host": host, "port": port}
    except Exception as exc:  # noqa: BLE001 - returned to caller for operations detail.
        return {"ok": False, "host": host, "port": port, "error": f"{type(exc).__name__}: {exc}"}


def _should_retry(exc: Exception | None, response: dict[str, Any] | None) -> bool:
    if exc is not None:
        return isinstance(exc, (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError))
    if not response:
        return False
    code = response.get("code")
    return isinstance(code, int) and code in RETRYABLE_CODES


def post_json(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: int = 20,
    retries: int = 3,
    backoff_seconds: float = 1.0,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        response: dict[str, Any] | None = None
        caught: Exception | None = None
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                response = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            response = {"code": exc.code, "msg": error_body[:1000], "http_status": exc.code}
        except Exception as exc:  # noqa: BLE001 - classified below for retry and detail.
            caught = exc

        if response is not None and response.get("code", 0) == 0:
            if attempts:
                response["_retry"] = {"attempt": attempt, "previous_attempts": attempts}
            return response

        attempt_detail = {"attempt": attempt}
        if response is not None:
            attempt_detail["response"] = response
        if caught is not None:
            attempt_detail["error"] = f"{type(caught).__name__}: {caught}"
        attempts.append(attempt_detail)

        if attempt >= retries or not _should_retry(caught, response):
            detail = {"url": url, "attempts": attempts}
            if caught is not None:
                raise FeishuSendError(f"{type(caught).__name__}: {caught}", detail=detail) from caught
            raise FeishuSendError(f"Feishu API returned non-zero code: {response}", detail=detail)

        time.sleep(backoff_seconds * attempt)

    raise FeishuSendError("Feishu send failed without response", detail={"url": url, "attempts": attempts})


def get_tenant_access_token(app_id: str, app_secret: str, *, retries: int = 3, timeout: int = 20) -> str:
    data = post_json(
        TOKEN_URL,
        {"app_id": app_id, "app_secret": app_secret},
        {"Content-Type": "application/json; charset=utf-8"},
        retries=retries,
        timeout=timeout,
    )
    token = data.get("tenant_access_token")
    if not token:
        raise FeishuSendError(
            f"failed to get tenant_access_token: code={data.get('code')} msg={data.get('msg')}",
            detail={"response": data},
        )
    return str(token)


def send_text_message(
    app_id: str,
    app_secret: str,
    chat_id: str,
    text: str,
    *,
    retries: int = 3,
    timeout: int = 20,
) -> dict[str, Any]:
    precheck = network_precheck(timeout=min(float(timeout), 5.0))
    if not precheck["ok"]:
        raise FeishuSendError("Feishu network precheck failed", detail={"precheck": precheck})
    token = get_tenant_access_token(app_id, app_secret, retries=retries, timeout=timeout)
    response = post_json(
        MESSAGE_URL,
        {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        retries=retries,
        timeout=timeout,
    )
    response.setdefault("_precheck", precheck)
    return response


def send_post_message(
    app_id: str,
    app_secret: str,
    chat_id: str,
    title: str,
    lines: list[list[dict[str, Any]]],
    *,
    retries: int = 3,
    timeout: int = 20,
) -> dict[str, Any]:
    precheck = network_precheck(timeout=min(float(timeout), 5.0))
    if not precheck["ok"]:
        raise FeishuSendError("Feishu network precheck failed", detail={"precheck": precheck})
    token = get_tenant_access_token(app_id, app_secret, retries=retries, timeout=timeout)
    content = {"zh_cn": {"title": title, "content": lines}}
    response = post_json(
        MESSAGE_URL,
        {
            "receive_id": chat_id,
            "msg_type": "post",
            "content": json.dumps(content, ensure_ascii=False),
        },
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        retries=retries,
        timeout=timeout,
    )
    response.setdefault("_precheck", precheck)
    return response


def send_interactive_card(
    app_id: str,
    app_secret: str,
    chat_id: str,
    card: dict[str, Any],
    *,
    message_uuid: str = "",
    retries: int = 3,
    timeout: int = 20,
) -> dict[str, Any]:
    precheck = network_precheck(timeout=min(float(timeout), 5.0))
    if not precheck["ok"]:
        raise FeishuSendError("Feishu network precheck failed", detail={"precheck": precheck})
    token = get_tenant_access_token(app_id, app_secret, retries=retries, timeout=timeout)
    body = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }
    if message_uuid:
        body["uuid"] = message_uuid
    response = post_json(
        MESSAGE_URL,
        body,
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        retries=retries,
        timeout=timeout,
    )
    response.setdefault("_precheck", precheck)
    return response


def _unique_texts(values: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def message_id_from_response(response: dict[str, Any] | None) -> str:
    if not isinstance(response, dict):
        return ""
    data = response.get("data")
    if isinstance(data, dict):
        for key in ("message_id", "messageId"):
            value = data.get(key)
            if value:
                return str(value)
    for key in ("message_id", "messageId"):
        value = response.get(key)
        if value:
            return str(value)
    return ""


def _target_delivery_check(chat_id: str, item: dict[str, Any]) -> dict[str, Any]:
    response = item.get("response") if "response" in item else item
    status = str(item.get("status") or "")
    if status and status != "sent":
        return {
            "chat_id": chat_id,
            "status": "failed",
            "reason": status,
            "error": item.get("error"),
            "detail": item.get("detail"),
        }
    if not isinstance(response, dict):
        return {"chat_id": chat_id, "status": "failed", "reason": "missing_response"}
    code = response.get("code")
    if code != 0:
        return {"chat_id": chat_id, "status": "failed", "reason": "nonzero_code", "code": code, "msg": response.get("msg")}
    message_id = message_id_from_response(response)
    if not message_id:
        return {"chat_id": chat_id, "status": "failed", "reason": "missing_message_id", "code": code}
    return {"chat_id": chat_id, "status": "checked", "code": code, "message_id": message_id}


def verify_post_message_delivery(
    expected_chat_ids: list[str] | tuple[str, ...] | set[str],
    send_response: dict[str, Any] | None,
    *,
    previously_sent_chat_ids: list[str] | tuple[str, ...] | set[str] | None = None,
) -> dict[str, Any]:
    expected = _unique_texts(expected_chat_ids)
    previous = _unique_texts(previously_sent_chat_ids)
    check: dict[str, Any] = {
        "status": "failed",
        "expected_chat_ids": expected,
        "previously_sent_chat_ids": previous,
        "checked_chat_ids": list(previous),
        "missing_chat_ids": [],
        "targets": [],
    }
    if not expected:
        check.update({"status": "skipped", "reason": "no_expected_chat_ids"})
        return check
    if not isinstance(send_response, dict):
        check.update({"reason": "missing_send_response", "missing_chat_ids": expected})
        return check
    if send_response.get("dry_run"):
        check.update({"status": "dry_run", "reason": "dry_run", "missing_chat_ids": []})
        return check

    current_expected = [chat_id for chat_id in expected if chat_id not in previous]
    raw_targets = send_response.get("targets")
    if isinstance(raw_targets, list):
        for raw_target in raw_targets:
            if not isinstance(raw_target, dict):
                continue
            chat_id = str(raw_target.get("chat_id") or "").strip()
            check["targets"].append(_target_delivery_check(chat_id, raw_target))
    else:
        chat_id = current_expected[0] if len(current_expected) == 1 else (expected[0] if len(expected) == 1 else "")
        check["targets"].append(_target_delivery_check(chat_id, send_response))

    checked = set(previous)
    failed_targets: list[dict[str, Any]] = []
    for target in check["targets"]:
        chat_id = str(target.get("chat_id") or "").strip()
        if target.get("status") == "checked" and chat_id:
            checked.add(chat_id)
        elif chat_id in expected or not chat_id:
            failed_targets.append(target)

    missing = [chat_id for chat_id in expected if chat_id not in checked]
    check["checked_chat_ids"] = [chat_id for chat_id in expected if chat_id in checked]
    check["missing_chat_ids"] = missing
    if missing:
        check["reason"] = "missing_expected_chat_ids"
    elif failed_targets:
        check["reason"] = "target_delivery_failed"
    else:
        check["status"] = "checked"
        check.pop("reason", None)
    if failed_targets:
        check["failed_targets"] = failed_targets
    return check


def text_node(text: str, *, bold: bool = False) -> dict[str, Any]:
    node: dict[str, Any] = {"tag": "text", "text": text}
    if bold:
        node["style"] = ["bold"]
    return node


def is_visible_heading(line: str, *, first_nonempty: bool = False) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if first_nonempty:
        return True
    if stripped[0].isdigit() and ". " in stripped[:4]:
        return True
    return stripped.startswith("- ") and stripped.endswith("：")


def body_to_post_lines(body: str, *, bold_headings: bool = True) -> list[list[dict[str, Any]]]:
    lines: list[list[dict[str, Any]]] = []
    seen_nonempty = False
    for raw_line in body.splitlines():
        text = raw_line.rstrip()
        if not text:
            lines.append([text_node(" ")])
            continue
        first_nonempty = not seen_nonempty
        seen_nonempty = True
        lines.append([text_node(text, bold=bold_headings and is_visible_heading(text, first_nonempty=first_nonempty))])
    return lines or [[text_node("（空消息）")]]
