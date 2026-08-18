#!/usr/bin/env python3
import sys
import inspect
import plistlib
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.utm_1_ax import (  # noqa: E402
    AXTargetError,
    MACSequenceError,
    is_valid_mac,
    is_valid_vm_name,
    matching_ax_nodes,
    normalize_mac,
    parse_utm_status,
    path_match_count,
    require_unique_ax_node,
    require_unique_labeled_node,
    config_uuid,
    running_status_verified,
    stopped_status_verified,
    verify_mac_sequence,
    checkbox_is_checked,
    unique_mac,
    exact_vm_card_nodes,
    unique_ax_mac_value,
)


def test_directory_picker_prefers_exact_shared_control_over_global_new(monkeypatch) -> None:
    from scripts import utm_1_ax

    shared_button = object()
    global_button = object()
    calls: list[tuple[tuple[str, ...], set[str] | None]] = []

    def fake_live_labeled_nodes(_ax, _root, labels, *, exact=True, roles=None):
        normalized = tuple(labels)
        calls.append((normalized, roles))
        if normalized == utm_1_ax.CHOOSE_DIRECTORY_LABELS:
            return []
        if normalized == utm_1_ax.ADD_SHARED_LABELS:
            return [shared_button]
        if normalized == ("新建…", "New…") and roles == {"AXButton"}:
            return [global_button]
        raise AssertionError((normalized, roles, exact))

    monkeypatch.setattr(utm_1_ax, "live_labeled_nodes", fake_live_labeled_nodes)

    assert utm_1_ax.unique_directory_picker_control(object(), object()) is shared_button
    assert calls == [
        (utm_1_ax.CHOOSE_DIRECTORY_LABELS, {"AXButton", "AXMenuButton"}),
        (utm_1_ax.ADD_SHARED_LABELS, {"AXButton"}),
    ]


def test_directory_picker_rejects_global_new_without_shared_control(monkeypatch) -> None:
    from scripts import utm_1_ax

    global_button = object()

    def fake_live_labeled_nodes(_ax, _root, labels, *, exact=True, roles=None):
        normalized = tuple(labels)
        if normalized == utm_1_ax.CHOOSE_DIRECTORY_LABELS:
            return []
        if normalized == utm_1_ax.ADD_SHARED_LABELS:
            return []
        if normalized == ("新建…", "New…") and roles == {"AXButton"}:
            return [global_button]
        raise AssertionError((normalized, roles, exact))

    monkeypatch.setattr(utm_1_ax, "live_labeled_nodes", fake_live_labeled_nodes)

    with pytest.raises(AXTargetError, match="shared control count=0"):
        utm_1_ax.unique_directory_picker_control(object(), object())


def test_utm_path_paste_waits_before_clearing_clipboard(monkeypatch) -> None:
    from scripts import utm_1_ax

    events: list[str] = []

    def fake_run(command, *, input=None, check):
        if command == ["pbcopy"] and input:
            events.append("copy")
        elif command == ["pbcopy"]:
            events.append("clear")
        else:
            events.append("paste")

    monkeypatch.setattr(utm_1_ax.subprocess, "run", fake_run)
    monkeypatch.setattr(utm_1_ax, "wait_and_reacquire", lambda: events.append("wait"))

    utm_1_ax.paste_path_into_utm_panel(Path("/tmp/shared"))

    assert events == ["copy", "paste", "wait", "clear"]


def main() -> None:
    assert is_valid_vm_name("dxgg")
    assert not is_valid_vm_name("DXGG")
    assert not is_valid_vm_name("dxg")
    assert len(exact_vm_card_nodes([{"role": "AXStaticText", "value": "yhlw"}], "yhlw")) == 1
    assert exact_vm_card_nodes([{"role": "AXStaticText", "value": "yhlw"}], "qwer") == []
    from scripts import utm_1_ax
    selection_source = inspect.getsource(utm_1_ax.select_exact_vm_card)
    assert "_ancestor_with_action(ax, cards[0], ax.kAXPressAction)" in selection_source
    assert "press_ax(ax, pressable)" in selection_source
    assert "_ancestor_with_settable_attribute(" in selection_source
    assert "click_ax_element(cards[0])" in selection_source
    run_source = inspect.getsource(utm_1_ax.run)
    assert 'subprocess.run(["open", str(bundle)], check=True)' in run_source
    assert unique_ax_mac_value(
        [
            {"role": "AXTextField", "value": "02:00:00:00:00:01"},
            {"role": "AXTextField", "value": "not-a-mac"},
        ]
    ) == "02:00:00:00:00:01"
    assert normalize_mac("02:05:9E:71:AA:C8") == "02:05:9e:71:aa:c8"
    assert is_valid_mac("02:05:9e:71:aa:c8")
    assert not is_valid_mac("01:05:9e:71:aa:c8")
    assert not is_valid_mac("02:05:9e:71:aa")
    assert unique_mac("02:05:9e:71:aa:c8", {"02:05:9e:71:aa:c8"}) != "02:05:9e:71:aa:c8"

    nodes = [
        {"role": "AXButton", "title": "", "description": "新建共享目录…", "identifier": ""},
        {"role": "AXButton", "title": "", "description": "运行", "identifier": "play"},
    ]
    result = matching_ax_nodes(nodes, description="新建共享目录…")
    assert len(result) == 1
    assert result[0]["identifier"] == ""
    category_nodes = [
        {"role": "AXButton", "description": "共享"},
        {"role": "AXUnknown", "description": "共享"},
    ]
    assert require_unique_ax_node(category_nodes, role="AXUnknown", description="共享")["role"] == "AXUnknown"
    assert require_unique_ax_node(nodes, description="运行")["identifier"] == "play"
    assert require_unique_labeled_node(nodes, ("新建共享目录…", "Add Shared Directory…"), description=True)["description"] == "新建共享目录…"
    try:
        require_unique_ax_node(nodes, description="不存在")
    except AXTargetError as error:
        assert "count=0" in str(error)
    else:
        raise AssertionError("missing AX target was accepted")
    try:
        require_unique_ax_node(
            nodes + [{"role": "AXButton", "title": "", "description": "运行"}],
            description="运行",
        )
    except AXTargetError as error:
        assert "count=2" in str(error)
    else:
        raise AssertionError("ambiguous AX target was accepted")

    with tempfile.TemporaryDirectory() as directory:
        bundle = Path(directory) / "dxgg.utm"
        bundle.mkdir()
        (bundle / "config.plist").write_bytes(
            plistlib.dumps({"Information": {"UUID": "11111111-1111-1111-1111-111111111111"}})
        )
        assert config_uuid(bundle) == "11111111-1111-1111-1111-111111111111".upper()

    assert path_match_count([{"value": "/tmp/share"}, {"value": "/other"}], "/tmp/share") == 1
    assert checkbox_is_checked(True)
    assert checkbox_is_checked("1")
    assert not checkbox_is_checked(False)

    assert parse_utm_status("started\n") == "started"
    assert parse_utm_status("running\n") == "running"
    assert parse_utm_status("stopped\n") == "stopped"
    assert running_status_verified(["started", "running"])
    assert not running_status_verified(["started", "stopped"])
    assert stopped_status_verified(["stopped", "stopped"])
    assert not stopped_status_verified(["stopped", "started"])
    try:
        parse_utm_status("started\nstopped\n")
    except RuntimeError as error:
        assert "ambiguous" in str(error)
    else:
        raise AssertionError("ambiguous UTM status was accepted")

    macs = [
        "02:00:00:00:00:01",
        "02:00:00:00:00:02",
        "02:00:00:00:00:03",
        "02:00:00:00:00:04",
    ]
    assert verify_mac_sequence(macs) == [normalize_mac(value) for value in macs]
    try:
        verify_mac_sequence(macs[:2] + [macs[1], macs[3]])
    except MACSequenceError as error:
        assert "changed" in str(error)
    else:
        raise AssertionError("unchanged MAC round was accepted")
    print("UTM_1_AX_LOGIC=verified")


if __name__ == "__main__":
    main()
