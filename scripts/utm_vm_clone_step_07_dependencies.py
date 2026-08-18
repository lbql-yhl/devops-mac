#!/usr/bin/env python3
"""Step 07: --vm-name read-only verification of all 26 dependencies."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.utm_vm_clone_steps import main_for_step, run_dependencies


if __name__ == "__main__":
    raise SystemExit(main_for_step(7, run_dependencies))
