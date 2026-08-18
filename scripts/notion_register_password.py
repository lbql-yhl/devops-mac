#!/usr/bin/env python3
"""Register a generated Apple Account password in the matching Notion page.

The password is accepted only through stdin and is never placed in argv or
printed. This utility performs the Notion API write/readback; the GUI password
changer remains unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys

from scripts.change_password_writeback import write_accepted_password
from scripts.notion_api import api_from_env


PASSWORD_LENGTH = 17
PASSWORD_SUFFIX = "y"


def validate_candidate(password: str) -> str:
    if not isinstance(password, str) or len(password) != PASSWORD_LENGTH:
        raise ValueError("password must be 17 characters")
    if not password.endswith(PASSWORD_SUFFIX):
        raise ValueError("password must end with y")
    if any(char.isspace() for char in password) or "\n" in password or "\r" in password:
        raise ValueError("password must be one line without whitespace")
    if not any(char.isupper() for char in password):
        raise ValueError("password must contain an uppercase letter")
    if not any(char.islower() for char in password):
        raise ValueError("password must contain a lowercase letter")
    if not any(char.isdigit() for char in password):
        raise ValueError("password must contain a number")
    return password


def register_password(api: object, parent_title: str, page_title: str, password: str) -> dict[str, object]:
    """Verify the host page and write/read back the candidate value."""
    candidate = validate_candidate(password)
    api.verify_parent(parent_title)  # type: ignore[attr-defined]
    return write_accepted_password(api, page_title, candidate)


def main() -> int:
    parser = argparse.ArgumentParser(description="Register generated password in Notion")
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--parent-title", default=os.environ.get("SUBMISSION_HOST_MACHINE", ""))
    args = parser.parse_args()
    if not args.parent_title.strip():
        print("SUBMISSION_HOST_MACHINE is required", file=sys.stderr)
        return 2
    password = sys.stdin.read()
    try:
        metadata = register_password(
            api_from_env(), args.parent_title.strip(), args.vm_name.strip(), password.strip()
        )
    except Exception as exc:  # noqa: BLE001 - CLI reports a non-secret reason.
        print(f"NOTION_PASSWORD_REGISTER=blocked: {exc}", file=sys.stderr)
        return 1
    print(f"PASSWORD_WRITE_BYTES={metadata['bytes']}")
    print(f"PASSWORD_WRITE_SHA256={metadata['sha256']}")
    print("NOTION_PASSWORD_REGISTER=verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
