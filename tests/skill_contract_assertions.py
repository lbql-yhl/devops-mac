from pathlib import Path
import re


_CONTRACT_LINK = re.compile(r"\]\(([^)\n]*AUTOMATION_CONTRACT\.md)\)")


def assert_shared_contract_reference(
    skill_file: Path, text: str, contract_file: Path
) -> None:
    canonical = contract_file.resolve()
    for raw_target in _CONTRACT_LINK.findall(text):
        target = Path(raw_target)
        resolved = (
            target.resolve()
            if target.is_absolute()
            else (skill_file.parent / target).resolve()
        )
        if resolved == canonical:
            return
    raise AssertionError(
        f"{skill_file.parent.name}: missing canonical shared contract reference"
    )
