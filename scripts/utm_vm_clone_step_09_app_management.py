#!/usr/bin/env python3
"""Step 09: --vm-name enable and verify Terminal App Management."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.utm_vm_clone_steps import main_for_step, run_app_management


if __name__ == "__main__":
    raise SystemExit(main_for_step(9, run_app_management))
