from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

import pytest

from scripts import utm_notion
from services.host_config import ConfigurationError
from services import utm_notion_source


WIKI_URL = (
    "https://example.feishu.cn/wiki/ExampleWikiNode123"
    "?table=tblExample123&view=vewExample123"
)
FIXTURE_APP_IDENTIFIER = "bascnExampleToken123"
FIXTURE_TABLE_IDENTIFIER = "tblExampleTable123"
FIXTURE_VIEW_IDENTIFIER = "vewExampleView123"


def valid_runtime_settings() -> dict[str, str]:
    return {
        "FEISHU_APP_ID": "cliExampleAppId123",
        "FEISHU_APP_SECRET": "feishu-app-secret-sentinel",
        "NOTION_TOKEN": "notion-token-sentinel",
        "NOTION_ROOT_PAGE_ID": "notion-root-page-id",
        "NOTION_TEMPLATE_TITLE": "模板",
        "SUBMISSION_HOST_MACHINE": "Test Host",
        "SUBMISSION_VM_IMAGES_DIR": "/tmp/utm-notion-vm-images",
        "UTM_NOTION_FEISHU_APP_TOKEN": FIXTURE_APP_IDENTIFIER,
        "UTM_NOTION_FEISHU_TABLE_ID": FIXTURE_TABLE_IDENTIFIER,
        "UTM_NOTION_FEISHU_VIEW_ID": FIXTURE_VIEW_IDENTIFIER,
    }


@pytest.fixture(autouse=True)
def isolate_source_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in utm_notion_source.SOURCE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    "configured",
    [
        {},
        {"UTM_NOTION_FEISHU_APP_TOKEN": "app-only"},
        {
            "UTM_NOTION_FEISHU_WIKI_URL": WIKI_URL,
            "UTM_NOTION_FEISHU_APP_TOKEN": "mixed-app",
            "UTM_NOTION_FEISHU_TABLE_ID": "mixed-table",
            "UTM_NOTION_FEISHU_VIEW_ID": "mixed-view",
        },
    ],
)
def test_execute_rejects_missing_partial_or_mixed_source_before_network(
    configured: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = valid_runtime_settings()
    for key in utm_notion_source.SOURCE_ENV_KEYS:
        settings.pop(key, None)
    settings.update(configured)
    monkeypatch.setattr(
        utm_notion,
        "host_settings_snapshot",
        lambda **_kwargs: MappingProxyType(settings),
        raising=False,
    )
    monkeypatch.setattr(
        utm_notion,
        "get_tenant_access_token",
        lambda *_args: pytest.fail("configuration must fail before tenant-token network"),
    )
    monkeypatch.setattr(
        utm_notion_source.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("configuration must fail before Wiki network"),
    )
    monkeypatch.setattr(
        utm_notion,
        "FeishuBitableClient",
        lambda *_args, **_kwargs: pytest.fail(
            "configuration must fail before Base client construction"
        ),
    )
    before = dict(os.environ)

    with pytest.raises(RuntimeError) as captured:
        utm_notion.execute(app_name="PackWise")

    message = str(captured.value)
    assert "UTM_NOTION_FEISHU" in message
    assert all(value not in message for value in configured.values())
    assert dict(os.environ) == before


@pytest.mark.parametrize(
    "missing_key",
    (
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "NOTION_TOKEN",
        "NOTION_ROOT_PAGE_ID",
        "SUBMISSION_HOST_MACHINE",
        "SUBMISSION_VM_IMAGES_DIR",
        "UTM_NOTION_FEISHU_APP_TOKEN",
        "UTM_NOTION_FEISHU_TABLE_ID",
        "UTM_NOTION_FEISHU_VIEW_ID",
    ),
)
def test_execute_preflights_every_required_setting_before_any_side_effect(
    missing_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = valid_runtime_settings()
    missing_value = settings.pop(missing_key)
    monkeypatch.setattr(
        utm_notion,
        "host_settings_snapshot",
        lambda **_kwargs: MappingProxyType(settings),
        raising=False,
    )
    for name in (
        "get_tenant_access_token",
        "resolve_utm_notion_source",
        "FeishuBitableClient",
        "_bind_vm",
        "NotionAPI",
    ):
        monkeypatch.setattr(
            utm_notion,
            name,
            lambda *_args, _name=name, **_kwargs: pytest.fail(
                f"{_name} must not run before configuration preflight"
            ),
        )
    before = dict(os.environ)

    with pytest.raises(ConfigurationError) as captured:
        utm_notion.execute(app_name="PackWise")

    message = str(captured.value)
    assert missing_key in message
    assert missing_value not in message
    assert all(value not in message for value in settings.values())
    assert dict(os.environ) == before


def test_runtime_config_uses_one_snapshot_and_is_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version_one = valid_runtime_settings()
    version_two = {
        **version_one,
        "SUBMISSION_HOST_MACHINE": "Changed Host",
        "SUBMISSION_VM_IMAGES_DIR": "/tmp/changed-images",
    }
    snapshots = iter(
        (MappingProxyType(version_one), MappingProxyType(version_two))
    )
    calls = 0

    def next_snapshot(**_kwargs: object):
        nonlocal calls
        calls += 1
        return next(snapshots)

    monkeypatch.setattr(
        utm_notion,
        "host_settings_snapshot",
        next_snapshot,
        raising=False,
    )

    config = utm_notion.load_runtime_config(environ={})

    assert calls == 1
    assert config.host_title == "Test Host"
    assert config.images_dir == Path("/tmp/utm-notion-vm-images")
    with pytest.raises(FrozenInstanceError):
        config.host_title = "mutated"


def test_complete_direct_source_needs_no_wiki_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UTM_NOTION_FEISHU_APP_TOKEN", FIXTURE_APP_IDENTIFIER)
    monkeypatch.setenv("UTM_NOTION_FEISHU_TABLE_ID", FIXTURE_TABLE_IDENTIFIER)
    monkeypatch.setenv("UTM_NOTION_FEISHU_VIEW_ID", FIXTURE_VIEW_IDENTIFIER)
    monkeypatch.setattr(
        utm_notion.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("direct source must not use Wiki network"),
    )

    configured = utm_notion_source.configured_feishu_source()

    assert utm_notion_source.resolve_feishu_source("tenant-token", configured) == (
        FIXTURE_APP_IDENTIFIER,
        FIXTURE_TABLE_IDENTIFIER,
        FIXTURE_VIEW_IDENTIFIER,
    )


@pytest.mark.parametrize(
    ("key", "invalid_value"),
    (
        ("UTM_NOTION_FEISHU_APP_TOKEN", "base-token-with-dashes"),
        ("UTM_NOTION_FEISHU_TABLE_ID", "tableWithoutTblPrefix"),
        ("UTM_NOTION_FEISHU_VIEW_ID", "viewWithoutVewPrefix"),
    ),
)
def test_direct_source_uses_shared_real_identifier_validation(
    key: str,
    invalid_value: str,
) -> None:
    settings = valid_runtime_settings()
    settings[key] = invalid_value

    with pytest.raises(RuntimeError) as captured:
        utm_notion_source.configured_feishu_source(settings)

    message = str(captured.value)
    assert key in message
    assert invalid_value not in message


def test_wiki_source_resolves_base_token_after_local_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "code": 0,
                    "data": {
                        "node": {
                            "obj_type": "bitable",
                            "obj_token": "ResolvedBaseToken123",
                        }
                    },
                }
            ).encode()

    monkeypatch.setenv("UTM_NOTION_FEISHU_WIKI_URL", WIKI_URL)
    monkeypatch.setattr(
        utm_notion_source.urllib.request,
        "urlopen",
        lambda _request, *, timeout: FakeResponse(),
    )

    configured = utm_notion_source.configured_feishu_source()

    assert utm_notion_source.resolve_feishu_source("tenant-token", configured) == (
        "ResolvedBaseToken123",
        "tblExample123",
        "vewExample123",
    )


def test_source_has_no_fixed_bitable_identifier_defaults() -> None:
    assert not hasattr(utm_notion, "DEFAULT_APP_TOKEN")
    assert not hasattr(utm_notion, "DEFAULT_TABLE_ID")
    assert not hasattr(utm_notion, "DEFAULT_VIEW_ID")


def test_execute_reads_project_source_without_mutating_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        f"UTM_NOTION_FEISHU_APP_TOKEN={FIXTURE_APP_IDENTIFIER}\n"
        f"UTM_NOTION_FEISHU_TABLE_ID={FIXTURE_TABLE_IDENTIFIER}\n"
        f"UTM_NOTION_FEISHU_VIEW_ID={FIXTURE_VIEW_IDENTIFIER}\n"
        "FEISHU_APP_ID=project-feishu-app\n"
        "FEISHU_APP_SECRET=project-feishu-secret\n"
        "NOTION_TOKEN=project-notion-token\n"
        "NOTION_ROOT_PAGE_ID=project-notion-root\n"
        "SUBMISSION_HOST_MACHINE=Project Host\n"
        "SUBMISSION_VM_IMAGES_DIR=/tmp/project-vm-images\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(utm_notion, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("UTM_NOTION_FEISHU_APP_TOKEN", "bascnProcessToken123")
    monkeypatch.setenv("UTM_NOTION_FEISHU_TABLE_ID", "tblProcessTable123")
    monkeypatch.setenv("UTM_NOTION_FEISHU_VIEW_ID", "vewProcessView123")
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    before = dict(os.environ)
    captured: dict[str, str] = {}

    class StopAfterClient(RuntimeError):
        pass

    class CapturingClient:
        def __init__(
            self,
            token: str,
            app_token: str,
            table_id: str,
            view_id: str,
        ) -> None:
            captured.update(
                token=token,
                app_token=app_token,
                table_id=table_id,
                view_id=view_id,
            )

        def find_exact_record(self, **_kwargs: str) -> dict[str, object]:
            raise StopAfterClient

    def tenant_token(app_id: str, _secret: str) -> str:
        captured["app_id"] = app_id
        return "token"

    monkeypatch.setattr(utm_notion, "get_tenant_access_token", tenant_token)
    monkeypatch.setattr(utm_notion, "FeishuBitableClient", CapturingClient)

    with pytest.raises(StopAfterClient):
        utm_notion.execute(app_name="PackWise")

    assert captured == {
        "app_id": "project-feishu-app",
        "token": "token",
        "app_token": FIXTURE_APP_IDENTIFIER,
        "table_id": FIXTURE_TABLE_IDENTIFIER,
        "view_id": FIXTURE_VIEW_IDENTIFIER,
    }
    assert dict(os.environ) == before
