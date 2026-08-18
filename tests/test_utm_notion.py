from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import scripts.utm_notion as utm_notion
from scripts.vm_inventory import (
    get_record as get_inventory_record,
    mark_used,
    register_clone,
    reserve_vm_name,
    scan_inventory,
    set_available,
)

from scripts.utm_notion import (
    ACCOUNT_LABELS,
    APPLICATION_LABELS,
    APPLICATION_MULTILINE_LABELS,
    FeishuBitableClient,
    build_registration_fields,
    fill_template_fields,
    normalize_category,
)


ROOT = Path(__file__).resolve().parents[1]
TARGET_FEISHU_WIKI_URL = (
    "https://qv0zc1dq6qy.feishu.cn/wiki/C70TwyJevipDZakMTWYcusTwnhh"
    "?table=tbl5OOyYst8BqZ6x&view=vewjQVBVeu"
)


@pytest.fixture(autouse=True)
def isolate_utm_notion_feishu_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in utm_notion.UTM_NOTION_FEISHU_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_account_template_keeps_one_explicit_year_month_day_birthday_label() -> None:
    assert ACCOUNT_LABELS.count("生日（格式年/月/日）：") == 1
    assert "生日：" not in ACCOUNT_LABELS


ACCOUNT_TEMPLATE = """用户名：

邮箱：

初始密码：

修改后的密码：

电话：+1

电话短信接收平台：

生日（格式年/月/日）：

team ID:

APP_ID：

Renewal date：

代理ip:

代理端口:

代理用户名：

代理用户密码：

代码链接：

ABA Routing Number：

Account Number：

"""

APPLICATION_TEMPLATE = """应用名: 
团队: 灵光
顶级域名: 
正式包名: 
正式域名: 
隐私协议: 
用户协议: 
支持链接: 
应用类型：
应用描述：
关键词:
"""


def stringbed_fields() -> dict[str, object]:
    return {
        "Run ID": "run-stringbed-20260731",
        "应用名": "StringBed",
        "账号信息": {
            "text": (
                "stringbed@example.comAbc1D2ef🇩🇪德国2025550123"
                "https://sms-display.example.test/?code=visible"
            ),
            "link": "https://sms.example.test/?code=authoritative",
        },
        "代理信息": "192.0.2.10:1080:proxy-user:proxy-password",
        "代码 URL": {
            "text": "https://code.example.com/team/stringbed.git",
            "link": "https://code.example.com/team/stringbed.git",
        },
        "ABA Routing Number": "123456789",
        "Account Number": "9876543210",
        "顶级域名": "example.com",
        "正式包名": "com.example.stringbed",
        "正式域名(app/h5/im/log)": "app.example.com\nh5.example.com\nim.example.com\nlog.example.com",
        "隐私协议": {"text": "隐私", "link": "https://example.com/privacy"},
        "用户协议": {"text": "用户", "link": "https://example.com/terms"},
        "支持协议": {"text": "支持", "link": "https://example.com/support"},
        "应用类型": "生活方式-LIFESTYLE",
        "应用描述": "第一段\n\n第二段",
        "关键词": "生活,记录",
        "虚拟机": "",
    }


def test_absolute_script_entry_starts_outside_project_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "utm_notion.py"), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--run-id RUN_ID" in result.stdout
    assert "--app-name APP_NAME" in result.stdout
    assert "--vm-name VM_NAME" in result.stdout


def test_parse_feishu_wiki_url_returns_exact_node_table_and_view() -> None:
    assert utm_notion.parse_feishu_wiki_url(TARGET_FEISHU_WIKI_URL) == (
        "C70TwyJevipDZakMTWYcusTwnhh",
        "tbl5OOyYst8BqZ6x",
        "vewjQVBVeu",
    )


@pytest.mark.parametrize(
    "url",
    [
        (
            "https://example.test/wiki/C70TwyJevipDZakMTWYcusTwnhh"
            "?table=tbl5OOyYst8BqZ6x&view=vewjQVBVeu"
        ),
        (
            "https://qv0zc1dq6qy.feishu.cn/base/C70TwyJevipDZakMTWYcusTwnhh"
            "?table=tbl5OOyYst8BqZ6x&view=vewjQVBVeu"
        ),
        (
            "https://qv0zc1dq6qy.feishu.cn/wiki/C70TwyJevipDZakMTWYcusTwnhh"
            "?table=tbl5OOyYst8BqZ6x"
        ),
        (
            "https://qv0zc1dq6qy.feishu.cn/wiki/C70TwyJevipDZakMTWYcusTwnhh"
            "?table=tbl5OOyYst8BqZ6x&table=tblOther&view=vewjQVBVeu"
        ),
    ],
)
def test_parse_feishu_wiki_url_rejects_ambiguous_or_non_wiki_sources(url: str) -> None:
    with pytest.raises(RuntimeError, match="Feishu Wiki URL"):
        utm_notion.parse_feishu_wiki_url(url)


def test_resolve_bitable_app_token_uses_wiki_node_obj_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return (
                b'{"code":0,"msg":"success","data":{"node":{'
                b'"obj_type":"bitable",'
                b'"obj_token":"XW2DbRbx7aG1URs8KfTcN0nlnhb"}}}'
            )

    def fake_urlopen(request: object, *, timeout: int):
        assert timeout == 30
        requested_urls.append(str(getattr(request, "full_url")))
        return FakeResponse()

    monkeypatch.setattr(utm_notion.urllib.request, "urlopen", fake_urlopen)

    app_token = utm_notion.resolve_bitable_app_token(
        "tenant-token",
        "C70TwyJevipDZakMTWYcusTwnhh",
    )

    assert app_token == "XW2DbRbx7aG1URs8KfTcN0nlnhb"
    assert requested_urls == [
        "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node"
        "?token=C70TwyJevipDZakMTWYcusTwnhh"
    ]


def test_execute_prefers_project_env_for_utm_notion_feishu_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "UTM_NOTION_FEISHU_APP_TOKEN=bascnNewAppToken123",
                "UTM_NOTION_FEISHU_TABLE_ID=tblNewTable123",
                "UTM_NOTION_FEISHU_VIEW_ID=vewNewView123",
                "FEISHU_APP_ID=cliExampleApp123",
                "FEISHU_APP_SECRET=example-secret",
                "NOTION_TOKEN=example-notion-token",
                "NOTION_ROOT_PAGE_ID=example-notion-root",
                "SUBMISSION_HOST_MACHINE=Test Host",
                "SUBMISSION_VM_IMAGES_DIR=/tmp/test-vm-images",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(utm_notion, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("UTM_NOTION_FEISHU_APP_TOKEN", "bascnOldAppToken123")
    monkeypatch.setenv("UTM_NOTION_FEISHU_TABLE_ID", "tblOldTable123")
    monkeypatch.setenv("UTM_NOTION_FEISHU_VIEW_ID", "vewOldView123")
    monkeypatch.setattr(utm_notion, "get_tenant_access_token", lambda *_: "token")

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

    monkeypatch.setattr(utm_notion, "FeishuBitableClient", CapturingClient)

    with pytest.raises(StopAfterClient):
        utm_notion.execute(app_name="PackWise")

    assert captured == {
        "token": "token",
        "app_token": "bascnNewAppToken123",
        "table_id": "tblNewTable123",
        "view_id": "vewNewView123",
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("生活", "LIFESTYLE"),
        ("生活方式", "LIFESTYLE"),
        ("LIFESTYLE", "LIFESTYLE"),
        ("生活方式-LIFESTYLE", "LIFESTYLE"),
        ("图形与设计", "GRAPHICS_AND_DESIGN"),
        ("图形与设计-Graphics & Design", "GRAPHICS_AND_DESIGN"),
        ("图形和设计-Graphics & Design", "GRAPHICS_AND_DESIGN"),
        ("摄影和录像", "PHOTO_AND_VIDEO"),
        ("摄影和录像-Photo & Video", "PHOTO_AND_VIDEO"),
    ],
)
def test_normalize_category_accepts_chinese_enum_and_enum_suffix(source: str, expected: str) -> None:
    assert normalize_category(source) == expected


def test_build_registration_fields_uses_verified_sources_and_fixed_team() -> None:
    account, application = build_registration_fields(stringbed_fields())

    assert account == {
        "邮箱：": "stringbed@example.com",
        "初始密码：": "Abc1D2ef",
        "电话：": "+12025550123",
        "电话短信接收平台：": "https://sms-display.example.test/?code=visible",
        "代理ip:": "192.0.2.10",
        "代理端口:": "1080",
        "代理用户名：": "proxy-user",
        "代理用户密码：": "proxy-password",
        "代码链接：": "https://code.example.com/team/stringbed.git",
        "ABA Routing Number：": "123456789",
        "Account Number：": "9876543210",
    }
    assert application == {
        "应用名: ": "StringBed",
        "团队: ": "灵光",
        "顶级域名: ": "example.com",
        "正式包名: ": "com.example.stringbed",
        "正式域名: ": "app.example.com\nh5.example.com\nim.example.com\nlog.example.com",
        "隐私协议: ": "https://example.com/privacy",
        "用户协议: ": "https://example.com/terms",
        "支持链接: ": "https://example.com/support",
        "应用类型：": "LIFESTYLE",
        "应用描述：": "第一段\n\n第二段",
        "关键词:": "生活,记录",
    }


def test_build_registration_fields_uses_url_cell_link_when_sms_url_is_not_inline() -> None:
    fields = stringbed_fields()
    fields["账号信息"] = {
        "text": "stringbed@example.comAbc1D2ef🇩🇪德国2025550123",
        "link": "https://sms.example.test/?code=authoritative",
    }

    account, _ = build_registration_fields(fields)

    assert account["电话短信接收平台："] == "https://sms.example.test/?code=authoritative"


@pytest.mark.parametrize(
    "account_cell",
    [
        (
            "stringbed@example.comAbc1D2ef🇩🇪德国2025550123"
            "https://sms-display.example.test/?code=visible"
        ),
        [
            {
                "text": (
                    "stringbed@example.comAbc1D2ef🇩🇪德国2025550123"
                    "https://sms-display.example.test/?code=visible"
                )
            }
        ],
    ],
)
def test_build_registration_fields_accepts_textual_account_cell_with_inline_url(
    account_cell: object,
) -> None:
    fields = stringbed_fields()
    fields["账号信息"] = account_cell

    account, _ = build_registration_fields(fields)

    assert account["邮箱："] == "stringbed@example.com"
    assert account["初始密码："] == "Abc1D2ef"
    assert account["电话："] == "+12025550123"
    assert account["电话短信接收平台："] == (
        "https://sms-display.example.test/?code=visible"
    )


def test_build_registration_fields_accepts_space_separated_international_account() -> None:
    fields = stringbed_fields()
    fields["账号信息"] = (
        "stringbed@example.xyz Letters 441234567890 "
        "https://sms-display.example.test/?code=visible"
    )

    account, _ = build_registration_fields(fields)

    assert account["邮箱："] == "stringbed@example.xyz"
    assert account["初始密码："] == "Letters"
    assert account["电话："] == "+441234567890"
    assert account["电话短信接收平台："] == (
        "https://sms-display.example.test/?code=visible"
    )


def test_build_registration_fields_accepts_labelled_account_info_with_prefix() -> None:
    fields = stringbed_fields()
    fields["账号信息"] = {
        "text": (
            "✅🇩🇪德国MC7账号：stringbed@example.com密码：Abc1D2ef接码："
            "2025550123https://sms-display.example.test/?code=visible"
        ),
        "link": "http://not-a-sms-url.example.test",
    }

    account, _ = build_registration_fields(fields)

    assert account["邮箱："] == "stringbed@example.com"
    assert account["初始密码："] == "Abc1D2ef"
    assert account["电话："] == "+12025550123"
    assert account["电话短信接收平台："] == "https://sms-display.example.test/?code=visible"


@pytest.mark.parametrize(
    "account_text",
    [
        (
            "https://sms-display.example.test/?code=visible "
            "✅🇩🇪德国MC7账号：stringbed@example.com密码：Abc1D2ef接码：2025550123"
        ),
        (
            "✅🇩🇪德国MC7账号：stringbed@example.com密码：Abc1D2ef "
            "https://sms-display.example.test/?code=visible 接码：2025550123"
        ),
    ],
)
def test_build_registration_fields_accepts_unique_inline_sms_url_in_any_position(
    account_text: str,
) -> None:
    fields = stringbed_fields()
    fields["账号信息"] = {
        "text": account_text,
        "link": "http://not-a-sms-url.example.test",
    }

    account, _ = build_registration_fields(fields)

    assert account["邮箱："] == "stringbed@example.com"
    assert account["初始密码："] == "Abc1D2ef"
    assert account["电话："] == "+12025550123"
    assert account["电话短信接收平台："] == (
        "https://sms-display.example.test/?code=visible"
    )


def test_build_registration_fields_accepts_unlabelled_phone_before_url_and_country() -> None:
    fields = stringbed_fields()
    fields["账号信息"] = {
        "text": (
            "stringbed@example.comAbc1D2ef+12025550123----"
            "https://sms-display.example.test/?code=visible 🇩🇰 丹麦"
        ),
        "link": "http://not-a-sms-url.example.test",
    }

    account, _ = build_registration_fields(fields)

    assert account["邮箱："] == "stringbed@example.com"
    assert account["初始密码："] == "Abc1D2ef"
    assert account["电话："] == "+12025550123"
    assert account["电话短信接收平台："] == (
        "https://sms-display.example.test/?code=visible"
    )


def test_build_registration_fields_accepts_api1997_phone_url_email_without_separator() -> None:
    fields = stringbed_fields()
    fixture_url_identifier = "Ab1" + "x" * 31
    assert len(fixture_url_identifier) == 34
    sms_url = f"https://api1997.com/smsrecord?token={fixture_url_identifier}"
    fields["账号信息"] = {
        "text": (
            f"+12025550123----{sms_url}"
            "stringbed@example.comAbc1D2ef 🇩🇰 丹麦"
        ),
        "link": (
            "http://+12025550123----"
            f"{sms_url}stringbed@example.comAbc1D2ef 🇩🇰 丹麦"
        ),
    }

    account, _ = build_registration_fields(fields)

    assert account["邮箱："] == "stringbed@example.com"
    assert account["初始密码："] == "Abc1D2ef"
    assert account["电话："] == "+12025550123"
    assert account["电话短信接收平台："] == sms_url


def test_build_registration_fields_accepts_xsd20vip_compact_account_format() -> None:
    fields = stringbed_fields()
    fixture_url_identifier = "PG2" + "c" * 31
    assert len(fixture_url_identifier) == 34
    sms_url = f"https://xsd20vip.com/smsrecord?token={fixture_url_identifier}"
    fields["账号信息"] = {
        "text": (
            f"+12025550123----{sms_url}"
            "haraldurposselt@icloud.comEk161121Denmark 🇩🇰 丹麦"
        ),
        "link": "http://not-authoritative.example.test",
    }

    account, _ = build_registration_fields(fields)

    assert account["邮箱："] == "haraldurposselt@icloud.com"
    assert account["初始密码："] == "Ek161121"
    assert account["电话："] == "+12025550123"
    assert account["电话短信接收平台："] == sms_url


def test_build_registration_fields_accepts_provider_country_email_password_dollar_phone() -> None:
    fields = stringbed_fields()
    fields["账号信息"] = {
        "text": (
            "Outlook 🇭🇷 克罗地亚stringbed@example.comAbc1D2ef$+12025550123----"
            "https://sms-display.example.test/?code=visible"
        ),
        "link": "https://sms.example.test/?code=authoritative",
    }

    account, _ = build_registration_fields(fields)

    assert account["邮箱："] == "stringbed@example.com"
    assert account["初始密码："] == "Abc1D2ef$"
    assert account["电话："] == "+12025550123"
    assert account["电话短信接收平台："] == (
        "https://sms-display.example.test/?code=visible"
    )


def test_build_registration_fields_accepts_provider_email_password_and_plain_phone() -> None:
    fields = stringbed_fields()
    fields["账号信息"] = {
        "text": (
            "Outlook 🇫🇮stringbed@example.comAbcdefg12"
            "2025550123https://sms-display.example.test/?code=visible"
        ),
        "link": "https://sms.example.test/?code=authoritative",
    }

    account, _ = build_registration_fields(fields)

    assert account["邮箱："] == "stringbed@example.com"
    assert account["初始密码："] == "Abcdefg12"
    assert account["电话："] == "+12025550123"
    assert account["电话短信接收平台："] == (
        "https://sms-display.example.test/?code=visible"
    )


def test_build_registration_fields_uses_pending_domain_when_legacy_domain_fields_are_empty() -> None:
    fields = stringbed_fields()
    fields["顶级域名"] = ""
    fields["正式域名(app/h5/im/log)"] = ""
    fields["待购买域名"] = "example.com"

    _, application = build_registration_fields(fields)

    assert application["顶级域名: "] == "example.com"
    assert application["正式域名: "] == (
        "app.example.com\nh5.example.com\nim.example.com\nlog.example.com"
    )


def test_build_registration_fields_rejects_unverified_email_link() -> None:
    fields = stringbed_fields()
    fields["账号信息"] = {
        "text": "Abc1D2ef🇩🇪德国2025550123https://sms-display.example.test/?code=visible",
        "link": "https://sms.example.test/?code=authoritative",
    }

    with pytest.raises(RuntimeError, match="email"):
        build_registration_fields(fields)


def test_build_registration_fields_remains_strict_when_required_values_are_missing() -> None:
    fields = stringbed_fields()
    fields["账号信息"] = None

    with pytest.raises(RuntimeError, match="账号信息"):
        build_registration_fields(fields)


def test_build_registration_fields_fills_missing_values_with_test_only_when_requested() -> None:
    fields = stringbed_fields()
    fields.update(
        {
            "账号信息": None,
            "代理信息": "",
            "代码 URL": None,
            "ABA Routing Number": "",
            "Account Number": None,
            "正式域名(app/h5/im/log)": "",
            "隐私协议": None,
            "用户协议": "",
            "支持协议": None,
        }
    )

    account, application = build_registration_fields(
        fields,
        fill_missing_with_test=True,
    )

    assert account == {
        "邮箱：": "test",
        "初始密码：": "test",
        "电话：": "test",
        "电话短信接收平台：": "test",
        "代理ip:": "test",
        "代理端口:": "test",
        "代理用户名：": "test",
        "代理用户密码：": "test",
        "代码链接：": "test",
    }
    assert "ABA Routing Number：" not in account
    assert "Account Number：" not in account
    assert application["隐私协议: "] == "test"
    assert application["用户协议: "] == "test"
    assert application["支持链接: "] == "test"
    assert application["应用名: "] == "StringBed"
    assert application["正式域名: "] == "test"
    assert application["应用类型："] == "LIFESTYLE"


def test_build_registration_fields_rejects_an_invalid_agreement_url() -> None:
    fields = stringbed_fields()
    fields["隐私协议"] = "not-a-url"

    with pytest.raises(RuntimeError, match="隐私协议"):
        build_registration_fields(fields)

def test_empty_optional_bank_fields_are_omitted_to_preserve_the_template() -> None:
    fields = stringbed_fields()
    fields["ABA Routing Number"] = ""
    fields["Account Number"] = None

    account, _ = build_registration_fields(fields)

    assert "ABA Routing Number：" not in account
    assert "Account Number：" not in account


def test_generated_modified_password_meets_required_character_classes() -> None:
    for _ in range(100):
        password = utm_notion.generate_random_password()
        assert len(password) == 16
        assert any(character.islower() for character in password)
        assert any(character.isupper() for character in password)
        assert any(character.isdigit() for character in password)
        assert password.isalnum()


def test_modified_password_is_generated_only_for_blank_template_field() -> None:
    replacements: dict[str, str] = {}
    generated = utm_notion.ensure_modified_password(ACCOUNT_TEMPLATE, replacements)

    assert generated is True
    assert utm_notion.password_meets_requirements(replacements["修改后的密码："])

    existing = ACCOUNT_TEMPLATE.replace("修改后的密码：", "修改后的密码：Existing9Password")
    replacements = {}
    generated = utm_notion.ensure_modified_password(existing, replacements)

    assert generated is False
    assert "修改后的密码：" not in replacements


def test_fill_template_fields_preserves_labels_spacing_and_mapped_values() -> None:
    account, application = build_registration_fields(stringbed_fields())

    account_text = fill_template_fields(ACCOUNT_TEMPLATE, ACCOUNT_LABELS, account)
    application_text = fill_template_fields(
        APPLICATION_TEMPLATE,
        APPLICATION_LABELS,
        application,
        multiline_labels=APPLICATION_MULTILINE_LABELS,
    )

    assert "邮箱：stringbed@example.com\n" in account_text
    assert "初始密码：Abc1D2ef\n" in account_text
    assert "电话：+12025550123\n" in account_text
    assert "电话短信接收平台：https://sms-display.example.test/?code=visible\n" in account_text
    assert "代码链接：https://code.example.com/team/stringbed.git\n" in account_text
    assert application_text == """应用名: StringBed
团队: 灵光
顶级域名: example.com
正式包名: com.example.stringbed
正式域名: app.example.com
h5.example.com
im.example.com
log.example.com
隐私协议: https://example.com/privacy
用户协议: https://example.com/terms
支持链接: https://example.com/support
应用类型：LIFESTYLE
应用描述：第一段

第二段
关键词:生活,记录
"""


class FakeFeishu(FeishuBitableClient):
    def __init__(self, records: list[dict]) -> None:
        super().__init__("token", "app", "table", "view")
        self.records = records
        self.patches: list[tuple[str, dict]] = []

    def _request(self, method: str, path: str, *, query=None, payload=None):
        if method == "GET" and path.endswith("/records"):
            return {"items": self.records, "has_more": False}
        if method == "GET" and "/records/" in path:
            record_id = path.rsplit("/", 1)[1]
            return {"record": next(item for item in self.records if item["record_id"] == record_id)}
        if method == "PUT" and "/records/" in path:
            record_id = path.rsplit("/", 1)[1]
            self.patches.append((record_id, payload))
            record = next(item for item in self.records if item["record_id"] == record_id)
            record["fields"].update(payload["fields"])
            return {"record": record}
        raise AssertionError((method, path, query, payload))


def test_execute_preflights_vm_write_before_binding_and_notion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    record = {"record_id": "rec-stringbed", "fields": stringbed_fields()}

    class ForbiddenClient:
        def __init__(
            self,
            _token: str,
            _app_token: str,
            _table_id: str,
            _view_id: str,
        ) -> None:
            pass

        def find_exact_record(self, **_kwargs: str) -> dict[str, object]:
            return record

        def preflight_vm_write(self, _record: dict[str, object]) -> None:
            events.append("preflight")
            raise RuntimeError("Feishu API HTTP 403: code=91403 msg=Forbidden")

        def ensure_vm(self, _record: dict[str, object], _vm_name: str) -> bool:
            events.append("ensure")
            raise RuntimeError("Feishu API HTTP 403: code=91403 msg=Forbidden")

    def unexpected_bind(*_args: object, **_kwargs: object) -> str:
        events.append("bind")
        return "gvby"

    def unexpected_notion() -> object:
        events.append("notion")
        raise AssertionError("Notion must not be called after Feishu write preflight fails")

    monkeypatch.setattr(utm_notion, "read_env_file", lambda _path: {})
    monkeypatch.setattr(utm_notion, "get_tenant_access_token", lambda *_args: "token")
    monkeypatch.setattr(
        utm_notion,
        "resolve_utm_notion_source",
        lambda *_args: ("app", "tbl5OOyYst8BqZ6x", "vewjQVBVeu"),
    )
    monkeypatch.setattr(utm_notion, "FeishuBitableClient", ForbiddenClient)
    monkeypatch.setattr(utm_notion, "_bind_vm", unexpected_bind)
    monkeypatch.setattr(utm_notion, "notion_api_from_settings", lambda _settings: unexpected_notion())
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("UTM_NOTION_FEISHU_WIKI_URL", TARGET_FEISHU_WIKI_URL)
    monkeypatch.setenv("SUBMISSION_VM_IMAGES_DIR", str(tmp_path))
    monkeypatch.setenv("SUBMISSION_HOST_MACHINE", "Mac Studio")

    with pytest.raises(RuntimeError, match="91403"):
        utm_notion.execute(app_name="StringBed")

    assert events == ["preflight"]


def test_execute_with_explicit_vm_does_not_require_feishu_record_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    record = {"record_id": "rec-stringbed", "fields": stringbed_fields()}

    class ReadOnlyClient:
        def __init__(self, *_args: str) -> None:
            pass

        def find_exact_record(self, **_kwargs: str) -> dict[str, object]:
            return record

        def preflight_vm_write(self, _record: dict[str, object]) -> None:
            raise AssertionError("explicit VM must not require Feishu write preflight")

        def ensure_vm(self, _record: dict[str, object], _vm_name: str) -> bool:
            raise AssertionError("explicit VM must not require Feishu record update")

    class StopAfterExplicitBinding(RuntimeError):
        pass

    class ReadOnlyNotion:
        def verify_parent(self, _host: str) -> None:
            events.append("notion-verify")

        def create_registration_copy(self, _title: str) -> tuple[str, bool]:
            events.append("notion-create")
            raise StopAfterExplicitBinding

    def bind_exact(*_args: object, **_kwargs: object) -> str:
        events.append("bind")
        return "gvby"

    monkeypatch.setattr(utm_notion, "read_env_file", lambda _path: {})
    monkeypatch.setattr(utm_notion, "get_tenant_access_token", lambda *_args: "token")
    monkeypatch.setattr(
        utm_notion,
        "resolve_utm_notion_source",
        lambda *_args: ("app", "tbl5OOyYst8BqZ6x", "vewjQVBVeu"),
    )
    monkeypatch.setattr(utm_notion, "FeishuBitableClient", ReadOnlyClient)
    monkeypatch.setattr(utm_notion, "_bind_vm", bind_exact)
    monkeypatch.setattr(
        utm_notion,
        "notion_api_from_settings",
        lambda _settings: ReadOnlyNotion(),
    )
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("UTM_NOTION_FEISHU_WIKI_URL", TARGET_FEISHU_WIKI_URL)
    monkeypatch.setenv("SUBMISSION_VM_IMAGES_DIR", str(tmp_path))
    monkeypatch.setenv("SUBMISSION_HOST_MACHINE", "Mac Studio")

    with pytest.raises(StopAfterExplicitBinding):
        utm_notion.execute(app_name="StringBed", vm_name="gvby")

    assert events == ["notion-verify", "bind", "notion-create"]


def test_empty_vm_write_preflight_is_same_value_write_with_independent_readback() -> None:
    fields = stringbed_fields()
    before_digest = utm_notion._fields_digest(fields, omit={"虚拟机"})

    class TrackingFeishu(FakeFeishu):
        def __init__(self, records: list[dict]) -> None:
            super().__init__(records)
            self.request_methods: list[str] = []

        def _request(self, method: str, path: str, *, query=None, payload=None):
            self.request_methods.append(method)
            return super()._request(method, path, query=query, payload=payload)

    client = TrackingFeishu([{"record_id": "rec-stringbed", "fields": fields}])
    record = client.find_exact_record(app_name="StringBed")
    client.request_methods.clear()

    client.preflight_vm_write(record)

    assert client.request_methods == ["GET", "PUT", "GET"]
    assert client.patches == [("rec-stringbed", {"fields": {"虚拟机": None}})]
    assert utm_notion._cell_text(record["fields"]["虚拟机"]) == ""
    assert utm_notion._fields_digest(record["fields"], omit={"虚拟机"}) == before_digest


def test_fields_digest_ignores_only_volatile_attachment_urls() -> None:
    before = {
        "研发截图": [
            {
                "file_token": "file-stable",
                "name": "shot.png",
                "size": 123,
                "type": "image/png",
                "url": "https://example.invalid/first",
                "tmp_url": "https://example.invalid/temp-first",
            }
        ]
    }
    after = {
        "研发截图": [
            {
                "file_token": "file-stable",
                "name": "shot.png",
                "size": 123,
                "type": "image/png",
                "url": "https://example.invalid/second",
                "tmp_url": "https://example.invalid/temp-second",
            }
        ]
    }

    assert utm_notion._fields_digest(before) == utm_notion._fields_digest(after)


def test_feishu_exact_run_match_and_vm_only_update() -> None:
    fields = stringbed_fields()
    client = FakeFeishu([{"record_id": "rec-stringbed", "fields": fields}])

    record = client.find_exact_record(run_id="run-stringbed-20260731")
    client.ensure_vm(record, "abcd")

    assert client.patches == [("rec-stringbed", {"fields": {"虚拟机": "abcd"}})]
    assert record["fields"]["应用名"] == "StringBed"


def test_feishu_does_not_overwrite_a_different_vm() -> None:
    fields = stringbed_fields()
    fields["虚拟机"] = "wxyz"
    client = FakeFeishu([{"record_id": "rec-stringbed", "fields": fields}])

    with pytest.raises(RuntimeError, match="already contains"):
        client.ensure_vm(client.find_exact_record(app_name="StringBed"), "abcd")

    assert client.patches == []


def test_feishu_idempotent_vm_binding_does_not_require_write_permission() -> None:
    fields = stringbed_fields()
    fields["虚拟机"] = "abcd"
    client = FakeFeishu([{"record_id": "rec-stringbed", "fields": fields}])

    changed = client.ensure_vm(client.find_exact_record(app_name="StringBed"), "abcd")

    assert changed is False
    assert client.patches == []


def test_bind_vm_reconciles_a_present_initialized_pool_bundle_before_claim(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.sqlite3"
    images = tmp_path / "images"
    images.mkdir()
    vm_name = reserve_vm_name(database, images, candidate_factory=lambda: "abcd")
    bundle = images / f"{vm_name}.utm"
    bundle.mkdir()
    register_clone(
        database,
        vm_name,
        "12345678-1234-1234-1234-1234567890AB",
        bundle,
        "02:00:00:00:00:01",
        "a" * 64,
    )
    set_available(database, vm_name, True)
    assert get_inventory_record(database, vm_name)["directory_present"] == 0

    claimed = utm_notion._bind_vm({}, "WaxCue", database, images)

    assert claimed == vm_name
    record = get_inventory_record(database, vm_name)
    assert record["directory_present"] == 1
    assert record["application_name"] == "WaxCue"
    assert record["available"] == 0


def test_bind_vm_claims_the_explicit_clone_instead_of_an_older_available_vm(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.sqlite3"
    images = tmp_path / "images"
    images.mkdir()
    for vm_name, suffix in (("abcd", "1"), ("wxyz", "2")):
        bundle = images / f"{vm_name}.utm"
        reserve_vm_name(
            database,
            images,
            candidate_factory=lambda value=vm_name: value,
        )
        bundle.mkdir()
        register_clone(
            database,
            vm_name,
            f"{suffix * 8}-{suffix * 4}-{suffix * 4}-{suffix * 4}-{suffix * 12}",
            bundle,
            f"02:00:00:00:00:0{suffix}",
            suffix * 64,
        )
        set_available(database, vm_name, True)

    claimed = utm_notion._bind_vm(
        {},
        "SowSheet",
        database,
        images,
        expected_vm_name="wxyz",
    )

    assert claimed == "wxyz"
    assert get_inventory_record(database, "abcd")["available"] == 1
    record = get_inventory_record(database, "wxyz")
    assert record["application_name"] == "SowSheet"
    assert record["available"] == 0


def test_bind_vm_allows_only_an_explicit_application_rename(tmp_path: Path) -> None:
    database = tmp_path / "inventory.sqlite3"
    images = tmp_path / "images"
    images.mkdir()
    bundle = images / "rbqi.utm"
    reserve_vm_name(database, images, candidate_factory=lambda: "rbqi")
    bundle.mkdir()
    register_clone(
        database,
        "rbqi",
        "12345678-1234-1234-1234-1234567890AB",
        bundle,
        "02:00:00:00:00:01",
        "a" * 64,
    )
    set_available(database, "rbqi", True)
    scan_inventory(database, images)
    mark_used(database, "rbqi", "RainyDay")

    claimed = utm_notion._bind_vm(
        {"虚拟机": "rbqi"},
        "RainyDay-Kids",
        database,
        images,
        expected_vm_name="rbqi",
        rename_from="RainyDay",
    )

    assert claimed == "rbqi"
    assert get_inventory_record(database, "rbqi")["application_name"] == "RainyDay-Kids"
