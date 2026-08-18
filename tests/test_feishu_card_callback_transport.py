#!/usr/bin/env python3
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.feishu_bot import (
    handle_card_action as _handle_card_action,
    normalize_ws_card_payload,
)


REPORT_ONLY_CHAT = "-".join(("daily", "report", "chat"))


def handle_card_action(payload: dict, submission_host_machine: str) -> dict:
    return _handle_card_action(payload, submission_host_machine, REPORT_ONLY_CHAT)


def main() -> None:
    run = {
        "id": "transport-test",
        "chat_id": "test-chat",
        "submission_data": {"host_machine": "海淋"},
        "pending_decision": {
            "kind": "fault",
            "status": "waiting",
            "decision_id": "decision-1",
            "stage": "utm-vm-clone",
            "fault": "test fault",
            "suggested_action": "continue",
            "failure_action": "stop",
            "evidence": "transport-test",
            "recovery_skill": "utm-vm-clone",
        },
    }
    # Some long-connection deliveries encode action.value as a JSON string,
    # while the SDK P2 model only accepts a dict. The raw transport adapter
    # must normalize it before invoking the business callback.
    payload = {
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": "operator-1"},
            "action": {
                "value": json.dumps(
                    {
                        "action": "submission_fault_decision",
                        "decision": "manual_continue",
                        "decision_id": "decision-1",
                        "run_id": "transport-test",
                    }
                )
            },
        },
    }
    answered = {
        **run,
        "pending_decision": {
            **run["pending_decision"],
            "status": "answered",
            "decision": "manual_continue",
        },
    }
    with (
        patch("services.feishu_bot.find_run", return_value=run),
        patch("services.feishu_bot.record_decision_by_run_id", return_value=answered),
        patch("services.feishu_bot.append_card_callback_log"),
    ):
        normalized = normalize_ws_card_payload(json.dumps(payload))
        result = handle_card_action(normalized, "海淋")

    assert result["toast"]["content"] == "已收到人工处理结果，正在继续流程"


if __name__ == "__main__":
    main()
