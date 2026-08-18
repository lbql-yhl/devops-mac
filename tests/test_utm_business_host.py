import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import utm_business


def test_business_entry_syncs_and_hash_verifies_guest_scripts_before_preflight() -> None:
    source = (ROOT / "scripts/utm_business.py").read_text(encoding="utf-8")
    assert "from scripts.utm_business_delivery import sync_business_guest_files" in source
    run_start = source.index("def run(")
    run_end = source.index("def build_parser", run_start)
    run_source = source[run_start:run_end]
    sync = run_source.index("sync_business_guest_files(")
    preflight = run_source.index("expected_session = _read_guest_session(")
    assert sync < preflight


def test_business_entry_preserves_the_guest_error_detail() -> None:
    source = (ROOT / "scripts/utm_business.py").read_text(encoding="utf-8")
    main_start = source.index("def main()")
    assert "preserve_error_detail=True" in source[main_start:]


ROOT = Path(__file__).resolve().parents[1]


class FakeNotion:
    def __init__(self, fields: dict[tuple[str, str], str]) -> None:
        self.fields = fields
        self.verified: list[str] = []

    def verify_parent(self, title: str) -> str:
        self.verified.append(title)
        return title

    def read_field(self, title: str, section: str, label: str) -> str:
        assert title in {"Demo-abcd", "SowSheet-igec"}
        return self.fields[(section, label)]


def notion_fields() -> dict[tuple[str, str], str]:
    return {
        ("账号信息", "生日（格式年/月/日）："): "1999/3/9",
        ("账号信息", "电话："): "+1 555 0100",
        ("账号信息", "电话短信接收平台："): "https://api1997.com/smsrecord?token=redacted",
        ("账号信息", "ABA Routing Number："): "123456789",
        ("账号信息", "Account Number："): "12345678",
        ("应用信息", "应用名: "): "Demo",
        ("应用信息", "正式包名: "): "com.example.demo",
    }


def guest_result() -> dict[str, object]:
    return {
        "EDGE_EXISTING_PID": "verified",
        "EDGE_PID": 4242,
        "EDGE_WEBSOCKET": "ws://127.0.0.1:9222/devtools/browser/one",
        "BUSINESS_TEXT": "verified business payload",
        "BUSINESS_RESULT": "verified",
        "DSA_COMPLIANCE": "verified",
        "PAID_APPS_AGREEMENT": "accepted_or_existing",
        "US_TAX_QUESTIONNAIRE": "No_No_saved",
        "FOREIGN_STATUS_FORM": "verified",
        "W8BEN_SUBMIT": "verified",
        "BANK_ACCOUNT_PROCESSING": "verified",
        "DAC7_READBACK": "No_saved",
        "DSA_STATUS": "existing",
        "LEGAL_ENTITY_STATUS": "completed",
        "PAID_APPS_STATUS": "existing",
        "TAX_QUESTIONNAIRE_STATUS": "existing",
        "FOREIGN_STATUS_FORM_STATUS": "existing",
        "W8BEN_STATUS": "existing",
        "BANK_ACCOUNT_STATUS": "existing",
        "DAC7_STATUS": "existing",
        "UTM_BUSINESS": "verified",
    }


def test_host_payload_comes_from_notion_without_manual_business_values() -> None:
    api = FakeNotion(notion_fields())
    payload = utm_business.notion_payload(
        api,
        parent_title="海淋",
        page_title="Demo-abcd",
        sleeper=lambda _seconds: None,
    )
    assert payload["APP_NAME"] == "Demo"
    assert payload["BIRTHDAY"] == "1999-03-09"
    assert payload["ABA_ROUTING_NUMBER"] == "123456789"
    assert payload["ACCOUNT_NUMBER"] == "12345678"
    assert api.verified


def test_host_birthday_accepts_only_notion_year_month_day_order() -> None:
    assert utm_business._normalize_birthday("1999/3/9") == "1999-03-09"
    assert utm_business._normalize_birthday("1999-03-09") == "1999-03-09"
    with pytest.raises(
        utm_business.UTMBusinessError, match="NOTION_BIRTHDAY_INVALID"
    ):
        utm_business._normalize_birthday("3/9/1999")


def test_guest_execution_uses_only_playwright_and_ssh_stdin() -> None:
    command = utm_business.guest_execution_command("abcd")
    assert "/Users/example/Downloads/AppleAccountScriptsBackup/utm_business_one.mjs" in command
    assert "exec node" in command
    for forbidden in (
        "utm_business_guest.py",
        "utm_business_workflow.py",
        "edge_accessibility.py",
        "scp",
    ):
        assert forbidden not in command


def test_guest_preflight_reads_live_playwright_session_without_legacy_ledger() -> None:
    command = utm_business.guest_preflight_command(
        "abcd", "direct-v1-abcd-0123456789abcdef01234567"
    )
    for filename in (
        "session.mjs",
        "utm_business_one.mjs",
    ):
        assert f"/Users/example/Downloads/AppleAccountScriptsBackup/{filename}" in command
    assert "currentSessionIdentity" in command
    assert ".utm-apps-state" not in command
    assert "completed_stages" not in command


def test_guest_result_requires_same_edge_session_and_all_markers() -> None:
    result = utm_business.parse_guest_result(json.dumps(guest_result()))
    utm_business.assert_same_edge_session(
        result, (4242, "ws://127.0.0.1:9222/devtools/browser/one")
    )
    with pytest.raises(utm_business.UTMBusinessError):
        utm_business.assert_same_edge_session(
            result, (4243, "ws://127.0.0.1:9222/devtools/browser/one")
        )
    incomplete = guest_result()
    incomplete.pop("DAC7_READBACK")
    with pytest.raises(
        utm_business.UTMBusinessError,
        match="UTM_BUSINESS_MARKERS_INCOMPLETE",
    ):
        utm_business.parse_guest_result(json.dumps(incomplete))


def test_cli_has_no_manual_business_value_arguments() -> None:
    parser = utm_business.build_parser()
    options = {
        option
        for action in parser._actions
        for option in action.option_strings
    }
    assert {
        "--run-id", "--page-title", "--vm-name", "--vm-ip", "--vm-user", "--stage"
    } <= options
    for forbidden in (
        "--birthday", "--app-name", "--parent-title",
        "--routing-number", "--account-number", "--sms-url",
    ):
        assert forbidden not in options


def test_bank_stage_guest_result_is_minimal() -> None:
    result = utm_business.parse_bank_guest_result(json.dumps({
        "EDGE_EXISTING_PID": "verified",
        "EDGE_PID": 4242,
        "EDGE_WEBSOCKET": "ws://127.0.0.1:9222/devtools/browser/one",
        "BANK_ACCOUNT_STATUS": "completed",
        "UTM_BUSINESS_BANK_STAGE": "verified",
    }))
    assert result.data["BANK_ACCOUNT_STATUS"] == "completed"


def test_direct_mode_uses_context_and_clean_step_output(monkeypatch, capsys) -> None:
    fields = notion_fields()
    fields[("应用信息", "应用名: ")] = "SowSheet"
    fields[("应用信息", "正式包名: ")] = "com.example.sowsheet"
    notion = FakeNotion(fields)
    direct_id = "direct-v1-igec-0123456789abcdef01234567"
    context = SimpleNamespace(
        context_id=direct_id,
        parent_title="海淋",
        page_title="SowSheet-igec",
        app_name="SowSheet",
    )
    payloads: list[dict[str, object]] = []

    monkeypatch.setattr(
        utm_business, "resolve_direct_context", lambda **_kwargs: context
    )
    monkeypatch.setattr(
        utm_business,
        "_read_guest_session",
        lambda *_args, **_kwargs: (
            4242,
            "ws://127.0.0.1:9222/devtools/browser/one",
        ),
    )
    monkeypatch.setattr(
        utm_business,
        "sync_business_guest_files",
        lambda *_args, **_kwargs: {},
    )

    def ssh_run(*_args: object, **kwargs: object):
        payloads.append(json.loads(kwargs["input_bytes"].decode("utf-8")))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(guest_result()).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(utm_business, "_ssh_run", ssh_run)
    monkeypatch.setattr(
        utm_business,
        "write_business_section",
        lambda *_args, **_kwargs: "written",
    )
    assert utm_business.run(
        SimpleNamespace(
            run_id=None,
            page_title="SowSheet-igec",
            vm_name="igec",
            vm_ip="192.168.64.52",
            vm_user="igec",
        ),
        api=notion,
        password_env={},
    ) == 0
    assert payloads[0]["CONTEXT_ID"] == direct_id
    assert payloads[0]["EXPECTED_EDGE_PID"] == 4242
    output = capsys.readouterr().out
    assert "UTM-Business DSA步骤检查已存在，跳过" in output
    assert "UTM-Business Legal Entity步骤已完成" in output
    assert "UTM_BUSINESS_APP_NAME=SowSheet" in output
    assert "UTM_BUSINESS_VM_NAME=igec" in output
    assert "UTM_BUSINESS_EDGE_PID=4242" in output
    assert "UTM_BUSINESS_NOTION=written" in output
    assert "UTM_BUSINESS_ELAPSED_SECONDS=" in output
    assert "UTM_BUSINESS=verified" in output
    assert "UTM-Business App Store Connect Business步骤已完成" in output
    assert "UTM_BUSINESS_DIRECT_CONTEXT" not in output


def test_host_preserves_the_complete_actual_guest_error(monkeypatch) -> None:
    fields = notion_fields()
    fields[("应用信息", "应用名: ")] = "SowSheet"
    fields[("应用信息", "正式包名: ")] = "com.example.sowsheet"
    notion = FakeNotion(fields)
    direct_id = "direct-v1-igec-0123456789abcdef01234567"
    context = SimpleNamespace(
        context_id=direct_id,
        parent_title="海淋",
        page_title="SowSheet-igec",
        app_name="SowSheet",
    )
    detail = (
        "radio for I'm not a trader under the DSA or I don't plan to distribute "
        "in the EU must be visible — found 0；"
        + "z" * 260
    )

    monkeypatch.setattr(
        utm_business, "resolve_direct_context", lambda **_kwargs: context
    )
    monkeypatch.setattr(
        utm_business,
        "_read_guest_session",
        lambda *_args, **_kwargs: (
            4242,
            "ws://127.0.0.1:9222/devtools/browser/one",
        ),
    )
    monkeypatch.setattr(
        utm_business,
        "sync_business_guest_files",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        utm_business,
        "_ssh_run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=(
                "UTM_BUSINESS_PROGRESS=步骤1已完成，跳过\n"
                f"UTM_BUSINESS_ERROR={detail}\n"
            ).encode(),
        ),
    )

    with pytest.raises(utm_business.UTMBusinessError) as captured:
        utm_business.run(
            SimpleNamespace(
                run_id=None,
                page_title="SowSheet-igec",
                vm_name="igec",
                vm_ip="192.168.64.52",
                vm_user="igec",
                stage="all",
            ),
            api=notion,
            password_env={},
        )

    assert str(captured.value) == (
        f"UTM_BUSINESS_PLAYWRIGHT_STAGE_FAILED:{detail}"
    )


def test_host_and_guest_sources_have_no_business_python_or_accessibility() -> None:
    source = "\n".join(
        (ROOT / "scripts" / name).read_text(encoding="utf-8")
        for name in (
            "utm_business.py",
            "utm_business_one.mjs",
        )
    )
    for forbidden in (
        "utm_business_guest.py",
        "utm_business_workflow.py",
        "edge_accessibility.py",
        "Accessibility",
        "UTM_14",
        "utm-14",
        "utm-15",
        "selenium",
        "webdriver",
    ):
        assert forbidden not in source
