"""LangGraph controller for the production Codex/Skills submission workflow."""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

try:
    from feishu_bot import ROOT, RUNTIME_DIR, SUBMISSION_SKILL_ORDER, find_run, mutate_run
except ModuleNotFoundError:
    from services.feishu_bot import ROOT, RUNTIME_DIR, SUBMISSION_SKILL_ORDER, find_run, mutate_run

try:
    from host_config import secure_write_text
except ModuleNotFoundError:
    from services.host_config import secure_write_text


FIXED_STAGES = (
    "vm-preparation",
    "environment-preparation",
    "assets-and-signing",
    "code-and-submission",
)
PLAN_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "你是提审流程的计划校验器。只能原样复述给定阶段顺序，不能新增、删除或重排阶段；"
            "不得建议任何账号、签名、浏览器或外部提交操作。",
        ),
        (
            "human",
            "应用名：{app_name}\n固定阶段 ID：{stages}\n固定 Skill 顺序：{skills}\n"
            "返回结构化计划：阶段和 Skill 列表必须分别与输入完全一致，不得给出其他阶段或操作。",
        ),
    ]
)


class WorkflowPlan(BaseModel):
    stage_ids: list[str] = Field(description="必须与给定的固定提审阶段顺序完全一致")
    skill_ids: list[str] = Field(description="必须与给定的 16 个 Skill 顺序完全一致")


class SubmissionState(TypedDict, total=False):
    run_id: str
    prompt_path: str
    run: dict[str, Any]
    status: str
    error_code: str


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record(run_id: str, status: str, note: str, **fields: str) -> None:
    def update(run: dict[str, Any]) -> None:
        run["status"] = status
        run.update({key: value for key, value in fields.items() if value})
        run.setdefault("events", []).append({"at": _now(), "status": status, "note": note})

    mutate_run(run_id, update)


def _validate_plan(state: SubmissionState) -> dict[str, Any]:
    run = state["run"]
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {"status": "blocked", "error_code": "OPENAI_API_KEY_MISSING"}
    try:
        model = ChatOpenAI(
            api_key=api_key,
            model=os.getenv("OPENAI_MODEL", "gpt-5.6"),
            use_responses_api=True,
            temperature=None,
        ).with_structured_output(WorkflowPlan)
        plan = (PLAN_PROMPT | model).invoke(
            {
                "app_name": str(run.get("app_name", ""))[:120],
                "stages": json.dumps(FIXED_STAGES, ensure_ascii=False),
                "skills": json.dumps(SUBMISSION_SKILL_ORDER, ensure_ascii=False),
            }
        )
    except Exception:
        return {"status": "blocked", "error_code": "LANGCHAIN_PLAN_API_FAILED"}

    if tuple(plan.stage_ids) != FIXED_STAGES or tuple(plan.skill_ids) != tuple(SUBMISSION_SKILL_ORDER):
        return {"status": "blocked", "error_code": "LANGCHAIN_PLAN_ORDER_MISMATCH"}
    _record(
        state["run_id"],
        "langgraph_plan_validated",
        "LangChain plan matched the immutable submission order",
        workflow_plan=json.dumps(
            {"stages": FIXED_STAGES, "skills": SUBMISSION_SKILL_ORDER},
            ensure_ascii=False,
        ),
    )
    return {"status": "ready", "error_code": ""}


def _execute_codex_workflow(state: SubmissionState) -> dict[str, Any]:
    run_id = state["run_id"]
    prompt_path = state["prompt_path"]
    output_path = RUNTIME_DIR / f"{run_id}.codex-output.txt"
    secure_write_text(output_path, "")
    command = [
        os.getenv("FEISHU_CODEX_COMMAND", "codex"),
        "exec",
        "--cd",
        str(ROOT),
        "--sandbox",
        "danger-full-access",
        "-m",
        os.getenv("FEISHU_CODEX_MODEL", "gpt-5.6-sol"),
        "-o",
        str(output_path),
        Path(prompt_path).read_text(encoding="utf-8"),
    ]
    timeout = int(os.getenv("SUBMISSION_RUNNER_TIMEOUT_SECONDS", "21600"))
    _record(
        run_id,
        "langgraph_execution_started",
        "LangGraph validated the fixed plan; Codex is executing the existing 16 Skills.",
        codex_output_path=str(output_path),
    )
    try:
        result = subprocess.run(
            command,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "blocked", "error_code": "CODEX_WORKFLOW_TIMEOUT"}
    except OSError as exc:
        return {"status": "blocked", "error_code": f"CODEX_START_{type(exc).__name__.upper()}"}

    if result.returncode != 0:
        return {"status": "blocked", "error_code": f"CODEX_EXIT_{result.returncode}"}
    return {"status": "completed", "error_code": ""}


def _route_after_plan(state: SubmissionState) -> str:
    return "execute" if state.get("status") == "ready" else "blocked"


def _finish(state: SubmissionState) -> dict[str, Any]:
    if state.get("status") == "completed":
        _record(state["run_id"], "runner_completed", "LangGraph submission workflow completed.")
        return {"status": "completed"}
    code = state.get("error_code", "WORKFLOW_BLOCKED")
    _record(state["run_id"], "needs_human", f"LangGraph stopped safely; error_code={code}")
    return {"status": "needs_human"}


def build_submission_graph():
    graph = StateGraph(SubmissionState)
    graph.add_node("validate_plan", _validate_plan)
    graph.add_node("execute_codex_workflow", _execute_codex_workflow)
    graph.add_node("finish", _finish)
    graph.add_edge(START, "validate_plan")
    graph.add_conditional_edges(
        "validate_plan", _route_after_plan,
        {"execute": "execute_codex_workflow", "blocked": "finish"},
    )
    graph.add_edge("execute_codex_workflow", "finish")
    graph.add_edge("finish", END)
    return graph.compile()


def run_submission_workflow(run_id: str, prompt_path: str) -> SubmissionState:
    if not re.fullmatch(r"submission-[0-9]{8}-[0-9]{6}-[a-f0-9]{6}", run_id):
        raise ValueError("SUBMISSION_RUN_ID_INVALID")
    path = Path(prompt_path).resolve()
    try:
        path.relative_to((RUNTIME_DIR / "prompts").resolve())
    except ValueError as exc:
        raise ValueError("SUBMISSION_PROMPT_PATH_INVALID") from exc
    run = find_run(run_id)
    if not run or not path.is_file():
        raise ValueError("SUBMISSION_RUN_CONTEXT_MISSING")
    return build_submission_graph().invoke(
        {"run_id": run_id, "prompt_path": str(path), "run": run, "status": "pending"}
    )
