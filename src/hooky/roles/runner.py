"""Shared role-invocation plumbing: runtime paths, execution, and logging."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hooky import loop_executor
from hooky import runtime
from hooky.runtime import AgentRunError, AgentRunResult, ToolRuntime, build_runtime_metadata, write_runtime_log

runtime_dir = runtime.runtime_dir


def runtime_rel(*parts: str) -> str:
    return "/".join([runtime_dir(), *[part.strip("/") for part in parts if part]])


def runtime_path(working_folder: Path, *parts: str) -> Path:
    return working_folder / runtime_rel(*parts)


def tool_results_rel() -> str:
    return runtime_rel("tool-results")


def read_loop_proposal(working_folder: Path) -> str:
    path = runtime_path(working_folder, "proposal.md")
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def run_role_agent(
    *,
    role: str,
    agent_name: str,
    model: str,
    model_metadata: dict[str, Any],
    system: str,
    user: str,
    runtime: ToolRuntime,
) -> AgentRunResult:
    return loop_executor.run_role(
        loop_executor.RoleInvocation(
            role=role,
            agent_name=agent_name,
            model=model,
            model_metadata=model_metadata,
            system=system,
            user=user,
            runtime=runtime,
        )
    )


def run_loop_role(
    *,
    role: str,
    agent_name: str,
    model: str,
    model_metadata: dict[str, Any],
    system: str,
    user: str,
    runtime: ToolRuntime,
    live_root: Path,
    missing_report_error: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        result = run_role_agent(
            role=role,
            agent_name=agent_name,
            model=model,
            model_metadata=model_metadata,
            system=system,
            user=user,
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
            metadata=build_runtime_metadata(agent_name, model, model_metadata, result, status="error", error=str(exc)),
        )
        raise
    write_runtime_log(
        live_root,
        result.transcript,
        result.tool_events,
        result.compaction_events,
        result.pre_compaction_archives,
        metadata=build_runtime_metadata(agent_name, model, model_metadata, result),
    )
    if result.final_report is None:
        raise RuntimeError(missing_report_error)
    return result.final_report, result.usage
