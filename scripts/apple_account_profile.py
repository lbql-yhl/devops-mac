#!/usr/bin/env python3
"""Read the signed-in Apple Account name and birthday through macOS AX only."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence


@dataclass(frozen=True)
class TextNode:
    path: tuple[int, ...]
    text: str


@dataclass(frozen=True)
class ValueNode:
    path: tuple[int, ...]
    role: str
    value: object


class ProfileReadError(RuntimeError):
    """Raised when the Apple Account profile cannot be read unambiguously."""


_CF_ABSOLUTE_TIME_UNIX_OFFSET_SECONDS = 978_307_200
_APPLE_DISPLAYED_BIRTHDAY_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_EXCLUDED_HEADER_TEXTS = {
    "Apple Account",
    "Some Features Are Unavailable",
    "Personal Information",
    "Sign-In & Security",
    "Payment & Shipping",
    "iCloud",
    "Family",
    "Media & Purchases",
}


def normalize_account_name(value: str) -> str:
    name = value.strip()
    if (
        not name
        or "@" in name
        or "\n" in name
        or "\r" in name
        or not any(character.isalpha() for character in name)
    ):
        raise ProfileReadError("APPLE_ACCOUNT_NAME_INVALID")
    return name


def parse_apple_birthday_ax_value(value: object) -> tuple[int, int, int]:
    reference_reader = getattr(value, "timeIntervalSinceReferenceDate", None)
    if not callable(reference_reader):
        raise ProfileReadError("APPLE_BIRTHDAY_INVALID")
    try:
        seconds = float(reference_reader())
    except (TypeError, ValueError) as error:
        raise ProfileReadError("APPLE_BIRTHDAY_INVALID") from error
    if not math.isfinite(seconds):
        raise ProfileReadError("APPLE_BIRTHDAY_INVALID")
    try:
        parsed = datetime.fromtimestamp(
            seconds + _CF_ABSOLUTE_TIME_UNIX_OFFSET_SECONDS,
            tz=timezone.utc,
        )
    except (OSError, OverflowError, ValueError) as error:
        raise ProfileReadError("APPLE_BIRTHDAY_INVALID") from error
    if not 1900 <= parsed.year <= date.today().year:
        raise ProfileReadError("APPLE_BIRTHDAY_INVALID")
    return parsed.year, parsed.month, parsed.day


def _parse_displayed_birthday(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise ProfileReadError("APPLE_BIRTHDAY_INVALID")
    match = _APPLE_DISPLAYED_BIRTHDAY_RE.fullmatch(value.strip())
    if not match:
        raise ProfileReadError("APPLE_BIRTHDAY_INVALID")
    month, day, year = (int(part) for part in match.groups())
    try:
        parsed = date(year, month, day)
    except ValueError as error:
        raise ProfileReadError("APPLE_BIRTHDAY_INVALID") from error
    if not 1900 <= parsed.year <= date.today().year:
        raise ProfileReadError("APPLE_BIRTHDAY_INVALID")
    return parsed.year, parsed.month, parsed.day


def _in_scope(node: TextNode, prefix: tuple[int, ...]) -> bool:
    return node.path[: len(prefix)] == prefix


def _plausible_header_name(value: str, expected_email: str) -> bool:
    if value.casefold() == expected_email.casefold():
        return False
    if value in _EXCLUDED_HEADER_TEXTS:
        return False
    if "/" in value or "://" in value:
        return False
    try:
        normalize_account_name(value)
    except ProfileReadError:
        return False
    return True


def select_account_header_name(
    nodes: Sequence[TextNode], expected_email: str
) -> str:
    email = expected_email.strip()
    matches = [node for node in nodes if node.text.casefold() == email.casefold()]
    if len(matches) != 1:
        raise ProfileReadError("APPLE_ACCOUNT_EMAIL_NOT_UNIQUE")

    email_path = matches[0].path
    for prefix_length in range(len(email_path) - 1, 0, -1):
        prefix = email_path[:prefix_length]
        names = {
            normalize_account_name(node.text)
            for node in nodes
            if _in_scope(node, prefix)
            and _plausible_header_name(node.text, email)
        }
        if len(names) == 1:
            return next(iter(names))
        if len(names) > 1:
            raise ProfileReadError("APPLE_ACCOUNT_NAME_NOT_UNIQUE")
    raise ProfileReadError("APPLE_ACCOUNT_NAME_NOT_UNIQUE")


def select_personal_information_name(
    nodes: Sequence[TextNode], expected_name: str = ""
) -> str:
    if expected_name.strip():
        return normalize_account_name(expected_name)
    labels = [node for node in nodes if node.text == "Name"]
    if len(labels) != 1:
        raise ProfileReadError("APPLE_ACCOUNT_NAME_NOT_UNIQUE")
    label_path = labels[0].path
    for prefix_length in range(len(label_path) - 1, 0, -1):
        prefix = label_path[:prefix_length]
        names = {
            normalize_account_name(node.text)
            for node in nodes
            if _in_scope(node, prefix)
            and node.text != "Name"
            and _plausible_header_name(node.text, "")
        }
        if len(names) == 1:
            return next(iter(names))
        if len(names) > 1:
            raise ProfileReadError("APPLE_ACCOUNT_NAME_NOT_UNIQUE")
    raise ProfileReadError("APPLE_ACCOUNT_NAME_NOT_UNIQUE")


def select_birthday_ax_value(
    nodes: Sequence[TextNode], values: Sequence[ValueNode]
) -> tuple[int, int, int]:
    labels = [node for node in nodes if node.text == "Birthday"]
    if len(labels) != 1:
        raise ProfileReadError("APPLE_BIRTHDAY_LABEL_NOT_UNIQUE")
    label_path = labels[0].path
    for prefix_length in range(len(label_path) - 1, 0, -1):
        prefix = label_path[:prefix_length]
        candidates: set[tuple[int, int, int]] = set()
        for node in values:
            if not _in_scope(node, prefix):
                continue
            try:
                parsed = parse_apple_birthday_ax_value(node.value)
            except ProfileReadError:
                continue
            candidates.add(parsed)
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1:
            raise ProfileReadError("APPLE_BIRTHDAY_NOT_UNIQUE")
    raise ProfileReadError("APPLE_BIRTHDAY_NOT_UNIQUE")


def select_birthday_display_value(
    nodes: Sequence[TextNode], values: Sequence[ValueNode]
) -> tuple[int, int, int]:
    labels = [node for node in nodes if node.text == "Birthday"]
    if len(labels) != 1:
        raise ProfileReadError("APPLE_BIRTHDAY_LABEL_NOT_UNIQUE")
    label_path = labels[0].path
    for prefix_length in range(len(label_path) - 1, 0, -1):
        prefix = label_path[:prefix_length]
        candidates: set[tuple[int, int, int]] = set()
        for node in values:
            if node.role != "AXIncrementor" or not _in_scope(node, prefix):
                continue
            try:
                candidates.add(_parse_displayed_birthday(node.value))
            except ProfileReadError:
                continue
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1:
            raise ProfileReadError("APPLE_BIRTHDAY_NOT_UNIQUE")
    raise ProfileReadError("APPLE_BIRTHDAY_NOT_UNIQUE")


def _text_snapshot(roots: Sequence[Any]) -> tuple[TextNode, ...]:
    try:
        from find_system_settings_general import AX, copy_attribute
    except ImportError:
        from scripts.find_system_settings_general import AX, copy_attribute

    attributes = (
        AX.kAXTitleAttribute,
        AX.kAXValueAttribute,
        AX.kAXDescriptionAttribute,
        AX.kAXHelpAttribute,
    )
    result: list[TextNode] = []
    stack: list[tuple[Any, tuple[int, ...]]] = [
        (root, (index,)) for index, root in reversed(tuple(enumerate(roots)))
    ]
    visited = 0
    while stack and visited < 5000:
        element, path = stack.pop()
        visited += 1
        values: set[str] = set()
        for attribute in attributes:
            raw = copy_attribute(element, attribute)
            if isinstance(raw, str) and raw.strip():
                values.add(raw.strip())
        result.extend(TextNode(path, value) for value in sorted(values))
        children = copy_attribute(element, AX.kAXChildrenAttribute)
        if not children:
            continue
        try:
            child_values = tuple(children)
        except TypeError:
            continue
        for index in range(len(child_values) - 1, -1, -1):
            stack.append((child_values[index], path + (index,)))
    return tuple(result)


def _value_snapshot(roots: Sequence[Any]) -> tuple[ValueNode, ...]:
    try:
        from find_system_settings_general import AX, copy_attribute
    except ImportError:
        from scripts.find_system_settings_general import AX, copy_attribute

    result: list[ValueNode] = []
    stack: list[tuple[Any, tuple[int, ...]]] = [
        (root, (index,)) for index, root in reversed(tuple(enumerate(roots)))
    ]
    visited = 0
    while stack and visited < 5000:
        element, path = stack.pop()
        visited += 1
        role = copy_attribute(element, AX.kAXRoleAttribute)
        value = copy_attribute(element, AX.kAXValueAttribute)
        is_reference_date = callable(
            getattr(value, "timeIntervalSinceReferenceDate", None)
        )
        if is_reference_date:
            result.append(ValueNode(path, role, value))
        children = copy_attribute(element, AX.kAXChildrenAttribute)
        if not children:
            continue
        try:
            child_values = tuple(children)
        except TypeError:
            continue
        for index in range(len(child_values) - 1, -1, -1):
            stack.append((child_values[index], path + (index,)))
    return tuple(result)


def _displayed_value_snapshot(roots: Sequence[Any]) -> tuple[ValueNode, ...]:
    try:
        from find_system_settings_general import AX, copy_attribute
    except ImportError:
        from scripts.find_system_settings_general import AX, copy_attribute

    result: list[ValueNode] = []
    stack: list[tuple[Any, tuple[int, ...]]] = [
        (root, (index,)) for index, root in reversed(tuple(enumerate(roots)))
    ]
    visited = 0
    while stack and visited < 5000:
        element, path = stack.pop()
        visited += 1
        role = copy_attribute(element, AX.kAXRoleAttribute)
        if role == "AXIncrementor":
            displayed_values: set[str] = set()
            for attribute in (
                AX.kAXValueAttribute,
                AX.kAXValueDescriptionAttribute,
            ):
                displayed = copy_attribute(element, attribute)
                if isinstance(displayed, str) and displayed.strip():
                    displayed_values.add(displayed.strip())
            result.extend(
                ValueNode(path, role, displayed)
                for displayed in sorted(displayed_values)
            )
        children = copy_attribute(element, AX.kAXChildrenAttribute)
        if not children:
            continue
        try:
            child_values = tuple(children)
        except TypeError:
            continue
        for index in range(len(child_values) - 1, -1, -1):
            stack.append((child_values[index], path + (index,)))
    return tuple(result)


def _birthday_source_diagnostic(roots: Sequence[Any]) -> str:
    try:
        from find_system_settings_general import AX, copy_attribute
    except ImportError:
        from scripts.find_system_settings_general import AX, copy_attribute

    attributes = (
        ("VALUE", AX.kAXValueAttribute),
        ("VALUE_DESCRIPTION", AX.kAXValueDescriptionAttribute),
        ("TITLE", AX.kAXTitleAttribute),
        ("DESCRIPTION", AX.kAXDescriptionAttribute),
        ("HELP", AX.kAXHelpAttribute),
    )
    sources: set[str] = set()
    native_sources: list[str] = []
    stack = list(reversed(tuple(roots)))
    visited = 0
    while stack and visited < 5000:
        element = stack.pop()
        visited += 1
        role = copy_attribute(element, AX.kAXRoleAttribute)
        role_token = re.sub(r"[^A-Z0-9]+", "_", str(role).upper()).strip("_")
        role_token = role_token or "UNKNOWN"
        native_value = copy_attribute(element, AX.kAXValueAttribute)
        if (
            isinstance(native_value, (int, float))
            and not isinstance(native_value, bool)
            and math.isfinite(float(native_value))
            and len(native_sources) < 12
        ):
            number = float(native_value)
            number_token = (
                str(int(number))
                if number.is_integer()
                else str(number)
            ).replace("-", "NEG").replace(".", "POINT")
            native_sources.append(f"{role_token}_NUMBER_{number_token}")
        elif callable(
            getattr(native_value, "timeIntervalSinceReferenceDate", None)
        ) and len(native_sources) < 12:
            native_sources.append(f"{role_token}_REFERENCE_DATE")
        for attribute_name, attribute in attributes:
            raw = copy_attribute(element, attribute)
            if isinstance(raw, str) and _APPLE_DISPLAYED_BIRTHDAY_RE.fullmatch(
                raw.strip()
            ):
                sources.add(f"{role_token}_{attribute_name}")
        children = copy_attribute(element, AX.kAXChildrenAttribute)
        if not children:
            continue
        try:
            stack.extend(reversed(tuple(children)))
        except TypeError:
            continue
    if not sources:
        if native_sources:
            return "APPLE_BIRTHDAY_NATIVE_SOURCE_" + "_AND_".join(
                native_sources
            )
        return "APPLE_BIRTHDAY_DISPLAY_SOURCE_MISSING"
    return "APPLE_BIRTHDAY_DISPLAY_SOURCE_" + "_OR_".join(sorted(sources))


def read_profile(expected_email: str, expected_name: str = "") -> dict[str, object]:
    try:
        from find_system_settings_general import (
            AX,
            activate,
            current_search_roots,
            find_enabled_pressable_text_candidates,
            get_running_system_settings,
        )
    except ImportError:
        from scripts.find_system_settings_general import (
            AX,
            activate,
            current_search_roots,
            find_enabled_pressable_text_candidates,
            get_running_system_settings,
        )

    email = expected_email.strip()
    if not email or "@" not in email:
        raise ProfileReadError("APPLE_ACCOUNT_EMAIL_INVALID")
    if not AX.AXIsProcessTrusted():
        raise ProfileReadError("ACCESSIBILITY_NOT_TRUSTED")

    pid = get_running_system_settings().processIdentifier()
    roots = current_search_roots(
        pid,
        auto_handle_mac_password=False,
        auto_handle_security_prompts=False,
    )
    initial_nodes = _text_snapshot(roots)
    birthday_page_visible = len(
        [node for node in initial_nodes if node.text == "Birthday"]
    ) == 1
    name = ""
    try:
        name = select_account_header_name(initial_nodes, email)
    except ProfileReadError as error:
        if not birthday_page_visible:
            if str(error) != "APPLE_ACCOUNT_NAME_NOT_UNIQUE":
                raise
        else:
            name = select_personal_information_name(initial_nodes, expected_name)
    if not birthday_page_visible:
        candidates = find_enabled_pressable_text_candidates(
            roots, ("Personal Information",)
        )
        if len(candidates) != 1:
            raise ProfileReadError("PERSONAL_INFORMATION_NOT_UNIQUE")
        activation_error = activate(candidates[0])
        time.sleep(3)
        if activation_error != AX.kAXErrorSuccess:
            page_verified = False
            for delay in (0, 1, 2):
                if delay:
                    time.sleep(delay)
                verification_roots = current_search_roots(
                    pid,
                    auto_handle_mac_password=False,
                    auto_handle_security_prompts=False,
                )
                verification_nodes = _text_snapshot(verification_roots)
                if len(
                    [node for node in verification_nodes if node.text == "Birthday"]
                ) == 1:
                    page_verified = True
                    break
            if not page_verified:
                raise ProfileReadError("PERSONAL_INFORMATION_ACTIVATION_FAILED")
    last_error: Exception | None = None
    last_roots: Sequence[Any] = ()
    for delay in (0, 5, 10):
        if delay:
            time.sleep(delay)
        roots = current_search_roots(
            pid,
            auto_handle_mac_password=False,
            auto_handle_security_prompts=False,
        )
        last_roots = roots
        try:
            text_nodes = _text_snapshot(roots)
            if not name:
                name = select_personal_information_name(text_nodes, expected_name)
            try:
                year, month, day = select_birthday_ax_value(
                    text_nodes, _value_snapshot(roots)
                )
            except ProfileReadError:
                year, month, day = select_birthday_display_value(
                    text_nodes, _displayed_value_snapshot(roots)
                )
            return {
                "name": name,
                "birth_year": year,
                "birth_month": month,
                "birth_day": day,
            }
        except ProfileReadError as error:
            last_error = error
    diagnostic = _birthday_source_diagnostic(last_roots)
    if diagnostic != "APPLE_BIRTHDAY_DISPLAY_SOURCE_MISSING":
        last_error = ProfileReadError(diagnostic)
    raise ProfileReadError("APPLE_BIRTHDAY_READ_EXHAUSTED") from last_error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdin-json", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ProfileReadError("STDIN_JSON_NOT_OBJECT")
        expected_email = payload.get("expected_email")
        if not isinstance(expected_email, str):
            raise ProfileReadError("APPLE_ACCOUNT_EMAIL_INVALID")
        expected_name = payload.get("expected_name", "")
        if not isinstance(expected_name, str):
            raise ProfileReadError("APPLE_ACCOUNT_NAME_INVALID")
        print(
            json.dumps(
                read_profile(expected_email, expected_name), ensure_ascii=False
            )
        )
        return 0
    except Exception as error:
        reason = str(error)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", reason):
            reason = type(error).__name__
        cause = error.__cause__
        cause_reason = str(cause) if cause is not None else ""
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", cause_reason):
            reason = f"{reason}:{cause_reason}"
        print(f"APPLE_ACCOUNT_PROFILE=blocked reason={reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
