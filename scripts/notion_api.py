#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


API_BASE = "https://api.notion.com/v1"
API_VERSION = "2026-03-11"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class NotionAPI:
    def __init__(self, token: str, root_page_id: str, template_title: str = "模板") -> None:
        if not token or not root_page_id:
            raise RuntimeError("NOTION_TOKEN and NOTION_ROOT_PAGE_ID are required")
        self.token = token
        self.root_page_id = root_page_id
        self.template_title = template_title

    def _request(self, method: str, path: str, payload=None, query=None) -> dict[str, Any]:
        url = API_BASE + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": API_VERSION,
                "Content-Type": "application/json",
            },
        )
        retry_delays = (0, 2, 5, 10) if method == "GET" else (0,)
        for attempt, delay in enumerate(retry_delays):
            if delay:
                time.sleep(delay)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                try:
                    body = json.load(error)
                except Exception:
                    body = {}
                raise RuntimeError(
                    f"Notion API HTTP {error.code}: {body.get('code', 'unknown')} - "
                    f"{body.get('message', 'request failed')}"
                ) from None
            except urllib.error.URLError:
                if attempt + 1 == len(retry_delays):
                    raise
        raise RuntimeError("Notion API retry loop exhausted")

    def _children(self, block_id: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        query: dict[str, Any] = {"page_size": 100}
        while True:
            response = self._request("GET", f"/blocks/{block_id}/children", query=query)
            results.extend(response.get("results", []))
            if not response.get("has_more"):
                return results
            query["start_cursor"] = response["next_cursor"]

    @staticmethod
    def _rich_text_text(items: list[dict[str, Any]]) -> str:
        return "".join(item.get("plain_text", item.get("text", {}).get("content", "")) for item in items)

    @staticmethod
    def _rich_text(text: str) -> list[dict[str, Any]]:
        return [
            {"type": "text", "text": {"content": text[index:index + 2000]}}
            for index in range(0, len(text), 2000)
        ]

    def _child_page(self, parent_id: str, title: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
        matches = [
            block for block in self._children(parent_id)
            if block.get("type") == "child_page" and block.get("child_page", {}).get("title") == title
        ]
        if not matches and missing_ok:
            return None
        if len(matches) != 1:
            raise RuntimeError(f"Expected exactly one direct child page named {title!r}; found {len(matches)}")
        return matches[0]

    def _registration_page(self, title: str) -> dict[str, Any]:
        page = self._child_page(self.root_page_id, title)
        assert page is not None
        return page

    def verify_parent(self, expected_title: str) -> str:
        page = self._request("GET", f"/pages/{self.root_page_id}")
        titles = [
            self._rich_text_text(prop.get("title", []))
            for prop in page.get("properties", {}).values()
            if prop.get("type") == "title"
        ]
        if titles != [expected_title]:
            raise RuntimeError(f"Configured Notion parent is not {expected_title!r}")
        return titles[0]

    def _code_after_heading(
        self, page_id: str, heading: str, *, missing_ok: bool = False
    ) -> dict[str, Any] | None:
        blocks = self._children(page_id)
        indexes = []
        for index, block in enumerate(blocks):
            block_type = block.get("type", "")
            if block_type not in {"heading_1", "heading_2", "heading_3"}:
                continue
            if self._rich_text_text(block.get(block_type, {}).get("rich_text", [])) == heading:
                indexes.append(index)
        if not indexes and missing_ok:
            return None
        if len(indexes) != 1:
            raise RuntimeError(f"Expected exactly one heading {heading!r}; found {len(indexes)}")
        next_index = indexes[0] + 1
        if next_index >= len(blocks) or blocks[next_index].get("type") != "code":
            if missing_ok:
                return None
            raise RuntimeError(f"Heading {heading!r} is not immediately followed by a code block")
        return blocks[next_index]

    def _wait_for_code(self, page_id: str, heading: str, seconds: int = 90) -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        while True:
            block = self._code_after_heading(page_id, heading, missing_ok=True)
            if block is not None:
                return block
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Timed out waiting for template section {heading!r}")
            time.sleep(2)

    def _code_text(self, block: dict[str, Any]) -> str:
        return self._rich_text_text(block.get("code", {}).get("rich_text", []))

    def _code_in_toggle(self, page_id: str, heading: str, toggle: str) -> dict[str, Any]:
        blocks = self._children(page_id)
        heading_indexes = [
            index
            for index, block in enumerate(blocks)
            if block.get("type") in {"heading_1", "heading_2", "heading_3"}
            and self._rich_text_text(
                block.get(block.get("type", ""), {}).get("rich_text", [])
            ) == heading
        ]
        if len(heading_indexes) != 1:
            raise RuntimeError(f"Expected exactly one heading {heading!r}; found {len(heading_indexes)}")

        start = heading_indexes[0] + 1
        end = next(
            (
                index
                for index in range(start, len(blocks))
                if blocks[index].get("type") in {"heading_1", "heading_2", "heading_3"}
            ),
            len(blocks),
        )
        toggles = [
            block
            for block in blocks[start:end]
            if block.get("type") == "toggle"
            and self._rich_text_text(block.get("toggle", {}).get("rich_text", [])) == toggle
        ]
        if len(toggles) != 1:
            raise RuntimeError(
                f"Expected exactly one toggle {toggle!r} under heading {heading!r}; found {len(toggles)}"
            )

        codes = [block for block in self._children(toggles[0]["id"]) if block.get("type") == "code"]
        if len(codes) != 1:
            raise RuntimeError(f"Expected exactly one code block in toggle {toggle!r}; found {len(codes)}")
        return codes[0]

    def _write_section_by_id(
        self, page_id: str, heading: str, text: str, *, replace_existing: bool = False
    ) -> bool:
        block = self._wait_for_code(page_id, heading)
        current = self._code_text(block)
        if current == text:
            return False
        if current.strip() and not replace_existing:
            raise RuntimeError(f"Refusing to replace non-empty Notion section {heading!r}")
        self._request(
            "PATCH",
            f"/blocks/{block['id']}",
            {"code": {"rich_text": self._rich_text(text)}},
        )
        saved = self._code_after_heading(page_id, heading)
        if saved is None or self._code_text(saved) != text:
            raise RuntimeError(f"Notion save verification failed for section {heading!r}")
        return True

    def create_registration(self, title: str, account_text: str) -> str:
        existing = self._child_page(self.root_page_id, title, missing_ok=True)
        if existing is not None:
            page_id = existing["id"]
            self._write_section_by_id(page_id, "账号信息", account_text)
            return page_id

        template = self._child_page(self.root_page_id, self.template_title)
        assert template is not None
        page = self._request(
            "POST",
            "/pages",
            {
                "parent": {"type": "page_id", "page_id": self.root_page_id},
                "properties": {
                    "title": {
                        "type": "title",
                        "title": [{"type": "text", "text": {"content": title}}],
                    }
                },
                "template": {
                    "type": "template_id",
                    "template_id": template["id"],
                    "timezone": "Asia/Shanghai",
                },
            },
        )
        page_id = page["id"]
        self._write_section_by_id(page_id, "账号信息", account_text)
        return page_id

    def create_registration_copy(self, title: str) -> tuple[str, bool]:
        """Create only the template copy, leaving every copied block intact."""
        existing = self._child_page(self.root_page_id, title, missing_ok=True)
        if existing is not None:
            return str(existing["id"]), False

        template = self._child_page(self.root_page_id, self.template_title)
        assert template is not None
        page = self._request(
            "POST",
            "/pages",
            {
                "parent": {"type": "page_id", "page_id": self.root_page_id},
                "properties": {
                    "title": {
                        "type": "title",
                        "title": [{"type": "text", "text": {"content": title}}],
                    }
                },
                "template": {
                    "type": "template_id",
                    "template_id": template["id"],
                    "timezone": "Asia/Shanghai",
                },
            },
        )
        page_id = str(page["id"])
        self._wait_for_code(page_id, "账号信息")
        self._wait_for_code(page_id, "应用信息")
        return page_id, True

    def write_registration_sections(
        self,
        title: str,
        sections: dict[str, str],
    ) -> dict[str, bool]:
        """Replace copied labeled sections as one rollback-protected operation."""
        page = self._registration_page(title)
        page_id = str(page["id"])
        before: dict[str, str] = {}
        for heading in sections:
            block = self._wait_for_code(page_id, heading)
            before[heading] = self._code_text(block)

        changed: dict[str, bool] = {}
        try:
            for heading, text in sections.items():
                changed[heading] = self._write_section_by_id(
                    page_id,
                    heading,
                    text,
                    replace_existing=True,
                )
            for heading, text in sections.items():
                block = self._code_after_heading(page_id, heading)
                if block is None or self._code_text(block) != text:
                    raise RuntimeError(
                        f"Notion independent readback failed for section {heading!r}"
                    )
            return changed
        except Exception as original_error:
            rollback_errors: list[str] = []
            for heading, text in reversed(list(before.items())):
                try:
                    self._write_section_by_id(
                        page_id,
                        heading,
                        text,
                        replace_existing=True,
                    )
                except Exception as rollback_error:
                    rollback_errors.append(f"{heading}: {rollback_error}")
            if rollback_errors:
                raise RuntimeError(
                    "Notion registration rollback failed: " + "; ".join(rollback_errors)
                ) from original_error
            raise

    def read_section(self, title: str, heading: str) -> str:
        page = self._registration_page(title)
        block = self._code_after_heading(page["id"], heading)
        assert block is not None
        return self._code_text(block)

    def write_section(
        self, title: str, heading: str, text: str, *, replace_existing: bool = False
    ) -> bool:
        page = self._registration_page(title)
        return self._write_section_by_id(page["id"], heading, text, replace_existing=replace_existing)

    def read_toggle_code(self, title: str, heading: str, toggle: str) -> str:
        page = self._registration_page(title)
        return self._code_text(self._code_in_toggle(page["id"], heading, toggle))

    def write_toggle_code(
        self,
        title: str,
        heading: str,
        toggle: str,
        text: str,
        *,
        replace_existing: bool = False,
    ) -> bool:
        page = self._registration_page(title)
        block = self._code_in_toggle(page["id"], heading, toggle)
        current = self._code_text(block)
        if current == text:
            return False
        if current.strip() and not replace_existing:
            raise RuntimeError(f"Refusing to replace non-empty Notion toggle {toggle!r}")
        self._request(
            "PATCH",
            f"/blocks/{block['id']}",
            {"code": {"rich_text": self._rich_text(text)}},
        )
        saved = self._code_in_toggle(page["id"], heading, toggle)
        if self._code_text(saved) != text:
            raise RuntimeError(f"Notion save verification failed for toggle {toggle!r}")
        return True

    @staticmethod
    def _split_field(line: str, label: str) -> tuple[str, str]:
        prefix = label
        value = line[len(label):]
        if not label.rstrip().endswith((":", "：")):
            for separator in (": ", "： ", ":", "："):
                if value.startswith(separator):
                    prefix += separator
                    value = value[len(separator):]
                    break
        return prefix, value

    def read_field(self, title: str, heading: str, label: str) -> str:
        lines = self.read_section(title, heading).splitlines()
        matches = [line for line in lines if line.startswith(label)]
        if len(matches) != 1:
            raise RuntimeError(f"Expected exactly one field {label!r}; found {len(matches)}")
        _prefix, value = self._split_field(matches[0], label)
        return value

    def set_field(
        self,
        title: str,
        heading: str,
        label: str,
        value: str,
        *,
        replace_existing: bool = False,
    ) -> bool:
        if "\n" in value or "\r" in value:
            raise RuntimeError("Notion field values must be one line")
        text = self.read_section(title, heading)
        lines = text.splitlines()
        indexes = [index for index, line in enumerate(lines) if line.startswith(label)]
        if len(indexes) != 1:
            raise RuntimeError(f"Expected exactly one field {label!r}; found {len(indexes)}")
        index = indexes[0]
        prefix, current = self._split_field(lines[index], label)
        if current == value:
            return False
        if current and not replace_existing:
            raise RuntimeError(f"Refusing to replace non-empty Notion field {label!r}")
        lines[index] = prefix + value
        return self.write_section(title, heading, "\n".join(lines), replace_existing=True)


def load_dotenv(
    path: Path,
    *,
    override_keys: frozenset[str] = frozenset(),
) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in override_keys:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def api_from_env() -> NotionAPI:
    load_dotenv(PROJECT_ROOT / ".env")
    return NotionAPI(
        os.environ.get("NOTION_TOKEN", ""),
        os.environ.get("NOTION_ROOT_PAGE_ID", ""),
        os.environ.get("NOTION_TEMPLATE_TITLE", "模板"),
    )


def write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def copy_verified(text: str) -> None:
    subprocess.run(["pbcopy"], input=text, text=True, check=True)
    if subprocess.check_output(["pbpaste"], text=True) != text:
        raise RuntimeError("Clipboard verification failed")


def safe_result(action: str, text: str = "", **extra: Any) -> None:
    result = {
        "action": action,
        "bytes": len(text.encode()),
        "lines": len(text.splitlines()),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        **extra,
    }
    print(json.dumps(result, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-registration")
    create.add_argument("--title", required=True)
    create.add_argument("--account-file", type=Path, required=True)

    verify_parent = commands.add_parser("verify-parent")
    verify_parent.add_argument("--title", required=True)

    read_section = commands.add_parser("read-section")
    read_section.add_argument("--title", required=True)
    read_section.add_argument("--heading", required=True)
    read_section.add_argument("--out", type=Path, required=True)

    write_section = commands.add_parser("write-section")
    write_section.add_argument("--title", required=True)
    write_section.add_argument("--heading", required=True)
    write_source = write_section.add_mutually_exclusive_group(required=True)
    write_source.add_argument("--file", type=Path)
    write_source.add_argument("--stdin", action="store_true")
    write_section.add_argument("--replace-existing", action="store_true")

    read_toggle_code = commands.add_parser("read-toggle-code")
    read_toggle_code.add_argument("--title", required=True)
    read_toggle_code.add_argument("--heading", required=True)
    read_toggle_code.add_argument("--toggle", required=True)
    read_toggle_code.add_argument("--out", type=Path, required=True)

    write_toggle_code = commands.add_parser("write-toggle-code")
    write_toggle_code.add_argument("--title", required=True)
    write_toggle_code.add_argument("--heading", required=True)
    write_toggle_code.add_argument("--toggle", required=True)
    toggle_source = write_toggle_code.add_mutually_exclusive_group(required=True)
    toggle_source.add_argument("--file", type=Path)
    toggle_source.add_argument("--stdin", action="store_true")
    write_toggle_code.add_argument("--replace-existing", action="store_true")

    read_field = commands.add_parser("read-field")
    read_field.add_argument("--title", required=True)
    read_field.add_argument("--heading", required=True)
    read_field.add_argument("--label", required=True)
    read_target = read_field.add_mutually_exclusive_group(required=True)
    read_target.add_argument("--out", type=Path)
    read_target.add_argument("--copy", action="store_true")

    set_field = commands.add_parser("set-field")
    set_field.add_argument("--title", required=True)
    set_field.add_argument("--heading", required=True)
    set_field.add_argument("--label", required=True)
    value_source = set_field.add_mutually_exclusive_group(required=True)
    value_source.add_argument("--value-file", type=Path)
    value_source.add_argument("--value-stdin", action="store_true")
    set_field.add_argument("--replace-existing", action="store_true")

    args = parser.parse_args()
    api = api_from_env()

    if args.command == "verify-parent":
        title = api.verify_parent(args.title)
        safe_result(args.command, title, title=title)
    elif args.command == "create-registration":
        text = args.account_file.read_text()
        page_id = api.create_registration(args.title, text)
        safe_result(args.command, text, page_id=page_id)
    elif args.command == "read-section":
        text = api.read_section(args.title, args.heading)
        write_private(args.out, text)
        safe_result(args.command, text, out=str(args.out))
    elif args.command == "write-section":
        text = sys.stdin.read() if args.stdin else args.file.read_text()
        changed = api.write_section(
            args.title, args.heading, text, replace_existing=args.replace_existing
        )
        safe_result(args.command, text, changed=changed)
    elif args.command == "read-toggle-code":
        text = api.read_toggle_code(args.title, args.heading, args.toggle)
        write_private(args.out, text)
        safe_result(args.command, text, out=str(args.out))
    elif args.command == "write-toggle-code":
        text = sys.stdin.read() if args.stdin else args.file.read_text()
        changed = api.write_toggle_code(
            args.title,
            args.heading,
            args.toggle,
            text,
            replace_existing=args.replace_existing,
        )
        safe_result(args.command, text, changed=changed)
    elif args.command == "read-field":
        text = api.read_field(args.title, args.heading, args.label)
        if args.copy:
            copy_verified(text)
            safe_result(args.command, text, copied=True)
        else:
            write_private(args.out, text)
            safe_result(args.command, text, out=str(args.out))
    elif args.command == "set-field":
        value = (sys.stdin.read() if args.value_stdin else args.value_file.read_text()).rstrip("\r\n")
        changed = api.set_field(
            args.title,
            args.heading,
            args.label,
            value,
            replace_existing=args.replace_existing,
        )
        safe_result(args.command, value, changed=changed)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
