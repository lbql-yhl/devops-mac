#!/usr/bin/env python3
"""Step 10: --vm-name shutdown, final identity gates, inventory availability."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.utm_vm_clone_steps import main_for_step, run_finalize


if __name__ == "__main__":
    raise SystemExit(main_for_step(10, run_finalize))
