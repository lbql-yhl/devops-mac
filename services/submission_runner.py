#!/usr/bin/env python3
"""Run the LangGraph controller around the existing Codex/Skills executor."""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from submission_graph import run_submission_workflow
    from feishu_bot import mutate_run, utc_now
except ModuleNotFoundError:
    from services.submission_graph import run_submission_workflow
    from services.feishu_bot import mutate_run, utc_now


def main() -> int:
    run_id = os.getenv("SUBMISSION_RUN_ID", "")
    prompt_path = Path(os.getenv("SUBMISSION_PROMPT_PATH", ""))
    if not run_id or not prompt_path.exists():
        print("SUBMISSION_RUN_CONTEXT_MISSING", file=sys.stderr)
        return 2

    try:
        state = run_submission_workflow(run_id, prompt_path)
    except Exception as exc:  # Never print API keys, prompt text, or exception messages.
        def mark_failed(run):
            run["status"] = "needs_human"
            run.setdefault("events", []).append({
                "at": utc_now(),
                "status": "langgraph_runner_failed",
                "note": f"error_code={type(exc).__name__.upper()}",
            })

        mutate_run(run_id, mark_failed)
        print(f"LANGGRAPH_RUN_FAILED={type(exc).__name__}", file=sys.stderr)
        return 1

    status = state.get("status", "blocked")
    if status == "completed":
        print("LANGGRAPH_RUN_STATUS=completed")
        return 0
    print(f"LANGGRAPH_RUN_STATUS={status}")
    print(f"LANGGRAPH_ERROR_CODE={state.get('error_code', 'WORKFLOW_BLOCKED')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
