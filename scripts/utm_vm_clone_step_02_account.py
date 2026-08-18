#!/usr/bin/env python3
"""Step 02: --vm-name account creation, secure token, demo removal, shutdown."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.utm_vm_clone_steps import main_for_step, run_account
if __name__ == "__main__":
    raise SystemExit(main_for_step(2, run_account))
