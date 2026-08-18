#!/usr/bin/env python3
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.feishu_bot import (
    build_fault_decision_card,
    create_or_update_fault,
    decision_deadline,
    ensure_decision_card_delivered,
    handle_card_action as _handle_card_action,
    notify_fault,
    record_decision_by_run_id,
    wait_decision,
)
from services.feishu_gateway import send_interactive_card


REPORT_ONLY_CHAT = "-".join(("daily", "report", "chat"))
FIXTURE_CHAT = "test-chat"
FIXTURE_WRONG_CHAT = "wrong-chat"


def handle_card_action(payload: dict, submission_host_machine: str) -> dict:
    return _handle_card_action(
        payload,
        submission_host_machine,
        REPORT_ONLY_CHAT,
    )


def main() -> None:
    first_delivery_deadline = decision_deadline(
        {
            "requested_at": "2026-07-20T08:00:00+00:00",
            "first_notified_at": "2026-07-20T08:05:00+00:00",
            "last_notified_at": "2026-07-20T08:45:00+00:00",
        },
        3600,
        9999999999.0,
    )
    assert first_delivery_deadline == 1784538300.0

    run = {
        "id": "fault-card-test",
        "chat_id": FIXTURE_CHAT,
        "submission_data": {"host_machine": "海淋"},
        "pending_decision": {
            "kind": "fault",
            "status": "waiting",
            "decision_id": "fault-decision-1",
            "stage": "test",
            "fault": "test fault",
            "retry_count": 0,
            "evidence": "test evidence",
            "suggested_action": "test action",
            "recovery_skill": "utm-24",
            "completed_steps": "截图已验证",
        },
    }
    card = build_fault_decision_card(run)

    assert card["schema"] == "2.0"
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "div"
    buttons = elements[1:]
    assert [button["text"]["content"] for button in buttons] == [
        "停止流程",
        "已人工处理，继续流程",
        "重试技能，跳过已处理成功的步骤",
    ]
    assert all(button["tag"] == "button" for button in buttons)
    assert all(button["behaviors"][0]["type"] == "callback" for button in buttons)
    assert buttons[0]["behaviors"][0]["value"] == {
        "action": "submission_fault_decision",
        "decision": "stop",
        "decision_id": "fault-decision-1",
        "run_id": "fault-card-test",
    }
    assert buttons[1]["behaviors"][0]["value"]["decision"] == "manual_continue"
    assert buttons[2]["behaviors"][0]["value"]["decision"] == "retry_skill"
    assert "**宿主机**：海淋" in elements[0]["text"]["content"]
    assert "**当前技能**：utm-24" in elements[0]["text"]["content"]
    assert "**已完成步骤**：截图已验证" in elements[0]["text"]["content"]
    assert "action" not in elements[0]

    answered = {
        **run,
        "pending_decision": {**run["pending_decision"], "status": "answered", "decision": "manual_continue"},
    }
    answered_elements = build_fault_decision_card(answered)["body"]["elements"]
    assert len(answered_elements) == 2
    assert answered_elements[1]["text"]["content"] == "已选择：已人工处理，继续流程"
    assert answered_elements[1]["disabled"] is True

    mutable_run = {**run, "pending_decision": dict(run["pending_decision"]), "events": []}

    def fake_mutate(run_id: str, mutate) -> dict:
        assert run_id == "fault-card-test"
        mutate(mutable_run)
        return mutable_run

    with patch("services.feishu_bot.mutate_run", side_effect=fake_mutate):
        retried = record_decision_by_run_id(
            "fault-card-test",
            "retry_skill",
            "card_button",
            "approver",
            "fault-decision-1",
        )
    assert retried is mutable_run
    assert mutable_run["status"] == "decision_retry_skill"
    assert mutable_run["pending_decision"]["decision"] == "retry_skill"

    retry_payload = {
        "event": {
            "operator": {"open_id": "approver"},
            "action": {
                "value": {
                    "action": "submission_fault_decision",
                    "decision": "retry_skill",
                    "decision_id": "fault-decision-1",
                    "run_id": "fault-card-test",
                }
            }
        }
    }
    waiting_run = {**run, "pending_decision": dict(run["pending_decision"])}
    for rejected_run in (
        {**waiting_run, "submission_data": {"host_machine": "dev1"}},
        {key: value for key, value in waiting_run.items() if key != "submission_data"},
        {**waiting_run, "chat_id": REPORT_ONLY_CHAT},
        {**waiting_run, "chat_id": ""},
    ):
        with (
            patch("services.feishu_bot.find_run", return_value=rejected_run),
            patch("services.feishu_bot.record_decision_by_run_id") as rejected_record,
            patch("services.feishu_bot.append_card_callback_log"),
        ):
            rejected = handle_card_action(retry_payload, "海淋")
        assert "非本机卡片" in rejected["toast"]["content"]
        rejected_record.assert_not_called()

    missing_operator_payload = {
        **retry_payload,
        "event": {
            **retry_payload["event"],
            "operator": {},
        },
    }
    with (
        patch("services.feishu_bot.find_run") as forbidden_find,
        patch("services.feishu_bot.record_decision_by_run_id") as forbidden_record,
        patch("services.feishu_bot.append_card_callback_log"),
    ):
        rejected = handle_card_action(missing_operator_payload, "海淋")
    assert "操作人" in rejected["toast"]["content"]
    forbidden_find.assert_not_called()
    forbidden_record.assert_not_called()

    stale_payload = {
        **retry_payload,
        "event": {
            **retry_payload["event"],
            "action": {
                "value": {
                    **retry_payload["event"]["action"]["value"],
                    "decision_id": "old-fault-decision",
                }
            },
        },
    }
    with (
        patch("services.feishu_bot.find_run", return_value=waiting_run),
        patch("services.feishu_bot.record_decision_by_run_id") as stale_record,
        patch("services.feishu_bot.append_card_callback_log"),
    ):
        rejected = handle_card_action(stale_payload, "海淋")
    assert "已失效或不匹配" in rejected["toast"]["content"]
    stale_record.assert_not_called()

    with (
        patch("services.feishu_bot.find_run", return_value=waiting_run),
        patch("services.feishu_bot.record_decision_by_run_id", return_value=mutable_run) as record_local,
        patch("services.feishu_bot.append_card_callback_log"),
    ):
        result = handle_card_action(retry_payload, "海淋")
    record_local.assert_called_once_with(
        "fault-card-test", "retry_skill", "card_button", "approver", "fault-decision-1"
    )
    assert result["toast"]["content"] == "已收到重试指令，正在重试当前技能"
    assert result["card"]["data"]["header"]["title"]["content"] == "已收到重试指令，正在重试技能"

    args = SimpleNamespace(
        run_id="fault-card-test",
        timeout_seconds=10,
        poll_seconds=1,
    )
    stdout = StringIO()
    with (
        patch("services.feishu_bot.find_run", return_value=mutable_run),
        patch("services.feishu_bot.time.time", side_effect=[0, 0]),
        redirect_stdout(stdout),
    ):
        assert wait_decision(args, object()) == 0
    assert stdout.getvalue().strip() == "retry_skill"

    notify_args = SimpleNamespace(
        chat_id=FIXTURE_CHAT,
        run_id="fault-card-test",
        stage="test",
        fault="test fault",
        suggested_action="test action",
        failure_action="test failure",
        retry_count=0,
        completed_steps="",
        evidence="test evidence",
        recovery_skill="",
        recovery_attempts=0,
        recovery_actions="",
        recovery_result="",
        unrepairable=False,
    )
    config = SimpleNamespace(
        submission_host_machine="海淋",
        daily_report_chat_id=REPORT_ONLY_CHAT,
    )
    with (
        patch(
            "services.feishu_bot.find_run",
            return_value={**run, "chat_id": FIXTURE_CHAT},
        ),
        patch("services.feishu_bot.create_or_update_fault", return_value=run) as create_fault,
        patch("services.feishu_bot.FeishuClient.send_card"),
        redirect_stderr(StringIO()),
    ):
        assert notify_fault(notify_args, config) == 2
    create_fault.assert_not_called()

    delivery_run = {
        **run,
        "chat_id": FIXTURE_CHAT,
        "pending_decision": {
            **run["pending_decision"],
            "requested_at": "2026-07-20T08:00:00+00:00",
            "message_uuid": "fault-card-test-message",
            "last_notified_at": "2026-07-20T08:01:00+00:00",
        },
        "events": [],
    }

    def delivery_mutate(run_id: str, mutate) -> dict:
        assert run_id == "fault-card-test"
        mutate(delivery_run)
        return delivery_run

    with (
        patch("services.feishu_bot.mutate_run", side_effect=delivery_mutate),
        patch(
            "services.feishu_bot.FeishuClient.send_card",
            return_value={"data": {"message_id": "om_first_delivery"}},
        ) as send_first,
        patch("services.feishu_bot.utc_now", return_value="2026-07-20T08:05:00+00:00"),
    ):
        delivered = ensure_decision_card_delivered(delivery_run, config)
    assert delivered["pending_decision"]["first_notified_at"] == "2026-07-20T08:05:00+00:00"
    assert delivered["pending_decision"]["last_message_id"] == "om_first_delivery"
    assert send_first.call_count == 1
    assert send_first.call_args.kwargs.get("message_uuid") == "fault-card-test-message"

    valid_notify_args = SimpleNamespace(**vars(notify_args))
    valid_notify_args.recovery_skill = "utm-24"
    valid_notify_args.recovery_attempts = 3
    valid_notify_args.recovery_actions = "diagnose,repair,reverify"
    valid_notify_args.recovery_result = "exhausted"
    with (
        patch("services.feishu_bot.find_run", return_value=delivery_run),
        patch("services.feishu_bot.create_or_update_fault", return_value=delivery_run),
        patch("services.feishu_bot.FeishuClient.send_card") as duplicate_fault_send,
        redirect_stdout(StringIO()),
    ):
        assert notify_fault(valid_notify_args, config) == 0
    duplicate_fault_send.assert_not_called()

    legacy_delivered_run = {
        **delivery_run,
        "pending_decision": {
            **delivery_run["pending_decision"],
            "first_notified_at": "",
            "last_notified_at": "2026-07-20T08:01:00+00:00",
            "last_message_id": "om_legacy_delivery",
        },
    }
    with (
        patch("services.feishu_bot.find_run", return_value=legacy_delivered_run),
        patch("services.feishu_bot.create_or_update_fault", return_value=legacy_delivered_run),
        patch("services.feishu_bot.FeishuClient.send_card") as legacy_duplicate_send,
        redirect_stdout(StringIO()),
    ):
        assert notify_fault(valid_notify_args, config) == 0
    legacy_duplicate_send.assert_not_called()

    def legacy_delivery_mutate(run_id: str, mutate) -> dict:
        assert run_id == "fault-card-test"
        mutate(legacy_delivered_run)
        return legacy_delivered_run

    with (
        patch("services.feishu_bot.mutate_run", side_effect=legacy_delivery_mutate),
        patch("services.feishu_bot.FeishuClient.send_card") as legacy_wait_send,
    ):
        migrated_delivery = ensure_decision_card_delivered(legacy_delivered_run, config)
    assert migrated_delivery["pending_decision"]["first_notified_at"] == "2026-07-20T08:01:00+00:00"
    legacy_wait_send.assert_not_called()

    undelivered_run = {
        **delivery_run,
        "pending_decision": {
            **delivery_run["pending_decision"],
            "first_notified_at": "",
            "last_notified_at": "",
            "last_message_id": "",
        },
    }
    with (
        patch("services.feishu_bot.find_run", return_value=undelivered_run),
        patch("services.feishu_bot.create_or_update_fault", return_value=undelivered_run) as create_valid_fault,
        patch("services.feishu_bot.FeishuClient.send_card", return_value={"data": {}}),
        patch("services.feishu_bot.record_fault_notification") as record_missing_id,
        redirect_stderr(StringIO()),
    ):
        assert notify_fault(valid_notify_args, config) == 2
    assert create_valid_fault.call_args.kwargs["host_machine"] == "海淋"
    record_missing_id.assert_not_called()

    # The shared fault-send boundary rejects foreign/missing ownership and any
    # non-original or daily-report chat before it creates/mutates a pending fault.
    boundary_cases = (
        ({**delivery_run, "submission_data": {"host_machine": "dev1"}}, valid_notify_args),
        (
            {key: value for key, value in delivery_run.items() if key != "submission_data"},
            valid_notify_args,
        ),
        (
            delivery_run,
            SimpleNamespace(
                **{**vars(valid_notify_args), "chat_id": FIXTURE_WRONG_CHAT}
            ),
        ),
        (delivery_run, SimpleNamespace(**{**vars(valid_notify_args), "chat_id": ""})),
        (
            {**delivery_run, "chat_id": REPORT_ONLY_CHAT},
            SimpleNamespace(**{**vars(valid_notify_args), "chat_id": REPORT_ONLY_CHAT}),
        ),
    )
    for rejected_run, rejected_args in boundary_cases:
        with (
            patch("services.feishu_bot.find_run", return_value=rejected_run),
            patch("services.feishu_bot.create_or_update_fault") as rejected_create,
            patch("services.feishu_bot.mutate_run") as rejected_mutate,
            patch("services.feishu_bot.FeishuClient.send_card") as rejected_send,
            redirect_stderr(StringIO()),
        ):
            assert notify_fault(rejected_args, config) == 2
        rejected_create.assert_not_called()
        rejected_mutate.assert_not_called()
        rejected_send.assert_not_called()

    # Even direct pending-fault construction cannot replace the run's original
    # chat. notify_fault enforces equality before reaching this helper.
    original_chat_run = {
        **delivery_run,
        "chat_id": FIXTURE_CHAT,
        "pending_decision": {},
        "events": [],
    }

    def original_chat_mutate(run_id: str, mutate) -> dict:
        assert run_id == "fault-card-test"
        mutate(original_chat_run)
        return original_chat_run

    with (
        patch("services.feishu_bot.find_run", return_value=original_chat_run),
        patch("services.feishu_bot.mutate_run", side_effect=original_chat_mutate),
    ):
        create_or_update_fault(
            run_id="fault-card-test",
            chat_id=FIXTURE_WRONG_CHAT,
            stage="new-stage",
            fault="new fault",
            suggested_action="retry",
            recovery_skill="utm-24",
            host_machine="海淋",
        )
    assert original_chat_run["chat_id"] == FIXTURE_CHAT
    assert original_chat_run["pending_decision"]["decision_id"]

    waiting_fault = {
        **original_chat_run,
        "pending_decision": {
            **original_chat_run["pending_decision"],
            "status": "waiting",
            "stage": "new-stage",
            "fault": "new fault",
            "recovery_skill": "utm-24",
        },
    }
    with (
        patch("services.feishu_bot.find_run", return_value=waiting_fault),
        patch("services.feishu_bot.mutate_run") as overlapping_mutate,
    ):
        try:
            create_or_update_fault(
                run_id="fault-card-test",
                chat_id=FIXTURE_CHAT,
                stage="different-stage",
                fault="different fault",
                suggested_action="retry",
                recovery_skill="utm-24",
                host_machine="海淋",
            )
        except RuntimeError as exc:
            assert "Another decision card is already waiting" in str(exc)
        else:
            raise AssertionError("a waiting fault decision was overwritten")
    overlapping_mutate.assert_not_called()

    timeout_run = {
        "id": "fault-card-timeout",
        "app_name": "Timeout App",
        "submission_data": {"host_machine": "海淋"},
        "chat_id": "timeout-chat",
        "status": "waiting_user_decision",
        "pending_decision": {
            "kind": "fault",
            "status": "waiting",
            "stage": "utm-business:bank",
            "fault": "bank state unclear",
            "recovery_skill": "utm-business",
            "requested_at": "2026-07-20T08:00:00+00:00",
            "first_notified_at": "2026-07-20T08:00:00+00:00",
            "message_uuid": "fault-card-timeout-message",
        },
        "events": [],
    }

    def timeout_mutate(run_id: str, mutate) -> dict:
        assert run_id == "fault-card-timeout"
        mutate(timeout_run)
        return timeout_run

    timeout_args = SimpleNamespace(
        run_id="fault-card-timeout",
        timeout_seconds=0,
        poll_seconds=1,
        decision_kind="fault",
    )
    with (
        patch("services.feishu_bot.find_run", return_value=timeout_run),
        patch("services.feishu_bot.mutate_run", side_effect=timeout_mutate),
        patch(
            "services.feishu_bot.FeishuClient.send_card",
            return_value={"data": {"message_id": "om_fault_timeout"}},
        ) as send_timeout,
        patch("services.feishu_bot.utc_now", return_value="2026-07-20T09:00:00+00:00"),
        redirect_stderr(StringIO()),
    ):
        assert wait_decision(timeout_args, object()) == 3

    assert timeout_run["status"] == "decision_timeout_stop"
    assert timeout_run["pending_decision"]["status"] == "expired"
    assert timeout_run["pending_decision"]["decision"] == "timeout"
    assert timeout_run["pending_decision"]["timeout_seconds"] == 0
    assert timeout_run["pending_decision"]["timeout_notification_attempted_at"]
    assert timeout_run["pending_decision"]["timeout_message_id"] == "om_fault_timeout"
    assert send_timeout.call_count == 1
    assert (
        send_timeout.call_args.kwargs.get("message_uuid")
        == timeout_run["pending_decision"]["timeout_message_uuid"]
    )
    timeout_chat_id, timeout_card = send_timeout.call_args.args
    assert timeout_chat_id == "timeout-chat"
    assert timeout_card["header"]["title"]["content"] == "等待回复超时，流程已停止"
    assert "**宿主机**：海淋" in timeout_card["body"]["elements"][0]["text"]["content"]
    assert all(element["tag"] != "button" for element in timeout_card["body"]["elements"])

    with (
        patch("services.feishu_bot.find_run", return_value=timeout_run),
        patch("services.feishu_bot.mutate_run", side_effect=timeout_mutate),
        patch("services.feishu_bot.FeishuClient.send_card") as duplicate_timeout,
        redirect_stderr(StringIO()),
    ):
        assert wait_decision(timeout_args, object()) == 3
    duplicate_timeout.assert_not_called()

    timeout_failure_run = {
        **timeout_run,
        "id": "fault-card-timeout-failure",
        "pending_decision": {
            **timeout_run["pending_decision"],
            "status": "waiting",
            "decision": "",
            "expired_at": "",
            "timeout_notification_attempted_at": "",
            "timeout_notified_at": "",
            "timeout_message_id": "",
        },
        "events": [],
    }

    deferred_failure_callbacks = []
    timeout_failure_mutation_count = 0

    def timeout_failure_mutate(run_id: str, mutate) -> dict:
        nonlocal timeout_failure_mutation_count
        assert run_id == "fault-card-timeout-failure"
        timeout_failure_mutation_count += 1
        if timeout_failure_mutation_count == 1:
            mutate(timeout_failure_run)
        else:
            deferred_failure_callbacks.append(mutate)
        return timeout_failure_run

    timeout_failure_args = SimpleNamespace(
        run_id="fault-card-timeout-failure",
        timeout_seconds=0,
        poll_seconds=1,
        decision_kind="fault",
    )
    with (
        patch("services.feishu_bot.find_run", return_value=timeout_failure_run),
        patch("services.feishu_bot.mutate_run", side_effect=timeout_failure_mutate),
        patch("services.feishu_bot.FeishuClient.send_card", side_effect=RuntimeError("send failed")),
        redirect_stderr(StringIO()),
    ):
        assert wait_decision(timeout_failure_args, object()) == 3
    assert len(deferred_failure_callbacks) == 1
    deferred_failure_callbacks[0](timeout_failure_run)
    assert timeout_failure_run["status"] == "decision_timeout_stop"
    assert timeout_failure_run["pending_decision"]["timeout_notification_attempted_at"]
    assert timeout_failure_run["pending_decision"]["timeout_notification_failed_at"]
    assert timeout_failure_run["events"][-1]["status"] == (
        "decision_timeout_notification_failed"
    )
    assert timeout_failure_run["events"][-1]["note"].endswith("RuntimeError")

    with (
        patch("services.feishu_bot.find_run", return_value=timeout_failure_run),
        patch("services.feishu_bot.mutate_run", side_effect=timeout_failure_mutate),
        patch("services.feishu_bot.FeishuClient.send_card") as retry_failed_timeout,
        redirect_stderr(StringIO()),
    ):
        assert wait_decision(timeout_failure_args, object()) == 3
    retry_failed_timeout.assert_not_called()

    late_payload = {
        "event": {
            "operator": {"open_id": "approver"},
            "action": {
                "value": {
                    "action": "submission_fault_decision",
                    "decision": "manual_continue",
                    "run_id": "fault-card-timeout",
                }
            }
        }
    }
    with (
        patch("services.feishu_bot.find_run", return_value=timeout_run),
        patch("services.feishu_bot.record_decision_by_run_id") as record_late,
        patch("services.feishu_bot.append_card_callback_log"),
    ):
        late_result = handle_card_action(late_payload, "海淋")
    record_late.assert_not_called()
    assert "超过一小时" in late_result["toast"]["content"]
    assert all(
        element["tag"] != "button"
        for element in late_result["card"]["data"]["body"]["elements"]
    )

    source = (Path(__file__).resolve().parents[1] / "services" / "feishu_bot.py").read_text(encoding="utf-8")
    assert "notify-stop" not in source
    assert "def notify_stop(" not in source
    assert "resend_fault_decision_if_due" not in source
    assert "FEISHU_DECISION_REMIND_SECONDS" not in source

    with (
        patch("services.feishu_gateway.network_precheck", return_value={"ok": True}),
        patch("services.feishu_gateway.get_tenant_access_token", return_value="token"),
        patch(
            "services.feishu_gateway.post_json",
            return_value={"code": 0, "data": {"message_id": "om_uuid"}},
        ) as post_uuid,
    ):
        send_interactive_card(
            "app-id",
            "app-secret",
            FIXTURE_CHAT,
            {"schema": "2.0"},
            message_uuid="fault-message-uuid",
        )
    assert post_uuid.call_args.args[1]["uuid"] == "fault-message-uuid"


if __name__ == "__main__":
    main()
