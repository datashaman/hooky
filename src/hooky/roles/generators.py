"""Per-role artifact generation: builds a ToolRuntime and runs the role agent."""

from __future__ import annotations

import os

from pathlib import Path
from typing import Any

from hooky import agent_skills
from hooky.agent_runtime import ToolRuntime

from hooky.roles.models import (
    ensure_model_available,
    selected_evaluator_attempt_model,
    selected_evaluator_attempt_model_metadata,
    selected_model,
    selected_model_metadata,
)
from hooky.roles.prompts import (
    evaluator_attempt_system_prompt,
    evaluator_attempt_user_prompt,
    evaluator_contract_system_prompt,
    evaluator_contract_user_prompt,
    generator_contract_system_prompt,
    generator_contract_user_prompt,
    generator_implementation_system_prompt,
    generator_implementation_user_prompt,
    planner_system_prompt,
    planner_user_prompt,
)
from hooky.roles.runner import read_loop_proposal, run_loop_role, runtime_dir, runtime_path, runtime_rel, tool_results_rel
from hooky.roles.schemas import (
    evaluator_attempt_schema,
    evaluator_contract_schema,
    generator_contract_schema,
    generator_implementation_schema,
    planner_schema,
)
from hooky.roles.validators import (
    validate_evaluator_attempt_report,
    validate_evaluator_contract_report,
    validate_generator_contract_report,
    validate_generator_contract_write,
    validate_generator_implementation_report,
    validate_planner_report,
)


def generate_planner_artifacts(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    working_folder = working_folder.resolve()
    model = selected_model()
    ensure_model_available(model, "loop planner")
    model_metadata = selected_model_metadata()
    live_root = runtime_path(working_folder)
    if attempt_id:
        live_root = runtime_path(working_folder, "attempts", attempt_id, "traces")
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=planner_schema(),
        max_cost_usd=float(os.environ.get("LOOP_PLANNER_MAX_COST_USD", "0.10")),
        max_seconds=int(os.environ.get("LOOP_PLANNER_MAX_SECONDS", "180")),
        context_window_tokens=model_metadata.get("context_length"),
        final_validator=lambda report: validate_planner_report(report, working_folder),
        skills=agent_skills.discover_skills(working_folder),
        write_enabled=True,
        write_allowed_prefixes=[runtime_dir()],
        write_blocked_prefixes=[],
        read_allowed_prefixes=[runtime_dir(), tool_results_rel()],
        read_blocked_prefixes=[".hooky"],
        live_log_root=live_root,
        live_event_log_paths=[runtime_path(working_folder, "log.runtime")],
        live_event_prefix="role=planner ",
    )
    return run_loop_role(
        role="planner",
        agent_name="loop-planner",
        model=model,
        model_metadata=model_metadata,
        system=planner_system_prompt(),
        user=planner_user_prompt(proposal, model_metadata),
        runtime=runtime,
        live_root=live_root,
        missing_report_error="Loop planner finished without final_report",
    )


def generate_generator_contract_artifacts(
    *,
    working_folder: Path,
    attempt_id: str | None = None,
    review_feedback: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    working_folder = working_folder.resolve()
    model = selected_model()
    ensure_model_available(model, "loop generator")
    model_metadata = selected_model_metadata()
    live_root = runtime_path(working_folder)
    if attempt_id:
        live_root = runtime_path(working_folder, "attempts", attempt_id, "traces")
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
        write_allowed_prefixes=[runtime_rel("contract.md"), runtime_rel("feature_list.json")],
        write_blocked_prefixes=[],
        read_allowed_prefixes=[runtime_dir(), tool_results_rel()],
        read_blocked_prefixes=[".hooky"],
        live_log_root=live_root,
        live_event_log_paths=[runtime_path(working_folder, "log.runtime")],
        live_event_prefix="role=generator ",
    )
    return run_loop_role(
        role="generator",
        agent_name="loop-generator-contract",
        model=model,
        model_metadata=model_metadata,
        system=generator_contract_system_prompt(),
        user=generator_contract_user_prompt(
            runtime_path(working_folder, "contract.md").read_text(encoding="utf-8"),
            read_loop_proposal(working_folder),
            model_metadata,
            review_feedback=review_feedback,
        ),
        runtime=runtime,
        live_root=live_root,
        missing_report_error="Loop generator finished without final_report",
    )


def generate_evaluator_contract_artifacts(*, working_folder: Path, attempt_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    working_folder = working_folder.resolve()
    model = selected_model()
    ensure_model_available(model, "loop evaluator")
    model_metadata = selected_model_metadata()
    live_root = runtime_path(working_folder)
    if attempt_id:
        live_root = runtime_path(working_folder, "attempts", attempt_id, "traces")
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
        read_allowed_prefixes=[runtime_dir(), tool_results_rel()],
        read_blocked_prefixes=[".hooky"],
        live_log_root=live_root,
        live_event_log_paths=[runtime_path(working_folder, "log.runtime")],
        live_event_prefix="role=evaluator ",
    )
    return run_loop_role(
        role="evaluator",
        agent_name="loop-evaluator-contract",
        model=model,
        model_metadata=model_metadata,
        system=evaluator_contract_system_prompt(),
        user=evaluator_contract_user_prompt(
            runtime_path(working_folder, "contract.md").read_text(encoding="utf-8"),
            runtime_path(working_folder, "feature_list.json").read_text(encoding="utf-8"),
            read_loop_proposal(working_folder),
            model_metadata,
        ),
        runtime=runtime,
        live_root=live_root,
        missing_report_error="Loop evaluator finished without final_report",
    )


def generate_generator_implementation_artifacts(
    *,
    working_folder: Path,
    attempt_id: str,
    evaluator_feedback: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    working_folder = working_folder.resolve()
    model = selected_model()
    ensure_model_available(model, "loop generator")
    model_metadata = selected_model_metadata()
    live_root = runtime_path(working_folder, "attempts", attempt_id, "traces")
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
        read_allowed_prefixes=[runtime_dir(), tool_results_rel()],
        read_blocked_prefixes=[".hooky"],
        live_log_root=live_root,
        live_event_log_paths=[runtime_path(working_folder, "log.runtime")],
        live_event_prefix="role=generator ",
    )
    return run_loop_role(
        role="generator",
        agent_name="loop-generator-implementation",
        model=model,
        model_metadata=model_metadata,
        system=generator_implementation_system_prompt(),
        user=generator_implementation_user_prompt(
            runtime_path(working_folder, "contract.md").read_text(encoding="utf-8"),
            runtime_path(working_folder, "feature_list.json").read_text(encoding="utf-8"),
            attempt_id,
            model_metadata,
            evaluator_feedback=evaluator_feedback,
        ),
        runtime=runtime,
        live_root=live_root,
        missing_report_error="Loop generator finished without final_report",
    )


def generate_evaluator_attempt_artifacts(*, working_folder: Path, attempt_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    working_folder = working_folder.resolve()
    model = selected_evaluator_attempt_model()
    ensure_model_available(model, "loop evaluator")
    model_metadata = selected_evaluator_attempt_model_metadata()
    live_root = runtime_path(working_folder, "attempts", attempt_id, "traces")
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=evaluator_attempt_schema(),
        max_cost_usd=float(os.environ.get("LOOP_EVALUATOR_MAX_COST_USD", "0.50")),
        max_seconds=int(os.environ.get("LOOP_EVALUATOR_MAX_SECONDS", "600")),
        context_window_tokens=model_metadata.get("context_length"),
        final_validator=lambda report: validate_evaluator_attempt_report(report, working_folder),
        skills=agent_skills.discover_skills(working_folder),
        write_enabled=False,
        read_allowed_prefixes=[runtime_dir(), tool_results_rel()],
        read_blocked_prefixes=[".hooky"],
        live_log_root=live_root,
        live_event_log_paths=[runtime_path(working_folder, "log.runtime")],
        live_event_prefix="role=evaluator ",
    )
    return run_loop_role(
        role="evaluator",
        agent_name="loop-evaluator-attempt",
        model=model,
        model_metadata=model_metadata,
        system=evaluator_attempt_system_prompt(),
        user=evaluator_attempt_user_prompt(
            runtime_path(working_folder, "contract.md").read_text(encoding="utf-8"),
            runtime_path(working_folder, "feature_list.json").read_text(encoding="utf-8"),
            attempt_id,
            model_metadata,
        ),
        runtime=runtime,
        live_root=live_root,
        missing_report_error="Loop evaluator finished without final_report",
    )
