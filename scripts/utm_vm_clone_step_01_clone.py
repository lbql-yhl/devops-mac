#!/usr/bin/env python3
"""Step 01: allocate and clone one stopped UTM VM with clean output."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.clean_cli import full_reason
from scripts.utm_clone_macos import CloneError, clone_once
from scripts.vm_inventory import InventoryError


DATABASE = (ROOT / "runtime" / "vm-inventory.sqlite3").resolve()


def main() -> int:
    print("开始执行：utm-vm-clone-step-01", flush=True)
    try:
        result = clone_once(DATABASE)
        vm_name = str(result["vm_name"])
    except (CloneError, InventoryError, OSError) as error:
        print(
            f"执行报错：utm-vm-clone-step-01；{full_reason(error)}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(
        "步骤1已操作\n"
        "执行成功：utm-vm-clone-step-01；"
        f"STEP_01_CLONE=verified;VM_NAME={vm_name}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
