#!/usr/bin/env python3
"""处理 Apple Account 登录后的 iPhone passcode 提示并等待账号页面。"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, List, Optional, Tuple

from find_system_settings_general import (
    AX,
    APPLE_ACCOUNT_TEXT,
    DONT_MERGE_TEXTS,
    ENTER_PASSCODE_LATER_TEXT,
    IPHONE_PASSCODE_PROMPT_TEXT,
    OPERATION_TIMEOUT_SECONDS,
    NSRunningApplication,
    WORKFLOW_RENDER_INTERVAL_SECONDS,
    activate,
    current_search_roots,
    copy_attribute,
    describe_element,
    find_pressable_text_candidate_any,
    find_sidebar_candidate,
    get_running_system_settings,
    iter_accessibility_tree,
    launch_system_settings,
    tree_contains_text,
)


DONT_KNOW_PASSCODE_TEXTS = (
    "Don't know passcode?",
    "Don’t know passcode?",
)
POST_LOGIN_WAIT_ATTEMPTS = int(
    OPERATION_TIMEOUT_SECONDS / WORKFLOW_RENDER_INTERVAL_SECONDS
)
SYSTEM_SETTINGS_CLOSE_ATTEMPTS = 20
SYSTEM_SETTINGS_CLOSE_INTERVAL_SECONDS = 0.25


def force_close_system_settings() -> bool:
    """关闭当前 System Settings；正常退出无效时强制终止。"""
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        "com.apple.systempreferences"
    )
    if not apps:
        return True
    for app in apps:
        try:
            app.terminate()
        except Exception:
            pass
    for _ in range(SYSTEM_SETTINGS_CLOSE_ATTEMPTS):
        remaining = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            "com.apple.systempreferences"
        )
        if not remaining:
            return True
        time.sleep(SYSTEM_SETTINGS_CLOSE_INTERVAL_SECONDS)
    remaining = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        "com.apple.systempreferences"
    )
    for app in remaining:
        try:
            app.forceTerminate()
        except Exception:
            pass
    for _ in range(SYSTEM_SETTINGS_CLOSE_ATTEMPTS):
        if not NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            "com.apple.systempreferences"
        ):
            return True
        time.sleep(SYSTEM_SETTINGS_CLOSE_INTERVAL_SECONDS)
    return False


def wait_for_email_after_reopen(expected_email: str) -> bool:
    """重新打开 System Settings 并确认同一邮箱仍显示。"""
    launch_system_settings()
    account_navigation_attempted = False
    for attempt in range(POST_LOGIN_WAIT_ATTEMPTS):
        try:
            pid = get_running_system_settings().processIdentifier()
        except RuntimeError:
            pid = None
        if pid is not None:
            roots = current_search_roots(
                pid,
                auto_handle_mac_password=False,
                auto_handle_security_prompts=False,
            )
            if tree_contains_casefold(roots, expected_email):
                return True
            # System Settings 重开后通常停在 General；先进入左侧当前账号，
            # 再读取 Apple Account 页面中的邮箱。
            if not account_navigation_attempted:
                account_candidate = find_sidebar_candidate(roots, APPLE_ACCOUNT_TEXT)
                if account_candidate is not None and account_candidate.action_info[
                    "enabled"
                ] is not False:
                    error = activate(account_candidate)
                    if error == AX.kAXErrorSuccess:
                        account_navigation_attempted = True
                    elif error not in (
                        AX.kAXErrorAttributeUnsupported,
                        AX.kAXErrorCannotComplete,
                    ):
                        return False
            if not account_navigation_attempted:
                # 只有无法直接定位账号入口时才调用随机弹窗处理器，避免邮箱
                # 已经可见时被不相关的安全提示扫描阻塞。
                current_search_roots(pid)
        if attempt < POST_LOGIN_WAIT_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    return False


def finalize_after_email_confirmation(pid: int, expected_email: str) -> int:
    """首次邮箱确认后关闭、重开复核，再关闭设置完成流程。"""
    print("已确认 Apple Account 邮箱，正在关闭 System Settings…", flush=True)
    if not force_close_system_settings():
        print("首次确认邮箱后无法关闭 System Settings。", file=sys.stderr)
        return 5
    if not wait_for_email_after_reopen(expected_email):
        print(
            "重新打开 System Settings 后未再次确认 Apple Account 邮箱 "
            "（超时：{}秒）。".format(OPERATION_TIMEOUT_SECONDS),
            file=sys.stderr,
        )
        return 5
    print("已重新打开并再次确认 Apple Account 邮箱。", flush=True)
    print("Apple Account 登录流程完成，保留 System Settings 打开。")
    return 0


def tree_contains_casefold(roots: List[Any], target: str) -> bool:
    """大小写不敏感地查找账号邮箱等最终页面文本。"""
    wanted = target.casefold()
    for element, _ in iter_accessibility_tree(roots):
        for attribute in (
            AX.kAXTitleAttribute,
            AX.kAXValueAttribute,
            AX.kAXDescriptionAttribute,
            AX.kAXHelpAttribute,
            AX.kAXIdentifierAttribute,
        ):
            value = copy_attribute(element, attribute)
            if value is not None and wanted in str(value).casefold():
                return True
    return False


def click_text_with_wait(
    pid: int,
    targets: Tuple[str, ...],
    error_message: str,
) -> int:
    """等待并点击一个可用的文本控件。"""
    for attempt in range(POST_LOGIN_WAIT_ATTEMPTS):
        candidate = find_pressable_text_candidate_any(
            current_search_roots(
                pid,
                auto_handle_mac_password=False,
                auto_handle_security_prompts=False,
            ),
            targets,
        )
        if candidate is not None:
            error = activate(candidate)
            if error != AX.kAXErrorSuccess:
                # 弹窗刚出现或刚切换时，AX 可能短暂返回
                # AttributeUnsupported/CannotComplete；继续读取同一页面，
                # 不要把这个瞬时状态当作不可恢复错误。
                if error in (
                    AX.kAXErrorAttributeUnsupported,
                    AX.kAXErrorCannotComplete,
                ):
                    if attempt < POST_LOGIN_WAIT_ATTEMPTS - 1:
                        time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
                        continue
                print(
                    "{}，AXError={}".format(error_message, error),
                    file=sys.stderr,
                )
                return 4
            return 0
        if attempt < POST_LOGIN_WAIT_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    print(
        "{}（超时：{}秒）。".format(error_message, OPERATION_TIMEOUT_SECONDS),
        file=sys.stderr,
    )
    return 5


def handle_post_login_prompts(pid: int, expected_email: str) -> int:
    """等待最终邮箱；随机安全提示由全局搜索钩子自动处理。"""
    if not expected_email:
        print("缺少用于最终确认的 Apple Account email。", file=sys.stderr)
        return 6

    for attempt in range(POST_LOGIN_WAIT_ATTEMPTS):
        roots = current_search_roots(pid)
        if tree_contains_casefold(roots, expected_email):
            return finalize_after_email_confirmation(pid, expected_email)
        if attempt < POST_LOGIN_WAIT_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    print(
        "已等待并处理随机安全提示，但未等到 Apple Account 邮箱页面 "
        "（超时：{}秒）。".format(OPERATION_TIMEOUT_SECONDS),
        file=sys.stderr,
    )
    return 5


def main() -> int:
    parser = argparse.ArgumentParser(description="Handle Apple Account post-login prompts")
    parser.add_argument("--email-var", default="APPLE_ACCOUNT_EMAIL")
    args = parser.parse_args()
    expected_email = os.environ.get(args.email_var, "").strip()
    if not expected_email:
        expected_email = input("Apple Account email: ").strip()
    trusted = AX.AXIsProcessTrustedWithOptions(
        {AX.kAXTrustedCheckOptionPrompt: True}
    )
    if not trusted:
        print("当前进程没有辅助功能权限。", file=sys.stderr)
        return 1
    launch_system_settings()
    time.sleep(2)
    pid = get_running_system_settings().processIdentifier()
    return handle_post_login_prompts(pid, expected_email)


if __name__ == "__main__":
    raise SystemExit(main())
