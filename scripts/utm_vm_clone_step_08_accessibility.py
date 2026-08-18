#!/usr/bin/env python3
"""Step 08: --vm-name enable AEServer, sshd-keygen-wrapper, Terminal Accessibility."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.utm_vm_clone_accessibility import run as enable_accessibility
from scripts.utm_vm_clone_steps import _prepare_step, main_for_step, record_accessibility


def run_accessibility(vm_name: str) -> None:
    _prepare_step(vm_name, 8)
    enable_accessibility(vm_name)
    record_accessibility(vm_name)


if __name__ == "__main__":
    raise SystemExit(main_for_step(8, run_accessibility))
