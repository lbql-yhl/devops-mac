"""Portable path resolution for the submission workflow."""

from __future__ import annotations

import os
from pathlib import Path

try:
    from services.host_config import optional_setting
except ModuleNotFoundError:  # Support direct ``python services/feishu_bot.py`` use.
    from host_config import optional_setting


SOURCE_ROOT = Path(__file__).resolve().parents[1]


def load_project_env(path: Path) -> None:
    """Load simple KEY=VALUE settings without replacing process overrides."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def configured_path(name: str, default: Path) -> Path:
    raw = optional_setting(name, env_path=SOURCE_ROOT / ".env")
    if not raw:
        return default.expanduser().resolve()
    return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()


PROJECT_ROOT = configured_path("SUBMISSION_PROJECT_ROOT", SOURCE_ROOT)
# Keep the blank-value fallback inside the copied project. A host with its UTM
# assets elsewhere must set the two variables explicitly; no source-host volume
# name is ever inherited by accident.
VM_IMAGES_DIR = configured_path(
    "SUBMISSION_VM_IMAGES_DIR", PROJECT_ROOT / "host-assets" / "vm-images"
)
VM_TEMPLATE = configured_path("SUBMISSION_VM_TEMPLATE", VM_IMAGES_DIR / "macOS.utm")
SHARED_DIR = configured_path("SUBMISSION_SHARED_DIR", Path.home() / "Desktop" / "共享文件")
PROJECT_SKILLS_DIR = configured_path("PROJECT_SKILLS_DIR", PROJECT_ROOT / "skills")
