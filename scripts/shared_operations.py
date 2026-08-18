#!/usr/bin/env python3
"""Small executable guards for operations shared by project skills."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from urllib.parse import urlsplit


SCHEME_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def browser_clipboard_value(raw: str, *, allow_bare: bool = False) -> str:
    """Return the exact browser value after removing one leading HTTP scheme."""
    if not raw or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in raw
    ):
        raise ValueError("browser URL must be one non-empty line without whitespace or controls")
    lowered = raw.lower()
    if lowered.startswith("https://"):
        value = raw[len("https://") :]
    elif lowered.startswith("http://"):
        value = raw[len("http://") :]
    elif allow_bare and "://" not in raw:
        value = raw
    else:
        raise ValueError("browser URL must use one leading http(s):// or approved bare mode")
    if not value or SCHEME_PREFIX.match(value):
        raise ValueError("browser URL contains an empty or nested scheme")
    parsed = urlsplit(f"https://{value}")
    if not parsed.hostname:
        raise ValueError("browser URL has no host after //")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a URL red line and place only the post-// value on the host clipboard."
    )
    parser.add_argument("operation", choices=("browser-url",))
    parser.add_argument(
        "--allow-bare",
        action="store_true",
        help="Accept an already-bare project constant; values containing :// remain invalid.",
    )
    args = parser.parse_args()
    try:
        value = browser_clipboard_value(sys.stdin.read(), allow_bare=args.allow_bare)
        subprocess.run(["pbcopy"], input=value.encode("utf-8"), check=True)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"BROWSER_URL_CLIPBOARD=refused reason={type(exc).__name__}", file=sys.stderr)
        return 2
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    print(f"BROWSER_URL_CLIPBOARD=verified bytes={len(value.encode('utf-8'))} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
