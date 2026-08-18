from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_explicit_test_entry_claims_named_vm_and_delegates_to_mainline() -> None:
    text = (ROOT / "scripts" / "utm_clash_ip_explicit_test.py").read_text(
        encoding="utf-8"
    )

    assert "claim_exact_available_vm" in text
    assert 'application_name="test"' in text
    assert '"--proxy-stdin"' in text
    assert "utm_clash_ip.main" in text
    assert "cua-driver" not in text
    assert "OCR" not in text
