#!/usr/bin/env python3
"""Step 03: --vm-name sharing and MAC configuration while stopped."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.utm_vm_clone_steps import main_for_step, run_hardware
if __name__ == "__main__":
    raise SystemExit(main_for_step(3, run_hardware))
