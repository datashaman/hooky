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


def generator_contract_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "contract_path", "feature_list_path", "summary"],
        "properties": {
            "status": {"type": "string", "enum": ["done", "blocked"]},
            "contract_path": {"type": "string", "enum": [".workflow/loop/contract.md"]},
            "feature_list_path": {"type": "string", "enum": [".workflow/loop/feature_list.json"]},
            "summary": {"type": "string"},
        },
    }


def evaluator_contract_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "accepted", "review", "required_changes"],
        "properties": {
            "status": {"type": "string", "enum": ["done", "blocked"]},
            "accepted": {"type": "boolean"},
            "review": {"type": "string"},
            "required_changes": {"type": "array", "items": {"type": "string"}},
        },
    }


def generator_implementation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "summary", "changed_files", "tests_run", "failures"],
        "properties": {
            "status": {"type": "string", "enum": ["done", "blocked"]},
            "summary": {"type": "string"},
            "changed_files": {"type": "array", "items": {"type": "string"}},
            "tests_run": {"type": "array", "items": {"type": "string"}},
            "failures": {"type": "array", "items": {"type": "string"}},
        },
    }


def evaluator_attempt_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "recommendation", "bottleneck", "findings", "score"],
        "properties": {
            "status": {"type": "string", "enum": ["pass", "fail"]},
            "recommendation": {"type": "string", "enum": ["continue", "restart-attempt", "restart-contract", "stop"]},
            "bottleneck": {"type": "string"},
            "findings": {"type": "array", "items": {"type": "string"}},
            "score": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }


def validate_planner_report(report: dict[str, Any], working_folder: Path) -> None:
    if report.get("contract_path") != ".workflow/loop/contract.md":
        raise ValueError("planner must report contract_path=.workflow/loop/contract.md")
    contract = working_folder / ".workflow/loop/contract.md"
    if not contract.exists() or not contract.read_text(encoding="utf-8").strip():
        raise ValueError("planner must write non-empty .workflow/loop/contract.md")


def validate_generator_contract_report(report: dict[str, Any], working_folder: Path) -> None:
    if report.get("contract_path") != ".workflow/loop/contract.md":
        raise ValueError("generator must report contract_path=.workflow/loop/contract.md")
    if report.get("feature_list_path") != ".workflow/loop/feature_list.json":
        raise ValueError("generator must report feature_list_path=.workflow/loop/feature_list.json")
    contract = working_folder / ".workflow/loop/contract.md"
    feature_list = working_folder / ".workflow/loop/feature_list.json"
    if not contract.exists() or "## Done Criteria" not in contract.read_text(encoding="utf-8"):
        raise ValueError("generator must write done criteria into .workflow/loop/contract.md")
    if not feature_list.exists():
        raise ValueError("generator must write .workflow/loop/feature_list.json")
    payload = json.loads(feature_list.read_text(encoding="utf-8"))
    if not isinstance(payload.get("features"), list):
        raise ValueError("feature_list.json must include features array")


def validate_evaluator_contract_report(report: dict[str, Any], _working_folder: Path) -> None:
    if not isinstance(report.get("accepted"), bool):
        raise ValueError("evaluator contract report must include accepted boolean")
    if not isinstance(report.get("review"), str) or not report["review"].strip():
        raise ValueError("evaluator contract report must include review text")
    changes = report.get("required_changes")
    if not isinstance(changes, list) or any(not isinstance(item, str) for item in changes):
        raise ValueError("evaluator contract report must include required_changes strings")
    if not report["accepted"] and not [item for item in changes if item.strip()]:
        raise ValueError("rejected contract must include required_changes")


def validate_generator_implementation_report(report: dict[str, Any], working_folder: Path) -> None:
    for key in ("changed_files", "tests_run", "failures"):
        value = report.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"generator implementation report must include {key} strings")
    for raw_path in report.get("changed_files", []):
        path = (working_folder / raw_path).resolve()
        try:
            relative = path.relative_to(working_folder.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(f"changed file is outside workspace: {raw_path}") from exc
        if relative.startswith(".workflow/"):
            raise ValueError(f"generator must not report .workflow changes: {raw_path}")


def validate_evaluator_attempt_report(report: dict[str, Any], _working_folder: Path) -> None:
    if report.get("recommendation") not in {"continue", "restart-attempt", "restart-contract", "stop"}:
        raise ValueError("evaluator recommendation is invalid")
    score = report.get("score")
    if not isinstance(score, int | float) or score < 0 or score > 1:
        raise ValueError("evaluator score must be between 0 and 1")
    findings = report.get("findings")
    if not isinstance(findings, list) or any(not isinstance(item, str) for item in findings):
        raise ValueError("evaluator findings must be strings")
    if report.get("status") == "fail" and not [item for item in findings if item.strip()]:
        raise ValueError("failed evaluator report must include findings")


def generate_planner_artifacts(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
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
            user=planner_user_prompt(proposal, model_metadata),
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


def generate_generator_contract_artifacts(
    *,
    working_folder: Path,
    attempt_id: str | None = None,
    review_feedback: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required; loop generator has no non-AI path")
    working_folder = working_folder.resolve()
    model = selected_model()
    model_metadata = selected_model_metadata()
    live_root = working_folder / ".workflow/loop"
    if attempt_id:
        live_root = working_folder / ".workflow/loop/attempts" / attempt_id / "traces"
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=generator_contract_schema(),
        max_cost_usd=float(os.environ.get("LOOP_GENERATOR_MAX_COST_USD", "0.20")),
        max_seconds=int(os.environ.get("LOOP_GENERATOR_MAX_SECONDS", "240")),
        context_window_tokens=model_metadata.get("context_length"),
        final_validator=lambda report: validate_generator_contract_report(report, working_folder),
        skills=agent_skills.discover_skills(working_folder),
        write_enabled=True,
        write_allowed_prefixes=[".workflow/loop/contract.md", ".workflow/loop/feature_list.json"],
        write_blocked_prefixes=[],
        read_allowed_prefixes=[".workflow/loop", ".workflow/tool-results"],
        read_blocked_prefixes=[".workflow"],
        live_log_root=live_root,
        live_event_log_paths=[working_folder / ".workflow/loop/log.runtime"],
        live_event_prefix="role=generator ",
    )
    try:
        result = run_tool_agent(
            model=model,
            system=generator_contract_system_prompt(),
            user=generator_contract_user_prompt(
                (working_folder / ".workflow/loop/contract.md").read_text(encoding="utf-8"),
                model_metadata,
                review_feedback=review_feedback,
            ),
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
            metadata=build_runtime_metadata("loop-generator-contract", model, model_metadata, result, status="error", error=str(exc)),
        )
        raise
    write_runtime_log(
        live_root,
        result.transcript,
        result.tool_events,
        result.compaction_events,
        result.pre_compaction_archives,
        metadata=build_runtime_metadata("loop-generator-contract", model, model_metadata, result),
    )
    if result.final_report is None:
        raise RuntimeError("Loop generator finished without final_report")
    return result.final_report, result.usage


def generate_evaluator_contract_artifacts(*, working_folder: Path, attempt_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required; loop evaluator has no non-AI path")
    working_folder = working_folder.resolve()
    model = selected_model()
    model_metadata = selected_model_metadata()
    live_root = working_folder / ".workflow/loop"
    if attempt_id:
        live_root = working_folder / ".workflow/loop/attempts" / attempt_id / "traces"
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=evaluator_contract_schema(),
        max_cost_usd=float(os.environ.get("LOOP_EVALUATOR_MAX_COST_USD", "0.20")),
        max_seconds=int(os.environ.get("LOOP_EVALUATOR_MAX_SECONDS", "240")),
        context_window_tokens=model_metadata.get("context_length"),
        final_validator=lambda report: validate_evaluator_contract_report(report, working_folder),
        skills=agent_skills.discover_skills(working_folder),
        write_enabled=False,
        enabled_tools=["final_report"],
        read_allowed_prefixes=[".workflow/loop", ".workflow/tool-results"],
        read_blocked_prefixes=[".workflow"],
        live_log_root=live_root,
        live_event_log_paths=[working_folder / ".workflow/loop/log.runtime"],
        live_event_prefix="role=evaluator ",
    )
    try:
        result = run_tool_agent(
            model=model,
            system=evaluator_contract_system_prompt(),
            user=evaluator_contract_user_prompt(
                (working_folder / ".workflow/loop/contract.md").read_text(encoding="utf-8"),
                (working_folder / ".workflow/loop/feature_list.json").read_text(encoding="utf-8"),
                model_metadata,
            ),
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
            metadata=build_runtime_metadata("loop-evaluator-contract", model, model_metadata, result, status="error", error=str(exc)),
        )
        raise
    write_runtime_log(
        live_root,
        result.transcript,
        result.tool_events,
        result.compaction_events,
        result.pre_compaction_archives,
        metadata=build_runtime_metadata("loop-evaluator-contract", model, model_metadata, result),
    )
    if result.final_report is None:
        raise RuntimeError("Loop evaluator finished without final_report")
    return result.final_report, result.usage


def generate_generator_implementation_artifacts(*, working_folder: Path, attempt_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required; loop generator has no non-AI path")
    working_folder = working_folder.resolve()
    model = selected_model()
    model_metadata = selected_model_metadata()
    live_root = working_folder / ".workflow/loop/attempts" / attempt_id / "traces"
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=generator_implementation_schema(),
        max_cost_usd=float(os.environ.get("LOOP_GENERATOR_MAX_COST_USD", "0.50")),
        max_seconds=int(os.environ.get("LOOP_GENERATOR_MAX_SECONDS", "600")),
        context_window_tokens=model_metadata.get("context_length"),
        final_validator=lambda report: validate_generator_implementation_report(report, working_folder),
        skills=agent_skills.discover_skills(working_folder),
        write_enabled=True,
        write_blocked_prefixes=[".workflow"],
        read_allowed_prefixes=[".workflow/loop", ".workflow/tool-results"],
        read_blocked_prefixes=[".workflow"],
        live_log_root=live_root,
        live_event_log_paths=[working_folder / ".workflow/loop/log.runtime"],
        live_event_prefix="role=generator ",
    )
    try:
        result = run_tool_agent(
            model=model,
            system=generator_implementation_system_prompt(),
            user=generator_implementation_user_prompt(
                (working_folder / ".workflow/loop/contract.md").read_text(encoding="utf-8"),
                (working_folder / ".workflow/loop/feature_list.json").read_text(encoding="utf-8"),
                attempt_id,
                model_metadata,
            ),
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
            metadata=build_runtime_metadata("loop-generator-implementation", model, model_metadata, result, status="error", error=str(exc)),
        )
        raise
    write_runtime_log(
        live_root,
        result.transcript,
        result.tool_events,
        result.compaction_events,
        result.pre_compaction_archives,
        metadata=build_runtime_metadata("loop-generator-implementation", model, model_metadata, result),
    )
    if result.final_report is None:
        raise RuntimeError("Loop generator finished without final_report")
    return result.final_report, result.usage


def generate_evaluator_attempt_artifacts(*, working_folder: Path, attempt_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required; loop evaluator has no non-AI path")
    working_folder = working_folder.resolve()
    model = selected_model()
    model_metadata = selected_model_metadata()
    live_root = working_folder / ".workflow/loop/attempts" / attempt_id / "traces"
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=evaluator_attempt_schema(),
        max_cost_usd=float(os.environ.get("LOOP_EVALUATOR_MAX_COST_USD", "0.50")),
        max_seconds=int(os.environ.get("LOOP_EVALUATOR_MAX_SECONDS", "600")),
        context_window_tokens=model_metadata.get("context_length"),
        final_validator=lambda report: validate_evaluator_attempt_report(report, working_folder),
        skills=agent_skills.discover_skills(working_folder),
        write_enabled=False,
        read_allowed_prefixes=[".workflow/loop", ".workflow/tool-results"],
        read_blocked_prefixes=[".workflow"],
        live_log_root=live_root,
        live_event_log_paths=[working_folder / ".workflow/loop/log.runtime"],
        live_event_prefix="role=evaluator ",
    )
    try:
        result = run_tool_agent(
            model=model,
            system=evaluator_attempt_system_prompt(),
            user=evaluator_attempt_user_prompt(
                (working_folder / ".workflow/loop/contract.md").read_text(encoding="utf-8"),
                (working_folder / ".workflow/loop/feature_list.json").read_text(encoding="utf-8"),
                attempt_id,
                model_metadata,
            ),
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
            metadata=build_runtime_metadata("loop-evaluator-attempt", model, model_metadata, result, status="error", error=str(exc)),
        )
        raise
    write_runtime_log(
        live_root,
        result.transcript,
        result.tool_events,
        result.compaction_events,
        result.pre_compaction_archives,
        metadata=build_runtime_metadata("loop-evaluator-attempt", model, model_metadata, result),
    )
    if result.final_report is None:
        raise RuntimeError("Loop evaluator finished without final_report")
    return result.final_report, result.usage


def planner_system_prompt() -> str:
    return """You are the planner in a three-role Karpathy-style loop.

You have one responsibility: turn vague user input into the problem proposal in .workflow/loop/contract.md.

You must never edit production code, tests, attempt artifacts, or evaluator reports.
You must not write the final grading contract. The generator proposes done criteria later and the evaluator reviews them.

Use write_file only for .workflow/loop/contract.md. Preserve the loop vocabulary: planner, generator, evaluator, loop-runner, attempt.
Finish only with final_report.
"""


def planner_user_prompt(proposal: str, model_metadata: dict[str, Any]) -> str:
    return f"""Problem proposal input:

{proposal.strip() or "(no proposal provided)"}

Selected model:
```json
{json.dumps(model_metadata, indent=2, sort_keys=True)}
```

Write .workflow/loop/contract.md with:
- title
- problem proposal
- non-goals or unknowns if any
- placeholder Done Criteria section stating that the generator must propose criteria
- placeholder Taste Rubric section stating that it is optional and must be explicit when subjective quality matters

Then call final_report with status, contract_path, and summary.
"""


def generator_contract_system_prompt() -> str:
    return """You are the generator in a three-role Karpathy-style loop.

This is contract negotiation only. You must not edit production code, tests, package files, or attempt artifacts.

Your job is to propose concrete, testable done criteria in .workflow/loop/contract.md and project them into .workflow/loop/feature_list.json.
The evaluator will accept or reject the contract. You cannot approve your own criteria.

Use write_file only for .workflow/loop/contract.md and .workflow/loop/feature_list.json.
Finish only with final_report.
"""


def generator_contract_user_prompt(contract: str, model_metadata: dict[str, Any], *, review_feedback: str = "") -> str:
    feedback_section = ""
    if review_feedback.strip():
        feedback_section = f"""
Evaluator rejected the previous contract. Required revision feedback:

```markdown
{review_feedback.strip()}
```

Address every required change before calling final_report.
"""
    return f"""Current contract.md:

```markdown
{contract}
```

Selected model:
```json
{json.dumps(model_metadata, indent=2, sort_keys=True)}
```
{feedback_section}

Revise .workflow/loop/contract.md so the Done Criteria section contains a checklist of concrete, testable assertions.

Write .workflow/loop/feature_list.json with this shape:
```json
{{
  "schema_version": 1,
  "features": [
    {{
      "id": "F001",
      "text": "testable assertion",
      "status": "pending"
    }}
  ]
}}
```

Then call final_report with status, contract_path, feature_list_path, and summary.
"""


def evaluator_contract_system_prompt() -> str:
    return """You are the evaluator in a three-role Karpathy-style loop.

This is contract negotiation only. You must not edit files.
The proposed contract and feature list are provided inline by the user message.

Your job is to reject weak, vague, untestable, self-serving, or under-specified done criteria before implementation starts.
Assume the contract is broken until the checklist is concrete enough for an independent evaluator to grade.
You cannot write code, tests, contract changes, or inspect the workspace. You can only call final_report.
"""


def evaluator_contract_user_prompt(contract: str, feature_list: str, model_metadata: dict[str, Any]) -> str:
    return f"""Review this proposed contract and feature list.

contract.md:
```markdown
{contract}
```

feature_list.json:
```json
{feature_list}
```

Selected model:
```json
{json.dumps(model_metadata, indent=2, sort_keys=True)}
```

Accept only if the Done Criteria are concrete, testable, within the planner proposal, and sufficient for a small working product.
If rejecting, list required_changes as specific edits the generator should make to the contract.
Finish only by calling final_report. Do not describe final_report in markdown; call the tool with JSON arguments matching the schema.
"""


def generator_implementation_system_prompt() -> str:
    return """You are the generator in a three-role Karpathy-style loop.

You have one responsibility now: implement the accepted contract.
You must not grade your own work and must not edit .workflow. The evaluator will grade independently.

Use files for code, tests, and project artifacts. Keep changes scoped to the contract.
Use todo_write for substantive work and run relevant commands before final_report.
"""


def generator_implementation_user_prompt(contract: str, feature_list: str, attempt_id: str, model_metadata: dict[str, Any]) -> str:
    return f"""Attempt: {attempt_id}

Accepted contract.md:
```markdown
{contract}
```

feature_list.json:
```json
{feature_list}
```

Selected model:
```json
{json.dumps(model_metadata, indent=2, sort_keys=True)}
```

Implement the contract in this workspace. Do not edit .workflow. Do not declare the attempt passed.
Finish only with final_report describing changed_files, tests_run, and any failures.
"""


def evaluator_attempt_system_prompt() -> str:
    return """You are the evaluator in a three-role Karpathy-style loop.

Assume the implementation is broken. Your job is to prove whether it satisfies the accepted contract.
You may read files and run commands, including browser/UI verification when relevant. You must not edit files.

Return a recommendation:
- continue when the attempt passes, or when failures are normal implementation defects that another generator pass can fix
- restart-attempt when the implementation has gone sideways but the accepted contract is still right
- restart-contract when the contract itself is wrong, incomplete, contradictory, or untestable
- stop only when automation is genuinely blocked, such as missing credentials, unavailable required services, corrupted workspace state, repeated invalid tool calls that prevent evidence gathering, or a hard external dependency failure

Do not recommend stop merely because one or more acceptance tests fail. A failing test is evidence for continue or restart-attempt unless the failure proves the contract is impossible or the evaluator cannot gather evidence.
"""


def evaluator_attempt_user_prompt(contract: str, feature_list: str, attempt_id: str, model_metadata: dict[str, Any]) -> str:
    return f"""Attempt: {attempt_id}

Accepted contract.md:
```markdown
{contract}
```

feature_list.json:
```json
{feature_list}
```

Selected model:
```json
{json.dumps(model_metadata, indent=2, sort_keys=True)}
```

Evaluate the workspace against the contract. Inspect diffs, run relevant commands, and use visual/browser checks when the product has a UI.
If tests fail, report the failing criteria and choose continue or restart-attempt unless there is a true automation blocker.
Finish only with final_report containing status, recommendation, bottleneck, findings, and score.
"""
