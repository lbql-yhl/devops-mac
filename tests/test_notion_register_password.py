from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.notion_register_password import register_password, validate_candidate


class FakeAPI:
    def __init__(self) -> None:
        self.parent = 0
        self.value = ""

    def verify_parent(self, title: str) -> str:
        self.parent += 1
        return title

    def read_section(self, title: str, heading: str) -> str:
        return "修改后的密码：" + self.value + "\n邮箱：test@example.test"

    def set_field(self, title, heading, label, value, *, replace_existing=False):
        self.value = value
        return True

    def read_field(self, title, heading, label):
        return self.value


def test_validate_candidate_requires_trailing_y() -> None:
    assert validate_candidate("K7mQ9vT2pL6xR4nZy").endswith("y")


def test_registers_and_reads_back_password() -> None:
    api = FakeAPI()
    result = register_password(api, "海淋", "test1", "K7mQ9vT2pL6xR4nZy")
    assert result["bytes"] == 17
    assert api.parent == 1
    assert api.value.endswith("y")
