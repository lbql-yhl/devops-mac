#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_globalized_post_clone_recovery_contract() -> None:
    shared = (ROOT / "skills/_shared/AUTOMATION_CONTRACT.md").read_text(
        encoding="utf-8"
    )
    for required in (
        "SSH_PROBE_TOTAL_TIMEOUT_SECONDS=12",
        "ConnectTimeout=5 只限制 TCP 建连",
        'launchctl asuser "$uid"',
        "不得写死 `gui/502`",
    ):
        assert required in shared, required

    guest = (ROOT / "scripts/utm_post_clone_guest.py").read_text(encoding="utf-8")
    post_clone = (ROOT / "scripts/utm_post_clone.py").read_text(encoding="utf-8")
    assert "DEMO_RETIREMENT_TIMEOUT_SECONDS = 180.0" in guest
    assert "echo 1234 |" not in guest
    assert '-adminUser "$target_user" -adminPassword -' in guest
    assert "DEMO_HOME=absent" in guest
    assert "demo_cleanup_verified(cleanup)" in post_clone


if __name__ == "__main__":
    test_globalized_post_clone_recovery_contract()
    print("EARLY_SKILL_REGRESSIONS=verified")
