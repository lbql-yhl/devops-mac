#!/usr/bin/env python3
"""Regression coverage for the UTM-1 resumable state ledger."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.utm_1_state import (
    StateError,
    next_phase,
    prepare_attempt,
    record_phase,
)


def context(root: Path) -> dict:
    bundle = root / "abcd.utm"
    bundle.mkdir(exist_ok=True)
    share = root / "share"
    share.mkdir(exist_ok=True)
    return {
        "run_id": "submission-20260723-example",
        "vm_name": "abcd",
        "bundle": str(bundle),
        "config_uuid": "12345678-1234-1234-1234-123456789abc",
        "share_path": str(share),
        "clone_marker_sha256": "a" * 64,
    }


def main() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        ledger = root / "runtime" / "utm-1-attempts" / "submission-20260723-example.json"
        state = prepare_attempt(ledger, context(root))
        assert ledger.stat().st_mode & 0o777 == 0o600
        assert ledger.parent.stat().st_mode & 0o777 == 0o700
        assert next_phase(state) == "handoff_verified"

        for phase in ("handoff_verified", "sharing_verified", "network_verified"):
            state = record_phase(ledger, context(root), phase, {"result": "verified"})
        assert next_phase(state) == "running_verified"

        state = record_phase(ledger, context(root), "running_verified", {"utm_status": "already_started"})
        assert next_phase(state) == "login_verified"
        state = record_phase(ledger, context(root), "login_verified", {"desktop": "verified"})
        assert next_phase(state) is None

        mismatched = {**context(root), "config_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
        try:
            prepare_attempt(ledger, mismatched)
        except StateError as error:
            assert str(error) == "UTM_1_ATTEMPT_IDENTITY_MISMATCH=config_uuid"
        else:
            raise AssertionError("config UUID drift was accepted")

    print("UTM_1_STATE_LEDGER=verified")


if __name__ == "__main__":
    main()
