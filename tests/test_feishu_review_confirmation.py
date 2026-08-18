#!/usr/bin/env python3
import sys
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.feishu_bot import (
    build_review_confirmation_card,
    ensure_decision_card_delivered,
    handle_card_action as _handle_card_action,
    notify_review,
    record_decision_by_run_id,
    wait_decision,
)


REPORT_ONLY_CHAT = "-".join(("daily", "report", "chat"))


def handle_card_action(payload: dict, submission_host_machine: str) -> dict:
    return _handle_card_action(payload, submission_host_machine, REPORT_ONLY_CHAT)


def callback_payload(decision: str, decision_id: str) -> dict:
    return {
        "event": {
            "operator": {"open_id": "approver"},
            "action": {
                "value": {
                    "action": "submission_review_decision",
                    "decision": decision,
                    "decision_id": decision_id,
                    "run_id": "review-card-test",
                }
            },
        }
    }


def main() -> None:
    run = {
        "id": "review-card-test",
        "app_name": "Xrimo",
        "chat_id": "test-chat",
        "submission_data": {"host_machine": "海淋"},
        "pending_decision": {
            "kind": "review_submit",
            "status": "waiting",
            "decision": "",
            "decision_id": "review-request-1",
            "app_version": "1.0",
            "build_number": "1",
            "iap_count": 14,
            "evidence": (
                "APP_INFORMATION_CLEAN=verified;"
                "REVIEW_SCREENSHOTS=verified_5;ITEMS_READY=15"
            ),
            "image_keys": [
                "img_media-manager",
                "img_iap-drafts",
                "img_app-information",
                "img_privacy-agreement",
                "img_small-business",
            ],
        },
    }

    card = build_review_confirmation_card(run)
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "是否现在提审？"
    elements = card["body"]["elements"]
    assert [element["tag"] for element in elements] == [
        "div", "img", "img", "img", "img", "img", "div", "button", "button",
    ]
    assert elements[0]["text"]["content"] == "**Xrimo1.0 准备提审**"
    assert [element["img_key"] for element in elements[1:6]] == [
        "img_media-manager",
        "img_iap-drafts",
        "img_app-information",
        "img_privacy-agreement",
        "img_small-business",
    ]
    assert all(element["preview"] is True for element in elements[1:6])
    assert all(element["scale_type"] == "fit_horizontal" for element in elements[1:6])
    assert elements[6]["text"]["content"] == "**确认问题**：是否现在提审？"
    buttons = elements[-2:]
    assert [button["text"]["content"] for button in buttons] == ["否，暂不提审", "是，现在提审"]
    assert [button["behaviors"][0]["value"]["decision"] for button in buttons] == [
        "do_not_submit", "submit_review",
    ]
    assert all(button["behaviors"][0]["value"]["decision_id"] == "review-request-1" for button in buttons)

    for rejected_run in (
        {**run, "submission_data": {"host_machine": "dev1"}},
        {key: value for key, value in run.items() if key != "submission_data"},
    ):
        with (
            patch("services.feishu_bot.find_run", return_value=rejected_run),
            patch("services.feishu_bot.record_decision_by_run_id") as rejected_record,
            patch("services.feishu_bot.append_card_callback_log"),
        ):
            rejected_host = handle_card_action(
                callback_payload("submit_review", "review-request-1"), "海淋"
            )
        assert "非本机卡片" in rejected_host["toast"]["content"]
        rejected_record.assert_not_called()

    with (
        patch("services.feishu_bot.find_run", return_value=run),
        patch("services.feishu_bot.append_card_callback_log"),
    ):
        stale = handle_card_action(callback_payload("submit_review", "old-review-request"), "海淋")
    assert "失效" in stale["toast"]["content"]

    missing_operator = callback_payload("submit_review", "review-request-1")
    missing_operator["event"]["operator"] = {}
    with (
        patch("services.feishu_bot.find_run", return_value=run),
        patch("services.feishu_bot.append_card_callback_log"),
        patch("services.feishu_bot.record_decision_by_run_id") as record,
    ):
        rejected = handle_card_action(missing_operator, "海淋")
    assert "操作人" in rejected["toast"]["content"]
    record.assert_not_called()

    mutable_run = {**run, "pending_decision": dict(run["pending_decision"]), "events": []}

    with (
        patch("services.feishu_bot.find_run", return_value=run),
        patch("services.feishu_bot.record_decision_by_run_id", return_value=mutable_run) as record_local,
        patch("services.feishu_bot.append_card_callback_log"),
    ):
        local_result = handle_card_action(
            callback_payload("submit_review", "review-request-1"), "海淋"
        )
    record_local.assert_called_once_with(
        "review-card-test", "submit_review", "card_button", "approver", "review-request-1"
    )
    assert local_result["toast"]["content"] == "已收到提审确认，正在提交"

    def fake_mutate(run_id: str, mutate) -> dict:
        assert run_id == "review-card-test"
        mutate(mutable_run)
        return mutable_run

    with patch("services.feishu_bot.mutate_run", side_effect=fake_mutate):
        answered = record_decision_by_run_id(
            "review-card-test", "submit_review", "card_button", "approver", "review-request-1"
        )
    assert answered is mutable_run
    assert mutable_run["status"] == "decision_submit_review"
    assert mutable_run["pending_decision"]["decision"] == "submit_review"
    assert mutable_run["pending_decision"]["operator_id"] == "approver"
    approval = mutable_run["review_submission_approval"]
    assert approval["kind"] == "review_submit"
    assert approval["status"] == "approved"
    assert approval["decision"] == "submit_review"
    assert approval["decision_id"] == "review-request-1"
    assert approval["app_version"] == "1.0"
    assert approval["build_number"] == "1"
    assert approval["iap_count"] == 14
    assert approval["evidence"] == run["pending_decision"]["evidence"]
    assert approval["answered_at"]
    assert approval["operator_id"] == "approver"

    # The mutation itself revalidates the exact current request. A stale ID or
    # non-waiting review state cannot create/replace the authorization snapshot.
    for invalid_pending, invalid_decision_id in (
        ({**run["pending_decision"], "status": "waiting"}, "old-review-request"),
        ({**run["pending_decision"], "status": "answered"}, "review-request-1"),
    ):
        invalid_run = {
            **run,
            "pending_decision": invalid_pending,
            "events": [],
        }

        def invalid_mutate(run_id: str, mutate) -> dict:
            assert run_id == "review-card-test"
            mutate(invalid_run)
            return invalid_run

        with patch("services.feishu_bot.mutate_run", side_effect=invalid_mutate):
            try:
                record_decision_by_run_id(
                    "review-card-test",
                    "submit_review",
                    "card_button",
                    "approver",
                    invalid_decision_id,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("invalid review decision was accepted")
        assert "review_submission_approval" not in invalid_run

    # A newer review card answered do_not_submit supersedes an older approval
    # with a rejected current snapshot; it never leaves usable authorization.
    rejected_run = {
        **run,
        "pending_decision": {
            **run["pending_decision"],
            "status": "waiting",
            "decision_id": "review-request-2",
        },
        "review_submission_approval": dict(approval),
        "events": [],
    }

    def rejected_mutate(run_id: str, mutate) -> dict:
        assert run_id == "review-card-test"
        mutate(rejected_run)
        return rejected_run

    with patch("services.feishu_bot.mutate_run", side_effect=rejected_mutate):
        record_decision_by_run_id(
            "review-card-test",
            "do_not_submit",
            "card_button",
            "approver-2",
            "review-request-2",
        )
    rejected_snapshot = rejected_run["review_submission_approval"]
    assert rejected_snapshot["status"] == "rejected"
    assert rejected_snapshot["decision"] == "do_not_submit"
    assert rejected_snapshot["decision_id"] == "review-request-2"

    answered_card = build_review_confirmation_card(mutable_run)
    assert answered_card["header"]["title"]["content"] == "已收到提审确认，正在提交"
    assert [element.get("img_key") for element in answered_card["body"]["elements"] if element["tag"] == "img"] == [
        "img_media-manager", "img_iap-drafts", "img_app-information", "img_privacy-agreement", "img_small-business",
    ]
    answered_button = answered_card["body"]["elements"][-1]
    assert answered_button["text"]["content"] == "已选择：是，现在提审"
    assert answered_button["disabled"] is True

    stored_run = {
        "id": "review-card-test",
        "app_name": "Xrimo",
        "submission_data": {"host_machine": "海淋"},
        "chat_id": "test-chat",
        "pending_decision": {},
        "events": [],
    }

    def persist_mutate(run_id: str, mutate) -> dict:
        assert run_id == "review-card-test"
        mutate(stored_run)
        return stored_run

    with TemporaryDirectory() as directory:
        screenshots = []
        for index in range(5):
            path = Path(directory) / f"0{index + 1}.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\ncard-test")
            screenshots.append(str(path))
        notify_args = SimpleNamespace(
            run_id="review-card-test",
            app_version="1.0",
            build_number="1",
            iap_count=14,
            evidence="REVIEW_SCREENSHOTS=verified_5;ITEMS_READY=15",
            screenshot=screenshots,
        )
        config = SimpleNamespace(submission_host_machine="海淋")
        with (
            patch("services.feishu_bot.find_run", return_value=stored_run),
            patch("services.feishu_bot.mutate_run", side_effect=persist_mutate),
            patch("services.feishu_bot.FeishuClient.upload_image", side_effect=[f"img-{i}" for i in range(5)]) as upload,
            patch("services.feishu_bot.FeishuClient.send_card", return_value={"data": {"message_id": "om_review"}}) as send_card,
            patch("services.feishu_bot.utc_now", return_value="2026-07-17T08:00:00+00:00"),
        ):
            assert notify_review(notify_args, config) == 0
        assert upload.call_count == 5
        assert send_card.call_count == 1
        assert stored_run["pending_decision"]["kind"] == "review_submit"
        assert stored_run["pending_decision"]["status"] == "waiting"
        assert stored_run["pending_decision"]["image_keys"] == [f"img-{i}" for i in range(5)]
        assert stored_run["pending_decision"]["decision_id"]
        assert stored_run["pending_decision"]["message_uuid"]
        message_uuid = stored_run["pending_decision"]["message_uuid"]
        assert send_card.call_args.kwargs["message_uuid"] == message_uuid
        assert stored_run["pending_decision"]["first_notified_at"] == "2026-07-17T08:00:00+00:00"

        pending_delivery_run = {
            **stored_run,
            "pending_decision": {
                **stored_run["pending_decision"],
                "first_notified_at": "",
                "last_notified_at": "",
            },
        }

        def retry_delivery_mutate(run_id: str, mutate) -> dict:
            assert run_id == "review-card-test"
            mutate(pending_delivery_run)
            return pending_delivery_run

        with (
            patch("services.feishu_bot.find_run", return_value=pending_delivery_run),
            patch("services.feishu_bot.mutate_run", side_effect=retry_delivery_mutate),
            patch("services.feishu_bot.FeishuClient.upload_image") as duplicate_upload,
            patch(
                "services.feishu_bot.FeishuClient.send_card",
                return_value={"data": {"message_id": "om_review_retry"}},
            ) as retry_send,
            patch("services.feishu_bot.utc_now", return_value="2026-07-17T08:01:00+00:00"),
            redirect_stderr(StringIO()),
        ):
            assert notify_review(notify_args, config) == 0
        duplicate_upload.assert_not_called()
        assert retry_send.call_count == 1
        assert retry_send.call_args.kwargs["message_uuid"] == message_uuid
        assert pending_delivery_run["pending_decision"]["decision_id"] == stored_run["pending_decision"]["decision_id"]
        assert pending_delivery_run["pending_decision"]["message_uuid"] == message_uuid
        assert pending_delivery_run["pending_decision"]["first_notified_at"] == "2026-07-17T08:01:00+00:00"

        foreign_run = {
            **stored_run,
            "submission_data": {"host_machine": "dev1"},
            "pending_decision": {},
        }
        with (
            patch("services.feishu_bot.find_run", return_value=foreign_run),
            patch("services.feishu_bot.mutate_run") as rejected_mutate,
            patch("services.feishu_bot.FeishuClient.upload_image") as rejected_upload,
            patch("services.feishu_bot.FeishuClient.send_card") as rejected_send,
            redirect_stderr(StringIO()),
        ):
            assert notify_review(notify_args, config) == 2
        rejected_mutate.assert_not_called()
        rejected_upload.assert_not_called()
        rejected_send.assert_not_called()

        foreign_pending = {
            **pending_delivery_run,
            "submission_data": {"host_machine": "dev1"},
            "pending_decision": {
                **pending_delivery_run["pending_decision"],
                "first_notified_at": "",
                "last_notified_at": "",
                "last_message_id": "",
            },
        }
        with (
            patch("services.feishu_bot.mutate_run") as rejected_recovery_mutate,
            patch("services.feishu_bot.FeishuClient.send_card") as rejected_recovery_send,
        ):
            try:
                ensure_decision_card_delivered(foreign_pending, config)
            except RuntimeError:
                pass
            else:
                raise AssertionError("foreign-host recovery delivery was not rejected")
        rejected_recovery_mutate.assert_not_called()
        rejected_recovery_send.assert_not_called()

    timeout_run = {
        **run,
        "chat_id": "test-chat",
        "status": "waiting_review_submission_confirmation",
        "pending_decision": {
            **run["pending_decision"],
            "requested_at": "2026-07-20T08:00:00+00:00",
            "first_notified_at": "2026-07-20T08:00:00+00:00",
        },
        "events": [],
    }

    def expire_mutate(run_id: str, mutate) -> dict:
        assert run_id == "review-card-test"
        mutate(timeout_run)
        return timeout_run

    args = SimpleNamespace(
        run_id="review-card-test",
        timeout_seconds=0,
        poll_seconds=1,
        remind_after_seconds=60,
        decision_kind="review_submit",
    )
    with (
        patch("services.feishu_bot.find_run", return_value=timeout_run),
        patch("services.feishu_bot.mutate_run", side_effect=expire_mutate),
        patch(
            "services.feishu_bot.FeishuClient.send_card",
            return_value={"data": {"message_id": "om_review_timeout"}},
        ) as send_timeout,
        patch("services.feishu_bot.utc_now", return_value="2026-07-20T09:00:00+00:00"),
        redirect_stderr(StringIO()),
    ):
        assert wait_decision(args, object()) == 3
    assert timeout_run["pending_decision"]["status"] == "expired"
    assert timeout_run["pending_decision"]["decision"] == "timeout"
    assert timeout_run["status"] == "decision_timeout_stop"
    assert timeout_run["pending_decision"]["timeout_message_id"] == "om_review_timeout"
    assert send_timeout.call_count == 1
    timeout_chat_id, timeout_card = send_timeout.call_args.args
    assert timeout_chat_id == "test-chat"
    assert timeout_card["header"]["title"]["content"] == "等待回复超时，流程已停止"
    assert all(element["tag"] != "button" for element in timeout_card["body"]["elements"])

    with (
        patch("services.feishu_bot.find_run", return_value=timeout_run),
        patch("services.feishu_bot.record_decision_by_run_id") as record_late,
        patch("services.feishu_bot.append_card_callback_log"),
    ):
        late_result = handle_card_action(
            callback_payload("submit_review", "review-request-1"), "海淋"
        )
    record_late.assert_not_called()
    assert "超过一小时" in late_result["toast"]["content"]
    assert all(
        element["tag"] != "button"
        for element in late_result["card"]["data"]["body"]["elements"]
    )

    unsafe_wait = {
        **run,
        "pending_decision": {**run["pending_decision"], "status": "answered", "decision": "submit_review"},
    }
    args.decision_kind = ""
    args.timeout_seconds = 10
    with (
        patch("services.feishu_bot.find_run", return_value=unsafe_wait),
        patch("services.feishu_bot.time.time", side_effect=[0, 0]),
        redirect_stderr(StringIO()),
    ):
        assert wait_decision(args, object()) == 4


if __name__ == "__main__":
    main()
