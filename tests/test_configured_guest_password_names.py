from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY_NAMES = (
    "FIXED" + "_VM_PASSWORD_",
    "fixed" + "_password_ssh_args",
    "fixed" + "_password_scp_args",
)


def test_production_tests_and_contract_use_configured_guest_password_names() -> None:
    sources = [
        *((ROOT / "scripts").rglob("*.py")),
        *((ROOT / "tests").rglob("*.py")),
        ROOT / "skills/_shared/AUTOMATION_CONTRACT.md",
    ]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for legacy_name in LEGACY_NAMES:
            assert legacy_name not in text, f"{path.relative_to(ROOT)}: {legacy_name}"

    prompt = (ROOT / "scripts/mac_password_prompt.py").read_text(encoding="utf-8")
    assert "CONFIGURED_GUEST_PASSWORD_CONTEXT=gui_auth" in prompt
    assert "CONFIGURED_GUEST_PASSWORD_RESULT=verified" in prompt
