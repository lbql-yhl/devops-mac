from __future__ import annotations

import sys
import io
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import apple_account_profile as profile
from scripts.apple_account_profile import (
    ProfileReadError,
    TextNode,
    ValueNode,
    normalize_account_name,
    parse_apple_birthday_ax_value,
    select_account_header_name,
    select_birthday_ax_value,
    select_personal_information_name,
)


ROOT = Path(__file__).resolve().parents[1]


def test_scripts_contain_no_hardcoded_reference_birthday() -> None:
    forbidden = re.compile(r"2001\s*(?:/|,)\s*1\s*(?:/|,)\s*1")
    matches = []
    for path in (ROOT / "scripts").rglob("*"):
        if path.is_file() and path.suffix in {".py", ".mjs", ".js", ".sh"}:
            if forbidden.search(path.read_text(encoding="utf-8")):
                matches.append(str(path.relative_to(ROOT)))

    assert matches == []


def test_account_name_keeps_internal_display_spacing() -> None:
    assert normalize_account_name("  Jan  Haren  ") == "Jan  Haren"
    with pytest.raises(ProfileReadError, match="APPLE_ACCOUNT_NAME_INVALID"):
        normalize_account_name("one-line@example.test")


def test_header_name_is_selected_from_smallest_scope_containing_unique_email() -> None:
    nodes = (
        TextNode((0, 0), "Jan Haren"),
        TextNode((0, 1), "fletchernancy0402@hotmail.com"),
        TextNode((1, 0), "Some Features Are Unavailable"),
        TextNode((2, 0), "Personal Information"),
    )
    assert (
        select_account_header_name(nodes, "fletchernancy0402@hotmail.com")
        == "Jan Haren"
    )


def test_header_name_rejects_duplicate_email_or_multiple_names_in_scope() -> None:
    duplicate_email = (
        TextNode((0, 0), "Jan Haren"),
        TextNode((0, 1), "account@example.test"),
        TextNode((1, 0), "account@example.test"),
    )
    with pytest.raises(ProfileReadError, match="APPLE_ACCOUNT_EMAIL_NOT_UNIQUE"):
        select_account_header_name(duplicate_email, "account@example.test")

    ambiguous_name = (
        TextNode((0, 0), "Jan Haren"),
        TextNode((0, 1), "Another Name"),
        TextNode((0, 2), "account@example.test"),
    )
    with pytest.raises(ProfileReadError, match="APPLE_ACCOUNT_NAME_NOT_UNIQUE"):
        select_account_header_name(ambiguous_name, "account@example.test")


def test_profile_uses_personal_name_when_header_scope_is_ambiguous(
    monkeypatch,
) -> None:
    initial_nodes = (
        TextNode((0, 0), "First Candidate"),
        TextNode((0, 1), "Second Candidate"),
        TextNode((0, 2), "account@example.test"),
        TextNode((1, 0), "Personal Information"),
    )
    personal_nodes = (
        TextNode((0, 0), "Personal Information"),
        TextNode((0, 1, 0), "Name"),
        TextNode((0, 1, 1), "Correct Person"),
        TextNode((0, 2, 0), "Birthday"),
    )
    initial_roots = ("initial",)
    personal_roots = ("personal",)
    roots = iter((initial_roots, personal_roots))

    fake_ax = SimpleNamespace(
        AXIsProcessTrusted=lambda: True,
        kAXErrorSuccess=0,
    )
    fake_module = SimpleNamespace(
        AX=fake_ax,
        activate=lambda _candidate: 0,
        current_search_roots=lambda *_args, **_kwargs: next(roots),
        find_enabled_pressable_text_candidates=lambda *_args, **_kwargs: [object()],
        get_running_system_settings=lambda: SimpleNamespace(
            processIdentifier=lambda: 123
        ),
    )
    monkeypatch.setitem(sys.modules, "find_system_settings_general", fake_module)
    monkeypatch.setattr(
        profile,
        "_text_snapshot",
        lambda current: initial_nodes if current == initial_roots else personal_nodes,
    )
    monkeypatch.setattr(profile, "_value_snapshot", lambda _roots: ())
    monkeypatch.setattr(
        profile,
        "select_birthday_ax_value",
        lambda _nodes, _values: (2001, 4, 13),
    )
    monkeypatch.setattr(profile.time, "sleep", lambda _seconds: None)

    assert profile.read_profile("account@example.test") == {
        "name": "Correct Person",
        "birth_year": 2001,
        "birth_month": 4,
        "birth_day": 13,
    }


def test_profile_accepts_ax_error_when_personal_page_is_read_back(
    monkeypatch,
) -> None:
    initial_nodes = (
        TextNode((0, 0), "Correct Person"),
        TextNode((0, 1), "account@example.test"),
        TextNode((1, 0), "Personal Information"),
    )
    personal_nodes = (
        TextNode((0, 0), "Personal Information"),
        TextNode((0, 1, 0), "Name"),
        TextNode((0, 1, 1), "Correct Person"),
        TextNode((0, 2, 0), "Birthday"),
    )
    calls = 0

    def current_roots(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return ("initial",) if calls == 1 else ("personal",)

    fake_ax = SimpleNamespace(
        AXIsProcessTrusted=lambda: True,
        kAXErrorSuccess=0,
    )
    fake_module = SimpleNamespace(
        AX=fake_ax,
        activate=lambda _candidate: -25204,
        current_search_roots=current_roots,
        find_enabled_pressable_text_candidates=lambda *_args, **_kwargs: [object()],
        get_running_system_settings=lambda: SimpleNamespace(
            processIdentifier=lambda: 123
        ),
    )
    monkeypatch.setitem(sys.modules, "find_system_settings_general", fake_module)
    monkeypatch.setattr(
        profile,
        "_text_snapshot",
        lambda roots: initial_nodes if roots == ("initial",) else personal_nodes,
    )
    monkeypatch.setattr(profile, "_value_snapshot", lambda _roots: ())
    monkeypatch.setattr(
        profile,
        "select_birthday_ax_value",
        lambda _nodes, _values: (2001, 4, 13),
    )
    monkeypatch.setattr(profile.time, "sleep", lambda _seconds: None)

    assert profile.read_profile("account@example.test") == {
        "name": "Correct Person",
        "birth_year": 2001,
        "birth_month": 4,
        "birth_day": 13,
    }


def test_name_is_read_from_the_personal_information_name_row_on_resume() -> None:
    nodes = (
        TextNode((0, 0), "Personal Information"),
        TextNode((0, 1, 0), "Name"),
        TextNode((0, 1, 1), "FERDIOL ERTURK"),
        TextNode((0, 2, 0), "Birthday"),
        TextNode((0, 2, 1), "4/13/2001"),
    )

    assert select_personal_information_name(nodes) == "FERDIOL ERTURK"


def test_existing_notion_name_is_verified_against_the_personal_information_page() -> None:
    nodes = (
        TextNode((0, 0), "FERDIOL ERTURK"),
        TextNode((1, 0), "Personal Information"),
        TextNode((1, 1), "Name"),
        TextNode((2, 0), "FERDIOL ERTURK"),
        TextNode((2, 1), "Birthday"),
    )

    assert (
        select_personal_information_name(nodes, "FERDIOL ERTURK")
        == "FERDIOL ERTURK"
    )
    assert select_personal_information_name(nodes, "OTHER PERSON") == "OTHER PERSON"


def test_numeric_ax_values_are_never_interpreted_as_birthday() -> None:
    labels = (TextNode((1, 0), "Birthday"),)
    values = (
        ValueNode((1, 1), "AXIncrementor", 37_584_000.0),
    )

    with pytest.raises(ProfileReadError, match="APPLE_BIRTHDAY_INVALID"):
        parse_apple_birthday_ax_value(37_584_000.0)
    with pytest.raises(ProfileReadError, match="APPLE_BIRTHDAY_NOT_UNIQUE"):
        select_birthday_ax_value(labels, values)


def test_birthday_prefers_the_real_nsdate_over_the_zero_incrementor() -> None:
    class FakeNSDate:
        def timeIntervalSinceReferenceDate(self) -> float:
            return 102 * 86_400

    labels = (TextNode((1, 0), "Birthday"),)
    values = (
        ValueNode((1, 1), "AXIncrementor", 0.5),
        ValueNode((1, 2), "AXDateTimeArea", FakeNSDate()),
    )

    assert parse_apple_birthday_ax_value(FakeNSDate()) == (2001, 4, 13)
    assert select_birthday_ax_value(labels, values) == (2001, 4, 13)


def test_birthday_does_not_fall_back_to_an_unscoped_ax_date() -> None:
    labels = (TextNode((1, 0), "Birthday"),)
    values = (
        ValueNode((0, 1), "AXDateField", 102.0),
        ValueNode((1, 1), "AXIncrementor", 0.0),
    )

    with pytest.raises(ProfileReadError, match="APPLE_BIRTHDAY_NOT_UNIQUE"):
        select_birthday_ax_value(labels, values)


def test_birthday_reads_only_the_scoped_incrementor_display_value() -> None:
    selector = getattr(profile, "select_birthday_display_value", None)
    assert selector is not None

    labels = (TextNode((1, 0), "Birthday"),)
    values = (
        ValueNode((0, 1), "AXIncrementor", "9/9/1999"),
        ValueNode((1, 1), "AXIncrementor", "4/13/2001"),
    )

    assert selector(labels, values) == (2001, 4, 13)


def test_displayed_value_snapshot_reads_only_incrementor_value_description(
    monkeypatch,
) -> None:
    snapshot = getattr(profile, "_displayed_value_snapshot", None)
    assert snapshot is not None

    ax = SimpleNamespace(
        kAXRoleAttribute="role",
        kAXValueAttribute="value",
        kAXValueDescriptionAttribute="value_description",
        kAXChildrenAttribute="children",
    )
    label = {"role": "AXStaticText", "value_description": "9/9/1999"}
    incrementor = {
        "role": "AXIncrementor",
        "value_description": "4/13/2001",
    }
    root = {"children": [label, incrementor]}
    fake_module = SimpleNamespace(
        AX=ax,
        copy_attribute=lambda element, attribute: element.get(attribute),
    )
    monkeypatch.setitem(sys.modules, "find_system_settings_general", fake_module)

    assert snapshot((root,)) == (
        ValueNode((0, 1), "AXIncrementor", "4/13/2001"),
    )


def test_displayed_value_snapshot_reads_incrementor_string_value(monkeypatch) -> None:
    snapshot = profile._displayed_value_snapshot
    ax = SimpleNamespace(
        kAXRoleAttribute="role",
        kAXValueAttribute="value",
        kAXValueDescriptionAttribute="value_description",
        kAXChildrenAttribute="children",
    )
    incrementor = {"role": "AXIncrementor", "value": "4/13/2001"}
    root = {"children": [incrementor]}
    fake_module = SimpleNamespace(
        AX=ax,
        copy_attribute=lambda element, attribute: element.get(attribute),
    )
    monkeypatch.setitem(sys.modules, "find_system_settings_general", fake_module)

    assert snapshot((root,)) == (
        ValueNode((0, 0), "AXIncrementor", "4/13/2001"),
    )


def test_text_snapshot_ignores_value_description(monkeypatch) -> None:
    ax = SimpleNamespace(
        kAXTitleAttribute="title",
        kAXValueAttribute="value",
        kAXDescriptionAttribute="description",
        kAXValueDescriptionAttribute="value_description",
        kAXHelpAttribute="help",
        kAXChildrenAttribute="children",
    )
    label = {"title": "Birthday"}
    incrementor = {"value": 0.0, "value_description": "4/13/2001"}
    root = {"children": [label, incrementor]}
    attributes_read = []

    def copy_attribute(element, attribute):
        attributes_read.append(attribute)
        return element.get(attribute)

    fake_module = SimpleNamespace(
        AX=ax,
        copy_attribute=copy_attribute,
    )
    monkeypatch.setitem(sys.modules, "find_system_settings_general", fake_module)

    nodes = profile._text_snapshot((root,))

    assert "value_description" not in attributes_read
    assert TextNode((0, 1), "4/13/2001") not in nodes


def test_profile_main_reports_the_terminal_birthday_read_cause(
    monkeypatch, capsys
) -> None:
    def fail_profile(_email: str, _name: str = ""):
        try:
            raise ProfileReadError("APPLE_BIRTHDAY_NOT_UNIQUE")
        except ProfileReadError as cause:
            raise ProfileReadError("APPLE_BIRTHDAY_READ_EXHAUSTED") from cause

    monkeypatch.setattr(profile, "read_profile", fail_profile)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO('{"expected_email":"account@example.test"}')
    )

    assert profile.main(["--stdin-json"]) == 1
    assert capsys.readouterr().err.strip() == (
        "APPLE_ACCOUNT_PROFILE=blocked "
        "reason=APPLE_BIRTHDAY_READ_EXHAUSTED:APPLE_BIRTHDAY_NOT_UNIQUE"
    )
