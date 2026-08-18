from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from services import project_paths


def test_import_does_not_copy_dotenv_values_into_process_environment(
    tmp_path: Path,
) -> None:
    package = tmp_path / "services"
    package.mkdir()
    (package / "__init__.py").touch()
    for name in ("host_config.py", "project_paths.py"):
        shutil.copyfile(project_paths.SOURCE_ROOT / "services" / name, package / name)
    setting = "SUBMISSION_TEST_IMPORT_POLLUTION"
    (tmp_path / ".env").write_text(f"{setting}=fixture-value\n", encoding="utf-8")
    child_env = dict(os.environ)
    child_env.pop(setting, None)
    child_env["PYTHONPATH"] = str(tmp_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "import services.project_paths; "
                f"raise SystemExit(1 if {setting!r} in os.environ else 0)"
            ),
        ],
        cwd=tmp_path,
        env=child_env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_configured_path_reads_updated_dotenv_without_mutating_environ(
    tmp_path: Path, monkeypatch
) -> None:
    setting = "SUBMISSION_TEST_CONFIGURED_PATH"
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.delenv(setting, raising=False)
    monkeypatch.setattr(project_paths, "SOURCE_ROOT", tmp_path)
    before = dict(os.environ)

    (tmp_path / ".env").write_text(f"{setting}={first}\n", encoding="utf-8")
    assert project_paths.configured_path(setting, tmp_path / "default") == first
    assert dict(os.environ) == before

    (tmp_path / ".env").write_text(f"{setting}={second}\n", encoding="utf-8")
    assert project_paths.configured_path(setting, tmp_path / "default") == second
    assert dict(os.environ) == before


def test_configured_path_process_environment_wins_over_dotenv(
    tmp_path: Path, monkeypatch
) -> None:
    setting = "SUBMISSION_TEST_CONFIGURED_PATH"
    process_path = tmp_path / "process"
    dotenv_path = tmp_path / "dotenv"
    monkeypatch.setattr(project_paths, "SOURCE_ROOT", tmp_path)
    monkeypatch.setenv(setting, str(process_path))
    (tmp_path / ".env").write_text(f"{setting}={dotenv_path}\n", encoding="utf-8")

    assert project_paths.configured_path(setting, tmp_path / "default") == process_path


def test_configured_path_blank_process_value_uses_default(
    tmp_path: Path, monkeypatch
) -> None:
    setting = "SUBMISSION_TEST_CONFIGURED_PATH"
    default = tmp_path / "default"
    monkeypatch.setattr(project_paths, "SOURCE_ROOT", tmp_path)
    monkeypatch.setenv(setting, "   ")
    (tmp_path / ".env").write_text(
        f"{setting}={tmp_path / 'dotenv'}\n", encoding="utf-8"
    )

    assert project_paths.configured_path(setting, default) == default


def test_public_path_constants_remain_paths() -> None:
    assert all(
        isinstance(value, Path)
        for value in (
            project_paths.PROJECT_ROOT,
            project_paths.VM_IMAGES_DIR,
            project_paths.VM_TEMPLATE,
            project_paths.SHARED_DIR,
            project_paths.PROJECT_SKILLS_DIR,
        )
    )
