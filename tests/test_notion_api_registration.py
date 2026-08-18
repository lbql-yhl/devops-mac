from __future__ import annotations

import pytest

from scripts.notion_api import NotionAPI


def child_page(block_id: str, title: str) -> dict:
    return {"id": block_id, "type": "child_page", "child_page": {"title": title}}


def heading(block_id: str, text: str) -> dict:
    return {
        "id": block_id,
        "type": "heading_2",
        "heading_2": {"rich_text": [{"plain_text": text}]},
    }


def code(block_id: str, text: str) -> dict:
    return {
        "id": block_id,
        "type": "code",
        "code": {"rich_text": [{"plain_text": text}] if text else []},
    }


class TemplateNotion(NotionAPI):
    def __init__(self, *, fail_application_write: bool = False) -> None:
        super().__init__("token", "root")
        self.created = False
        self.account = "邮箱：\n\n电话：+1\n"
        self.application = "应用名: \n团队: 灵光\n"
        self.fail_application_write = fail_application_write
        self.patches: list[tuple[str, str]] = []

    def _request(self, method: str, path: str, payload=None, query=None):
        if method == "GET" and path == "/blocks/root/children":
            children = [child_page("template", "模板")]
            if self.created:
                children.append(child_page("page", "StringBed-abcd"))
            return {"results": children, "has_more": False}
        if method == "POST" and path == "/pages":
            self.created = True
            return {"id": "page"}
        if method == "GET" and path == "/blocks/page/children":
            return {
                "results": [
                    heading("account-heading", "账号信息"),
                    code("account-code", self.account),
                    heading("application-heading", "应用信息"),
                    code("application-code", self.application),
                ],
                "has_more": False,
            }
        if method == "PATCH" and path == "/blocks/account-code":
            value = "".join(item["text"]["content"] for item in payload["code"]["rich_text"])
            self.account = value
            self.patches.append(("账号信息", value))
            return code("account-code", value)
        if method == "PATCH" and path == "/blocks/application-code":
            if self.fail_application_write:
                self.fail_application_write = False
                raise RuntimeError("application write failed")
            value = "".join(item["text"]["content"] for item in payload["code"]["rich_text"])
            self.application = value
            self.patches.append(("应用信息", value))
            return code("application-code", value)
        raise AssertionError((method, path, payload, query))


def test_create_registration_copy_accepts_nonempty_template_sections() -> None:
    api = TemplateNotion()

    page_id, created = api.create_registration_copy("StringBed-abcd")

    assert (page_id, created) == ("page", True)
    assert api.patches == []


def test_write_registration_sections_updates_both_and_reads_back() -> None:
    api = TemplateNotion()
    api.create_registration_copy("StringBed-abcd")

    changed = api.write_registration_sections(
        "StringBed-abcd",
        {"账号信息": "邮箱：a@example.com\n", "应用信息": "应用名: StringBed\n团队: 灵光\n"},
    )

    assert changed == {"账号信息": True, "应用信息": True}
    assert api.account == "邮箱：a@example.com\n"
    assert api.application == "应用名: StringBed\n团队: 灵光\n"


def test_write_registration_sections_rolls_back_first_section_when_second_fails() -> None:
    api = TemplateNotion(fail_application_write=True)
    api.create_registration_copy("StringBed-abcd")
    before_account = api.account
    before_application = api.application

    with pytest.raises(RuntimeError, match="application write failed"):
        api.write_registration_sections(
            "StringBed-abcd",
            {"账号信息": "邮箱：a@example.com\n", "应用信息": "应用名: StringBed\n"},
        )

    assert api.account == before_account
    assert api.application == before_application
