#!/usr/bin/env python3
"""Read-only portability and run-readiness checks."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.host_config import host_settings_snapshot  # noqa: E402
from services.project_paths import (  # noqa: E402
    PROJECT_ROOT,
    PROJECT_SKILLS_DIR,
    SHARED_DIR,
    VM_IMAGES_DIR,
    VM_TEMPLATE,
)


ORDERED = (
    "utm-vm-clone",
    "utm-notion",
    "utm-clash-ip",
    "utm-login",
    "utm-edit",
    "utm-key",
    "utm-apps",
    "utm-business",
    "utm-env",
    "utm-script",
    "utm-image",
    "utm-p8",
    "utm-21",
    "utm-22",
    "utm-23",
    "utm-24",
)
STANDALONE: tuple[str, ...] = ()


def executable_exists(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    executable = Path(os.path.expanduser(parts[0]))
    if executable.parent != Path("."):
        if not executable.is_absolute():
            executable = ROOT / executable
        return executable.is_file() and os.access(executable, os.X_OK)
    return shutil.which(parts[0]) is not None


def runner_command_valid(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts or not executable_exists(parts[0]):
        return False
    for item in parts[1:]:
        if item.startswith("-"):
            continue
        candidate = Path(os.path.expanduser(item))
        if candidate.suffix in {".py", ".sh", ".zsh"}:
            if not candidate.is_absolute():
                candidate = ROOT / candidate
            return candidate.is_file()
    return True


def check(project_only: bool) -> dict[str, object]:
    settings: Mapping[str, str] = os.environ
    if not project_only:
        settings = host_settings_snapshot(
            environ=os.environ,
            env_path=ROOT / ".env",
        )
    discovered = {
        path.parent.name for path in PROJECT_SKILLS_DIR.glob("*/SKILL.md")
    }
    codex_command = str(settings.get("FEISHU_CODEX_COMMAND", "codex")).strip()
    runner_command = str(
        settings.get(
            "SUBMISSION_RUNNER_COMMAND", "python3 services/submission_runner.py"
        )
    ).strip()
    checks: dict[str, object] = {
        "project_root": PROJECT_ROOT == ROOT and (ROOT / "README.md").is_file(),
        "skills_count": sum(
            (PROJECT_SKILLS_DIR / name / "SKILL.md").is_file()
            for name in ORDERED
        ),
        "standalone_skills_count": sum(
            (PROJECT_SKILLS_DIR / name / "SKILL.md").is_file() for name in STANDALONE
        ),
        "skill_set_exact": discovered == set(ORDERED) | set(STANDALONE),
        "skill_names_unique": (
            len(ORDERED) == len(set(ORDERED)) == 16
            and len(STANDALONE) == len(set(STANDALONE)) == 0
            and set(ORDERED).isdisjoint(STANDALONE)
        ),
        "shared_contract": (
            PROJECT_SKILLS_DIR / "_shared" / "AUTOMATION_CONTRACT.md"
        ).is_file(),
        "python3": shutil.which("python3") is not None,
        "codex": executable_exists(codex_command),
        "runner_enabled": runner_command_valid(runner_command),
    }
    if not project_only:
        checks.update(
            {
                "vm_images_dir": VM_IMAGES_DIR.is_dir(),
                "vm_template": VM_TEMPLATE.is_dir(),
                "shared_dir": SHARED_DIR.is_dir(),
                "submission_host_machine": bool(
                    str(settings.get("SUBMISSION_HOST_MACHINE", "")).strip()
                ),
                "feishu_app_id": bool(
                    str(settings.get("FEISHU_APP_ID", "")).strip()
                ),
                "feishu_app_secret": bool(
                    str(settings.get("FEISHU_APP_SECRET", "")).strip()
                ),
                "notion_token": bool(
                    str(settings.get("NOTION_TOKEN", "")).strip()
                ),
                "notion_root_page_id": bool(
                    str(settings.get("NOTION_ROOT_PAGE_ID", "")).strip()
                ),
                "codeup_username": bool(
                    str(settings.get("CODEUP_USERNAME", "")).strip()
                ),
                "codeup_password": bool(
                    str(settings.get("CODEUP_PASSWORD", "")).strip()
                ),
                "ssh": shutil.which("ssh") is not None,
                "git": shutil.which("git") is not None,
                "node": shutil.which("node") is not None,
                "npm": shutil.which("npm") is not None,
                "pbcopy": shutil.which("pbcopy") is not None,
                "pbpaste": shutil.which("pbpaste") is not None,
            }
        )
    checks["ok"] = all(
        (
            value == 16
            if key == "skills_count"
            else value == 0
            if key == "standalone_skills_count"
            else bool(value)
        )
        for key, value in checks.items()
        if key != "ok"
    )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--emit-shell", action="store_true")
    args = parser.parse_args()
    result = check(args.project_only)
    if args.emit_shell:
        if not result["ok"]:
            return 1
        values = {
            "PROJECT_ROOT": PROJECT_ROOT,
            "PROJECT_SKILLS_DIR": PROJECT_SKILLS_DIR,
            "SUBMISSION_VM_IMAGES_DIR": VM_IMAGES_DIR,
            "SUBMISSION_VM_TEMPLATE": VM_TEMPLATE,
            "SUBMISSION_SHARED_DIR": SHARED_DIR,
        }
        for key, value in values.items():
            print(f"export {key}={shlex.quote(str(value))}")
    elif args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        for key, value in result.items():
            print(f"{key.upper()}={value}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
