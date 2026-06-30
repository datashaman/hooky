#!/usr/bin/env python3
"""Karpathy-style loop model role runners."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import agent_runtime
import eval_runtime
import agent_skills
from agent_runtime import AgentRunError, ToolRuntime, build_runtime_metadata, run_tool_agent, write_runtime_log


SELECTED_MODEL_PATH = Path(".workflow/agents/spec/selected_model.json")
COMMON_STATIC_CONTEXT_ROOT = Path(".workflow/agents/common/static")


def selected_model() -> str:
    env_model = os.environ.get("OPENROUTER_MODEL")
    if env_model:
        return env_model
    if SELECTED_MODEL_PATH.exists():
        data = eval_runtime.read_selected_model(SELECTED_MODEL_PATH)
        model = data.get("model")
        if isinstance(model, str) and model:
            return model
    return "openai/gpt-4.1-mini"


def selected_model_metadata() -> dict[str, Any]:
    env_model = os.environ.get("OPENROUTER_MODEL")
    if env_model:
        return {"model": env_model, "source": "OPENROUTER_MODEL"}
    if SELECTED_MODEL_PATH.exists():
        return eval_runtime.read_selected_model(SELECTED_MODEL_PATH)
    return {"model": "openai/gpt-4.1-mini", "source": "fallback"}


def common_static_files() -> dict[str, str]:
    if not COMMON_STATIC_CONTEXT_ROOT.exists():
        return {}
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(COMMON_STATIC_CONTEXT_ROOT.glob("*.md"))
    }


def planner_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "contract_path", "summary"],
        "properties": {
            "status": {"type": "string", "enum": ["done", "blocked"]},
            "contract_path": {"type": "string", "enum": [".workflow/loop/contract.md"]},
            "summary": {"type": "string"},
        },
    }


def validate_planner_report(report: dict[str, Any], working_folder: Path) -> None:
    if report.get("contract_path") != ".workflow/loop/contract.md":
        raise ValueError("planner must report contract_path=.workflow/loop/contract.md")
    contract = working_folder / ".workflow/loop/contract.md"
    if not contract.exists() or not contract.read_text(encoding="utf-8").strip():
        raise ValueError("planner must write non-empty .workflow/loop/contract.md")


def generate_planner_artifacts(*, working_folder: Path, boundary: str, attempt_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required; loop planner has no non-AI path")
    working_folder = working_folder.resolve()
    model = selected_model()
    model_metadata = selected_model_metadata()
    live_root = working_folder / ".workflow/loop"
    if attempt_id:
        live_root = working_folder / ".workflow/loop/attempts" / attempt_id / "traces"
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=planner_schema(),
        max_cost_usd=float(os.environ.get("LOOP_PLANNER_MAX_COST_USD", "0.10")),
        max_seconds=int(os.environ.get("LOOP_PLANNER_MAX_SECONDS", "180")),
        context_window_tokens=model_metadata.get("context_length"),
        final_validator=lambda report: validate_planner_report(report, working_folder),
        skills=agent_skills.discover_skills(working_folder),
        write_enabled=True,
        write_allowed_prefixes=[".workflow/loop"],
        write_blocked_prefixes=[],
        read_allowed_prefixes=[".workflow/loop", ".workflow/tool-results"],
        read_blocked_prefixes=[".workflow"],
        live_log_root=live_root,
        live_event_log_paths=[working_folder / ".workflow/loop/log.runtime"],
        live_event_prefix="role=planner ",
    )
    try:
        result = run_tool_agent(
            model=model,
            system=planner_system_prompt(),
            user=planner_user_prompt(boundary, model_metadata),
            runtime=runtime,
        )
    except AgentRunError as exc:
        result = exc.result
        write_runtime_log(
            live_root,
            result.transcript,
            result.tool_events,
            result.compaction_events,
            result.pre_compaction_archives,
            metadata=build_runtime_metadata("loop-planner", model, model_metadata, result, status="error", error=str(exc)),
        )
        raise
    write_runtime_log(
        live_root,
        result.transcript,
        result.tool_events,
        result.compaction_events,
        result.pre_compaction_archives,
        metadata=build_runtime_metadata("loop-planner", model, model_metadata, result),
    )
    if result.final_report is None:
        raise RuntimeError("Loop planner finished without final_report")
    return result.final_report, result.usage


def planner_system_prompt() -> str:
    return """You are the planner in a three-role Karpathy-style loop.

You have one responsibility: turn vague user input into the problem boundary in .workflow/loop/contract.md.

You must never edit production code, tests, attempt artifacts, or evaluator reports.
You must not write the final grading contract. The generator proposes done criteria later and the evaluator reviews them.

Use write_file only for .workflow/loop/contract.md. Preserve the loop vocabulary: planner, generator, evaluator, loop-runner, attempt.
Finish only with final_report.
"""


def planner_user_prompt(boundary: str, model_metadata: dict[str, Any]) -> str:
    return f"""Problem boundary input:

{boundary.strip() or "(no boundary provided)"}

Selected model:
```json
{json.dumps(model_metadata, indent=2, sort_keys=True)}
```

Write .workflow/loop/contract.md with:
- title
- problem boundary
- non-goals or unknowns if any
- placeholder Done Criteria section stating that the generator must propose criteria
- placeholder Taste Rubric section stating that it is optional and must be explicit when subjective quality matters

Then call final_report with status, contract_path, and summary.
"""
