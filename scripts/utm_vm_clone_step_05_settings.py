#!/usr/bin/env python3
"""Step 05: --vm-name persistent macOS settings and Clash app-data."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.utm_vm_clone_steps import main_for_step, run_settings
if __name__ == "__main__":
    raise SystemExit(main_for_step(5, run_settings))
