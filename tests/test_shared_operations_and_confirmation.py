#!/usr/bin/env python3
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.shared_operations import browser_clipboard_value  # noqa: E402
from services.feishu_bot import (  # noqa: E402
    build_confirmation_card,
    create_or_update_confirmation,
    ensure_decision_card_delivered,
    handle_card_action as _handle_card_action,
    notify_confirmation,
    user_confirmation_api_decision,
    wait_decision,
)


REPORT_ONLY_CHAT = "-".join(("daily", "report", "chat"))
FIXTURE_ORIGINAL_CHAT = "original-chat"


def handle_card_action(payload: dict, submission_host_machine: str) -> dict:
    return _handle_card_action(payload, submission_host_machine, REPORT_ONLY_CHAT)


def callback_payload(decision: str, decision_id: str) -> dict:
    return {
        "event": {
            "operator": {"open_id": "approver"},
            "action": {
                "value": {
                    "action": "submission_confirmation_decision",
                    "decision": decision,
                    "decision_id": decision_id,
                    "run_id": "confirmation-test",
                }
            },
        }
    }


def main() -> None:
    assert browser_clipboard_value("https://example.com/a?x=1") == "example.com/a?x=1"
    assert browser_clipboard_value("http://example.com") == "example.com"
    assert browser_clipboard_value("HTTPS://Example.COM/A?next=https://other.test/x") == (
        "Example.COM/A?next=https://other.test/x"
    )
    assert browser_clipboard_value("example.com/fixed", allow_bare=True) == "example.com/fixed"
    for invalid, allow_bare in (
        ("example.com", False),
        ("ftp://example.com", False),
        (" https://example.com", False),
        ("https://example.com\n", False),
        ("https://", False),
        ("https://https://example.com", False),
        ("https://ftp://example.com", False),
        ("https://example.com/\x00", False),
    ):
        try:
            browser_clipboard_value(invalid, allow_bare=allow_bare)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe browser clipboard value accepted: {invalid!r}")

    run = {
        "id": "confirmation-test",
        "app_name": "Xrimo",
        "chat_id": "original-chat",
        "submission_data": {"host_machine": "海淋"},
        "pending_decision": {
            "kind": "confirmation",
            "status": "waiting",
            "decision": "",
            "decision_id": "decision-1",
            "stage": "utm-test:irreversible-step",
            "current_skill": "utm-test",
            "question": "是否继续当前不可逆操作？",
            "action_summary": "只执行一次目标操作",
            "evidence": "TARGET=verified",
            "message_uuid": "confirmation-message-uuid",
        },
        "events": [],
    }
    card = build_confirmation_card(run)
    rendered_card = str(card)
    for required_evidence in (
        "utm-test:irreversible-step",
        "是否继续当前不可逆操作？",
        "只执行一次目标操作",
        "TARGET=verified",
    ):
        assert required_evidence in rendered_card
    buttons = [item for item in card["body"]["elements"] if item["tag"] == "button"]
    assert [button["text"]["content"] for button in buttons] == ["取消并停止", "确认并继续"]
    confirm_value = buttons[1]["behaviors"][0]["value"]
    assert confirm_value == {
        "action": "submission_confirmation_decision",
        "decision": "confirm_continue",
        "decision_id": "decision-1",
        "run_id": "confirmation-test",
    }

    mutable_run = {
        **run,
        "pending_decision": dict(run["pending_decision"]),
        "events": [],
    }

    def mutate_confirmation(run_id: str, mutator):
        assert run_id == "confirmation-test"
        mutator(mutable_run)
        return mutable_run

    with (
        patch("services.feishu_bot.find_run", return_value=mutable_run),
        patch("services.feishu_bot.mutate_run", side_effect=mutate_confirmation),
        patch("services.feishu_bot.append_card_callback_log"),
    ):
        result = handle_card_action(callback_payload("confirm_continue", "decision-1"), "海淋")
    assert result["toast"]["content"] == "已确认，正在继续流程"
    assert mutable_run["status"] == "decision_confirm_continue"
    assert mutable_run["pending_decision"]["status"] == "answered"
    assert mutable_run["pending_decision"]["decision"] == "confirm_continue"
    assert mutable_run["pending_decision"]["operator_id"] == "approver"

    safety_confirmation_run = {
        **run,
        "id": "safety-policy-confirmation",
        "app_name": "yehailin",
        "submission_data": {"host_machine": "yehailin"},
        "pending_decision": {
            **run["pending_decision"],
            "decision_id": "safety-decision-1",
            "stage": "utm-test:safety-policy",
            "question": "检测到安全策略限制，是否允许继续当前操作？",
            "action_summary": "测试操作",
            "evidence": "SECURITY_POLICY=user_confirmation_required",
        },
    }

    class FakeResponse:
        returncode = 0
        stderr = b""

        @property
        def stdout(self):
            return json.dumps(
                {
                    "ok": True,
                    "approved": True,
                    "can_operate": True,
                    "status": "approved",
                }
            ).encode()

    def fake_curl(cmd, input, stdout, stderr, timeout, check):
        assert cmd == [
            "curl",
            "-sS",
            "--fail-with-body",
            "-X",
            "POST",
            "http://127.0.0.1:8000/confirm",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
        ]
        assert timeout == 1
        assert check is False
        assert stdout is not None
        assert stderr is not None
        assert json.loads(input.decode()) == {
            "宿主机名": "yehailin",
            "应用名": "yehailin",
            "要确认的动作": "测试操作",
        }
        return FakeResponse()

    with patch("services.feishu_bot.subprocess.run", side_effect=fake_curl):
        api_decision, api_response = user_confirmation_api_decision(
            safety_confirmation_run,
            "测试操作",
            SimpleNamespace(
                user_confirm_api_url="http://127.0.0.1:8000/confirm",
                user_confirm_timeout_seconds=1,
            ),
        )
    assert api_decision == "confirm_continue"
    assert json.loads(api_response)["status"] == "approved"

    api_run = {
        "id": "safety-policy-confirmation",
        "app_name": "yehailin",
        "chat_id": "original-chat",
        "submission_data": {"host_machine": "yehailin"},
        "status": "running",
        "events": [],
    }

    def mutate_api_confirmation(run_id: str, mutator):
        assert run_id == "safety-policy-confirmation"
        mutator(api_run)
        return api_run

    notify_api_args = SimpleNamespace(
        run_id="safety-policy-confirmation",
        chat_id=FIXTURE_ORIGINAL_CHAT,
        stage="utm-test:safety-policy",
        current_skill="utm-test",
        confirmation_question="检测到安全策略限制，是否允许继续当前操作？",
        confirmation_action="测试操作",
        evidence="SECURITY_POLICY=user_confirmation_required",
    )
    stdout = StringIO()
    with (
        patch("services.feishu_bot.find_run", return_value=api_run),
        patch("services.feishu_bot.mutate_run", side_effect=mutate_api_confirmation),
        patch("services.feishu_bot.subprocess.run", side_effect=fake_curl),
        patch("services.feishu_bot.FeishuClient.send_card") as forbidden_send_card,
        redirect_stdout(stdout),
    ):
        assert notify_confirmation(
            notify_api_args,
            SimpleNamespace(
                submission_host_machine="yehailin",
                user_confirm_api_url="http://127.0.0.1:8000/confirm",
                user_confirm_timeout_seconds=1,
            ),
        ) == 0
    assert stdout.getvalue().strip() == "safety-policy-confirmation"
    assert api_run["status"] == "decision_confirm_continue"
    assert api_run["pending_decision"]["status"] == "answered"
    assert api_run["pending_decision"]["operator_id"] == "user-api"
    forbidden_send_card.assert_not_called()
    stdout = StringIO()
    with patch("services.feishu_bot.find_run", return_value=api_run), redirect_stdout(stdout):
        assert wait_decision(
            SimpleNamespace(
                run_id="safety-policy-confirmation",
                decision_kind="confirmation",
                timeout_seconds=3600,
                poll_seconds=1,
            ),
            object(),
        ) == 0
    assert stdout.getvalue().strip() == "confirm_continue"

    wait_args = SimpleNamespace(
        run_id="confirmation-test",
        decision_kind="confirmation",
        timeout_seconds=3600,
        poll_seconds=1,
    )
    stdout = StringIO()
    with patch("services.feishu_bot.find_run", return_value=mutable_run), redirect_stdout(stdout):
        assert wait_decision(wait_args, object()) == 0
    assert stdout.getvalue().strip() == "confirm_continue"

    cancel_run = {
        **run,
        "pending_decision": dict(run["pending_decision"]),
        "events": [],
    }

    def mutate_cancellation(run_id: str, mutator):
        assert run_id == "confirmation-test"
        mutator(cancel_run)
        return cancel_run

    with (
        patch("services.feishu_bot.find_run", return_value=cancel_run),
        patch("services.feishu_bot.mutate_run", side_effect=mutate_cancellation),
        patch("services.feishu_bot.append_card_callback_log"),
    ):
        result = handle_card_action(callback_payload("cancel_operation", "decision-1"), "海淋")
    assert result["toast"]["content"] == "已取消，流程停止"
    assert cancel_run["status"] == "decision_cancel_operation"
    assert cancel_run["pending_decision"]["decision"] == "cancel_operation"
    stdout = StringIO()
    with patch("services.feishu_bot.find_run", return_value=cancel_run), redirect_stdout(stdout):
        assert wait_decision(wait_args, object()) == 2
    assert stdout.getvalue().strip() == "cancel_operation"

    nonlocal_run = {
        **run,
        "submission_data": {"host_machine": "其他宿主机"},
        "pending_decision": dict(run["pending_decision"]),
        "events": [],
    }
    with (
        patch("services.feishu_bot.find_run", return_value=nonlocal_run),
        patch("services.feishu_bot.mutate_run") as forbidden_mutation,
        patch("services.feishu_bot.append_card_callback_log"),
    ):
        result = handle_card_action(callback_payload("confirm_continue", "decision-1"), "海淋")
    forbidden_mutation.assert_not_called()
    assert result["toast"]["content"] == "非本机卡片，未执行任何操作"

    fresh_run = {
        key: value
        for key, value in run.items()
        if key != "pending_decision"
    }
    fresh_run["status"] = "running"

    def create_mutate(run_id: str, mutator):
        assert run_id == "confirmation-test"
        mutator(fresh_run)
        return fresh_run

    with (
        patch("services.feishu_bot.find_run", return_value=fresh_run),
        patch("services.feishu_bot.mutate_run", side_effect=create_mutate) as mutate,
    ):
        created = create_or_update_confirmation(
            "confirmation-test",
            "utm-test:irreversible-step",
            "utm-test",
            "是否继续当前不可逆操作？",
            "只执行一次目标操作",
            "TARGET=verified",
        )
    assert mutate.call_count == 1
    assert created["pending_decision"]["kind"] == "confirmation"
    assert created["pending_decision"]["decision_id"]
    first_uuid = created["pending_decision"]["message_uuid"]

    normalized_run = {
        key: value
        for key, value in run.items()
        if key != "pending_decision"
    }

    def normalize_mutate(run_id: str, mutator):
        mutator(normalized_run)
        return normalized_run

    with (
        patch("services.feishu_bot.find_run", return_value=normalized_run),
        patch("services.feishu_bot.mutate_run", side_effect=normalize_mutate),
    ):
        normalized = create_or_update_confirmation(
            "confirmation-test",
            "  utm-test:stage  ",
            "  utm-test  ",
            "  是否继续？  ",
            "  执行一次  ",
            "  TARGET=verified  ",
        )
    assert normalized["pending_decision"]["stage"] == "utm-test:stage"
    assert normalized["pending_decision"]["current_skill"] == "utm-test"
    assert normalized["pending_decision"]["question"] == "是否继续？"
    assert normalized["pending_decision"]["action_summary"] == "执行一次"
    assert normalized["pending_decision"]["evidence"] == "TARGET=verified"

    with (
        patch("services.feishu_bot.find_run", return_value=created),
        patch("services.feishu_bot.mutate_run") as duplicate_mutate,
    ):
        repeated = create_or_update_confirmation(
            "confirmation-test",
            "utm-test:irreversible-step",
            "utm-test",
            "是否继续当前不可逆操作？",
            "只执行一次目标操作",
            "TARGET=verified",
        )
    duplicate_mutate.assert_not_called()
    assert repeated["pending_decision"]["message_uuid"] == first_uuid

    with (
        patch("services.feishu_bot.find_run", return_value=created),
        patch("services.feishu_bot.mutate_run") as changed_evidence_mutate,
    ):
        try:
            create_or_update_confirmation(
                "confirmation-test",
                "utm-test:irreversible-step",
                "utm-test",
                "是否继续当前不可逆操作？",
                "只执行一次目标操作",
                "TARGET=changed",
            )
        except RuntimeError as exc:
            assert "Another decision card is already waiting" in str(exc)
        else:
            raise AssertionError("changed confirmation evidence silently reused a stale card")
    changed_evidence_mutate.assert_not_called()

    for missing_index in range(5):
        required = [
            "utm-test:irreversible-step",
            "utm-test",
            "是否继续当前不可逆操作？",
            "只执行一次目标操作",
            "TARGET=verified",
        ]
        required[missing_index] = ""
        with patch("services.feishu_bot.find_run") as forbidden_find:
            try:
                create_or_update_confirmation("confirmation-test", *required)
            except RuntimeError as exc:
                assert "Confirmation requires non-empty" in str(exc)
            else:
                raise AssertionError(f"missing confirmation field accepted: {missing_index}")
        forbidden_find.assert_not_called()

    notify_args = SimpleNamespace(
        run_id="confirmation-test",
        chat_id=FIXTURE_ORIGINAL_CHAT,
        stage="utm-test:irreversible-step",
        current_skill="utm-test",
        confirmation_question="是否继续？",
        confirmation_action="执行一次",
        evidence="",
    )
    stderr = StringIO()
    with (
        patch("services.feishu_bot.find_run") as forbidden_find,
        redirect_stderr(stderr),
    ):
        assert notify_confirmation(notify_args, object()) == 2
    forbidden_find.assert_not_called()
    assert "requires --stage" in stderr.getvalue()

    delivery_run = created

    def delivery_mutate(run_id: str, mutator):
        assert run_id == "confirmation-test"
        mutator(delivery_run)
        return delivery_run

    config = SimpleNamespace(submission_host_machine="海淋")
    with (
        patch("services.feishu_bot.mutate_run", side_effect=delivery_mutate),
        patch(
            "services.feishu_bot.FeishuClient.send_card",
            return_value={"data": {"message_id": "om_confirmation"}},
        ) as send_card,
    ):
        delivered = ensure_decision_card_delivered(delivery_run, config)
    assert send_card.call_count == 1
    assert delivered["pending_decision"]["last_message_id"] == "om_confirmation"
    assert delivered["pending_decision"]["first_notified_at"]

    with patch("services.feishu_bot.FeishuClient.send_card") as duplicate_send:
        ensure_decision_card_delivered(delivered, config)
    duplicate_send.assert_not_called()

    root = Path(__file__).resolve().parents[1]
    browser_url_commands = {
        "utm-script": ("developer.apple.com/account/",),
        "utm-23": ("appstoreconnect.apple.com",),
        "utm-24": (
            "appstoreconnect.apple.com/apps",
            "pbpaste | python3 scripts/shared_operations.py browser-url",
            "developer.apple.com/contact/app-store/?topic=expedite",
        ),
    }
    for skill_name, sources in browser_url_commands.items():
        skill = (root / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")
        assert "OP-BROWSER-URL-NO-SCHEME" in skill, skill_name
        assert "python3 scripts/shared_operations.py browser-url" in skill, skill_name
        for source in sources:
            assert source in skill, f"{skill_name}: {source}"
    combined_browser_skills = "\n".join(
        (root / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        for name in browser_url_commands
    )
    for simplified_step in (
        "新开一个 tab，在地址栏原生粘贴并打开",
        "打开无协议地址 `developer.apple.com/account/`",
        "打开或切换到 `developer.apple.com/account/`",
    ):
        assert simplified_step not in combined_browser_skills, simplified_step

    utm_image = (root / "skills" / "utm-image" / "SKILL.md").read_text(encoding="utf-8")
    assert "本技能取得飞书截图来源时不使用剪贴板或浏览器 URL 操作" in utm_image
    assert "scripts/utm_image_source.py" in utm_image

    for skill_name, script_name in {
        "utm-apps": "utm_apps.py",
    }.items():
        skill = (root / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")
        assert "OP-APPLE-WEB-SCRIPT" in skill, skill_name
        assert str(ROOT / "scripts" / script_name) in skill
        assert "同一 PID" in skill

    for contract_file in ("AGENTS.md", "README.md"):
        contract = (root / contract_file).read_text(encoding="utf-8")
        assert "OP-APPLE-WEB-SCRIPT 是唯一例外" in contract, contract_file
        assert "精确 HTTPS URL 命令" in contract, contract_file

    print("SHARED_OPERATIONS_AND_CONFIRMATION=verified")


if __name__ == "__main__":
    main()
