from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "utm_image_source.py"
SKILL = ROOT / "skills" / "utm-image" / "SKILL.md"
BASE_URL = (
    "https://qv0zc1dq6qy.feishu.cn/base/LNFdbb2cxaauuvsVGuGcebMhnxm"
    "?table=tblcENvSwMO6lhSH&view=vewfxNVEGj"
)


def load_source_module():
    assert SOURCE.is_file(), "utm_image_source.py must exist"
    spec = importlib.util.spec_from_file_location("utm_image_source_tested", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, records: list[dict], payloads: dict[str, bytes] | None = None):
        self.records = records
        self.payloads = payloads or {}
        self.downloaded: list[str] = []

    def list_records(self, _location):
        return self.records

    def download_media(self, file_token: str) -> bytes:
        self.downloaded.append(file_token)
        return self.payloads[file_token]


def record(app_name: str, *, beauty=None, development=None) -> dict:
    fields = {"应用名": app_name}
    if beauty is not None:
        fields["美女截图 链接"] = beauty
    if development is not None:
        fields["研发截图"] = development
    return {"record_id": f"record-{app_name}", "fields": fields}


def test_beauty_link_has_priority_without_downloading_attachment(tmp_path: Path) -> None:
    module = load_source_module()
    client = FakeClient(
        [
            record(
                "Xrimo",
                beauty="https://c.wss.ink/f/beauty_123",
                development=[{"file_token": "dev-token", "name": "dev.zip"}],
            )
        ]
    )

    result = module.prepare_screenshot_source(
        base_url=BASE_URL,
        app_name="Xrimo",
        output_dir=tmp_path,
        client=client,
    )

    assert result["source_field"] == "美女截图 链接"
    assert result["kind"] == "share_url"
    assert result["app_name_sha256"] == hashlib.sha256(b"Xrimo").hexdigest()
    assert client.downloaded == []
    assert (tmp_path / "source.json").stat().st_mode & 0o777 == 0o600


def test_blank_beauty_link_falls_back_to_one_development_attachment(
    tmp_path: Path,
) -> None:
    module = load_source_module()
    client = FakeClient(
        [
            record(
                "Xrimo",
                beauty="   ",
                development=[{"file_token": "dev-token", "name": "screens.zip"}],
            )
        ],
        {"dev-token": b"PK\x03\x04test-archive"},
    )

    result = module.prepare_screenshot_source(
        base_url=BASE_URL,
        app_name="Xrimo",
        output_dir=tmp_path,
        client=client,
    )

    assert result["source_field"] == "研发截图"
    assert result["kind"] == "attachment"
    assert client.downloaded == ["dev-token"]
    attachment = Path(result["path"])
    assert attachment.is_file()
    assert attachment.parent == tmp_path
    assert attachment.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "beauty",
    [
        "https://example.com/f/not-wenshushu",
        [
            {"text": "https://c.wss.ink/f/first"},
            {"text": "https://c.wss.ink/f/second"},
        ],
    ],
)
def test_non_empty_invalid_beauty_value_blocks_without_attachment_fallback(
    tmp_path: Path, beauty
) -> None:
    module = load_source_module()
    client = FakeClient(
        [
            record(
                "Xrimo",
                beauty=beauty,
                development=[{"file_token": "dev-token", "name": "screens.zip"}],
            )
        ],
        {"dev-token": b"PK\x03\x04test-archive"},
    )

    with pytest.raises(ValueError, match="美女截图 链接"):
        module.prepare_screenshot_source(
            base_url=BASE_URL,
            app_name="Xrimo",
            output_dir=tmp_path,
            client=client,
        )

    assert client.downloaded == []


@pytest.mark.parametrize(
    "records, error",
    [
        ([], "found 0"),
        ([record("Xrimo"), record("Xrimo")], "found 2"),
        ([record("Xrimo", beauty="", development=[])], "exactly one attachment"),
        (
            [
                record(
                    "Xrimo",
                    beauty="",
                    development=[
                        {"file_token": "one", "name": "one.zip"},
                        {"file_token": "two", "name": "two.zip"},
                    ],
                )
            ],
            "exactly one attachment",
        ),
    ],
)
def test_source_selection_rejects_non_unique_or_missing_authority(
    tmp_path: Path, records: list[dict], error: str
) -> None:
    module = load_source_module()
    with pytest.raises(ValueError, match=error):
        module.prepare_screenshot_source(
            base_url=BASE_URL,
            app_name="Xrimo",
            output_dir=tmp_path,
            client=FakeClient(records),
        )


@pytest.mark.parametrize(
    "attachment",
    [
        {"file_token": "token", "name": "../screens.zip"},
        {"file_token": "token", "name": r"..\screens.zip"},
        {"file_token": "token", "name": "screens.txt"},
        {"file_token": "   ", "name": "screens.zip"},
        {"name": "screens.zip"},
    ],
)
def test_development_attachment_rejects_unsafe_metadata(
    tmp_path: Path, attachment: dict
) -> None:
    module = load_source_module()
    client = FakeClient([record("Xrimo", beauty="", development=[attachment])])

    with pytest.raises(ValueError, match="研发截图"):
        module.prepare_screenshot_source(
            base_url=BASE_URL,
            app_name="Xrimo",
            output_dir=tmp_path,
            client=client,
        )

    assert client.downloaded == []


def test_skill_contract_uses_feishu_automatically_without_manual_link_input() -> None:
    assert SKILL.is_file(), "utm-image must be the canonical skill name"
    text = SKILL.read_text(encoding="utf-8")
    assert "$PROJECT_ROOT/scripts/utm_image.py" in text
    for implementation_detail in (BASE_URL, "美女截图 链接", "研发截图", "utm_image_source.py"):
        assert implementation_detail not in text
    assert "shareUrl" not in text
    assert "手动链接" not in text
    assert "用户确认" not in text
