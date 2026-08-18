#!/usr/bin/env python3
"""Explicitly bind one named inventory VM to ``test`` and run stdin proxy test."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import utm_clash_ip  # noqa: E402
from scripts.vm_inventory import (  # noqa: E402
    InventoryError,
    claim_exact_available_vm,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "runtime" / "vm-inventory.sqlite3",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        claimed = claim_exact_available_vm(
            args.database,
            args.vm_name,
            application_name="test",
        )
        if claimed != args.vm_name:
            raise InventoryError("exact test VM claim returned another name")
        print("EXPLICIT_TEST_VM_BINDING=verified")
        result = utm_clash_ip.main(
            [
                "--vm-name",
                args.vm_name,
                "--application-name",
                "test",
                "--proxy-stdin",
                "--database",
                str(args.database.resolve()),
            ]
        )
        if result == 0:
            print("UTM_CLASH_IP_EXPLICIT_TEST=verified")
        return result
    except (InventoryError, OSError, ValueError) as error:
        print(
            f"UTM_CLASH_IP_EXPLICIT_TEST=failed:{type(error).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
