#!/usr/bin/env python3
"""Tests for the post-acceptance Apple Account password writeback."""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURE_ACCEPTED_VALUE = "K7mQ9vT2pL6xR4nZy"

from scripts.change_password_writeback import restore_previous_password, write_accepted_password


class FakeNotion:
    def __init__(self) -> None:
        self.section = "用户名：Test User\n修改后的密码：\n邮箱：test@example.test"

    def read_section(self, title: str, heading: str) -> str:
        return self.section

    def set_field(
        self,
        title: str,
        heading: str,
        label: str,
        value: str,
        *,
        replace_existing: bool = False,
    ) -> bool:
        lines = self.section.splitlines()
        matches = [i for i, line in enumerate(lines) if line.startswith(label)]
        if len(matches) != 1:
            raise RuntimeError("target label is not unique")
        index = matches[0]
        lines[index] = label + value
        self.section = "\n".join(lines)
        return True

    def read_field(self, title: str, heading: str, label: str) -> str:
        matches = [
            line[len(label):]
            for line in self.section.splitlines()
            if line.startswith(label)
        ]
        if len(matches) != 1:
            raise RuntimeError("target label is not unique")
        return matches[0]


class ChangePasswordWritebackTests(unittest.TestCase):
    def test_writes_and_independently_verifies_accepted_password(self) -> None:
        api = FakeNotion()
        fixture_value = FIXTURE_ACCEPTED_VALUE

        result = write_accepted_password(api, "test1", fixture_value)

        self.assertEqual(result["bytes"], len(fixture_value.encode()))
        self.assertEqual(result["sha256"], hashlib.sha256(fixture_value.encode()).hexdigest())
        self.assertEqual(api.read_field("test1", "账号信息", "修改后的密码："), fixture_value)
        self.assertIn("用户名：Test User", api.section)
        self.assertIn("邮箱：test@example.test", api.section)

    def test_rejects_empty_or_multiline_password(self) -> None:
        api = FakeNotion()
        with self.assertRaisesRegex(ValueError, "non-empty"):
            write_accepted_password(api, "test1", "")
        with self.assertRaisesRegex(ValueError, "one line"):
            write_accepted_password(api, "test1", "one\ntwo")

    def test_restores_blank_preflight_value(self) -> None:
        api = FakeNotion()
        write_accepted_password(api, "test1", FIXTURE_ACCEPTED_VALUE)
        result = restore_previous_password(api, "test1", "")
        self.assertEqual(result["bytes"], 0)
        self.assertEqual(api.read_field("test1", "账号信息", "修改后的密码："), "")


if __name__ == "__main__":
    unittest.main()
