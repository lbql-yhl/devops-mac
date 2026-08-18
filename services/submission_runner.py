#!/usr/bin/env python3
"""Run one Feishu-triggered submission workflow through Codex CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "runtime"
RUNS_FILE = RUNTIME_DIR / "feishu-runs.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_run_event(run_id: str, status: str, note: str, **fields: str) -> None:
    data = read_json(RUNS_FILE, {"runs": []})
    for run in data.get("runs", []):
        if run.get("id") != run_id:
            continue
        run["status"] = status
        run["updated_at"] = utc_now()
        run.update({key: value for key, value in fields.items() if value})
        run.setdefault("events", []).append({"at": utc_now(), "status": status, "note": note})
        write_json(RUNS_FILE, data)
        return


def main() -> int:
    run_id = os.getenv("SUBMISSION_RUN_ID", "")
    prompt_path = Path(os.getenv("SUBMISSION_PROMPT_PATH", ""))
    if not run_id or not prompt_path.exists():
        print("SUBMISSION_RUN_ID or SUBMISSION_PROMPT_PATH is missing", file=sys.stderr)
        return 2

    codex_command = os.getenv("FEISHU_CODEX_COMMAND", "codex")
    codex_model = os.getenv("FEISHU_CODEX_MODEL", "gpt-5.6-sol")
    timeout_seconds = int(os.getenv("SUBMISSION_RUNNER_TIMEOUT_SECONDS", "21600"))
    output_path = RUNTIME_DIR / f"{run_id}.codex-output.txt"

    append_run_event(
        run_id,
        "running",
        "Codex CLI submission runner started",
        codex_output_path=str(output_path),
    )

    prompt = prompt_path.read_text(encoding="utf-8")
    cmd = [
        codex_command,
        "exec",
        "--cd",
        str(ROOT),
        "--sandbox",
        "danger-full-access",
        "-m",
        codex_model,
        "-o",
        str(output_path),
        prompt,
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        append_run_event(run_id, "runner_timeout", "Codex CLI submission runner timed out")
        print("Codex CLI submission runner timed out", file=sys.stderr)
        return 124

    if result.stdout:
        print(result.stdout)
    if result.returncode == 0:
        append_run_event(run_id, "runner_completed", "Codex CLI submission runner completed")
        return 0

    append_run_event(
        run_id,
        "runner_failed",
        f"Codex CLI submission runner exited with code {result.returncode}",
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
