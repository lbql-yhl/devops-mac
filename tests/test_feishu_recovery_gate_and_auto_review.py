#!/usr/bin/env python3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.feishu_bot import (  # noqa: E402
    is_approved_review_submission,
    record_automatic_review_approval,
    record_review_submit_attempt,
    validate_fault_recovery_evidence,
)


def recovery_args(**overrides):
    values = {
        "recovery_attempts": 0,
        "recovery_actions": "",
        "recovery_result": "",
        "unrepairable": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def main() -> None:
    ok, reason = validate_fault_recovery_evidence(recovery_args())
    assert ok is False
    assert "recovery" in reason.lower()

    ok, _ = validate_fault_recovery_evidence(
        recovery_args(
            recovery_attempts=3,
            recovery_actions="refresh_state,reidentify_target,retry_and_verify",
            recovery_result="exhausted",
        )
    )
    assert ok is True

    for attempts in (0, 1, 2):
        ok, reason = validate_fault_recovery_evidence(
            recovery_args(
                recovery_attempts=attempts,
                recovery_actions="read_only_classification:captcha",
                recovery_result="unrepairable",
                unrepairable=True,
            )
        )
        assert ok is False
        assert "three" in reason.lower()

    ok, _ = validate_fault_recovery_evidence(
        recovery_args(
            recovery_attempts=3,
            recovery_actions="read_only_check_1,read_only_check_2,read_only_check_3",
            recovery_result="unrepairable",
            unrepairable=True,
        )
    )
    assert ok is True

    for attempts in (0, 1, 2):
        ok, reason = validate_fault_recovery_evidence(
            recovery_args(
                recovery_attempts=attempts,
                recovery_actions="diagnose,repair,reverify",
                recovery_result="exhausted",
            )
        )
        assert ok is False
        assert "three" in reason.lower()

    ok, _ = validate_fault_recovery_evidence(
        recovery_args(
            recovery_attempts=1,
            recovery_actions="single_retry",
            recovery_result="unrepairable",
            unrepairable=False,
        )
    )
    assert ok is False

    mutable_run = {
        "id": "auto-review-test",
        "status": "running",
        "events": [],
    }

    def fake_mutate(run_id: str, mutator):
        assert run_id == "auto-review-test"
        mutator(mutable_run)
        return mutable_run

    hashes = [f"{index:064x}" for index in range(1, 6)]
    with patch("services.feishu_bot.mutate_run", side_effect=fake_mutate):
        updated = record_automatic_review_approval(
            run_id="auto-review-test",
            app_version="1.0",
            build_number="7",
            iap_count=14,
            evidence="REVIEW_SCREENSHOTS=verified_5;ITEMS_READY=15",
            screenshot_hashes=hashes,
        )

    assert updated is mutable_run
    snapshot = mutable_run["review_submission_approval"]
    assert is_approved_review_submission(snapshot)
    assert snapshot["source"] == "automatic_self_check"
    assert snapshot["operator_id"] == "automation:self-check"
    assert snapshot["screenshot_hashes"] == hashes
    assert "pending_decision" not in mutable_run
    assert mutable_run["status"] == "review_submission_approved_automatic"
    first_decision_id = snapshot["decision_id"]
    first_answered_at = snapshot["answered_at"]
    first_event_count = len(mutable_run["events"])

    malformed_snapshot = {**snapshot, "screenshot_hashes": ["not-a-sha256"] * 5}
    assert is_approved_review_submission(malformed_snapshot) is False

    with patch("services.feishu_bot.mutate_run", side_effect=fake_mutate):
        try:
            record_automatic_review_approval(
                run_id="auto-review-test",
                app_version="1.0",
                build_number="7",
                iap_count=14,
                evidence="REVIEW_SCREENSHOTS=verified_5;ITEMS_READY=15",
                screenshot_hashes=["not-a-sha256"] * 5,
            )
        except ValueError as exc:
            assert "evidence" in str(exc).lower()
        else:
            raise AssertionError("Automatic approval must reject malformed screenshot hashes")

    with patch("services.feishu_bot.mutate_run", side_effect=fake_mutate):
        repeated = record_automatic_review_approval(
            run_id="auto-review-test",
            app_version="1.0",
            build_number="7",
            iap_count=14,
            evidence="REVIEW_SCREENSHOTS=verified_5;ITEMS_READY=15",
            screenshot_hashes=hashes,
        )
    assert repeated["review_submission_approval"]["decision_id"] == first_decision_id
    assert repeated["review_submission_approval"]["answered_at"] == first_answered_at
    assert len(mutable_run["events"]) == first_event_count

    with patch("services.feishu_bot.mutate_run", side_effect=fake_mutate):
        record_review_submit_attempt(
            run_id="auto-review-test",
            attempt_id="submit-attempt-1",
            decision_id=first_decision_id,
            app_version="1.0",
            build_number="7",
            items_ready=15,
            status="prepared",
        )
    submit_attempt = mutable_run["review_submit_attempt"]
    assert submit_attempt["decision_id"] == first_decision_id
    assert submit_attempt["status"] == "prepared"

    with patch("services.feishu_bot.mutate_run", side_effect=fake_mutate):
        record_review_submit_attempt(
            run_id="auto-review-test",
            attempt_id="submit-attempt-1",
            decision_id=first_decision_id,
            app_version="1.0",
            build_number="7",
            items_ready=15,
            status="clicking",
        )
    assert mutable_run["review_submit_attempt"]["status"] == "clicking"
    with patch("services.feishu_bot.mutate_run", side_effect=fake_mutate):
        try:
            record_automatic_review_approval(
                run_id="auto-review-test",
                app_version="1.0",
                build_number="7",
                iap_count=14,
                evidence="REVIEW_SCREENSHOTS=verified_5;ITEMS_READY=15;CHANGED=1",
                screenshot_hashes=hashes,
            )
        except ValueError as exc:
            assert "submit attempt" in str(exc).lower()
        else:
            raise AssertionError("Evidence must not be superseded after a submit attempt exists")

    print("FEISHU_RECOVERY_GATE_AND_AUTO_REVIEW=verified")


if __name__ == "__main__":
    main()
