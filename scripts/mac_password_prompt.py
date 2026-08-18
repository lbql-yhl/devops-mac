#!/usr/bin/env python3
"""独立处理 Apple Account 的 macOS 密码授权弹窗。"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections.abc import Mapping
from typing import Any, List, Optional

from find_system_settings_general import (
    AX,
    LOGIN_CONTINUE_TEXT,
    MINIMUM_GUI_SETTLE_SECONDS,
    OPERATION_TIMEOUT_SECONDS,
    TEXT_FIELD_ROLES,
    WORKFLOW_RENDER_ATTEMPTS,
    WORKFLOW_RENDER_INTERVAL_SECONDS,
    activate,
    copy_attribute,
    current_search_roots,
    describe_element,
    element_contains_text,
    find_enabled_pressable_text_candidates,
    get_running_system_settings,
    launch_system_settings,
    set_login_field,
    tree_contains_text,
)


MAC_PASSWORD_PROMPT_TEXT = "Enter Mac Password"
APPLE_ACCOUNT_CHANGES_PROMPT_TEXT = "Apple Account wants to make changes."
MAC_PASSWORD_PROMPT_TEXTS = (
    MAC_PASSWORD_PROMPT_TEXT,
    APPLE_ACCOUNT_CHANGES_PROMPT_TEXT,
)


def guest_password(*, environ: Mapping[str, str] | None = None) -> str:
    """Read the short-lived guest runtime password without host dependencies."""
    runtime_environment = os.environ if environ is None else environ
    value = str(runtime_environment.get("SUBMISSION_GUEST_PASSWORD", "")).strip()
    if not value:
        raise RuntimeError(
            "missing required guest runtime setting: SUBMISSION_GUEST_PASSWORD"
        )
    if re.fullmatch(r"[0-9]{4}", value) is None:
        raise RuntimeError(
            "invalid guest runtime setting: SUBMISSION_GUEST_PASSWORD must be "
            "exactly four ASCII digits"
        )
    return value


def find_mac_password_field(roots: List[Any]) -> Optional[Any]:
    """查找安全弹窗中的密码框，排除背景搜索框和禁用控件。"""
    unique_candidates: List[Any] = []
    for element, _ in _iter_tree(roots):
        info = describe_element(element)
        if info["role"] not in TEXT_FIELD_ROLES:
            continue
        if info["enabled"] is False or info["subrole"] == "AXSearchField":
            continue
        unique_candidates.append(element)
    secure = [
        element
        for element in unique_candidates
        if describe_element(element)["role"] == "AXSecureTextField"
    ]
    if len(secure) == 1:
        return secure[0]
    labeled = [
        element
        for element in unique_candidates
        if element_contains_text(describe_element(element), "Password")
    ]
    if len(labeled) == 1:
        return labeled[0]
    return unique_candidates[0] if len(unique_candidates) == 1 else None


def mac_password_readback_matches(
    rendered_value: Any,
    field_info: dict[str, Any],
    expected_password: str | None = None,
) -> bool:
    """Validate a secure readback without assuming its mask glyph."""
    password = guest_password() if expected_password is None else expected_password
    rendered_text = str(rendered_value or "")
    if rendered_text == password:
        return True
    if len(rendered_text) != len(password):
        return False
    mask_characters = {"•", "●", "*", "·"}
    if set(rendered_text) <= mask_characters:
        return True
    return (
        field_info.get("role") == "AXSecureTextField"
        or field_info.get("subrole") == "AXSecureTextField"
    )


def _iter_tree(roots: List[Any]):
    """通过通用 AX 遍历器读取当前弹窗的控件。"""
    from find_system_settings_general import iter_accessibility_tree

    return iter_accessibility_tree(roots)


def handle_mac_password_prompt(pid: int) -> Optional[int]:
    """若授权弹窗存在则输入运行时客机密码并复验关闭。"""
    roots = current_search_roots(pid, auto_handle_mac_password=False)
    visible_prompts = [
        text for text in MAC_PASSWORD_PROMPT_TEXTS if tree_contains_text(roots, text)
    ]
    if not visible_prompts:
        return None
    if len(visible_prompts) != 1:
        print("Apple Account Mac 密码授权弹窗归属不唯一。", file=sys.stderr)
        return 5
    prompt_text = visible_prompts[0]
    submit_labels = (
        ("Allow",)
        if prompt_text == APPLE_ACCOUNT_CHANGES_PROMPT_TEXT
        else (LOGIN_CONTINUE_TEXT,)
    )

    password_field = find_mac_password_field(roots)
    if password_field is None:
        print("检测到 Apple Account Mac 密码授权，但密码框不唯一。", file=sys.stderr)
        return 5
    password = guest_password()
    password_error = set_login_field(password_field, password)
    if password_error != AX.kAXErrorSuccess:
        print("写入 Mac 密码失败，AXError={}".format(password_error), file=sys.stderr)
        return 4
    time.sleep(MINIMUM_GUI_SETTLE_SECONDS)

    current_roots = current_search_roots(pid, auto_handle_mac_password=False)
    refreshed_field = find_mac_password_field(current_roots)
    rendered_value = (
        copy_attribute(refreshed_field, AX.kAXValueAttribute)
        if refreshed_field is not None
        else None
    )
    refreshed_info = (
        describe_element(refreshed_field) if refreshed_field is not None else {}
    )
    if not mac_password_readback_matches(
        rendered_value, refreshed_info, expected_password=password
    ):
        print("Mac 密码字段四位回读不匹配。", file=sys.stderr)
        return 5

    submit_candidate = None
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        current_roots = current_search_roots(pid, auto_handle_mac_password=False)
        candidates = find_enabled_pressable_text_candidates(
            current_roots,
            submit_labels,
        )
        if len(candidates) == 1:
            submit_candidate = candidates[0]
            break
        if len(candidates) > 1:
            print("Mac 密码授权弹窗提交按钮不唯一。", file=sys.stderr)
            return 5
        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    if submit_candidate is None:
        print(
            "Mac 密码授权弹窗未找到唯一可用的提交按钮（超时：{}秒）。".format(
                OPERATION_TIMEOUT_SECONDS
            ),
            file=sys.stderr,
        )
        return 5
    error = activate(submit_candidate)
    if error != AX.kAXErrorSuccess:
        print("提交 Mac 密码失败，AXError={}".format(error), file=sys.stderr)
        return 4
    time.sleep(MINIMUM_GUI_SETTLE_SECONDS)
    final_roots = current_search_roots(pid, auto_handle_mac_password=False)
    if any(tree_contains_text(final_roots, text) for text in MAC_PASSWORD_PROMPT_TEXTS):
        print("Mac 密码授权弹窗在提交后仍可见。", file=sys.stderr)
        return 5
    print("CONFIGURED_GUEST_PASSWORD_CONTEXT=gui_auth")
    print("CONFIGURED_GUEST_PASSWORD_RESULT=verified")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Handle Apple Account Mac password authorization")
    parser.parse_args()
    trusted = AX.AXIsProcessTrustedWithOptions(
        {AX.kAXTrustedCheckOptionPrompt: True}
    )
    if not trusted:
        print("当前进程没有辅助功能权限。", file=sys.stderr)
        return 1
    launch_system_settings()
    time.sleep(MINIMUM_GUI_SETTLE_SECONDS)
    pid = get_running_system_settings().processIdentifier()
    result = handle_mac_password_prompt(pid)
    if result is None:
        print("当前没有检测到 Apple Account Mac 密码授权弹窗。", file=sys.stderr)
        return 5
    return result


if __name__ == "__main__":
    raise SystemExit(main())
