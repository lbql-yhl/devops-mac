#!/usr/bin/env python3
"""查找并导航到 macOS“系统设置”中的指定侧边栏项。

系统设置的 SwiftUI 侧边栏在部分 macOS 版本中将项目暴露为
AXRow/AXCell，而不是 AXButton。此类 AXRow 通常没有 AXPress，
但 AXSelected 属性可写；本脚本会同时支持这两种导航方式。

默认只打印候选节点。传入 --press（或 --activate）后才会执行导航。

内置工作流：
  --workflow apple-account-login-security --press
会依次打开“Apple Account”和“Sign-In & Security”。
"""

from __future__ import annotations

import argparse
import getpass
import html
import importlib
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Deque, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def load_pyobjc_dependencies() -> Tuple[Any, Any]:
    """缺少 PyObjC 时自动安装本脚本所需的 macOS 框架。"""
    try:
        import ApplicationServices as application_services
        from AppKit import NSRunningApplication as running_application

        return application_services, running_application
    except ModuleNotFoundError as import_error:
        if import_error.name not in {"ApplicationServices", "AppKit"}:
            raise
        print(
            "缺少 PyObjC 依赖，正在自动安装 ApplicationServices 和 AppKit…",
            file=sys.stderr,
        )
        packages = (
            "pyobjc-framework-ApplicationServices",
            "pyobjc-framework-Cocoa",
        )
        command = [sys.executable, "-m", "pip", "install", "--user", *packages]
        install = subprocess.run(command, check=False)
        if install.returncode != 0:
            # 虚拟环境通常不允许 --user，改用当前解释器环境重试。
            install = subprocess.run(
                [sys.executable, "-m", "pip", "install", *packages],
                check=False,
            )
        if install.returncode != 0:
            raise RuntimeError(
                "PyObjC 自动安装失败，请手动执行：python3 -m pip install "
                "pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa"
            ) from import_error
        importlib.invalidate_caches()
        import ApplicationServices as application_services
        from AppKit import NSRunningApplication as running_application

        return application_services, running_application


AX, NSRunningApplication = load_pyobjc_dependencies()
try:
    import Quartz as QUARTZ
except ModuleNotFoundError:
    QUARTZ = None


SYSTEM_SETTINGS_BUNDLE_ID = "com.apple.systempreferences"
TARGET_TEXT = "General"
SIGN_IN_TEXT = "Sign in"
APPLE_ACCOUNT_TEXT = "Apple Account"
LOGIN_AND_SECURITY_TEXT = "Sign-In & Security"
APPLE_SIGN_IN_FIELD_TEXT = "Email or Phone Number"
LOGIN_PASSWORD_FIELD_TEXT = "Password"
LOGIN_CONTINUE_TEXT = "Continue"
MAC_PASSWORD_PROMPT_TEXT = "Enter Mac Password"
APPLE_ACCOUNT_CHANGES_PROMPT_TEXT = "Apple Account wants to make changes."
MAC_PASSWORD_PROMPT_TEXTS = (
    MAC_PASSWORD_PROMPT_TEXT,
    APPLE_ACCOUNT_CHANGES_PROMPT_TEXT,
)
IPHONE_PASSCODE_PROMPT_TEXT = "Enter the passcode you use to unlock the iPhone"
DONT_KNOW_PASSCODE_TEXTS = (
    "Don't know passcode?",
    "Don’t know passcode?",
)
ENTER_PASSCODE_LATER_TEXT = "Enter Passcode Later"
TRY_AGAIN_LATER_TEXT = "Try Again Later"
DONT_MERGE_TEXTS = (
    "Don't Merge",
    "Don’t Merge",
)
DID_NOT_RECEIVE_CODE_TEXTS = (
    "Didn't receive a verification code?",
    "Didn’t receive a verification code?",
)
SMS_URL_DEFAULT_VARIABLE = "APPLE_ACCOUNT_SMS_URL"
SMS_CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
SMS_CONTEXT_KEYWORDS = (
    "apple",
    "apple account",
    "verification",
    "security code",
    "verification code",
    "code",
    "验证码",
)
CHANGE_PASSWORD_TEXT = "Change Password"
PERSONAL_INFORMATION_TEXT = "Personal Information"
NESTED_BACK_IDENTIFIER = "go back"
CHANGE_PASSWORD_TEXTS = (
    CHANGE_PASSWORD_TEXT,
    "Change Password...",
    "Change Password…",
)
SIGN_OUT_OTHER_DEVICES_TEXT = "Sign out other devices using your Apple Account?"
DONT_SIGN_OUT_TEXTS = ("Don't Sign Out", "Don’t Sign Out")
NEW_PASSWORD_FIELD_TEXTS = ("New Password", "New password")
VERIFY_PASSWORD_FIELD_TEXTS = (
    "Verify",
    "Verify Password",
    "Confirm Password",
    "Re-enter Password",
    "Re-enter password",
)
PASSWORD_FIELD_TEXTS = (
    *NEW_PASSWORD_FIELD_TEXTS,
    *VERIFY_PASSWORD_FIELD_TEXTS,
)
TEXT_FIELD_ROLES = {"AXTextField", "AXSecureTextField"}

# 防止异常 UI 树导致无限扫描。
MAX_NODES = 20_000
MAX_PARENT_DEPTH = 12
OPERATION_TIMEOUT_SECONDS = 30
WORKFLOW_RENDER_INTERVAL_SECONDS = 0.5
WORKFLOW_RENDER_ATTEMPTS = int(
    OPERATION_TIMEOUT_SECONDS / WORKFLOW_RENDER_INTERVAL_SECONDS
)
MINIMUM_GUI_SETTLE_SECONDS = 3.0
POST_SUBMIT_STABLE_READS_REQUIRED = 3
POST_SUBMIT_STABLE_INTERVAL_SECONDS = 5.0
MAC_PASSWORD_WAIT_ATTEMPTS = WORKFLOW_RENDER_ATTEMPTS
MAC_PASSWORD_WAIT_INTERVAL_SECONDS = 0.5
SMS_FETCH_INTERVAL_SECONDS = 2
SMS_FETCH_ATTEMPTS = int(OPERATION_TIMEOUT_SECONDS / SMS_FETCH_INTERVAL_SECONDS)
_MAC_PASSWORD_AUTO_HANDLER_ACTIVE = False
_SECURITY_PROMPT_AUTO_HANDLER_ACTIVE = False

# 系统设置的侧边栏常将可选项目暴露为这两类角色。
SELECTABLE_ROLES = {"AXRow", "AXCell"}


@dataclass
class NavigationCandidate:
    """匹配文本及其最近的可导航祖先节点。"""

    matched_element: Any
    tree_depth: int
    matched_info: Dict[str, Any]
    action_element: Any
    parent_distance: int
    action_info: Dict[str, Any]
    method: str


class VisibleTextParser(HTMLParser):
    """将短信页面 HTML 转成不含标签的可搜索文本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        return " ".join(self.parts)


SMS_TEXT_FIELDS = (
    "body",
    "message",
    "content",
    "text",
    "sms",
)
SMS_TIME_FIELDS = (
    "created_at",
    "createdAt",
    "received_at",
    "receivedAt",
    "timestamp",
    "time",
    "date",
)
SMS_CLOCK_SKEW_SECONDS = 30


def _fetch_sms_url(url: str) -> str:
    """读取一个短信页面/API正文，不打印 URL、令牌或页面内容。"""
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 AppleAccountLogin/1.0",
            "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=15) as response:
        payload = response.read(2_000_000)
        charset = response.headers.get_content_charset() or "utf-8"
    raw_text = payload.decode(charset, errors="replace")
    if raw_text.lstrip().startswith(("{", "[")):
        return raw_text
    parser = VisibleTextParser()
    try:
        parser.feed(raw_text)
        visible_text = parser.text()
    except Exception:
        visible_text = ""
    return html.unescape(" ".join((visible_text, raw_text)))


def fetch_sms_page_text(sms_url: str) -> str:
    """读取短信页面或 API 正文。"""
    parsed = urlparse(sms_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("短信页面 URL 必须是 http(s) URL")
    return _fetch_sms_url(sms_url)


def _parse_sms_timestamp(value: Any) -> Optional[float]:
    """解析常见 JSON/HTML 时间表示，统一为 UTC epoch 秒。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        return number if number > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}(?:\.\d+)?", text):
        number = float(text)
        if number > 10_000_000_000:
            number /= 1000.0
        return number
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            parsed = None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        # 没有时区标记的页面时间按运行脚本的系统本地时区解释，
        # 避免把供应商显示的本地时间误当成 UTC。
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.timestamp()


def _iter_json_objects(value: Any) -> Iterator[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_objects(child)


def _extract_json_sms_candidates(page_text: str) -> List[Tuple[int, Optional[float], int, int]]:
    try:
        parsed = json.loads(page_text.strip())
    except (TypeError, ValueError):
        return []
    candidates: List[Tuple[int, Optional[float], int, int]] = []
    order = 0
    for record in _iter_json_objects(parsed):
        text_parts = [str(record[key]) for key in SMS_TEXT_FIELDS if key in record]
        if not text_parts:
            continue
        body = " ".join(text_parts)
        timestamp = next(
            (_parse_sms_timestamp(record[key]) for key in SMS_TIME_FIELDS if key in record),
            None,
        )
        for match in SMS_CODE_PATTERN.finditer(body):
            context = body[max(0, match.start() - 120) : match.end() + 120].lower()
            score = sum(2 if keyword in context else 0 for keyword in SMS_CONTEXT_KEYWORDS)
            candidates.append((order, timestamp, score, int(match.group(1))))
            order += 1
    return candidates


def _extract_context_timestamp(
    context: str,
    code_offset: Optional[int] = None,
) -> Optional[float]:
    patterns = (
        r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?\b",
        r"\b\d{1,2}[-/]\d{1,2}[-/]20\d{2}[ T]\d{1,2}:\d{2}(?::\d{2})?\b",
        r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+[^,]{1,30},?\s+20\d{2}\s+\d{1,2}:\d{2}(?::\d{2})?\b",
    )
    matches: List[Tuple[int, float]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, context, re.IGNORECASE):
            parsed = _parse_sms_timestamp(match.group(0))
            if parsed is None:
                continue
            distance = (
                abs(match.end() - code_offset)
                if code_offset is not None
                else match.start()
            )
            matches.append((distance, parsed))
    if not matches:
        return None
    return min(matches, key=lambda item: item[0])[1]


def extract_latest_sms_code(
    page_text: str,
    not_before: Optional[float] = None,
    now: Optional[float] = None,
) -> Optional[str]:
    """提取最新验证码；有时间按时间筛选，无时间按页面顺序取最后一条。"""
    candidates = _extract_json_sms_candidates(page_text)
    if not candidates:
        candidates = []
        for order, match in enumerate(SMS_CODE_PATTERN.finditer(page_text)):
            start, end = match.span(1)
            context_start = max(0, start - 180)
            context = page_text[context_start : min(len(page_text), end + 180)]
            lowered = context.lower()
            score = sum(2 if keyword in lowered else 0 for keyword in SMS_CONTEXT_KEYWORDS)
            if "apple account code" in lowered:
                score += 5
            if "verification code" in lowered:
                score += 3
            candidates.append(
                (
                    order,
                    _extract_context_timestamp(context, start - context_start),
                    score,
                    int(match.group(1)),
                )
            )
    if not candidates:
        return None
    timestamped = [candidate for candidate in candidates if candidate[1] is not None]
    if timestamped:
        current = time.time() if now is None else now
        if not_before is not None:
            timestamped = [
                candidate
                for candidate in timestamped
                if not_before - SMS_CLOCK_SKEW_SECONDS <= candidate[1] <= current + SMS_CLOCK_SKEW_SECONDS
            ]
        if not timestamped:
            return None
        selected = max(timestamped, key=lambda candidate: (candidate[1], candidate[0]))
        return str(selected[3]).zfill(6)
    # 该页面没有时间字段时，按页面记录顺序取最后一条候选。
    contextual = [candidate for candidate in candidates if candidate[2] > 0]
    selected = (contextual or candidates)[-1]
    return str(selected[3]).zfill(6)


def fetch_latest_sms_code(
    sms_url: str,
    not_before: Optional[float] = None,
) -> Optional[str]:
    """访问一次短信页面并返回验证码；网络/解析失败返回 None。"""
    try:
        return extract_latest_sms_code(
            fetch_sms_page_text(sms_url),
            not_before=not_before,
        )
    except Exception:
        return None


def copy_attribute(element: Any, attribute: str) -> Any:
    """读取 AXUIElement 属性；读取失败时返回 None。"""
    try:
        error, value = AX.AXUIElementCopyAttributeValue(
            element,
            attribute,
            None,
        )
    except Exception:
        return None

    if error != AX.kAXErrorSuccess:
        return None

    return value


def copy_actions(element: Any) -> List[str]:
    """读取节点支持的 Accessibility 动作。"""
    try:
        error, actions = AX.AXUIElementCopyActionNames(element, None)
    except Exception:
        return []

    if error != AX.kAXErrorSuccess or not actions:
        return []

    return [str(action) for action in actions]


def is_attribute_settable(element: Any, attribute: str) -> bool:
    """判断 AX 属性是否可写。"""
    try:
        error, settable = AX.AXUIElementIsAttributeSettable(
            element,
            attribute,
            None,
        )
    except Exception:
        return False

    return error == AX.kAXErrorSuccess and bool(settable)


def normalize_text(value: Any) -> Optional[str]:
    """将 AX 属性值安全地规范为字符串。"""
    if value is None:
        return None

    if isinstance(value, str):
        return value.strip()

    try:
        return str(value).strip()
    except Exception:
        return None


def describe_element(element: Any) -> Dict[str, Any]:
    """提取适合调试的 AX 节点信息。"""
    return {
        "role": normalize_text(copy_attribute(element, AX.kAXRoleAttribute)),
        "subrole": normalize_text(
            copy_attribute(element, AX.kAXSubroleAttribute)
        ),
        "title": normalize_text(copy_attribute(element, AX.kAXTitleAttribute)),
        "value": normalize_text(copy_attribute(element, AX.kAXValueAttribute)),
        "description": normalize_text(
            copy_attribute(element, AX.kAXDescriptionAttribute)
        ),
        "identifier": normalize_text(
            copy_attribute(element, AX.kAXIdentifierAttribute)
        ),
        "help": normalize_text(copy_attribute(element, AX.kAXHelpAttribute)),
        "url": normalize_text(copy_attribute(element, AX.kAXURLAttribute)),
        "enabled": copy_attribute(element, AX.kAXEnabledAttribute),
        "selected": copy_attribute(element, AX.kAXSelectedAttribute),
        "selected_settable": is_attribute_settable(
            element,
            AX.kAXSelectedAttribute,
        ),
        "actions": copy_actions(element),
    }


def iter_accessibility_tree(
    roots: List[Any],
    max_nodes: int = MAX_NODES,
) -> Iterator[Tuple[Any, int]]:
    """广度优先遍历 Accessibility 树。"""
    queue: Deque[Tuple[Any, int]] = deque((root, 0) for root in roots)
    visited_count = 0

    while queue and visited_count < max_nodes:
        element, depth = queue.popleft()
        visited_count += 1

        yield element, depth

        children = copy_attribute(element, AX.kAXChildrenAttribute)
        if not children:
            continue

        try:
            for child in children:
                queue.append((child, depth + 1))
        except TypeError:
            continue


def element_matches_text(info: Dict[str, Any], target: str) -> bool:
    """检查节点是否与目标显示文本匹配。"""
    visible_fields = (
        info["title"],
        info["value"],
        info["description"],
        info["help"],
    )

    if any(value == target for value in visible_fields):
        return True

    # AXIdentifier 通常不是显示文本；保留包含匹配，便于调试或自定义 target。
    identifier = info["identifier"]
    return bool(identifier and target in identifier)


def element_contains_text(info: Dict[str, Any], target: str) -> bool:
    """检查节点任一文本属性是否包含目标文本。"""
    searchable_fields = (
        info["title"],
        info["value"],
        info["description"],
        info["help"],
        info["identifier"],
    )
    return any(value and target in value for value in searchable_fields)


def tree_contains_text(roots: List[Any], target: str) -> bool:
    """检查当前 AX 树中是否出现指定文本。"""
    return any(
        element_contains_text(describe_element(element), target)
        for element, _ in iter_accessibility_tree(roots)
    )


def tree_contains_text_casefold(roots: List[Any], target: str) -> bool:
    """大小写不敏感地检查 AX 树中的账号邮箱等文本。"""
    wanted = str(target).casefold()
    for element, _ in iter_accessibility_tree(roots):
        # 邮箱成功判定必须是轻量路径；describe_element 还会读取动作名和
        # settable 属性，在 System Settings 某些页面上可能被 AX 服务阻塞。
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


def is_signed_in_apple_account_page(roots: List[Any], expected_email: str) -> bool:
    """Accept the account root or its Personal Information birthday page."""
    if tree_contains_text_casefold(roots, expected_email):
        return True
    return tree_contains_text_casefold(
        roots, "Personal Information"
    ) and tree_contains_text_casefold(roots, "Birthday")


def find_navigable_ancestor(
    element: Any,
    max_parent_depth: int = MAX_PARENT_DEPTH,
) -> Optional[Tuple[Any, int, Dict[str, Any], str]]:
    """寻找最近可通过 AXPress 或 AXSelected 导航的节点。

    优先 AXPress。若没有，则将可写 AXSelected 的 AXRow/AXCell 视为
    系统设置侧边栏的导航目标。
    """
    current = element

    for distance in range(max_parent_depth + 1):
        info = describe_element(current)

        # 在当前英文 macOS 版本中，Apple Account 侧边栏行虽然提供
        # AXShowDefaultUI，但该动作可能返回成功却不切换页面；对可写的
        # AXRow/AXCell，设置 AXSelected=True 才是实际生效的导航方式。
        if (
            info["role"] in SELECTABLE_ROLES
            and info["selected_settable"]
        ):
            return current, distance, info, "AXSelected=True"

        if AX.kAXPressAction in info["actions"]:
            return current, distance, info, "AXPress"

        if AX.kAXShowDefaultUIAction in info["actions"]:
            return current, distance, info, "AXShowDefaultUI"

        current = copy_attribute(current, AX.kAXParentAttribute)
        if current is None:
            break

    return None


def launch_system_settings() -> None:
    """关闭旧实例后通过 Bundle ID 重新打开系统设置。"""
    applications = list(
        NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            SYSTEM_SETTINGS_BUNDLE_ID
        )
    )
    for application in applications:
        application.terminate()
    deadline = time.monotonic() + 5.0
    while (
        NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            SYSTEM_SETTINGS_BUNDLE_ID
        )
        and time.monotonic() < deadline
    ):
        time.sleep(0.1)
    if NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        SYSTEM_SETTINGS_BUNDLE_ID
    ):
        raise RuntimeError("旧的系统设置实例未能关闭")
    subprocess.run(
        ["open", "-b", SYSTEM_SETTINGS_BUNDLE_ID],
        check=True,
    )


def get_running_system_settings() -> Any:
    """获取正在运行的系统设置 NSRunningApplication。"""
    applications = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        SYSTEM_SETTINGS_BUNDLE_ID
    )
    if not applications:
        raise RuntimeError("没有找到正在运行的系统设置进程")

    return applications[0]


def get_search_roots(app_element: Any) -> List[Any]:
    """优先从窗口开始遍历，避免扫描无关的全局菜单节点。"""
    windows = copy_attribute(app_element, AX.kAXWindowsAttribute)
    if windows:
        return list(windows)

    return [app_element]


def print_element_info(prefix: str, info: Dict[str, Any]) -> None:
    """输出可读的 AX 节点调试信息。"""
    print(prefix)
    print("  role              = {!r}".format(info["role"]))
    print("  subrole           = {!r}".format(info["subrole"]))
    print("  title             = {!r}".format(info["title"]))
    print("  value             = {!r}".format(info["value"]))
    print("  description       = {!r}".format(info["description"]))
    print("  identifier        = {!r}".format(info["identifier"]))
    print("  help              = {!r}".format(info["help"]))
    print("  enabled           = {!r}".format(info["enabled"]))
    print("  selected          = {!r}".format(info["selected"]))
    print("  selected_settable = {!r}".format(info["selected_settable"]))
    print("  actions           = {!r}".format(info["actions"]))


def activate(candidate: NavigationCandidate) -> int:
    """执行候选节点对应的导航操作，并返回 AXError。"""
    if candidate.method == "AXPress":
        return AX.AXUIElementPerformAction(
            candidate.action_element,
            AX.kAXPressAction,
        )

    if candidate.method == "AXShowDefaultUI":
        return AX.AXUIElementPerformAction(
            candidate.action_element,
            AX.kAXShowDefaultUIAction,
        )

    if candidate.method == "AXSelected=True":
        return AX.AXUIElementSetAttributeValue(
            candidate.action_element,
            AX.kAXSelectedAttribute,
            True,
        )

    raise ValueError("不支持的导航方式: {}".format(candidate.method))


def _resolve_current_system_settings_pid(preferred_pid: int) -> int:
    """System Settings 重启/换 PID 后优先返回当前活动实例。"""
    applications = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        SYSTEM_SETTINGS_BUNDLE_ID
    )
    if not applications:
        return preferred_pid
    active = [app for app in applications if app.isActive()]
    if active:
        return active[0].processIdentifier()
    if any(app.processIdentifier() == preferred_pid for app in applications):
        return preferred_pid
    return applications[0].processIdentifier()


def current_search_roots(
    pid: int,
    auto_handle_mac_password: bool = True,
    auto_handle_security_prompts: bool = True,
) -> List[Any]:
    """取得当前活动实例的搜索根节点，并处理随机出现的安全提示。"""
    global _MAC_PASSWORD_AUTO_HANDLER_ACTIVE, _SECURITY_PROMPT_AUTO_HANDLER_ACTIVE
    current_pid = _resolve_current_system_settings_pid(pid)
    app_element = AX.AXUIElementCreateApplication(current_pid)
    roots = get_search_roots(app_element)
    if (
        auto_handle_mac_password
        and not _MAC_PASSWORD_AUTO_HANDLER_ACTIVE
        and any(tree_contains_text(roots, text) for text in MAC_PASSWORD_PROMPT_TEXTS)
    ):
        _MAC_PASSWORD_AUTO_HANDLER_ACTIVE = True
        try:
            result = invoke_mac_password_prompt(current_pid)
            if result not in (None, 0):
                print(
                    "自动处理 Enter Mac Password 失败，AXError={}".format(result),
                    file=sys.stderr,
                )
        finally:
            _MAC_PASSWORD_AUTO_HANDLER_ACTIVE = False
        current_pid = _resolve_current_system_settings_pid(current_pid)
        roots = get_search_roots(AX.AXUIElementCreateApplication(current_pid))
    if (
        auto_handle_security_prompts
        and not _SECURITY_PROMPT_AUTO_HANDLER_ACTIVE
    ):
        _SECURITY_PROMPT_AUTO_HANDLER_ACTIVE = True
        try:
            result = invoke_security_prompt_autohandler(current_pid)
            if result not in (None, 0):
                print(
                    "自动处理安全提示失败，AXError={}".format(result),
                    file=sys.stderr,
                )
        finally:
            _SECURITY_PROMPT_AUTO_HANDLER_ACTIVE = False
        current_pid = _resolve_current_system_settings_pid(current_pid)
        roots = get_search_roots(AX.AXUIElementCreateApplication(current_pid))
    return roots


def find_sidebar_candidate(
    roots: List[Any],
    target: str,
) -> Optional[NavigationCandidate]:
    """查找包含 target 的侧边栏入口。

    优先使用可写 AXSelected 的 AXRow/AXCell；部分英文 macOS 版本会将
    Apple Account 入口暴露为 AXStaticText（例如“Sign in, with your
    Apple Account”），此时再从匹配文本节点向上寻找 AXPress/AXSelected
    祖先。
    """
    for element, tree_depth in iter_accessibility_tree(roots):
        row_info = describe_element(element)
        if (
            row_info["role"] not in SELECTABLE_ROLES
            or not row_info["selected_settable"]
        ):
            continue

        # Apple Account 一项常显示为“用户名, Apple Account”，所以这里使用包含匹配。
        for descendant, descendant_depth in iter_accessibility_tree(
            [element],
            max_nodes=100,
        ):
            matched_info = describe_element(descendant)
            if not element_contains_text(matched_info, target):
                continue

            navigable = find_navigable_ancestor(descendant)
            if navigable is None:
                break

            action_element, parent_distance, action_info, method = navigable
            return NavigationCandidate(
                matched_element=descendant,
                tree_depth=tree_depth + descendant_depth,
                matched_info=matched_info,
                action_element=action_element,
                parent_distance=parent_distance,
                action_info=action_info,
                method=method,
            )

    # 英文 macOS 的 Apple Account 入口可能只有文本节点可见，
    # 例如标题为“Sign in, with your Apple Account”的 AXStaticText。
    for element, tree_depth in iter_accessibility_tree(roots):
        matched_info = describe_element(element)
        is_apple_account_identifier = (
            target in (SIGN_IN_TEXT, APPLE_ACCOUNT_TEXT)
            and matched_info["identifier"]
            and "AppleIDSettings" in matched_info["identifier"]
        )
        if not element_contains_text(matched_info, target) and not is_apple_account_identifier:
            continue

        navigable = find_navigable_ancestor(element)
        if navigable is None:
            continue

        action_element, parent_distance, action_info, method = navigable
        return NavigationCandidate(
            matched_element=element,
            tree_depth=tree_depth,
            matched_info=matched_info,
            action_element=action_element,
            parent_distance=parent_distance,
            action_info=action_info,
            method=method,
        )

    return None


def find_pressable_text_candidate(
    roots: List[Any],
    target: str,
) -> Optional[NavigationCandidate]:
    """查找文本精确匹配且能通过 AXPress 激活的控件。"""
    for element, tree_depth in iter_accessibility_tree(roots):
        matched_info = describe_element(element)
        if not element_matches_text(matched_info, target):
            continue

        navigable = find_navigable_ancestor(element)
        if navigable is None:
            continue

        action_element, parent_distance, action_info, method = navigable
        if method != "AXPress":
            continue

        return NavigationCandidate(
            matched_element=element,
            tree_depth=tree_depth,
            matched_info=matched_info,
            action_element=action_element,
            parent_distance=parent_distance,
            action_info=action_info,
            method=method,
        )

    return None


def find_pressable_text_candidate_any(
    roots: List[Any],
    targets: Tuple[str, ...],
) -> Optional[NavigationCandidate]:
    """查找多个等价文本中的第一个可按控件。"""
    for target in targets:
        candidate = find_pressable_text_candidate(roots, target)
        if candidate is not None:
            return candidate
    return None


def find_enabled_pressable_text_candidate(
    roots: List[Any],
    target: str,
) -> Optional[NavigationCandidate]:
    """查找文本匹配且未被禁用的 AXPress 控件。"""
    for element, tree_depth in iter_accessibility_tree(roots):
        matched_info = describe_element(element)
        if not element_matches_text(matched_info, target):
            continue
        navigable = find_navigable_ancestor(element)
        if navigable is None:
            continue
        action_element, parent_distance, action_info, method = navigable
        if method != "AXPress" or action_info["enabled"] is False:
            continue
        return NavigationCandidate(
            matched_element=element,
            tree_depth=tree_depth,
            matched_info=matched_info,
            action_element=action_element,
            parent_distance=parent_distance,
            action_info=action_info,
            method=method,
        )
    return None


def find_enabled_pressable_text_candidates(
    roots: List[Any],
    targets: Tuple[str, ...],
) -> List[NavigationCandidate]:
    """返回所有唯一、可用的按钮动作，不按“第一个”猜测。"""
    candidates: List[NavigationCandidate] = []
    for element, tree_depth in iter_accessibility_tree(roots):
        matched_info = describe_element(element)
        if not any(element_matches_text(matched_info, target) for target in targets):
            continue
        navigable = find_navigable_ancestor(element)
        if navigable is None:
            continue
        action_element, parent_distance, action_info, method = navigable
        if method != "AXPress" or action_info["enabled"] is False:
            continue
        candidate = NavigationCandidate(
            matched_element=element,
            tree_depth=tree_depth,
            matched_info=matched_info,
            action_element=action_element,
            parent_distance=parent_distance,
            action_info=action_info,
            method=method,
        )
        if any(
            existing.action_element == candidate.action_element
            for existing in candidates
        ):
            continue
        candidates.append(candidate)
    return candidates


def digits_only(value: Any) -> str:
    """保留电话号码或页面候选文本中的数字。"""
    return "".join(character for character in str(value or "") if character.isdigit())


def find_phone_radio_candidate(
    roots: List[Any],
    phone_suffix: str,
) -> Optional[NavigationCandidate]:
    """按页面可见尾号查找唯一的验证码电话号码单选项。"""
    matches: List[NavigationCandidate] = []
    for element, tree_depth in iter_accessibility_tree(roots):
        info = describe_element(element)
        if info["role"] != "AXRadioButton":
            continue
        # AXRadioButton 的 value 是选中状态（通常为 0/1），不能把它
        # 当作电话号码的一部分；优先使用 title/description/help。
        label_values = (
            info["title"],
            info["description"],
            info["help"],
        )
        visible_text = " ".join(
            str(value) for value in label_values if value is not None
        )
        if not visible_text:
            visible_text = str(info["value"] or "")
        if not digits_only(visible_text).endswith(phone_suffix):
            continue

        navigable = find_navigable_ancestor(element)
        if navigable is None and info["selected_settable"]:
            navigable = (element, 0, info, "AXSelected=True")
        if navigable is None:
            continue
        action_element, parent_distance, action_info, method = navigable
        matches.append(
            NavigationCandidate(
                matched_element=element,
                tree_depth=tree_depth,
                matched_info=info,
                action_element=action_element,
                parent_distance=parent_distance,
                action_info=action_info,
                method=method,
            )
        )

    if len(matches) != 1:
        return None
    return matches[0]


def find_phone_or_recovery_candidate(
    roots: List[Any],
    phone_suffix: str,
) -> Optional[Tuple[str, NavigationCandidate]]:
    """识别“Didn't receive…”后续页或直接出现的电话号码选择页。"""
    recovery = find_pressable_text_candidate_any(roots, DID_NOT_RECEIVE_CODE_TEXTS)
    if recovery is not None:
        return ("recovery", recovery)
    radio = find_phone_radio_candidate(roots, phone_suffix)
    if radio is not None:
        return ("phone", radio)
    return None


def find_verification_code_fields(roots: List[Any]) -> List[Any]:
    """查找验证码输入框，支持六个分格或一个整码输入框。"""
    fields: List[Tuple[Any, int, Dict[str, Any]]] = []
    labeled: List[Tuple[Any, int, Dict[str, Any]]] = []
    positioned: Dict[int, Tuple[Any, int, Dict[str, Any]]] = {}
    groups: Dict[str, List[Tuple[Any, int, Dict[str, Any]]]] = {}
    for element, tree_depth in iter_accessibility_tree(roots):
        info = describe_element(element)
        if info["role"] not in TEXT_FIELD_ROLES:
            continue
        fields.append((element, tree_depth, info))
        # 当前 System Settings 的六格验证码框暴露为 title="1" … "6"，
        # 每格的父节点并不相同，因此按位置标题建立独立识别路径。
        title = str(info["title"] or "").strip()
        if title in {"1", "2", "3", "4", "5", "6"}:
            positioned[int(title)] = (element, tree_depth, info)
        searchable = " ".join(
            str(value)
            for value in (
                info["title"],
                info["description"],
                info["identifier"],
                info["help"],
            )
            if value is not None
        ).lower()
        if any(
            keyword in searchable
            for keyword in ("verification", "security code", "one-time", "otp")
        ):
            labeled.append((element, tree_depth, info))
        parent = copy_attribute(element, AX.kAXParentAttribute)
        groups.setdefault(repr(parent), []).append((element, tree_depth, info))

    if len(positioned) == 6:
        return [positioned[index][0] for index in range(1, 7)]

    if len(labeled) >= 6:
        return [item[0] for item in labeled[:6]]

    grouped = [group for group in groups.values() if len(group) >= 6]
    if grouped:
        # 选择最深的成组输入框，排除侧边栏搜索框等无关控件。
        selected_group = max(grouped, key=lambda group: max(item[1] for item in group))
        return [item[0] for item in selected_group[:6]]

    if len(labeled) == 1:
        return [labeled[0][0]]

    # 某些版本只暴露一个无标签的整码输入框；排除浅层搜索框后再接受
    # 唯一候选，避免把系统设置搜索框误当成验证码框。
    deep_fields = [item for item in fields if item[1] >= 8]
    if len(deep_fields) == 1:
        return [deep_fields[0][0]]
    return []


def set_verification_code(fields: List[Any], code: str) -> int:
    """将六位验证码写入分格或整码输入框。"""
    if len(fields) >= 6:
        for field, digit in zip(fields[:6], code):
            error = set_login_field(field, digit)
            if error != AX.kAXErrorSuccess:
                return error
        return AX.kAXErrorSuccess
    return set_login_field(fields[0], code)


def _click_security_prompt_text_with_wait(
    pid: int,
    targets: Tuple[str, ...],
    error_message: str,
) -> int:
    """在不再次触发全局钩子的前提下点击安全弹窗按钮。"""
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        roots = current_search_roots(
            pid,
            auto_handle_mac_password=False,
            auto_handle_security_prompts=False,
        )
        candidate = find_pressable_text_candidate_any(roots, targets)
        if candidate is not None:
            error = activate(candidate)
            if error == AX.kAXErrorSuccess:
                return 0
            if error in (
                AX.kAXErrorAttributeUnsupported,
                AX.kAXErrorCannotComplete,
            ):
                if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
                    time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
                    continue
            print(
                "{}，AXError={}".format(error_message, error),
                file=sys.stderr,
            )
            return 4
        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    print(
        "{}（超时：{}秒）。".format(error_message, OPERATION_TIMEOUT_SECONDS),
        file=sys.stderr,
    )
    return 5


def _try_click_security_prompt_once(
    pid: int,
    targets: Tuple[str, ...],
) -> Optional[int]:
    """只检查一次安全提示按钮，用于处理弹窗在点击瞬间切换的竞态。"""
    roots = current_search_roots(
        pid,
        auto_handle_mac_password=False,
        auto_handle_security_prompts=False,
    )
    candidate = find_pressable_text_candidate_any(roots, targets)
    if candidate is None:
        return None
    return activate(candidate)


def invoke_iphone_passcode_prompt(pid: int) -> Optional[int]:
    """两个 iPhone 安全按钮任意一个出现就点击，不要求固定页面顺序。"""
    enter_later_targets = (ENTER_PASSCODE_LATER_TEXT,)
    try_again_later_targets = (TRY_AGAIN_LATER_TEXT,)
    saw_iphone_page = False
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        roots = current_search_roots(
            pid,
            auto_handle_mac_password=False,
            auto_handle_security_prompts=False,
        )
        saw_iphone_page = saw_iphone_page or tree_contains_text(
            roots, IPHONE_PASSCODE_PROMPT_TEXT
        )

        # 两个按钮同时检查；第二层按钮优先，避免被背景页的 AX 节点遮挡。
        candidates = (
            (try_again_later_targets, "Try Again Later"),
            (enter_later_targets, "Enter Passcode Later"),
            (DONT_KNOW_PASSCODE_TEXTS, "Don't know passcode?"),
        )
        found_button = False
        for targets, label in candidates:
            candidate = find_pressable_text_candidate_any(roots, targets)
            if candidate is None:
                continue
            found_button = True
            error = activate(candidate)
            if error == AX.kAXErrorSuccess:
                return 0
            if error in (
                AX.kAXErrorAttributeUnsupported,
                AX.kAXErrorCannotComplete,
            ):
                # 弹窗正在切换，下一轮同时重读两个按钮。
                break
            print("点击 {} 失败，AXError={}".format(label, error), file=sys.stderr)
            return 4

        if not saw_iphone_page and not found_button:
            return None
        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)

    print(
        "iPhone 安全提示未找到可用的 Don't know passcode? 或 "
        "Enter Passcode Later（超时：{}秒）。".format(OPERATION_TIMEOUT_SECONDS),
        file=sys.stderr,
    )
    return 5


def invoke_dont_merge_prompt(pid: int) -> Optional[int]:
    """若 Safari/iCloud 合并提示出现，点击 Don't Merge。"""
    roots = current_search_roots(
        pid,
        auto_handle_mac_password=False,
        auto_handle_security_prompts=False,
    )
    merge_candidate = find_pressable_text_candidate_any(roots, DONT_MERGE_TEXTS)
    if not any(tree_contains_text(roots, text) for text in DONT_MERGE_TEXTS) and (
        merge_candidate is None
    ):
        return None
    return _click_security_prompt_text_with_wait(
        pid,
        DONT_MERGE_TEXTS,
        "未找到 Don't Merge 控件",
    )


def invoke_security_prompt_autohandler(pid: int) -> Optional[int]:
    """处理随机插入的 iPhone 与 Don't Merge 安全提示。"""
    handled = False
    # Don't Merge 可能遮住 iPhone 页面；必须优先处理顶层弹窗，不能先等待
    # 被遮挡的 Don't know passcode? 控件。
    merge_result = invoke_dont_merge_prompt(pid)
    if merge_result is not None:
        handled = True
        if merge_result != 0:
            return merge_result

    iphone_result = invoke_iphone_passcode_prompt(pid)
    if iphone_result is not None:
        handled = True
        if iphone_result != 0:
            return iphone_result

    # Enter Passcode Later 后可能紧接着再次出现合并提示；同一次扫描周期内
    # 再检查一次顶层弹窗。
    merge_result = invoke_dont_merge_prompt(pid)
    if merge_result is not None:
        handled = True
        if merge_result != 0:
            return merge_result
    return 0 if handled else None


def invoke_mac_password_prompt(pid: int) -> Optional[int]:
    """延迟导入独立 Mac 密码模块，避免登录模块和弹窗模块循环导入。"""
    from mac_password_prompt import handle_mac_password_prompt

    return handle_mac_password_prompt(pid)


def wait_for_mac_password_prompt(pid: int) -> Optional[int]:
    """验证码提交后持续调用独立弹窗模块，等待延迟出现的安全弹窗。"""
    for attempt in range(MAC_PASSWORD_WAIT_ATTEMPTS):
        result = invoke_mac_password_prompt(pid)
        if result is not None:
            return result
        if attempt < MAC_PASSWORD_WAIT_ATTEMPTS - 1:
            time.sleep(MAC_PASSWORD_WAIT_INTERVAL_SECONDS)
    return None


def invoke_post_login_prompts(pid: int, expected_email: str) -> int:
    """调用独立的 Apple Account 后置安全提示处理模块。"""
    from apple_account_post_login import handle_post_login_prompts

    return handle_post_login_prompts(pid, expected_email)


def complete_verification_code_workflow(
    pid: int,
    sms_url_variable: str,
    expected_email: str,
    verification_requested_at: Optional[float] = None,
) -> int:
    """从运行时短信 URL 获取最新验证码并填写 Apple 验证页。"""
    sms_url = os.environ.get(sms_url_variable, "").strip()
    if not sms_url:
        sms_url = input("SMS page URL: ").strip()
    if not sms_url:
        print("SMS page URL 不能为空。", file=sys.stderr)
        return 6

    code_fields: List[Any] = []
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        code_fields = find_verification_code_fields(current_search_roots(pid))
        if code_fields:
            break
        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    if not code_fields:
        print(
            "未找到验证码输入框（超时：{}秒）。".format(OPERATION_TIMEOUT_SECONDS),
            file=sys.stderr,
        )
        return 5
    print("已找到验证码输入框，正在读取短信验证码…", flush=True)

    code: Optional[str] = None
    # 给短信平台留出到达时间；每次重新读取同一个当前 URL，取最新候选。
    for attempt in range(SMS_FETCH_ATTEMPTS):
        code = fetch_latest_sms_code(
            sms_url,
            not_before=verification_requested_at,
        )
        if code is not None:
            break
        if attempt < SMS_FETCH_ATTEMPTS - 1:
            time.sleep(SMS_FETCH_INTERVAL_SECONDS)
    if code is None:
        print(
            "短信页面中未找到唯一可识别的六位验证码（超时：{}秒）。".format(
                OPERATION_TIMEOUT_SECONDS
            ),
            file=sys.stderr,
        )
        return 5

    error = set_verification_code(code_fields, code)
    code = ""
    if error != AX.kAXErrorSuccess:
        print("写入验证码失败，AXError={}".format(error), file=sys.stderr)
        return 4
    print("已写入验证码，等待可用的 Continue…", flush=True)

    continue_candidate: Optional[NavigationCandidate] = None
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        continue_candidate = find_enabled_pressable_text_candidate(
            current_search_roots(pid), LOGIN_CONTINUE_TEXT
        )
        if continue_candidate is not None:
            break
        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    if continue_candidate is None:
        print(
            "验证码页面未找到可用的 Continue 按钮（超时：{}秒）。".format(
                OPERATION_TIMEOUT_SECONDS
            ),
            file=sys.stderr,
        )
        return 5
    error = activate(continue_candidate)
    if error != AX.kAXErrorSuccess:
        print("提交验证码失败，AXError={}".format(error), file=sys.stderr)
        return 4
    print("已提交验证码，正在处理后续安全提示…", flush=True)
    # 看到目标邮箱即视为登录成功；Mac 密码或其他随机提示不是必经步骤。
    # 先用无副作用读取确认邮箱；只有未看到邮箱时才处理随机安全弹窗。
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        direct_roots = current_search_roots(
            pid,
            auto_handle_mac_password=False,
            auto_handle_security_prompts=False,
        )
        if is_signed_in_apple_account_page(direct_roots, expected_email):
            print("已检测到 Apple Account 邮箱，跳过剩余登录步骤。", flush=True)
            return 0
        current_search_roots(pid)
        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    final_roots = current_search_roots(
        pid,
        auto_handle_mac_password=False,
        auto_handle_security_prompts=False,
    )
    if not is_signed_in_apple_account_page(final_roots, expected_email):
        print(
            "验证码已提交，但在超时时间内未检测到 Apple Account 邮箱；"
            "流程未完成（超时：{}秒）。".format(OPERATION_TIMEOUT_SECONDS),
            file=sys.stderr,
        )
        return 5
    return 0


def find_login_field(
    roots: List[Any],
    label: str,
) -> Optional[Any]:
    """查找登录页的文本或安全文本输入框。"""
    for element, _ in iter_accessibility_tree(roots):
        info = describe_element(element)
        if info["role"] not in TEXT_FIELD_ROLES:
            continue
        if element_contains_text(info, label):
            return element

        # 英文 macOS 15 的登录页会把“Email or Phone Number”作为相邻的
        # AXStaticText 暴露，输入框自身没有 title/value，只有
        # identifier=USERNAME_TEXT_FIELD。密码框同样可能只有 PASSWORD
        # 标识符，因此不能只依赖输入框自身的可见文本。
        identifier = (info["identifier"] or "").upper()
        if label == APPLE_SIGN_IN_FIELD_TEXT and (
            "USERNAME" in identifier or "EMAIL" in identifier
        ):
            return element
        if label == LOGIN_PASSWORD_FIELD_TEXT and "PASSWORD" in identifier:
            return element
    return None


def find_field_by_labels(roots: List[Any], labels: Tuple[str, ...]) -> Optional[Any]:
    """查找带有任一标签的文本输入框。"""
    for element, _ in iter_accessibility_tree(roots):
        info = describe_element(element)
        if info["role"] not in TEXT_FIELD_ROLES:
            continue
        if any(element_contains_text(info, label) for label in labels):
            return element
    return None


def set_login_field(element: Any, value: str) -> int:
    """在已确认的登录输入框中设置值，不打印值内容。"""
    try:
        focus_error = AX.AXUIElementSetAttributeValue(
            element,
            AX.kAXFocusedAttribute,
            True,
        )
        if focus_error != AX.kAXErrorSuccess:
            return focus_error
        return AX.AXUIElementSetAttributeValue(
            element,
            AX.kAXValueAttribute,
            value,
        )
    except Exception:
        return -1


def run_apple_account_login_workflow(
    pid: int,
    email_variable: str,
    password_variable: str,
    phone_variable: str,
    sms_url_variable: str,
) -> int:
    """打开 Sign in，按邮箱页 → Continue → 密码页顺序提交凭据。"""
    email = os.environ.get(email_variable, "").strip()
    if not email:
        email = input("Apple Account email: ").strip()
    if not email:
        print("Apple Account email 不能为空。", file=sys.stderr)
        return 6
    expected_email = email

    # 账号页面已经显示目标邮箱时，登录步骤已完成，直接进入最终复核。
    initial_roots = current_search_roots(
        pid,
        auto_handle_mac_password=False,
        auto_handle_security_prompts=False,
    )
    if is_signed_in_apple_account_page(initial_roots, expected_email):
        print("已检测到 Apple Account 邮箱，跳过登录输入步骤。", flush=True)
        return 0

    candidate = find_sidebar_candidate(
        current_search_roots(pid),
        SIGN_IN_TEXT,
    )
    if candidate is not None:
        if candidate.action_info["enabled"] is False:
            print("Sign in 入口当前处于禁用状态", file=sys.stderr)
            return 3
        error = activate(candidate)
        if error != AX.kAXErrorSuccess:
            print("打开 Sign in 失败，AXError={}".format(error), file=sys.stderr)
            return 4

    # Apple 的英文登录页是分步渲染：初始页只有邮箱字段，点击
    # Continue 后才会创建 Password 字段。不能要求两个字段同时存在。
    email_field: Optional[Any] = None
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        # System Settings 启动后首轮 AX 树可能尚未暴露邮箱；每一轮都先
        # 做轻量邮箱判定，避免把已登录页面误报成缺少输入框。
        roots = current_search_roots(
            pid,
            auto_handle_mac_password=False,
            auto_handle_security_prompts=False,
        )
        if is_signed_in_apple_account_page(roots, expected_email):
            print("已检测到 Apple Account 邮箱，跳过登录输入步骤。", flush=True)
            return 0
        email_field = find_login_field(roots, APPLE_SIGN_IN_FIELD_TEXT)
        if email_field is not None:
            break
        current_search_roots(pid)
        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)

    if email_field is None:
        print("未找到 Email or Phone Number 输入框。", file=sys.stderr)
        return 5

    email_error = set_login_field(email_field, email)
    email = ""
    if email_error != AX.kAXErrorSuccess:
        print("写入邮箱字段失败。", file=sys.stderr)
        return 4

    continue_candidate: Optional[NavigationCandidate] = None
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        continue_candidate = find_pressable_text_candidate(
            current_search_roots(pid), LOGIN_CONTINUE_TEXT
        )
        if continue_candidate is not None:
            break
        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    if continue_candidate is None:
        print("未找到 Continue 按钮。", file=sys.stderr)
        return 5
    error = activate(continue_candidate)
    if error != AX.kAXErrorSuccess:
        print("点击 Continue 失败，AXError={}".format(error), file=sys.stderr)
        return 4

    password_field: Optional[Any] = None
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        password_field = find_login_field(
            current_search_roots(pid), LOGIN_PASSWORD_FIELD_TEXT
        )
        if password_field is not None:
            break
        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    if password_field is None:
        print("点击邮箱页 Continue 后未找到 Password 输入框。", file=sys.stderr)
        return 5

    password = os.environ.get(password_variable, "")
    if not password:
        password = getpass.getpass("Apple Account password: ")
    if not password:
        print("Apple Account password 不能为空。", file=sys.stderr)
        return 6
    password_error = set_login_field(password_field, password)
    password = ""
    if password_error != AX.kAXErrorSuccess:
        print("写入密码字段失败。", file=sys.stderr)
        return 4

    continue_candidate = None
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        continue_candidate = find_pressable_text_candidate(
            current_search_roots(pid), LOGIN_CONTINUE_TEXT
        )
        if continue_candidate is not None:
            break
        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    if continue_candidate is None:
        print("密码页未找到 Continue 按钮。", file=sys.stderr)
        return 5
    error = activate(continue_candidate)
    if error != AX.kAXErrorSuccess:
        print("提交密码失败，AXError={}".format(error), file=sys.stderr)
        return 4

    # 登录成功后可能显示验证码页，也可能直接显示电话号码选择页。
    # 后者没有“Didn't receive…”链接，直接按电话号码页处理。
    phone = os.environ.get(phone_variable, "").strip()
    if not phone:
        phone = input("Trusted phone number (include the visible ending): ").strip()
    phone_digits = digits_only(phone)
    if len(phone_digits) < 2:
        print("Trusted phone number 至少需要两个数字。", file=sys.stderr)
        return 6
    phone_suffix = phone_digits[-2:]
    print(
        "正在识别验证码页面，匹配电话号码尾号 {}…".format(phone_suffix),
        flush=True,
    )

    phone_or_recovery: Optional[Tuple[str, NavigationCandidate]] = None
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        roots = current_search_roots(pid)
        phone_or_recovery = find_phone_or_recovery_candidate(roots, phone_suffix)
        if phone_or_recovery is not None:
            break
        if find_verification_code_fields(roots):
            break
        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)

    if phone_or_recovery is not None:
        page_kind, selected_candidate = phone_or_recovery
        if page_kind == "recovery":
            error = activate(selected_candidate)
            if error != AX.kAXErrorSuccess:
                print(
                    "点击 Didn't receive a verification code? 失败，AXError={}".format(error),
                    file=sys.stderr,
                )
                return 4
            print("已打开电话号码选择页，正在匹配尾号 {}…".format(phone_suffix), flush=True)
            radio_candidate: Optional[NavigationCandidate] = None
            for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
                radio_candidate = find_phone_radio_candidate(
                    current_search_roots(pid), phone_suffix
                )
                if radio_candidate is not None:
                    break
                if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
                    time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
            if radio_candidate is None:
                print(
                    "没有找到唯一匹配电话号码尾号的选项：{}。".format(phone_suffix),
                    file=sys.stderr,
                )
                return 5
        else:
            radio_candidate = selected_candidate

        error = activate(radio_candidate)
        if error != AX.kAXErrorSuccess:
            print("选择验证码电话号码失败，AXError={}".format(error), file=sys.stderr)
            return 4
        print("已选择验证码电话号码，等待可用的 Continue…", flush=True)

        continue_candidate = None
        for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
            # 号码单选后页面会短暂保留一个 AXPress 可用、但 enabled=False
            # 的旧 Continue 节点；不能提前按它，否则 AXPress 返回成功而页面
            # 实际不跳转，后续就会一直等不到验证码输入框。
            continue_candidate = find_enabled_pressable_text_candidate(
                current_search_roots(pid), LOGIN_CONTINUE_TEXT
            )
            if continue_candidate is not None:
                break
            if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
                time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
        if continue_candidate is None:
            print("选择电话号码后未找到 Continue 按钮。", file=sys.stderr)
            return 5
        verification_requested_at = time.time()
        error = activate(continue_candidate)
        if error != AX.kAXErrorSuccess:
            print("提交验证码电话号码选择失败，AXError={}".format(error), file=sys.stderr)
            return 4
        print("已提交电话号码选择，正在等待验证码输入框…", flush=True)
        return complete_verification_code_workflow(
            pid,
            sms_url_variable,
            expected_email,
            verification_requested_at,
        )

    # 某些账号会直接进入验证码输入页，不显示号码选择链接。
    return complete_verification_code_workflow(
        pid, sms_url_variable, expected_email
    )


def run_apple_account_change_password_workflow(
    pid: int,
    new_password_variable: str,
) -> int:
    """保留纯 AX 导航入口，导航后使用同一严格表单处理器。"""
    initial_roots = current_search_roots(pid)
    existing_new_field = _unique_field_by_labels(
        initial_roots, NEW_PASSWORD_FIELD_TEXTS
    )
    existing_verify_field = _unique_field_by_labels(
        initial_roots, VERIFY_PASSWORD_FIELD_TEXTS
    )
    if (
        existing_new_field is not None
        and existing_verify_field is not None
        and existing_new_field != existing_verify_field
    ):
        print("PASSWORD_CHANGE_FORM=resume_existing")
        return run_apple_account_change_password_form(
            pid, new_password_variable
        )

    roots = current_search_roots(pid)
    security_candidate = find_pressable_text_candidate(
        roots, LOGIN_AND_SECURITY_TEXT
    )
    if security_candidate is None and tree_contains_text(
        roots, PERSONAL_INFORMATION_TEXT
    ):
        back_buttons = []
        for element, _ in iter_accessibility_tree(roots):
            if copy_attribute(element, AX.kAXRoleAttribute) != "AXButton":
                continue
            if copy_attribute(element, AX.kAXIdentifierAttribute) != NESTED_BACK_IDENTIFIER:
                continue
            if AX.kAXPressAction not in copy_actions(element):
                continue
            enabled = copy_attribute(element, AX.kAXEnabledAttribute)
            if enabled is not False:
                back_buttons.append(element)
        if len(back_buttons) != 1:
            print("Apple Account 嵌套页面返回按钮不唯一。", file=sys.stderr)
            return 5
        error = AX.AXUIElementPerformAction(back_buttons[0], AX.kAXPressAction)
        if error != AX.kAXErrorSuccess:
            print("Apple Account 嵌套页面返回失败。", file=sys.stderr)
            return 4
        time.sleep(MINIMUM_GUI_SETTLE_SECONDS)
        roots = current_search_roots(pid)
        security_candidate = find_pressable_text_candidate(
            roots, LOGIN_AND_SECURITY_TEXT
        )
        if security_candidate is None:
            print("Apple Account 根页回读失败。", file=sys.stderr)
            return 5
        print("PASSWORD_CHANGE_ROOT_RECOVERY=verified")

    if security_candidate is None:
        account_candidate = find_sidebar_candidate(
            roots, APPLE_ACCOUNT_TEXT
        ) or find_sidebar_candidate(roots, SIGN_IN_TEXT)
        if account_candidate is None:
            print("没有找到 Apple Account / Sign in 入口。", file=sys.stderr)
            return 2
        if account_candidate.action_info["enabled"] is False:
            print("Apple Account 入口当前处于禁用状态。", file=sys.stderr)
            return 3
        error = activate(account_candidate)
        if error != AX.kAXErrorSuccess:
            print("Apple Account 嵌套页面返回失败。", file=sys.stderr)
            return 4
        time.sleep(MINIMUM_GUI_SETTLE_SECONDS)
        for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
            security_candidate = find_pressable_text_candidate(
                current_search_roots(pid), LOGIN_AND_SECURITY_TEXT
            )
            if security_candidate is not None:
                break
            if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
                time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    if security_candidate is None:
        print("未找到 Sign-In & Security。请先确认 Apple Account 已登录。", file=sys.stderr)
        return 5
    error = activate(security_candidate)
    if error != AX.kAXErrorSuccess:
        print("打开 Sign-In & Security 失败，AXError={}".format(error), file=sys.stderr)
        return 4
    time.sleep(MINIMUM_GUI_SETTLE_SECONDS)

    change_candidate: Optional[NavigationCandidate] = None
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        change_candidates = find_enabled_pressable_text_candidates(
                current_search_roots(pid), CHANGE_PASSWORD_TEXTS
        )
        if len(change_candidates) == 1:
            change_candidate = change_candidates[0]
            break
        if len(change_candidates) > 1:
            print("Change Password 按钮不唯一。", file=sys.stderr)
            return 5
        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    if change_candidate is None:
        print("未找到 Change Password 按钮。", file=sys.stderr)
        return 5
    error = activate(change_candidate)
    if error != AX.kAXErrorSuccess:
        print("打开 Change Password 失败，AXError={}".format(error), file=sys.stderr)
        return 4
    time.sleep(MINIMUM_GUI_SETTLE_SECONDS)

    return run_apple_account_change_password_form(pid, new_password_variable)


def _unique_field_by_labels(
    roots: List[Any],
    labels: Tuple[str, ...],
) -> Optional[Any]:
    """只在标签组唯一命中一个可用密码框时返回。"""
    candidates: List[Any] = []
    for element, _ in iter_accessibility_tree(roots):
        info = describe_element(element)
        if info["role"] not in TEXT_FIELD_ROLES or info["enabled"] is False:
            continue
        nearest_labels = _nearest_password_field_labels(element)
        if not any(element_contains_text(info, label) for label in labels) and not any(
            label in nearest_labels for label in labels
        ):
            continue
        if any(existing == element for existing in candidates):
            continue
        candidates.append(element)
    return candidates[0] if len(candidates) == 1 else None


def _nearest_password_field_labels(element: Any) -> set[str]:
    """Read the nearest two ancestor groups for unlabeled secure-field siblings."""
    current = element
    for _distance in range(2):
        current = copy_attribute(current, AX.kAXParentAttribute)
        if current is None:
            break
        found = {
            label
            for label in PASSWORD_FIELD_TEXTS
            if tree_contains_text([current], label)
        }
        if found:
            return found
    return set()


def _ancestor_chain(element: Any) -> List[Any]:
    """返回元素到窗口的有界父链。"""
    chain: List[Any] = []
    current = element
    for _ in range(MAX_PARENT_DEPTH * 2):
        if current is None:
            break
        chain.append(current)
        current = copy_attribute(current, AX.kAXParentAttribute)
    return chain


def _candidate_form_roots(new_field: Any, verify_field: Any) -> List[Any]:
    """从两密码框的最小公共父区域向外只读搜索。"""
    new_chain = _ancestor_chain(new_field)
    verify_chain = _ancestor_chain(verify_field)
    return [
        candidate
        for candidate in new_chain
        if any(candidate == other for other in verify_chain)
    ]


def _find_unique_password_submit(
    new_field: Any,
    verify_field: Any,
) -> Optional[NavigationCandidate]:
    """在两密码框的公共表单区域内查找唯一最终按钮。"""
    labels = ("Change", "Continue")
    for root in _candidate_form_roots(new_field, verify_field):
        candidates = find_enabled_pressable_text_candidates([root], labels)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            return None
    return None


def _password_field_matches(element: Any, expected: str) -> bool:
    """在内存中验证明文或等长安全掩码，绝不输出字段值。"""
    value = copy_attribute(element, AX.kAXValueAttribute)
    if value is None:
        return False
    rendered = str(value)
    if rendered == expected:
        return True
    mask_characters = {"•", "●", "*", "·"}
    return len(rendered) == len(expected) and bool(rendered) and set(rendered) <= mask_characters


def password_field_length(element: Any) -> Optional[int]:
    """Read only a secure field's character count."""
    value = copy_attribute(element, AX.kAXValueAttribute)
    if isinstance(value, str):
        return len(value)
    count_attribute = getattr(AX, "kAXNumberOfCharactersAttribute", None)
    if count_attribute:
        count = copy_attribute(element, count_attribute)
        if isinstance(count, int):
            return count
    return None


def set_password_change_field(pid: int, element: Any, value: str) -> Tuple[int, str]:
    """AX-set the prefix, then deliver the final character as a real edit event."""
    if not value:
        return AX.kAXErrorCannotComplete, "empty-value"
    if QUARTZ is None:
        return AX.kAXErrorCannotComplete, "quartz-unavailable"
    last_reason = "not-attempted"
    for _attempt in range(3):
        error = set_login_field(element, value[:-1])
        if error != AX.kAXErrorSuccess:
            last_reason = f"ax-prefix-error-{error}"
            continue
        time.sleep(MINIMUM_GUI_SETTLE_SECONDS)
        focused = copy_attribute(element, AX.kAXFocusedAttribute)
        prefix_length = password_field_length(element)
        if focused not in (True, 1):
            last_reason = "field-not-focused"
            continue
        if prefix_length != len(value) - 1:
            last_reason = f"prefix-length-{prefix_length}-expected-{len(value) - 1}"
            continue
        try:
            application = AX.AXUIElementCreateApplication(pid)
            if copy_attribute(application, AX.kAXFrontmostAttribute) not in (True, 1):
                frontmost_error = AX.AXUIElementSetAttributeValue(
                    application, AX.kAXFrontmostAttribute, True
                )
                if frontmost_error != AX.kAXErrorSuccess:
                    last_reason = f"frontmost-error-{frontmost_error}"
                    continue
                time.sleep(MINIMUM_GUI_SETTLE_SECONDS)
            if copy_attribute(application, AX.kAXFrontmostAttribute) not in (True, 1):
                last_reason = "system-settings-not-frontmost"
                continue
            focus_error = AX.AXUIElementSetAttributeValue(
                element, AX.kAXFocusedAttribute, True
            )
            if focus_error != AX.kAXErrorSuccess:
                last_reason = f"refocus-error-{focus_error}"
                continue
            time.sleep(MINIMUM_GUI_SETTLE_SECONDS)
            if copy_attribute(element, AX.kAXFocusedAttribute) not in (True, 1):
                last_reason = "refocus-readback-failed"
                continue
            key_down = QUARTZ.CGEventCreateKeyboardEvent(None, 0, True)
            key_up = QUARTZ.CGEventCreateKeyboardEvent(None, 0, False)
            QUARTZ.CGEventKeyboardSetUnicodeString(key_down, 1, value[-1])
            QUARTZ.CGEventKeyboardSetUnicodeString(key_up, 1, value[-1])
            QUARTZ.CGEventPost(QUARTZ.kCGHIDEventTap, key_down)
            QUARTZ.CGEventPost(QUARTZ.kCGHIDEventTap, key_up)
        except Exception as error:
            return AX.kAXErrorCannotComplete, f"quartz-event-{type(error).__name__}"
        time.sleep(MINIMUM_GUI_SETTLE_SECONDS)
        full_length = password_field_length(element)
        if full_length == len(value):
            return AX.kAXErrorSuccess, "verified"
        last_reason = f"full-length-{full_length}-expected-{len(value)}"
    return AX.kAXErrorCannotComplete, last_reason


def handle_sign_out_other_devices_prompt(pid: int) -> Optional[int]:
    """Keep the account signed in on its other devices after the change."""
    roots = current_search_roots(pid)
    if not (
        tree_contains_text(roots, SIGN_OUT_OTHER_DEVICES_TEXT)
        or (
            tree_contains_text(roots, "Sign out other devices")
            and tree_contains_text(roots, APPLE_ACCOUNT_TEXT)
        )
    ):
        return None
    candidate = None
    for label in DONT_SIGN_OUT_TEXTS:
        candidate = find_enabled_pressable_text_candidate(roots, label)
        if candidate is not None:
            break
    if candidate is None:
        return 5
    error = activate(candidate)
    if error != AX.kAXErrorSuccess:
        return 4
    time.sleep(MINIMUM_GUI_SETTLE_SECONDS)
    if tree_contains_text(current_search_roots(pid), "Sign out other devices"):
        return 5
    print("PASSWORD_CHANGE_OTHER_DEVICES=kept_signed_in")
    return 0


def close_system_settings_after_password_change() -> bool:
    """Close System Settings and independently prove the process is gone."""
    from apple_account_post_login import force_close_system_settings

    if not force_close_system_settings():
        return False
    time.sleep(MINIMUM_GUI_SETTLE_SECONDS)
    if NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        SYSTEM_SETTINGS_BUNDLE_ID
    ):
        return False
    print("SYSTEM_SETTINGS_CLOSED=verified")
    return True


def run_apple_account_change_password_form(
    pid: int,
    new_password_variable: str,
) -> int:
    """处理视觉路由已打开的授权弹窗/密码表单，且最终只提交一次。"""

    new_password = os.environ.get(new_password_variable, "")
    if not new_password:
        new_password = getpass.getpass("New Apple Account password: ")
    if not new_password:
        print("New Apple Account password 不能为空。", file=sys.stderr)
        return 6

    new_field: Optional[Any] = None
    verify_field: Optional[Any] = None
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        roots = current_search_roots(pid)
        new_field = _unique_field_by_labels(roots, NEW_PASSWORD_FIELD_TEXTS)
        verify_field = _unique_field_by_labels(roots, VERIFY_PASSWORD_FIELD_TEXTS)
        if new_field is not None and verify_field is not None and new_field != verify_field:
            break
        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)
    if new_field is None or verify_field is None:
        print("未找到新密码或确认密码输入框。", file=sys.stderr)
        return 5

    first_error, first_reason = set_password_change_field(pid, new_field, new_password)
    if first_error != AX.kAXErrorSuccess:
        print(f"写入新密码字段失败：{first_reason}。", file=sys.stderr)
        return 4
    time.sleep(MINIMUM_GUI_SETTLE_SECONDS)
    roots = current_search_roots(pid)
    new_field = _unique_field_by_labels(roots, NEW_PASSWORD_FIELD_TEXTS)
    verify_field = _unique_field_by_labels(roots, VERIFY_PASSWORD_FIELD_TEXTS)
    if (
        new_field is None
        or verify_field is None
        or not _password_field_matches(new_field, new_password)
    ):
        print("新密码字段回读不匹配。", file=sys.stderr)
        return 5

    second_error, second_reason = set_password_change_field(pid, verify_field, new_password)
    if second_error != AX.kAXErrorSuccess:
        print(f"写入确认密码字段失败：{second_reason}。", file=sys.stderr)
        return 4
    time.sleep(MINIMUM_GUI_SETTLE_SECONDS)
    roots = current_search_roots(pid)
    new_field = _unique_field_by_labels(roots, NEW_PASSWORD_FIELD_TEXTS)
    verify_field = _unique_field_by_labels(roots, VERIFY_PASSWORD_FIELD_TEXTS)
    fields_verified = bool(
        new_field is not None
        and verify_field is not None
        and new_field != verify_field
        and _password_field_matches(new_field, new_password)
        and _password_field_matches(verify_field, new_password)
    )
    new_password = ""
    if not fields_verified or new_field is None or verify_field is None:
        print("两个新密码字段回读不匹配。", file=sys.stderr)
        return 5
    print("PASSWORD_CHANGE_FIELDS=verified")

    submit_candidate = _find_unique_password_submit(new_field, verify_field)
    if submit_candidate is None:
        print("未找到唯一可用的最终改密按钮。", file=sys.stderr)
        return 5
    print("PASSWORD_CHANGE_BUTTON=enabled")
    error = activate(submit_candidate)
    if error != AX.kAXErrorSuccess:
        print("提交改密失败，AXError={}".format(error), file=sys.stderr)
        return 4
    print("PASSWORD_CHANGE_SUBMISSION=clicked_once")
    return verify_password_change_result(
        pid,
        fields_verified=fields_verified,
        submission_clicked=True,
    )


def verify_password_change_result(
    pid: int,
    *,
    fields_verified: bool = False,
    submission_clicked: bool = False,
) -> int:
    """Require complete submit evidence before accepting the closed sheet."""
    if not fields_verified or not submission_clicked:
        print("缺少字段回读或唯一提交动作证据。", file=sys.stderr)
        return 5
    time.sleep(MINIMUM_GUI_SETTLE_SECONDS)
    stable_reads = 0
    for attempt in range(POST_SUBMIT_STABLE_READS_REQUIRED * 3):
        if attempt:
            time.sleep(POST_SUBMIT_STABLE_INTERVAL_SECONDS)
        roots = current_search_roots(
            pid,
            auto_handle_mac_password=False,
            auto_handle_security_prompts=False,
        )
        if any(tree_contains_text(roots, text) for text in MAC_PASSWORD_PROMPT_TEXTS):
            mac_password_result = invoke_mac_password_prompt(pid)
            if mac_password_result != 0:
                print("提交后的 Mac 密码授权处理失败。", file=sys.stderr)
                return 5 if mac_password_result is None else mac_password_result
            print("PASSWORD_CHANGE_POST_SUBMIT_MAC_AUTH=verified")
            stable_reads = 0
            continue
        security_result = invoke_security_prompt_autohandler(pid)
        if security_result is not None:
            if security_result != 0:
                print("提交后的 Apple 安全提示处理失败。", file=sys.stderr)
                return security_result
            print("PASSWORD_CHANGE_POST_SUBMIT_SECURITY_PROMPT=verified")
            stable_reads = 0
            continue
        sign_out_result = handle_sign_out_other_devices_prompt(pid)
        if sign_out_result not in (None, 0):
            print("处理其他设备登录状态提示失败。", file=sys.stderr)
            return sign_out_result
        if sign_out_result == 0:
            stable_reads = 0
            continue
        roots = current_search_roots(
            pid,
            auto_handle_mac_password=False,
            auto_handle_security_prompts=False,
        )
        new_field = _unique_field_by_labels(roots, NEW_PASSWORD_FIELD_TEXTS)
        verify_field = _unique_field_by_labels(roots, VERIFY_PASSWORD_FIELD_TEXTS)
        if new_field is None and verify_field is None and (
            tree_contains_text(roots, LOGIN_AND_SECURITY_TEXT)
            or tree_contains_text(roots, APPLE_ACCOUNT_TEXT)
        ):
            stable_reads += 1
            if stable_reads >= POST_SUBMIT_STABLE_READS_REQUIRED:
                if not close_system_settings_after_password_change():
                    print("改密完成后无法关闭 System Settings。", file=sys.stderr)
                    return 5
                print("PASSWORD_CHANGE_RESULT=verified")
                return 0
            continue
        stable_reads = 0
    print(
        "提交后密码表单未在限定时间内关闭，结果未验证。",
        file=sys.stderr,
    )
    return 5


def print_workflow_step(
    name: str,
    candidate: NavigationCandidate,
) -> None:
    """打印工作流一步使用的匹配节点和操作节点。"""
    print()
    print("=" * 60)
    print("工作流步骤：{}（导航方式：{}）".format(name, candidate.method))
    print_element_info("匹配文本节点", candidate.matched_info)
    print_element_info("导航目标", candidate.action_info)


def run_apple_account_login_security_workflow(
    pid: int,
    should_activate: bool,
) -> int:
    """执行“Sign in → Sign-In & Security”工作流。"""
    account_candidate = find_sidebar_candidate(
        current_search_roots(pid),
        SIGN_IN_TEXT,
    )
    if account_candidate is None:
        print("没有找到 Sign in 侧边栏入口。", file=sys.stderr)
        return 2

    print_workflow_step("打开 Sign in", account_candidate)

    if not should_activate:
        print(
            "\n当前为只读测试，没有执行工作流。\n"
            "使用 --workflow apple-account-login-security --press 执行。"
        )
        return 0

    if account_candidate.action_info["enabled"] is False:
        print("Sign in 入口当前处于禁用状态", file=sys.stderr)
        return 3

    error = activate(account_candidate)
    if error != AX.kAXErrorSuccess:
        print("打开 Sign in 失败，AXError={}".format(error), file=sys.stderr)
        return 4

    print("\n已打开 Sign in，等待“Sign-In & Security”按钮加载。")

    security_candidate: Optional[NavigationCandidate] = None
    for attempt in range(WORKFLOW_RENDER_ATTEMPTS):
        security_candidate = find_pressable_text_candidate(
            current_search_roots(pid),
            LOGIN_AND_SECURITY_TEXT,
        )
        if security_candidate is not None:
            break

        if attempt < WORKFLOW_RENDER_ATTEMPTS - 1:
            time.sleep(WORKFLOW_RENDER_INTERVAL_SECONDS)

    if security_candidate is None:
        if tree_contains_text(
            current_search_roots(pid),
            APPLE_SIGN_IN_FIELD_TEXT,
        ):
            print(
                "Apple Account sign-in page is open. "
                "Sign in first, then rerun this workflow for "
                "Sign-In & Security."
            )
            return 6

        print(
            "Sign in 已打开，但未找到“Sign-In & Security”按钮。",
            file=sys.stderr,
        )
        return 5

    print_workflow_step("点击 Sign-In & Security", security_candidate)

    if security_candidate.action_info["enabled"] is False:
        print("Sign-In & Security 按钮当前处于禁用状态", file=sys.stderr)
        return 3

    error = activate(security_candidate)
    if error != AX.kAXErrorSuccess:
        print("点击 Sign-In & Security 失败，AXError={}".format(error), file=sys.stderr)
        return 4

    print("\n工作流执行成功：Apple Account → Sign-In & Security。")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="查找并测试系统设置中的侧边栏入口"
    )
    parser.add_argument(
        "--press",
        "--activate",
        dest="activate",
        action="store_true",
        help="找到可导航节点后执行 AXPress 或 AXSelected=True",
    )
    parser.add_argument(
        "--target",
        default=TARGET_TEXT,
        help="要查找的界面文本，默认为“General”",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="打开 Sign in，按变量读取凭据并点击 Continue",
    )
    parser.add_argument(
        "--handle-mac-password",
        action="store_true",
        help="处理当前已显示的 Enter Mac Password 弹窗并输入宿主配置的客机密码",
    )
    parser.add_argument(
        "--change-password",
        action="store_true",
        help="打开 Sign-In & Security，填写新密码并提交",
    )
    parser.add_argument(
        "--change-password-form-only",
        action="store_true",
        help="视觉路由已打开 Change Password 后，只处理授权弹窗与密码表单",
    )
    parser.add_argument(
        "--new-password-var",
        default="APPLE_ACCOUNT_NEW_PASSWORD",
        help="新密码环境变量名，默认 APPLE_ACCOUNT_NEW_PASSWORD；密码值不放入 argv",
    )
    parser.add_argument(
        "--email-var",
        default="APPLE_ACCOUNT_EMAIL",
        help="邮箱环境变量名，默认 APPLE_ACCOUNT_EMAIL",
    )
    parser.add_argument(
        "--password-var",
        default="APPLE_ACCOUNT_PASSWORD",
        help="密码环境变量名，默认 APPLE_ACCOUNT_PASSWORD；密码值不放入 argv",
    )
    parser.add_argument(
        "--phone-var",
        default="APPLE_ACCOUNT_PHONE",
        help="受信任电话号码环境变量名，默认 APPLE_ACCOUNT_PHONE",
    )
    parser.add_argument(
        "--sms-url-var",
        default=SMS_URL_DEFAULT_VARIABLE,
        help="短信页面 URL 环境变量名，默认 APPLE_ACCOUNT_SMS_URL",
    )
    parser.add_argument(
        "--workflow",
        choices=("apple-account-login-security",),
        help="执行内置工作流；仍须搭配 --press 才会实际导航",
    )
    args = parser.parse_args(argv)

    trusted = AX.AXIsProcessTrustedWithOptions(
        {AX.kAXTrustedCheckOptionPrompt: True}
    )
    if not trusted:
        print(
            "当前进程没有辅助功能权限。\n"
            "请在“系统设置 → 隐私与安全性 → 辅助功能”中，"
            "允许当前终端或 Python 运行程序。",
            file=sys.stderr,
        )
        return 1

    launch_system_settings()
    time.sleep(MINIMUM_GUI_SETTLE_SECONDS)

    # 权限可能在启动 System Settings 后才被系统撤销/拒绝；再次检查，
    # 避免 AX 树返回 kAXErrorAPIDisabled 后无意义地轮询到超时。
    if not AX.AXIsProcessTrustedWithOptions(
        {AX.kAXTrustedCheckOptionPrompt: False}
    ):
        print(
            "启动系统设置后当前进程仍没有辅助功能权限（AX API disabled）。"
            "请在虚拟机的‘系统设置 → 隐私与安全性 → 辅助功能’中允许"
            "实际运行脚本的 Terminal/Python，然后重新运行。",
            file=sys.stderr,
        )
        return 1

    running_app = get_running_system_settings()
    pid = running_app.processIdentifier()
    print("系统设置 PID：{}".format(pid))

    if args.handle_mac_password:
        result = invoke_mac_password_prompt(pid)
        if result is None:
            print("当前没有检测到 Enter Mac Password 弹窗。", file=sys.stderr)
            return 5
        return result

    if args.login:
        return run_apple_account_login_workflow(
            pid,
            args.email_var,
            args.password_var,
            args.phone_var,
            args.sms_url_var,
        )

    if args.change_password:
        return run_apple_account_change_password_workflow(
            pid,
            args.new_password_var,
        )

    if args.change_password_form_only:
        return run_apple_account_change_password_form(
            pid,
            args.new_password_var,
        )

    if args.workflow == "apple-account-login-security":
        return run_apple_account_login_security_workflow(
            pid,
            args.activate,
        )

    app_element = AX.AXUIElementCreateApplication(pid)
    roots = get_search_roots(app_element)

    candidates: List[NavigationCandidate] = []
    scanned_count = 0

    for element, tree_depth in iter_accessibility_tree(roots):
        scanned_count += 1
        info = describe_element(element)

        if not element_matches_text(info, args.target):
            continue

        navigable = find_navigable_ancestor(element)
        if navigable is None:
            print()
            print_element_info(
                "找到文本节点，但没有可导航的父节点，tree_depth={}".format(
                    tree_depth
                ),
                info,
            )
            continue

        action_element, parent_distance, action_info, method = navigable
        candidates.append(
            NavigationCandidate(
                matched_element=element,
                tree_depth=tree_depth,
                matched_info=info,
                action_element=action_element,
                parent_distance=parent_distance,
                action_info=action_info,
                method=method,
            )
        )

    print()
    print("扫描节点数量：{}".format(scanned_count))
    print("可导航匹配数量：{}".format(len(candidates)))

    if not candidates:
        print(
            "\n没有找到可导航的目标节点。\n"
            "这通常表示：\n"
            "1. 系统设置窗口尚未完成渲染；\n"
            "2. 当前系统语言与 --target 不匹配；\n"
            "3. 目标节点位于未加载的滚动区域；\n"
            "4. 当前 macOS 版本以其他 AX 属性或动作实现导航。"
        )
        return 2

    # 优先最近的祖先；同距离时优先标准 AXPress。
    candidates.sort(
        key=lambda item: (
            item.parent_distance,
            0 if item.method == "AXPress" else 1,
            item.tree_depth,
        )
    )

    for index, candidate in enumerate(candidates, start=1):
        print()
        print("=" * 60)
        print("候选 {}（导航方式：{}）".format(index, candidate.method))
        print_element_info(
            "匹配文本节点，tree_depth={}".format(candidate.tree_depth),
            candidate.matched_info,
        )
        print_element_info(
            "可导航节点，向上距离={}".format(candidate.parent_distance),
            candidate.action_info,
        )

    selected = candidates[0]
    print()
    print("=" * 60)
    print("最终选择的节点（导航方式：{}）：".format(selected.method))
    print_element_info("导航目标", selected.action_info)

    if not args.activate:
        print(
            "\n当前为只读测试，没有执行导航。\n"
            "确认节点信息正确后，使用 --press 或 --activate 执行。"
        )
        return 0

    if selected.action_info["enabled"] is False:
        print("目标节点当前处于禁用状态", file=sys.stderr)
        return 3

    error = activate(selected)
    if error != AX.kAXErrorSuccess:
        print(
            "导航执行失败，AXError={}".format(error),
            file=sys.stderr,
        )
        return 4

    print("\n导航请求执行成功（{}）。".format(selected.method))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
