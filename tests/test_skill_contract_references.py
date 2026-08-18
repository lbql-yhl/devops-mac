from pathlib import Path

import pytest

from skill_contract_assertions import assert_shared_contract_reference


ROOT = Path(__file__).resolve().parents[1]


def test_shared_contract_reference_accepts_only_the_canonical_target(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills"
    skill_file = skills / "utm-key" / "SKILL.md"
    contract = skills / "_shared" / "AUTOMATION_CONTRACT.md"
    skill_file.parent.mkdir(parents=True)
    contract.parent.mkdir(parents=True)
    contract.write_text("# shared\n", encoding="utf-8")
    relative_contract = Path("..") / "_shared" / contract.name

    assert_shared_contract_reference(
        skill_file,
        f"[shared]({relative_contract})",
        contract,
    )
    assert_shared_contract_reference(
        skill_file,
        f"[shared]({contract})",
        contract,
    )
    with pytest.raises(AssertionError, match="canonical shared contract"):
        assert_shared_contract_reference(
            skill_file,
            "[decoy](../../archive/AUTOMATION_CONTRACT.md)",
            contract,
        )


def test_contract_tests_do_not_require_legacy_relative_link_spelling() -> None:
    legacy_assertion = '"' + str(Path("..") / "_shared" / "AUTOMATION_CONTRACT.md") + '"'
    for name in (
        "test_detailed_skill_contract.py",
        "test_feishu_fault_recovery_docs.py",
        "test_unattended_portable_skill_contract.py",
    ):
        text = (ROOT / "tests" / name).read_text(encoding="utf-8")
        assert legacy_assertion not in text, name
