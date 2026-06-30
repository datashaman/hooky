#!/usr/bin/env python3
"""Karpathy-style loop model role runners."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import agent_runtime
import agent_skills
from agent_runtime import AgentRunError, ToolRuntime, build_runtime_metadata, run_tool_agent, write_runtime_log


SELECTED_MODEL_PATH = Path(".hooky/models/generator.json")
EVALUATOR_SELECTED_MODEL_PATH = Path(".hooky/models/evaluator.json")
LOOP_PROPOSAL_PATH = Path(".hooky/proposal.md")


def read_selected_model(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError(f"selected model file must contain a non-empty model string: {path}")
    return payload


def selected_model() -> str:
    env_model = os.environ.get("OPENROUTER_MODEL")
    if env_model:
        return env_model
    if SELECTED_MODEL_PATH.exists():
        data = read_selected_model(SELECTED_MODEL_PATH)
        model = data.get("model")
        if isinstance(model, str) and model:
            return model
    return "openai/gpt-4.1-mini"


def selected_model_metadata() -> dict[str, Any]:
    env_model = os.environ.get("OPENROUTER_MODEL")
    if env_model:
        return {"model": env_model, "source": "OPENROUTER_MODEL"}
    if SELECTED_MODEL_PATH.exists():
        return read_selected_model(SELECTED_MODEL_PATH)
    return {"model": "openai/gpt-4.1-mini", "source": "fallback"}


def selected_evaluator_attempt_model() -> str:
    env_model = os.environ.get("LOOP_EVALUATOR_MODEL")
    if env_model:
        return env_model
    if EVALUATOR_SELECTED_MODEL_PATH.exists():
        data = read_selected_model(EVALUATOR_SELECTED_MODEL_PATH)
        model = data.get("model")
        if isinstance(model, str) and model:
            return model
    return "openai/gpt-4.1-mini"


def selected_evaluator_attempt_model_metadata() -> dict[str, Any]:
    env_model = os.environ.get("LOOP_EVALUATOR_MODEL")
    if env_model:
        return {"model": env_model, "variant_id": env_model, "source": "LOOP_EVALUATOR_MODEL", "reasoning_request": None}
    if EVALUATOR_SELECTED_MODEL_PATH.exists():
        return read_selected_model(EVALUATOR_SELECTED_MODEL_PATH)
    return {
        "model": "openai/gpt-4.1-mini",
        "variant_id": "openai/gpt-4.1-mini",
        "source": "fallback-multimodal-required",
        "reasoning_request": None,
    }


def read_loop_proposal(working_folder: Path) -> str:
    path = working_folder / LOOP_PROPOSAL_PATH
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def proposal_checklist_items(proposal: str) -> list[str]:
    items: list[str] = []
    for raw_line in proposal.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("- [ ] ", "- [x] ", "- [X] ")):
            items.append(line[6:].strip())
        elif line.startswith(("- ", "* ")):
            items.append(line[2:].strip())
        else:
            numbered = re.match(r"^\d+\.\s+(.+)$", line)
            if numbered:
                items.append(numbered.group(1).strip())
    return [item for item in items if item]


def planner_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "contract_path", "summary"],
        "properties": {
            "status": {"type": "string", "enum": ["done", "blocked"]},
            "contract_path": {"type": "string", "enum": [".hooky/contract.md"]},
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
            "contract_path": {"type": "string", "enum": [".hooky/contract.md"]},
            "feature_list_path": {"type": "string", "enum": [".hooky/feature_list.json"]},
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
            "rubric_scores": {
                "type": "object",
                "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "score_explanation": {"type": "string"},
        },
    }


def validate_planner_report(report: dict[str, Any], working_folder: Path) -> None:
    if report.get("contract_path") != ".hooky/contract.md":
        raise ValueError("planner must report contract_path=.hooky/contract.md")
    contract = working_folder / ".hooky/contract.md"
    if not contract.exists() or not contract.read_text(encoding="utf-8").strip():
        raise ValueError("planner must write non-empty .hooky/contract.md")


def validate_generator_contract_report(report: dict[str, Any], working_folder: Path) -> None:
    if report.get("contract_path") != ".hooky/contract.md":
        raise ValueError("generator must report contract_path=.hooky/contract.md")
    if report.get("feature_list_path") != ".hooky/feature_list.json":
        raise ValueError("generator must report feature_list_path=.hooky/feature_list.json")
    contract = working_folder / ".hooky/contract.md"
    feature_list = working_folder / ".hooky/feature_list.json"
    contract_text = contract.read_text(encoding="utf-8") if contract.exists() else ""
    if not contract.exists() or "## Done Criteria" not in contract_text:
        raise ValueError("generator must write done criteria into .hooky/contract.md")
    if reference_visual_rubric_required(read_loop_proposal(working_folder)) and not taste_rubric_is_substantive(contract_text):
        raise ValueError("generator must define a substantive Taste Rubric for reference visual requirements")
    if not feature_list.exists():
        raise ValueError("generator must write .hooky/feature_list.json")
    payload = json.loads(feature_list.read_text(encoding="utf-8"))
    if not isinstance(payload.get("features"), list):
        raise ValueError("feature_list.json must include features array")
    proposal_items = proposal_checklist_items(read_loop_proposal(working_folder))
    if proposal_items and len(payload["features"]) < len(proposal_items):
        raise ValueError(
            f"feature_list.json must cover every proposal checklist item: "
            f"{len(payload['features'])} features for {len(proposal_items)} proposal items"
        )


def validate_generator_contract_write(working_folder: Path, path: Path, content: str) -> None:
    relative = path.resolve().relative_to(working_folder.resolve()).as_posix()
    if relative == ".hooky/contract.md":
        if "## Done Criteria" not in content or len(content.strip()) < 100:
            raise ValueError("contract.md writes must preserve a substantive ## Done Criteria section")
        if reference_visual_rubric_required(read_loop_proposal(working_folder)) and not taste_rubric_is_substantive(content):
            raise ValueError("contract.md must include a substantive Taste Rubric for reference visual requirements")
        return
    if relative == ".hooky/feature_list.json":
        payload = json.loads(content)
        if not isinstance(payload.get("features"), list):
            raise ValueError("feature_list.json writes must include a features array")
        return
    raise ValueError(f"unexpected generator contract write: {relative}")


def validate_evaluator_contract_report(report: dict[str, Any], working_folder: Path) -> None:
    if not isinstance(report.get("accepted"), bool):
        raise ValueError("evaluator contract report must include accepted boolean")
    if not isinstance(report.get("review"), str) or not report["review"].strip():
        raise ValueError("evaluator contract report must include review text")
    changes = report.get("required_changes")
    if not isinstance(changes, list) or any(not isinstance(item, str) for item in changes):
        raise ValueError("evaluator contract report must include required_changes strings")
    if not report["accepted"] and not [item for item in changes if item.strip()]:
        raise ValueError("rejected contract must include required_changes")
    if report["accepted"]:
        contract = working_folder / ".hooky/contract.md"
        contract_text = contract.read_text(encoding="utf-8") if contract.exists() else ""
        if reference_visual_rubric_required(read_loop_proposal(working_folder)) and not taste_rubric_is_substantive(contract_text):
            raise ValueError("accepted contract must include a substantive Taste Rubric for reference visual requirements")


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
        if relative.startswith(".hooky/"):
            raise ValueError(f"generator must not report .hooky changes: {raw_path}")


def validate_evaluator_attempt_report(report: dict[str, Any], working_folder: Path) -> None:
    if report.get("recommendation") not in {"continue", "restart-attempt", "restart-contract", "stop"}:
        raise ValueError("evaluator recommendation is invalid")
    if not isinstance(report.get("bottleneck"), str) or not report["bottleneck"].strip():
        raise ValueError("evaluator report must include a non-empty bottleneck")
    score = report.get("score")
    if not isinstance(score, int | float) or score < 0 or score > 1:
        raise ValueError("evaluator score must be between 0 and 1")
    findings = report.get("findings")
    if not isinstance(findings, list) or any(not isinstance(item, str) for item in findings):
        raise ValueError("evaluator findings must be strings")
    if report.get("status") == "fail" and not [item for item in findings if item.strip()]:
        raise ValueError("failed evaluator report must include findings")
    contract = working_folder / ".hooky/contract.md"
    contract_text = contract.read_text(encoding="utf-8") if contract.exists() else ""
    if taste_rubric_is_substantive(contract_text):
        rubric_scores = report.get("rubric_scores")
        if not isinstance(rubric_scores, dict) or not rubric_scores:
            raise ValueError("evaluator report must include rubric_scores when contract.md defines a Taste Rubric")
        required_axes = {"design", "originality", "craft", "functionality"}
        missing_axes = sorted(required_axes.difference(str(key) for key in rubric_scores))
        if missing_axes:
            raise ValueError("rubric_scores missing required axes: " + ", ".join(missing_axes))
        if any(not isinstance(value, int | float) or value < 0 or value > 1 for value in rubric_scores.values()):
            raise ValueError("rubric_scores values must be between 0 and 1")
        if not isinstance(report.get("score_explanation"), str) or not report["score_explanation"].strip():
            raise ValueError("evaluator report must include score_explanation when grading a Taste Rubric")


def markdown_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start: int | None = None
    marker = f"## {heading}"
    for index, line in enumerate(lines):
        if line.strip().lower() == marker.lower():
            start = index + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def markdown_without_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    marker = f"## {heading}"
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip().lower() == marker.lower():
            start = index
            break
    if start is None:
        return text
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[:start] + lines[end:])


def taste_rubric_is_substantive(contract: str) -> bool:
    rubric = markdown_section(contract, "Taste Rubric").lower()
    if not rubric:
        return False
    placeholders = ("optional", "required only", "subjective quality matters", "_")
    stripped = re.sub(r"[\s_*`.-]+", " ", rubric).strip()
    if not stripped:
        return False
    if all(term in rubric for term in ("optional", "subjective")) and len(stripped) < 120:
        return False
    if any(axis in rubric for axis in ("design", "originality", "craft", "functionality", "weight", "reference")):
        return True
    return not any(term in rubric for term in placeholders)


def taste_rubric_required(contract: str) -> bool:
    without_rubric = markdown_without_section(contract, "Taste Rubric")
    taste_terms = (
        "taste",
        "aesthetic",
        "aesthetics",
        "beautiful",
        "polished",
        "delightful",
        "premium",
        "original",
        "originality",
        "brand",
        "branded",
        "visual design",
        "craft",
    )
    return any(term in without_rubric.lower() for term in taste_terms)


def reference_visual_rubric_required(text: str) -> bool:
    lower = text.lower()
    reference_terms = (
        "reference implementation",
        "reference site",
        "reference visual",
        "template",
        "canonical",
        "look and behave exactly",
        "visually match",
    )
    visual_terms = ("visual", "layout", "style", "css", "html", "ui", "screenshot")
    if any(term in lower for term in reference_terms) and any(term in lower for term in visual_terms):
        return True
    todomvc_terms = ("todomvc", "todomvc-app-css", "todomvc-common", "todoapp", "app-spec.md")
    return any(term in lower for term in todomvc_terms) and any(term in lower for term in ("canonical", "template", "official", "css", "visual"))


def generate_planner_artifacts(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required; loop planner has no non-AI path")
    working_folder = working_folder.resolve()
    model = selected_model()
    model_metadata = selected_model_metadata()
    live_root = working_folder / ".hooky"
    if attempt_id:
        live_root = working_folder / ".hooky/attempts" / attempt_id / "traces"
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=planner_schema(),
        max_cost_usd=float(os.environ.get("LOOP_PLANNER_MAX_COST_USD", "0.10")),
        max_seconds=int(os.environ.get("LOOP_PLANNER_MAX_SECONDS", "180")),
        context_window_tokens=model_metadata.get("context_length"),
        final_validator=lambda report: validate_planner_report(report, working_folder),
        skills=agent_skills.discover_skills(working_folder),
        write_enabled=True,
        write_allowed_prefixes=[".hooky"],
        write_blocked_prefixes=[],
        read_allowed_prefixes=[".hooky", ".hooky/tool-results"],
        read_blocked_prefixes=[".hooky"],
        live_log_root=live_root,
        live_event_log_paths=[working_folder / ".hooky/log.runtime"],
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
    live_root = working_folder / ".hooky"
    if attempt_id:
        live_root = working_folder / ".hooky/attempts" / attempt_id / "traces"
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=generator_contract_schema(),
        max_cost_usd=float(os.environ.get("LOOP_GENERATOR_MAX_COST_USD", "0.20")),
        max_seconds=int(os.environ.get("LOOP_GENERATOR_MAX_SECONDS", "240")),
        context_window_tokens=model_metadata.get("context_length"),
        final_validator=lambda report: validate_generator_contract_report(report, working_folder),
        write_validator=lambda path, content: validate_generator_contract_write(working_folder, path, content),
        skills=agent_skills.discover_skills(working_folder),
        write_enabled=True,
        write_allowed_prefixes=[".hooky/contract.md", ".hooky/feature_list.json"],
        write_blocked_prefixes=[],
        read_allowed_prefixes=[".hooky", ".hooky/tool-results"],
        read_blocked_prefixes=[".hooky"],
        live_log_root=live_root,
        live_event_log_paths=[working_folder / ".hooky/log.runtime"],
        live_event_prefix="role=generator ",
    )
    try:
        result = run_tool_agent(
            model=model,
            system=generator_contract_system_prompt(),
            user=generator_contract_user_prompt(
                (working_folder / ".hooky/contract.md").read_text(encoding="utf-8"),
                read_loop_proposal(working_folder),
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
    live_root = working_folder / ".hooky"
    if attempt_id:
        live_root = working_folder / ".hooky/attempts" / attempt_id / "traces"
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
        read_allowed_prefixes=[".hooky", ".hooky/tool-results"],
        read_blocked_prefixes=[".hooky"],
        live_log_root=live_root,
        live_event_log_paths=[working_folder / ".hooky/log.runtime"],
        live_event_prefix="role=evaluator ",
    )
    try:
        result = run_tool_agent(
            model=model,
            system=evaluator_contract_system_prompt(),
            user=evaluator_contract_user_prompt(
                (working_folder / ".hooky/contract.md").read_text(encoding="utf-8"),
                (working_folder / ".hooky/feature_list.json").read_text(encoding="utf-8"),
                read_loop_proposal(working_folder),
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


def generate_generator_implementation_artifacts(
    *,
    working_folder: Path,
    attempt_id: str,
    evaluator_feedback: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required; loop generator has no non-AI path")
    working_folder = working_folder.resolve()
    model = selected_model()
    model_metadata = selected_model_metadata()
    live_root = working_folder / ".hooky/attempts" / attempt_id / "traces"
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=generator_implementation_schema(),
        max_cost_usd=float(os.environ.get("LOOP_GENERATOR_MAX_COST_USD", "0.50")),
        max_seconds=int(os.environ.get("LOOP_GENERATOR_MAX_SECONDS", "600")),
        context_window_tokens=model_metadata.get("context_length"),
        final_validator=lambda report: validate_generator_implementation_report(report, working_folder),
        skills=agent_skills.discover_skills(working_folder),
        write_enabled=True,
        write_blocked_prefixes=[".hooky"],
        read_allowed_prefixes=[".hooky", ".hooky/tool-results"],
        read_blocked_prefixes=[".hooky"],
        live_log_root=live_root,
        live_event_log_paths=[working_folder / ".hooky/log.runtime"],
        live_event_prefix="role=generator ",
    )
    try:
        result = run_tool_agent(
            model=model,
            system=generator_implementation_system_prompt(),
            user=generator_implementation_user_prompt(
                (working_folder / ".hooky/contract.md").read_text(encoding="utf-8"),
                (working_folder / ".hooky/feature_list.json").read_text(encoding="utf-8"),
                attempt_id,
                model_metadata,
                evaluator_feedback=evaluator_feedback,
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
    model = selected_evaluator_attempt_model()
    model_metadata = selected_evaluator_attempt_model_metadata()
    live_root = working_folder / ".hooky/attempts" / attempt_id / "traces"
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=evaluator_attempt_schema(),
        max_cost_usd=float(os.environ.get("LOOP_EVALUATOR_MAX_COST_USD", "0.50")),
        max_seconds=int(os.environ.get("LOOP_EVALUATOR_MAX_SECONDS", "600")),
        context_window_tokens=model_metadata.get("context_length"),
        final_validator=lambda report: validate_evaluator_attempt_report(report, working_folder),
        skills=agent_skills.discover_skills(working_folder),
        write_enabled=False,
        read_allowed_prefixes=[".hooky", ".hooky/tool-results"],
        read_blocked_prefixes=[".hooky"],
        live_log_root=live_root,
        live_event_log_paths=[working_folder / ".hooky/log.runtime"],
        live_event_prefix="role=evaluator ",
    )
    try:
        result = run_tool_agent(
            model=model,
            system=evaluator_attempt_system_prompt(),
            user=evaluator_attempt_user_prompt(
                (working_folder / ".hooky/contract.md").read_text(encoding="utf-8"),
                (working_folder / ".hooky/feature_list.json").read_text(encoding="utf-8"),
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

You have one responsibility: turn vague user input into the problem proposal in .hooky/contract.md.

You must never edit production code, tests, attempt artifacts, or evaluator reports.
You must not write the final grading contract. The generator proposes done criteria later and the evaluator reviews them.

Use write_file only for .hooky/contract.md. Preserve the loop vocabulary: planner, generator, evaluator, loop-runner, attempt.
Finish only with final_report.
"""


def planner_user_prompt(proposal: str, model_metadata: dict[str, Any]) -> str:
    return f"""Problem proposal input:

{proposal.strip() or "(no proposal provided)"}

Selected model:
```json
{json.dumps(model_metadata, indent=2, sort_keys=True)}
```

Write .hooky/contract.md with:
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

Your job is to propose concrete, testable done criteria in .hooky/contract.md and project them into .hooky/feature_list.json.
The evaluator will accept or reject the contract. You cannot approve your own criteria.

Use write_file only for .hooky/contract.md and .hooky/feature_list.json.
Finish only with final_report.
"""


def generator_contract_user_prompt(
    contract: str,
    proposal: str,
    model_metadata: dict[str, Any],
    *,
    review_feedback: str = "",
) -> str:
    feedback_section = ""
    if review_feedback.strip():
        feedback_section = f"""
Evaluator rejected the previous contract. Required revision feedback:

```markdown
{review_feedback.strip()}
```

Address every required change before calling final_report.
"""
    return f"""Original proposal artifact:

```markdown
{proposal.strip() or "(no durable proposal artifact was provided)"}
```

Current contract.md:

```markdown
{contract}
```

Selected model:
```json
{json.dumps(model_metadata, indent=2, sort_keys=True)}
```
{feedback_section}

Revise .hooky/contract.md so the Done Criteria section contains a checklist of concrete, testable assertions.
If the original proposal contains bullet, checkbox, or numbered checklist items, preserve every item as an acceptance requirement or split it into more specific requirements. Do not drop checklist items just because they seem obvious.
If the original proposal cites a visual reference, canonical template, official CSS, or reference implementation, convert that into a substantive Taste Rubric instead of leaving taste optional. The rubric must include design, originality, craft, and functionality axes. For canonical/template work, originality should score restraint and fidelity rather than novelty.
For TodoMVC-style reference work, explicitly require:
- official CSS/assets are imported and local CSS remains minimal;
- canonical DOM/classes are preserved so official CSS applies;
- visual checks cover empty, populated, completed/filter, and editing states;
- browser-default controls, overlapping footer/filter controls, collapsed footer/main regions, or missing canonical affordances fail the attempt.

Write .hooky/feature_list.json with this shape:
```json
{{
  "schema_version": 1,
  "features": [
    {{
      "id": "F001",
      "text": "testable assertion",
      "proposal_refs": ["short quote or identifier from the proposal item covered by this feature"],
      "status": "pending"
    }}
  ]
}}
```

Every proposal checklist item must be represented by at least one feature. Use proposal_refs to make coverage auditable.
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


def evaluator_contract_user_prompt(
    contract: str,
    feature_list: str,
    proposal: str,
    model_metadata: dict[str, Any],
) -> str:
    return f"""Review this proposed contract and feature list.

Original proposal artifact:
```markdown
{proposal.strip() or "(no durable proposal artifact was provided)"}
```

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
If the original proposal contains bullet, checkbox, or numbered checklist items, reject unless every item is covered by Done Criteria and feature_list entries. Use proposal_refs when available, but inspect the text yourself.
If the original proposal cites a visual reference, canonical template, official CSS, or reference implementation, reject unless contract.md contains a substantive Taste Rubric covering design, originality, craft, and functionality. For canonical/template work, the rubric must define originality as appropriate restraint/fidelity, not novelty.
For TodoMVC-style reference work, reject unless the contract requires canonical DOM/classes, official CSS/assets, minimal local CSS, and visual evaluation of empty, populated, completed/filter, and editing states.
If rejecting, list required_changes as specific edits the generator should make to the contract.
Finish only by calling final_report. Do not describe final_report in markdown; call the tool with JSON arguments matching the schema.
"""


def generator_implementation_system_prompt() -> str:
    return """You are the generator in a three-role Karpathy-style loop.

You have one responsibility now: implement the accepted contract.
You must not grade your own work and must not edit .hooky. The evaluator will grade independently.

Use files for code, tests, and project artifacts. Keep changes scoped to the contract.
Use todo_write for substantive work and run relevant commands before final_report.
"""


def generator_implementation_user_prompt(
    contract: str,
    feature_list: str,
    attempt_id: str,
    model_metadata: dict[str, Any],
    *,
    evaluator_feedback: str = "",
) -> str:
    feedback_section = ""
    if evaluator_feedback.strip():
        feedback_section = f"""
Previous evaluator feedback to address:
```markdown
{evaluator_feedback.strip()}
```
"""
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
{feedback_section}

Implement the contract in this workspace. Do not edit .hooky. Do not declare the attempt passed.
Use only the provided tools. To edit files, use write_file; do not call apply_patch or search_files.
Finish only with final_report describing changed_files, tests_run, and any failures.
"""


def evaluator_attempt_system_prompt() -> str:
    return """You are the evaluator in a three-role Karpathy-style loop.

Assume the implementation is broken. Your job is to prove whether it satisfies the accepted contract.
You may read files and run commands, including browser/UI verification when relevant. You must not edit files.
Do not pass an attempt based on placeholder tests, dummy tests, smoke-only assertions, or source inspection alone.
When the accepted contract describes a browser UI, web app, layout, CSS, or visual behavior, you must start or use the running app, call capture_visual_snapshot, inspect the attached screenshot image, and include visual findings. Obvious layout defects, overlapping controls, clipped content, browser-default styling where styled UI was required, or console errors are failures even when functional tests pass.
When the contract cites a visual reference, canonical template, official CSS, or reference implementation, one screenshot is not enough. Exercise representative states before passing: initial/empty state, populated state, completed/filter state, and editing or modal/active interaction state where applicable. Inspect that the canonical classes/DOM expected by the reference CSS are present and that controls do not collapse or overlap.
For TodoMVC-style contracts, explicitly verify the populated view uses `.main` and `.footer`, the official CSS applies to footer/filter layout, completed items are line-through, the selected filter has canonical styling, editing mode uses `.editing` plus `.edit`, and local CSS is minimal.
When the accepted contract defines a Taste Rubric, grade it explicitly with rubric_scores for design, originality, craft, and functionality plus score_explanation. When the task asks for subjective taste, polish, aesthetics, originality, brand fit, or craft but the contract lacks a substantive Taste Rubric, do not invent criteria after the fact; fail with recommendation=restart-contract.
Always set a non-empty bottleneck. On pass, name the weakest remaining part of the loop or product process. Use none_visible_after_trace_review only when you inspected traces/artifacts and found no meaningful bottleneck.

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
For browser/UI products, do not pass without capture_visual_snapshot evidence from the running app and explicit findings from the screenshot image.
For reference/canonical UI products, capture visual evidence from multiple meaningful UI states, not just first load. If a canonical CSS/template contract is present, source inspect the expected classes and then verify those classes render correctly in the browser.
If the available tests are placeholder-only, report that as a verification failure even if the test command exits 0.
If tests fail, report the failing criteria and choose continue or restart-attempt unless there is a true automation blocker.
Finish only with final_report containing status, recommendation, a non-empty bottleneck, findings, and score.
If the contract includes a substantive Taste Rubric, also include rubric_scores and score_explanation.
"""
