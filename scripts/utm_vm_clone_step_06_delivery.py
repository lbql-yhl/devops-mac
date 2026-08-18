#!/usr/bin/env python3
"""Step 06: --vm-name stage and verify shared guest files."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.utm_vm_clone_steps import main_for_step, run_delivery
if __name__ == "__main__":
    raise SystemExit(main_for_step(6, run_delivery))
