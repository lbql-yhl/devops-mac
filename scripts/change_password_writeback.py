#!/usr/bin/env python3
"""Persist an Apple-accepted password without exposing its value."""

from __future__ import annotations

import hashlib
from typing import Any


PASSWORD_LABEL = "修改后的密码："


def _write_password_value(api: Any, page_title: str, password: str) -> dict[str, object]:
    """Write one password-field value and verify the whole section is stable."""
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    if "\n" in password or "\r" in password:
        raise ValueError("password must be one line")

    before = api.read_section(page_title, "账号信息")
    before_lines = before.splitlines()
    target_indexes = [
        index for index, line in enumerate(before_lines) if line.startswith(PASSWORD_LABEL)
    ]
    if len(target_indexes) != 1:
        raise RuntimeError(
            f"Expected exactly one field {PASSWORD_LABEL!r}; found {len(target_indexes)}"
        )
    target_index = target_indexes[0]

    api.set_field(
        page_title,
        "账号信息",
        PASSWORD_LABEL,
        password,
        replace_existing=True,
    )

    after = api.read_section(page_title, "账号信息")
    after_lines = after.splitlines()
    if len(after_lines) != len(before_lines):
        raise RuntimeError("Notion account section changed shape during password writeback")
    for index, (before_line, after_line) in enumerate(zip(before_lines, after_lines)):
        if index == target_index:
            continue
        if before_line != after_line:
            raise RuntimeError("Unrelated Notion account field changed during password writeback")

    readback = api.read_field(page_title, "账号信息", PASSWORD_LABEL)
    if readback != password:
        raise RuntimeError("Notion modified-password readback mismatch")

    encoded = password.encode("utf-8")
    return {
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def write_accepted_password(api: Any, page_title: str, password: str) -> dict[str, object]:
    """Write and independently verify a non-empty accepted/preflight value."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be non-empty")
    return _write_password_value(api, page_title, password)


def restore_previous_password(
    api: Any,
    page_title: str,
    previous_password: str,
) -> dict[str, object]:
    """Restore the exact preflight value, including an intentionally blank field."""
    return _write_password_value(api, page_title, previous_password)
