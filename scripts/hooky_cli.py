#!/usr/bin/env python3
"""Typer CLI for running Hooky task pipelines."""

from __future__ import annotations

import json
import os
import shlex
import socket
import shutil
import subprocess
import sys
import time
import difflib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import typer


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import builder_agent
import agent_runtime
import agent_skills
import eval_agent
import generate_eval_report
import loop_agent
import spec_agent
import test_agent
import verifier_agent


app = typer.Typer(help="Run the Hooky agentic SDLC pipeline.", no_args_is_help=True)
task_app = typer.Typer(help="Create, inspect, and switch tasks.", no_args_is_help=True)
run_app = typer.Typer(help="Run pipeline stages for the current task.", no_args_is_help=True)
approve_app = typer.Typer(help="Record human approval gates.", no_args_is_help=True)
skills_app = typer.Typer(help="Inspect available agent skills.", no_args_is_help=True)
loop_app = typer.Typer(help="Run the Karpathy-style loop.", no_args_is_help=True)
app.add_typer(task_app, name="task")
app.add_typer(run_app, name="run")
app.add_typer(approve_app, name="approve")
app.add_typer(skills_app, name="skills")
app.add_typer(loop_app, name="loop")

PIPELINE_STAGES = ["spec", "builder", "verifier", "eval"]
REMEDIABLE_STAGES = {"spec", "builder", "verifier", "eval"}
PIPELINE_PHASES = ["gather", "reason", "act", "verify", "repeat"]
STAGE_PHASES = {
    "spec": "reason",
    "test": "act",
    "builder": "act",
    "verifier": "verify",
    "eval": "repeat",
}
DEFAULT_LAST_RUN_PATH = Path("/tmp/hooky-last-run-path")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@app.callback()
def main(
    ctx: typer.Context,
    workspace: Annotated[Path, typer.Option("--workspace", "-C", help="Workspace directory. Defaults to current directory.")] = Path("."),
) -> None:
    ctx.obj = {"workspace": workspace.resolve()}


def workspace_from_ctx(ctx: typer.Context) -> Path:
    return ctx.obj["workspace"]


@contextmanager
def in_workspace(workspace: Path):
    previous = Path.cwd()
    os.chdir(workspace)
    try:
        yield
    finally:
        os.chdir(previous)


def repo_file(path: str) -> Path:
    return REPO_ROOT / path


def workflow_dir(workspace: Path) -> Path:
    return workspace / ".workflow"


def loop_dir(workspace: Path) -> Path:
    return workflow_dir(workspace) / "loop"


def loop_attempts_dir(workspace: Path) -> Path:
    return loop_dir(workspace) / "attempts"


def loop_feature_list_path(workspace: Path) -> Path:
    return loop_dir(workspace) / "feature_list.json"


def loop_progress_path(workspace: Path) -> Path:
    return loop_dir(workspace) / "progress.md"


def loop_contract_path(workspace: Path) -> Path:
    return loop_dir(workspace) / "contract.md"


def loop_log_path(workspace: Path) -> Path:
    return loop_dir(workspace) / "log.md"


def loop_state_path(workspace: Path) -> Path:
    return loop_dir(workspace) / "state.json"


def tasks_dir(workspace: Path) -> Path:
    return workflow_dir(workspace) / "tasks"


def global_state_path(workspace: Path) -> Path:
    return workflow_dir(workspace) / "state.json"


def task_dir(workspace: Path, task_id: str) -> Path:
    return tasks_dir(workspace) / task_id


def task_state_path(workspace: Path, task_id: str) -> Path:
    return task_dir(workspace, task_id) / "state.json"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def default_loop_feature_list() -> dict[str, Any]:
    return {"schema_version": 1, "features": []}


def default_loop_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "initialized",
        "current_attempt": None,
        "attempts": [],
        "contract_accepted": False,
        "last_action": None,
    }


def read_loop_state(workspace: Path) -> dict[str, Any]:
    path = loop_state_path(workspace)
    if not path.exists():
        raise typer.BadParameter("loop is not initialized. Run `hooky loop init` first.")
    return read_json(path)


def write_loop_state(workspace: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_json(loop_state_path(workspace), state)


def ensure_loop_initialized(workspace: Path) -> None:
    if not loop_state_path(workspace).exists():
        raise typer.BadParameter("loop is not initialized. Run `hooky loop init` first.")


def append_loop_log(workspace: Path, op: str, title: str, body: str = "") -> None:
    path = loop_log_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    date = now.date().isoformat()
    time_label = now.time().isoformat().replace("+00:00", "") + "Z"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    day_header = f"## {date}"
    entry = ""
    if day_header not in existing.splitlines():
        entry += ("" if not existing.strip() else "\n") + f"{day_header}\n\n"
    entry += f"- {time_label} {op} | {title}\n"
    if body.strip():
        body_lines = body.strip().splitlines()
        for line in body_lines:
            stripped = line.strip()
            if not stripped:
                entry += "  \n"
            elif stripped.startswith(("- ", "* ")):
                entry += f"  {stripped}\n"
            else:
                entry += f"  - {stripped}\n"
    entry += "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def write_loop_progress(workspace: Path, state: dict[str, Any], *, note: str | None = None) -> None:
    current_attempt = state.get("current_attempt")
    lines = [
        "# Loop Progress",
        "",
        f"- status: {state.get('status', 'unknown')}",
        f"- current_attempt: {current_attempt if current_attempt is not None else 'none'}",
        f"- contract_accepted: {str(bool(state.get('contract_accepted'))).lower()}",
        f"- last_action: {state.get('last_action') or 'none'}",
    ]
    if note:
        lines.extend(["", "## Note", "", note.strip()])
    attempts = state.get("attempts") if isinstance(state.get("attempts"), list) else []
    if attempts:
        lines.extend(["", "## Attempts", ""])
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            lines.append(f"- {attempt.get('id')}: {attempt.get('status')} ({attempt.get('started_at')})")
    loop_progress_path(workspace).write_text("\n".join(lines) + "\n", encoding="utf-8")


def next_loop_attempt_id(state: dict[str, Any]) -> str:
    attempts = state.get("attempts") if isinstance(state.get("attempts"), list) else []
    numbers: list[int] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        raw = str(attempt.get("id") or "")
        if raw.isdigit():
            numbers.append(int(raw))
    return f"{(max(numbers) if numbers else 0) + 1:03d}"


def loop_attempt_dir(workspace: Path, attempt_id: str) -> Path:
    return loop_attempts_dir(workspace) / attempt_id


def initialize_loop_files(workspace: Path, *, title: str | None, proposal: str = "", force: bool = False) -> Path:
    loop_root = loop_dir(workspace)
    if loop_state_path(workspace).exists() and not force:
        raise typer.BadParameter("loop already initialized. Use --force to overwrite.")
    loop_root.mkdir(parents=True, exist_ok=True)
    write_json(loop_feature_list_path(workspace), default_loop_feature_list())
    state = default_loop_state()
    state["created_at"] = utc_now()
    write_loop_state(workspace, state)
    contract_lines = ["# Loop Contract", ""]
    if title:
        contract_lines.extend(["## Problem", "", f"# {title}", ""])
    if proposal:
        contract_lines.extend(["## Proposal", "", proposal.strip(), ""])
    contract_lines.extend(
        [
            "## Done Criteria",
            "",
            "_The generator proposes criteria here; the evaluator accepts or rejects them before implementation._",
            "",
            "## Taste Rubric",
            "",
            "_Optional. Required only when subjective quality matters._",
            "",
        ]
    )
    loop_contract_path(workspace).write_text("\n".join(contract_lines), encoding="utf-8")
    write_loop_progress(workspace, state, note="Loop initialized.")
    loop_log_path(workspace).write_text("", encoding="utf-8")
    append_loop_log(workspace, "init", "loop initialized", f"workspace: {workspace}")
    return loop_root


def active_loop_attempt(state: dict[str, Any]) -> str:
    attempt_id = state.get("current_attempt")
    if not attempt_id:
        raise typer.BadParameter("no active attempt")
    return str(attempt_id)


def update_loop_attempt(state: dict[str, Any], attempt_id: str, **updates: Any) -> None:
    for attempt in state.get("attempts", []):
        if isinstance(attempt, dict) and attempt.get("id") == attempt_id:
            attempt.update(updates)
            return
    raise typer.BadParameter(f"attempt not found: {attempt_id}")


def start_loop_attempt_state(workspace: Path, state: dict[str, Any]) -> tuple[str, Path, dict[str, Any]]:
    if not state.get("contract_accepted"):
        raise typer.BadParameter("contract is not accepted. Run `hooky loop accept-contract` first.")
    active = state.get("current_attempt")
    if active:
        raise typer.BadParameter(f"attempt already active: {active}")
    attempt_id = next_loop_attempt_id(state)
    attempt_dir = loop_attempt_dir(workspace, attempt_id)
    (attempt_dir / "traces").mkdir(parents=True, exist_ok=True)
    (attempt_dir / "otel").mkdir(parents=True, exist_ok=True)
    (attempt_dir / "traces/planner.jsonl").touch()
    (attempt_dir / "traces/generator.jsonl").touch()
    (attempt_dir / "traces/evaluator.jsonl").touch()
    (attempt_dir / "otel/spans.jsonl").touch()
    attempt = {
        "id": attempt_id,
        "status": "running",
        "started_at": utc_now(),
        "path": attempt_dir.relative_to(workspace).as_posix(),
    }
    state.setdefault("attempts", []).append(attempt)
    state["current_attempt"] = attempt_id
    state["status"] = "attempt-running"
    return attempt_id, attempt_dir, state


def apply_loop_evaluator_report(
    workspace: Path,
    state: dict[str, Any],
    *,
    attempt_id: str,
    report: dict[str, Any],
    report_path: Path,
    action: str,
) -> dict[str, Any]:
    status = str(report.get("status"))
    recommendation = str(report.get("recommendation"))
    bottleneck = report.get("bottleneck")
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    attempt_status = "passed" if status == "pass" else "failed"
    if recommendation == "restart-attempt":
        attempt_status = "restarted"
    update_loop_attempt(
        state,
        attempt_id,
        status=attempt_status,
        completed_at=utc_now(),
        evaluator_report=report_path.relative_to(workspace).as_posix(),
        **({"bottleneck": bottleneck} if bottleneck else {}),
    )
    state["current_attempt"] = None
    if recommendation == "restart-attempt":
        state["status"] = "restart-attempt"
    elif recommendation == "restart-contract":
        state["status"] = "restart-contract"
        state["contract_accepted"] = False
    elif status == "pass":
        state["status"] = "passed"
    elif recommendation == "stop":
        state["status"] = "stopped"
    else:
        state["status"] = "attempt-failed"
    state["last_action"] = f"{action}:{recommendation}"
    if bottleneck:
        state["bottleneck"] = bottleneck
    note = f"Evaluator status={status}, recommendation={recommendation}."
    if bottleneck:
        note += f" Bottleneck: {bottleneck}."
    if findings:
        note += " Findings: " + "; ".join(str(item) for item in findings)
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=note)
    append_loop_log(workspace, "evaluator", f"attempt {attempt_id} report", note)
    return state


def format_evaluator_feedback(report: dict[str, Any]) -> str:
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    lines = [
        f"Status: {report.get('status')}",
        f"Recommendation: {report.get('recommendation')}",
    ]
    if report.get("bottleneck"):
        lines.extend(["", f"Bottleneck: {report['bottleneck']}"])
    if findings:
        lines.extend(["", "Findings:"])
        lines.extend(f"- {item}" for item in findings)
    if report.get("score") is not None:
        lines.extend(["", f"Score: {report['score']}"])
    return "\n".join(lines).strip()


def fallback_evaluator_report_from_error(
    *,
    attempt_id: str,
    error: Exception,
    generator_report: dict[str, Any],
) -> dict[str, Any]:
    findings = [
        f"Evaluator did not produce a valid final_report: {error}",
    ]
    summary = str(generator_report.get("summary") or "").strip()
    if summary:
        findings.append(f"Generator summary: {summary}")
    changed_files = generator_report.get("changed_files")
    if isinstance(changed_files, list):
        findings.append("Generator changed files: " + ", ".join(str(item) for item in changed_files))
    failures = generator_report.get("failures")
    if isinstance(failures, list) and failures:
        findings.extend(str(item) for item in failures)
    return {
        "schema_version": 1,
        "attempt": attempt_id,
        "written_at": utc_now(),
        "status": "fail",
        "recommendation": "restart-attempt",
        "bottleneck": "evaluator failed to submit a typed report; retrying with generator evidence",
        "findings": findings,
        "score": 0,
    }


def reset_loop_attempt_workspace(workspace: Path) -> str:
    if not (workspace / ".git").exists():
        return "no git repository; preserving workspace for next attempt"
    commands = [
        ["git", "-C", str(workspace), "reset", "--hard", "HEAD"],
        ["git", "-C", str(workspace), "clean", "-fd", "-e", ".workflow", "-e", "node_modules"],
    ]
    outputs: list[str] = []
    for command in commands:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            raise typer.BadParameter((completed.stderr or completed.stdout or "git cleanup failed").strip())
        output = (completed.stdout or completed.stderr or "").strip()
        if output:
            outputs.append(output)
    return "\n".join(outputs) if outputs else "workspace reset to git baseline"


def replace_markdown_section(text: str, heading: str, body: str) -> str:
    marker = f"## {heading}"
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == marker:
            start = index
            break
    replacement = [marker, "", body.strip(), ""]
    if start is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + "\n".join(replacement) + "\n"
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[:start] + replacement + lines[end:]).rstrip() + "\n"


def read_body_arg_or_file(body: str | None, body_file: Path | None) -> str:
    if body and body_file:
        raise typer.BadParameter("use either --body or --body-file, not both")
    if body_file:
        return body_file.read_text(encoding="utf-8")
    if body:
        return body
    raise typer.BadParameter("body is required via --body or --body-file")


def read_optional_proposal_file_or_stdin(proposal_file: Path | None) -> str:
    if proposal_file:
        return proposal_file.read_text(encoding="utf-8").strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return ""


def title_from_body(body: str) -> str | None:
    if not body:
        return None
    first_line = body.splitlines()[0].strip()
    return first_line or None


def read_global_state(workspace: Path) -> dict[str, Any]:
    path = global_state_path(workspace)
    return read_json(path) if path.exists() else {}


def write_global_state(workspace: Path, state: dict[str, Any]) -> None:
    write_json(global_state_path(workspace), state)


def set_current_task(workspace: Path, task_id: str) -> None:
    state = read_global_state(workspace)
    state["current_task"] = task_id
    state["updated_at"] = utc_now()
    write_global_state(workspace, state)


def resolve_current_task(workspace: Path) -> str:
    current = read_global_state(workspace).get("current_task")
    if current:
        if not task_state_path(workspace, str(current)).exists():
            raise typer.BadParameter(f"current task does not exist: {current}")
        return str(current)
    task_ids = sorted(path.name for path in tasks_dir(workspace).glob("*") if (path / "state.json").exists()) if tasks_dir(workspace).exists() else []
    if len(task_ids) == 1:
        set_current_task(workspace, task_ids[0])
        return task_ids[0]
    if not task_ids:
        raise typer.BadParameter("no task exists. Run `hooky task create --title ... --body-file ...` first.")
    raise typer.BadParameter("multiple tasks exist. Run `hooky task switch <task-id>` or pass `--task`.")


def load_task_state(workspace: Path, task_id: str | None = None) -> dict[str, Any]:
    resolved = task_id or resolve_current_task(workspace)
    return read_json(task_state_path(workspace, resolved))


def save_task_state(workspace: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_json(task_state_path(workspace, state["task_id"]), state)


def set_stage_status(workspace: Path, state: dict[str, Any], stage: str, status: str, **extra: Any) -> None:
    phase = phase_for_stage(stage)
    if status == "running":
        extra = {
            "owner_pid": os.getpid(),
            "owner_host": socket.gethostname(),
            "started_at": utc_now(),
            "trace": stage_runtime_events_path(workspace, stage, state),
            "phase": phase,
            **extra,
        }
    else:
        extra.setdefault("phase", phase)
    state.setdefault("stage_status", {})[stage] = {
        "status": status,
        "updated_at": utc_now(),
        **extra,
    }
    update_phase_status_from_stage(state, stage, status, extra)
    save_task_state(workspace, state)
    event_fields = dict(extra)
    if status == "running":
        event_fields.setdefault("pid", event_fields.get("owner_pid"))
    append_pipeline_event(
        workspace,
        "stage",
        task=state.get("task_id"),
        stage=stage,
        status=status,
        **event_fields,
    )


def set_pipeline_status(workspace: Path, state: dict[str, Any], status: str, **extra: Any) -> None:
    if status == "running":
        ensure_gather_phase(state)
        extra = {
            "owner_pid": os.getpid(),
            "owner_host": socket.gethostname(),
            "started_at": utc_now(),
            "trace": pipeline_log_path(workspace).relative_to(workspace).as_posix(),
            **extra,
        }
    state["pipeline_status"] = {
        "status": status,
        "updated_at": utc_now(),
        **extra,
    }
    save_task_state(workspace, state)
    event_fields = dict(extra)
    if status == "running":
        event_fields.setdefault("pid", event_fields.get("owner_pid"))
    append_pipeline_event(
        workspace,
        "pipeline",
        task=state.get("task_id"),
        status=status,
        **event_fields,
    )


def phase_for_stage(stage: str) -> str:
    return STAGE_PHASES.get(stage, stage)


def ensure_gather_phase(state: dict[str, Any]) -> None:
    phases = state.setdefault("phase_status", {})
    phases.setdefault(
        "gather",
        {
            "status": "passed",
            "updated_at": utc_now(),
            "source": "workspace_task_context",
        },
    )


def update_phase_status_from_stage(state: dict[str, Any], stage: str, status: str, extra: dict[str, Any]) -> None:
    phase = phase_for_stage(stage)
    if phase not in PIPELINE_PHASES:
        return
    phase_payload = {
        "status": status,
        "updated_at": utc_now(),
        "stage": stage,
        "agent": stage,
    }
    for key in ("error", "trace", "contract", "report_dir", "cost", "safe_to_merge", "owner_pid", "owner_host", "started_at"):
        if key in extra and extra[key] is not None:
            phase_payload[key] = extra[key]
    state.setdefault("phase_status", {})[phase] = phase_payload


def phase_statuses_from_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    phases = state.get("phase_status") if isinstance(state.get("phase_status"), dict) else {}
    merged: dict[str, dict[str, Any]] = {
        phase: dict(payload)
        for phase, payload in phases.items()
        if isinstance(payload, dict)
    }
    if "gather" not in merged:
        merged["gather"] = {
            "status": "passed" if state.get("issue") else "not-run",
            "source": "workspace_task_context",
        }
    for stage in PIPELINE_STAGES:
        stage_payload = state.get("stage_status", {}).get(stage, {})
        if not isinstance(stage_payload, dict):
            continue
        phase = phase_for_stage(stage)
        merged.setdefault(
            phase,
            {
                "status": stage_payload.get("status", "not-run"),
                "stage": stage,
                "agent": stage,
                **({"error": stage_payload["error"]} if stage_payload.get("error") else {}),
            },
        )
    for phase in PIPELINE_PHASES:
        merged.setdefault(phase, {"status": "not-run"})
    return merged


def pipeline_log_path(workspace: Path) -> Path:
    return workflow_dir(workspace) / "runtime_events.log"


def write_last_run_workspace(path: Path, workspace: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workspace.resolve().as_posix() + "\n", encoding="utf-8")


def workspace_from_last_run(path: Path) -> Path | None:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def workspace_for_status(ctx: typer.Context, last_run_path: Path) -> Path:
    workspace = workspace_from_ctx(ctx)
    if read_global_state(workspace).get("current_task"):
        return workspace
    last_workspace = workspace_from_last_run(last_run_path)
    if last_workspace and read_global_state(last_workspace).get("current_task"):
        return last_workspace
    return workspace


def workspace_for_loop(ctx: typer.Context, last_run_path: Path = DEFAULT_LAST_RUN_PATH) -> Path:
    workspace = workspace_from_ctx(ctx)
    if loop_state_path(workspace).exists():
        return workspace
    last_workspace = workspace_from_last_run(last_run_path)
    if last_workspace and loop_state_path(last_workspace).exists():
        return last_workspace
    return workspace


def last_nonempty_line(path: Path) -> str | None:
    if not path.exists():
        return None
    last: str | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.rstrip("\n")
            if stripped:
                last = stripped
    return last


def remediation_dir(workspace: Path, state: dict[str, Any]) -> Path:
    return workflow_dir(workspace) / "artifacts" / "remediation" / str(state["task_id"])


def current_remediation_path(workspace: Path) -> Path:
    return workflow_dir(workspace) / "artifacts" / "remediation" / "current.json"


def append_pipeline_event(workspace: Path, event: str, **fields: Any) -> None:
    path = pipeline_log_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [utc_now(), event]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={quote_log_value(value)}")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(" ".join(parts) + "\n")


def create_remediation_plan(
    workspace: Path,
    state: dict[str, Any],
    eval_contract: dict[str, Any],
    eval_contract_path: Path,
    *,
    root_cause_stage: str | None = None,
    source: str = "eval",
) -> Path | None:
    root_cause_stage = str(root_cause_stage or eval_contract.get("root_cause_stage") or "unknown")
    if root_cause_stage == "test":
        root_cause_stage = "builder"
    if root_cause_stage not in REMEDIABLE_STAGES or root_cause_stage == "eval":
        return None
    root_cause_phase = phase_for_stage(root_cause_stage)
    generated_at = utc_now()
    rerun_command = f"uv run hooky -C {workspace.as_posix()} run remediation --auto-approve"
    payload = {
        "schema_version": 1,
        "task_id": state.get("task_id"),
        "generated_at": generated_at,
        "root_cause_stage": root_cause_stage,
        "root_cause_phase": root_cause_phase,
        "resume_from_stage": root_cause_stage,
        "resume_from_phase": root_cause_phase,
        "source": source,
        "eval_contract": display_workspace_path(eval_contract_path, workspace),
        "status": eval_contract.get("status"),
        "safe_to_merge": eval_contract.get("safe_to_merge"),
        "findings": eval_contract.get("findings", []),
        "trajectory_findings": eval_contract.get("trajectory_findings", []),
        "artifact_findings": eval_contract.get("artifact_findings", []),
        "tooling_findings": eval_contract.get("tooling_findings", []),
        "human_review_focus": eval_contract.get("human_review_focus", []),
        "rerun_command": rerun_command,
    }
    plan_dir = remediation_dir(workspace, state)
    plan_path = plan_dir / f"{generated_at.replace(':', '').replace('+', 'Z')}-{root_cause_stage}.json"
    write_json(plan_path, payload)
    write_json(current_remediation_path(workspace), payload)
    state.setdefault("artifacts", {})["remediation"] = {
        "current": current_remediation_path(workspace).relative_to(workspace).as_posix(),
        "plan": plan_path.relative_to(workspace).as_posix(),
        "root_cause_stage": root_cause_stage,
        "rerun_command": rerun_command,
    }
    save_task_state(workspace, state)
    append_pipeline_event(
        workspace,
        "remediation",
        task=state.get("task_id"),
        status="created",
        root_cause_stage=root_cause_stage,
        root_cause_phase=root_cause_phase,
        source=source,
        plan=plan_path.relative_to(workspace).as_posix(),
    )
    return plan_path


def maybe_create_remediation_plan(workspace: Path, state: dict[str, Any], eval_contract: dict[str, Any], eval_contract_path: Path) -> Path | None:
    if eval_contract.get("status") == "pass" and eval_contract.get("safe_to_merge") is True:
        return None
    current_path = current_remediation_path(workspace)
    if current_path.exists():
        current = read_json(current_path)
        if (
            current.get("source") == "builder-test-contract-finding"
            and current.get("root_cause_stage") == "test"
            and eval_contract.get("root_cause_stage") != "test"
        ):
            append_pipeline_event(
                workspace,
                "remediation",
                task=state.get("task_id"),
                status="preserved",
                root_cause_stage="test",
                source="builder-test-contract-finding",
                ignored_eval_root_cause=eval_contract.get("root_cause_stage"),
                plan=current_path.relative_to(workspace).as_posix(),
            )
            return current_path
    return create_remediation_plan(workspace, state, eval_contract, eval_contract_path)


def recover_artifacts_for_resume(workspace: Path, state: dict[str, Any], start_stage: str, *, auto_approve: bool) -> None:
    artifacts = state.setdefault("artifacts", {})
    stage_status = state.setdefault("stage_status", {})
    task_id = str(state["task_id"])
    prior_stages = PIPELINE_STAGES[: PIPELINE_STAGES.index(start_stage)]

    if "spec" in prior_stages and "spec" not in artifacts:
        spec_contract = workspace / "docs/specs" / task_id / "contract.json"
        if spec_contract.exists():
            artifacts["spec"] = {
                "artifact_dir": spec_contract.parent.relative_to(workspace).as_posix(),
                "contract": spec_contract.relative_to(workspace).as_posix(),
            }
    if "spec" in prior_stages and "spec" in artifacts:
        stage_status["spec"] = {
            "status": "passed",
            "updated_at": utc_now(),
            "contract": artifacts["spec"].get("contract"),
        }

    if "test" in prior_stages and "test" not in artifacts:
        test_contract = workspace / ".workflow/artifacts/test-agent" / task_id / "contract.json"
        if test_contract.exists():
            test_contract_payload = read_json(test_contract)
            artifacts["test"] = {
                "report_dir": test_contract.parent.relative_to(workspace).as_posix(),
                "contract": test_contract.relative_to(workspace).as_posix(),
                "test_files": [item["path"] for item in test_contract_payload.get("test_files", [])],
                "fixtures": [item["path"] for item in test_contract_payload.get("fixtures", [])],
            }
    if "test" in prior_stages and "test" in artifacts:
        stage_status["test"] = {
            "status": "passed",
            "updated_at": utc_now(),
            "contract": artifacts["test"].get("contract"),
            "report_dir": artifacts["test"].get("report_dir"),
        }

    if "builder" in prior_stages and "builder" not in artifacts:
        builder_contract = workspace / ".workflow/artifacts/builder-agent/contract.json"
        if builder_contract.exists():
            proposal_dir = change_proposal_dir(workspace, task_id)
            artifacts["builder"] = {
                "report_dir": builder_contract.parent.relative_to(workspace).as_posix(),
                "contract": builder_contract.relative_to(workspace).as_posix(),
                "proposal_dir": proposal_dir.relative_to(workspace).as_posix(),
                "proposal": (proposal_dir / "proposal.json").relative_to(workspace).as_posix(),
                "patch": (proposal_dir / "patch.diff").relative_to(workspace).as_posix(),
                "summary": (proposal_dir / "summary.md").relative_to(workspace).as_posix(),
            }
    if "builder" in prior_stages and "builder" in artifacts:
        stage_status["builder"] = {
            "status": "passed",
            "updated_at": utc_now(),
            "contract": artifacts["builder"].get("contract"),
            "report_dir": artifacts["builder"].get("report_dir"),
            "proposal": artifacts["builder"].get("proposal"),
        }

    if auto_approve:
        approvals = state.setdefault("approvals", {})
        if "spec" in prior_stages and "spec" in artifacts and "spec" not in approvals:
            approvals["spec"] = {"approved_at": utc_now(), "source": "remediation-auto-approve"}

    save_task_state(workspace, state)


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stage_max_seconds(stage: str) -> int:
    defaults = {
        "spec": 180,
        "test": 600,
        "builder": 420,
        "verifier": 300,
        "eval": 300,
    }
    env_names = {
        "spec": "SPEC_AGENT_MAX_SECONDS",
        "test": "TEST_AGENT_MAX_SECONDS",
        "builder": "BUILDER_AGENT_MAX_SECONDS",
        "verifier": "VERIFIER_AGENT_MAX_SECONDS",
        "eval": "EVAL_AGENT_MAX_SECONDS",
    }
    env_name = env_names.get(stage)
    if env_name and os.environ.get(env_name):
        return int(os.environ[env_name])
    return defaults.get(stage, 300)


def running_stale_seconds(stage: str) -> int:
    override = os.environ.get("HOOKY_RUNNING_STALE_SECONDS")
    if override:
        return int(override)
    return stage_max_seconds(stage) + 60


def live_artifact_updated_at(workspace: Path, stage: str, state: dict[str, Any]) -> float | None:
    log_dir = runtime_log_dir(workspace, stage, state)
    candidates: list[float] = []
    events_path = log_dir / "runtime_events.log"
    metadata_path = log_dir / "runtime_metadata.json"
    if events_path.exists():
        timestamp = last_event_timestamp(events_path)
        candidates.append(timestamp if timestamp is not None else events_path.stat().st_mtime)
    if metadata_path.exists():
        timestamp = metadata_timestamp(metadata_path)
        candidates.append(timestamp if timestamp is not None else metadata_path.stat().st_mtime)
    return max(candidates) if candidates else None


def parse_timestamp(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def last_event_timestamp(path: Path) -> float | None:
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        return parse_timestamp(line.split(maxsplit=1)[0])
    return None


def metadata_timestamp(path: Path) -> float | None:
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    for key in ("written_at", "ended_at", "started_at"):
        value = payload.get(key)
        if isinstance(value, str):
            timestamp = parse_timestamp(value)
            if timestamp is not None:
                return timestamp
    return None


def refresh_interrupted_stages(workspace: Path, state: dict[str, Any]) -> bool:
    changed = False
    current_host = socket.gethostname()
    pipeline_payload = state.get("pipeline_status")
    if isinstance(pipeline_payload, dict) and pipeline_payload.get("status") == "running":
        owner_pid = pipeline_payload.get("owner_pid")
        owner_host = pipeline_payload.get("owner_host")
        if isinstance(owner_pid, int) and owner_host == current_host and not process_exists(owner_pid):
            pipeline_payload["status"] = "interrupted"
            pipeline_payload["updated_at"] = utc_now()
            pipeline_payload["error"] = f"pipeline owner process {owner_pid} is no longer running"
            append_pipeline_event(
                workspace,
                "pipeline",
                task=state.get("task_id"),
                status="interrupted",
                error=pipeline_payload["error"],
                trace=pipeline_payload.get("trace"),
                pid=owner_pid,
            )
            changed = True
    for stage, payload in list(state.get("stage_status", {}).items()):
        if not isinstance(payload, dict) or payload.get("status") != "running":
            continue
        owner_pid = payload.get("owner_pid")
        owner_host = payload.get("owner_host")
        if isinstance(owner_pid, int):
            if owner_host != current_host or process_exists(owner_pid):
                continue
            reason = f"run owner process {owner_pid} is no longer running"
        else:
            updated_at = live_artifact_updated_at(workspace, stage, state)
            if updated_at is None:
                continue
            idle_seconds = int(time.time() - updated_at)
            stale_seconds = running_stale_seconds(stage)
            if idle_seconds < stale_seconds:
                continue
            reason = f"run has no owner process and no live output for {idle_seconds}s (stale threshold {stale_seconds}s)"
        payload["status"] = "interrupted"
        payload["updated_at"] = utc_now()
        payload["error"] = reason
        append_pipeline_event(
            workspace,
            "stage",
            task=state.get("task_id"),
            stage=stage,
            status="interrupted",
            error=payload["error"],
            trace=payload.get("trace"),
            pid=owner_pid,
        )
        changed = True
    if changed:
        save_task_state(workspace, state)
    return changed


def pipeline_complete(state: dict[str, Any]) -> bool:
    statuses = state.get("stage_status") if isinstance(state.get("stage_status"), dict) else {}
    return all(isinstance(statuses.get(stage), dict) and statuses[stage].get("status") == "passed" for stage in PIPELINE_STAGES)


def quote_log_value(value: Any) -> str:
    text = str(value)
    if not text:
        return '""'
    if any(char.isspace() for char in text) or '"' in text:
        return '"' + " ".join(text.split()).replace('"', '\\"')[:500] + '"'
    return text


def ensure_initialized(workspace: Path) -> None:
    if not (workflow_dir(workspace) / "agents").exists():
        raise typer.BadParameter("workspace is not initialized. Run `hooky init` first.")


def copy_missing(src: Path, dst: Path, force: bool = False) -> None:
    if dst.exists() and not force:
        return
    if dst.exists():
        shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def make_task_id(title: str, issue_number: int) -> str:
    return f"issue-{issue_number}-{spec_agent.slugify(title)}"


def resolve_workspace_path(workspace: Path, path: Path) -> Path:
    if path.is_absolute() or path.exists():
        return path
    workspace_path = workspace / path
    return workspace_path if workspace_path.exists() else path


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def git_command(workspace: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def is_git_worktree(workspace: Path) -> bool:
    result = git_command(workspace, ["rev-parse", "--is-inside-work-tree"], check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def git_optional(workspace: Path, args: list[str]) -> str | None:
    result = git_command(workspace, args, check=False)
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def git_status_entries(workspace: Path) -> list[dict[str, str]]:
    result = git_command(
        workspace,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".", ":!.workflow"],
        check=False,
    )
    if result.returncode != 0:
        return []
    records = [record for record in result.stdout.split("\0") if record]
    entries: list[dict[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        status = record[:2]
        path = record[3:]
        if status[:1] in {"R", "C"} and index + 1 < len(records):
            index += 1
            path = records[index]
        entries.append({"status": status.strip() or status, "path": path})
        index += 1
    return entries


def git_patch_for_workspace(workspace: Path, entries: list[dict[str, str]]) -> str:
    parts: list[str] = []
    tracked = git_command(workspace, ["diff", "--binary", "--", ".", ":!.workflow"], check=False)
    if tracked.stdout:
        parts.append(tracked.stdout.rstrip() + "\n")
    for entry in entries:
        if entry["status"] != "??":
            continue
        path = workspace / entry["path"]
        if not path.is_file():
            continue
        diff = git_command(workspace, ["diff", "--no-index", "--binary", "--", "/dev/null", entry["path"]], check=False)
        output = diff.stdout or diff.stderr
        if output:
            parts.append(output.rstrip() + "\n")
    return "\n".join(parts)


def snapshot_project_files(workspace: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    skipped_dirs = {
        ".git",
        ".workflow",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "vendor",
    }
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(workspace)
        except ValueError:
            continue
        if any(part in skipped_dirs for part in relative.parts):
            continue
        snapshot[relative.as_posix()] = path.read_bytes()
    return snapshot


def snapshot_delta_entries(before: dict[str, bytes], after: dict[str, bytes]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            entries.append({"status": "A", "path": path})
        elif path not in after:
            entries.append({"status": "D", "path": path})
        elif before[path] != after[path]:
            entries.append({"status": "M", "path": path})
    return entries


def patch_from_snapshots(before: dict[str, bytes], after: dict[str, bytes], entries: list[dict[str, str]]) -> str:
    patches: list[str] = []
    for entry in entries:
        path = entry["path"]
        before_bytes = before.get(path)
        after_bytes = after.get(path)
        before_text = decode_patch_text(before_bytes)
        after_text = decode_patch_text(after_bytes)
        if before_text is None or after_text is None:
            patches.append(binary_patch_notice(path, entry["status"]))
            continue
        fromfile = "/dev/null" if before_bytes is None else f"a/{path}"
        tofile = "/dev/null" if after_bytes is None else f"b/{path}"
        diff = difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
        body = "".join(diff)
        if body:
            patches.append(f"diff --git a/{path} b/{path}\n{body.rstrip()}\n")
    return "\n".join(patches)


def decode_patch_text(content: bytes | None) -> str | None:
    if content is None:
        return ""
    if b"\0" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def binary_patch_notice(path: str, status: str) -> str:
    return f"diff --git a/{path} b/{path}\nBinary file changed ({status})\n"


def change_proposal_dir(workspace: Path, task_id: str) -> Path:
    return workflow_dir(workspace) / "artifacts/change-proposals" / task_id


def create_change_proposal(
    *,
    workspace: Path,
    state: dict[str, Any],
    builder_contract_path: Path,
    generated_at: str,
    before_snapshot: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    task_id = state["task_id"]
    proposal_dir = change_proposal_dir(workspace, task_id)
    proposal_dir.mkdir(parents=True, exist_ok=True)
    builder_contract = read_json(builder_contract_path)
    git_available = is_git_worktree(workspace)
    git_entries = git_status_entries(workspace) if git_available else []
    if before_snapshot is not None:
        after_snapshot = snapshot_project_files(workspace)
        entries = snapshot_delta_entries(before_snapshot, after_snapshot)
        patch = patch_from_snapshots(before_snapshot, after_snapshot, entries)
    else:
        entries = git_entries
        patch = git_patch_for_workspace(workspace, entries) if git_available else ""
    branch_suggestion = f"sdlc/{task_id}"
    origin = git_optional(workspace, ["remote", "get-url", "origin"]) if git_available else None
    current_branch = git_optional(workspace, ["branch", "--show-current"]) if git_available else None
    changed_files = [entry["path"] for entry in entries]
    proposal = {
        "schema_version": 1,
        "type": "local_git_change_proposal",
        "task_id": task_id,
        "title": state.get("title"),
        "created_at": generated_at,
        "git": {
            "available": git_available,
            "current_branch": current_branch,
            "proposal_branch": branch_suggestion,
            "origin": origin,
            "hosted_pr_url": None,
            "hosted_pr_available": bool(origin),
        },
        "builder": {
            "contract": display_workspace_path(builder_contract_path, workspace),
            "summary": builder_contract.get("summary"),
            "tests_run": builder_contract.get("tests_run", []),
            "tests_passing": builder_contract.get("tests_passing"),
            "failures_remaining": builder_contract.get("failures_remaining", []),
        },
        "changed_files": changed_files,
        "status_entries": entries,
        "git_status_entries": git_entries,
        "artifacts": {
            "summary": "summary.md",
            "patch": "patch.diff",
            "proposal": "proposal.json",
        },
    }
    (proposal_dir / "patch.diff").write_text(patch, encoding="utf-8")
    (proposal_dir / "summary.md").write_text(render_change_proposal_summary(proposal), encoding="utf-8")
    write_json(proposal_dir / "proposal.json", proposal)
    return proposal


def display_workspace_path(path: Path, workspace: Path) -> str:
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return path.as_posix()


def render_change_proposal_summary(proposal: dict[str, Any]) -> str:
    builder = proposal.get("builder", {})
    git = proposal.get("git", {})
    lines = [
        f"# {proposal.get('title') or proposal.get('task_id')}",
        "",
        "## Summary",
        "",
        str(builder.get("summary") or "No builder summary provided."),
        "",
        "## Git Proposal",
        "",
        f"- Type: {proposal.get('type')}",
        f"- Current branch: {git.get('current_branch') or 'unknown'}",
        f"- Suggested branch: {git.get('proposal_branch')}",
        f"- Remote: {git.get('origin') or 'none'}",
        f"- Hosted PR URL: {git.get('hosted_pr_url') or 'not created'}",
        "",
        "## Changed Files",
        "",
    ]
    changed_files = proposal.get("changed_files") or []
    lines.extend(f"- {path}" for path in changed_files)
    if not changed_files:
        lines.append("- None detected")
    lines.extend(
        [
            "",
            "## Tests Run",
            "",
        ]
    )
    tests_run = builder.get("tests_run") or []
    lines.extend(f"- {test}" for test in tests_run)
    if not tests_run:
        lines.append("- None reported")
    failures = builder.get("failures_remaining") or []
    lines.extend(["", "## Remaining Failures", ""])
    lines.extend(f"- {failure}" for failure in failures)
    if not failures:
        lines.append("- None reported")
    return "\n".join(lines) + "\n"


@app.command()
def init(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option(help="Overwrite existing Hooky runtime files.")] = False,
    git: Annotated[bool, typer.Option("--git/--no-git", help="Initialize a local git repository when the workspace is not already a worktree.")] = True,
) -> None:
    """Initialize a workspace with Hooky runtime context."""
    workspace = workspace_from_ctx(ctx)
    workspace.mkdir(parents=True, exist_ok=True)
    if git and not is_git_worktree(workspace):
        git_command(workspace, ["init"], check=True)
    copy_missing(repo_file(".workflow/agents"), workspace / ".workflow/agents", force=force)
    copy_missing(repo_file("AGENTS.md"), workspace / "AGENTS.md", force=force)
    (workspace / ".workflow/tasks").mkdir(parents=True, exist_ok=True)
    if git:
        agent_runtime.ensure_git_baseline(workspace)
    typer.echo(f"initialized: {workspace}")


@task_app.command("create")
def task_create(
    ctx: typer.Context,
    title: Annotated[str, typer.Option(help="Issue/task title.")],
    body_file: Annotated[Path | None, typer.Option(help="Path to a Markdown issue body.")] = None,
    body: Annotated[str | None, typer.Option(help="Issue body text.")] = None,
    issue_number: Annotated[int, typer.Option(help="Issue number for artifact naming.")] = 1001,
) -> None:
    """Create a task and make it current."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    if body_file and body:
        raise typer.BadParameter("use either --body-file or --body, not both")
    if body_file:
        resolved_body_file = resolve_workspace_path(workspace, body_file)
        if is_relative_to(resolved_body_file, workspace):
            raise typer.BadParameter("task --body-file must be outside the workspace; use --body or an external temp file so issue input is not visible to later agents")
        body_text = resolved_body_file.read_text(encoding="utf-8")
    else:
        body_text = body or ""
    if not body_text.strip():
        raise typer.BadParameter("task body is required via --body-file or --body")
    task_id = make_task_id(title, issue_number)
    state = {
        "schema_version": 1,
        "task_id": task_id,
        "title": title,
        "issue_number": issue_number,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "approvals": {},
        "artifacts": {},
        "issue": {"number": issue_number, "title": title, "body": body_text, "user": {"login": "local"}},
    }
    write_json(task_dir(workspace, task_id) / "issue.json", {"label": {"name": "sdlc:spec"}, "issue": state["issue"]})
    save_task_state(workspace, state)
    set_current_task(workspace, task_id)
    typer.echo(f"created current task: {task_id}")
    typer.echo("next: hooky run spec")


@task_app.command("list")
def task_list(ctx: typer.Context) -> None:
    """List known tasks."""
    workspace = workspace_from_ctx(ctx)
    current = read_global_state(workspace).get("current_task")
    for path in sorted(tasks_dir(workspace).glob("*")) if tasks_dir(workspace).exists() else []:
        if (path / "state.json").exists():
            typer.echo(f"{'*' if path.name == current else ' '} {path.name}")


@task_app.command("current")
def task_current(ctx: typer.Context) -> None:
    """Print the current task."""
    typer.echo(resolve_current_task(workspace_from_ctx(ctx)))


@task_app.command("switch")
def task_switch(ctx: typer.Context, task_id: Annotated[str, typer.Argument(help="Task id to make current.")]) -> None:
    """Switch the current task."""
    workspace = workspace_from_ctx(ctx)
    if not task_state_path(workspace, task_id).exists():
        raise typer.BadParameter(f"task does not exist: {task_id}")
    set_current_task(workspace, task_id)
    typer.echo(f"current task: {task_id}")


@skills_app.command("list")
def skills_list(ctx: typer.Context) -> None:
    """List available agent skills for this workspace."""
    workspace = workspace_from_ctx(ctx)
    skills = agent_skills.discover_skills(workspace)
    if not skills:
        typer.echo("No skills found.")
        return
    for skill in skills:
        suffix = f" - {skill.description}" if skill.description else ""
        typer.echo(f"{skill.name}{suffix}")
        typer.echo(f"  path: {skill.path}")


@skills_app.command("show")
def skills_show(ctx: typer.Context, name: Annotated[str, typer.Argument(help="Skill name to inspect.")]) -> None:
    """Show one skill's instructions and resource index."""
    workspace = workspace_from_ctx(ctx)
    for skill in agent_skills.discover_skills(workspace):
        if skill.name != name:
            continue
        typer.echo(f"name: {skill.name}")
        typer.echo(f"description: {skill.description}")
        typer.echo(f"path: {skill.path}")
        resources = agent_skills.skill_resources(skill)
        if resources:
            typer.echo("resources:")
            for resource in resources:
                typer.echo(f"  {resource['path']} ({resource['bytes']} bytes)")
        typer.echo("")
        typer.echo(skill.body)
        return
    raise typer.BadParameter(f"unknown skill: {name}")


@loop_app.command("init")
def loop_init(
    ctx: typer.Context,
    title: Annotated[str | None, typer.Option(help="Problem title for contract.md.")] = None,
    proposal_file: Annotated[Path | None, typer.Option(help="Optional problem proposal Markdown file.")] = None,
    force: Annotated[bool, typer.Option(help="Overwrite existing loop files.")] = False,
    last_run_path: Annotated[Path, typer.Option(help="Path used by loop status/watch to find the latest loop workspace.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Initialize the Karpathy-style loop durable state files."""
    workspace = workspace_from_ctx(ctx)
    proposal = read_optional_proposal_file_or_stdin(proposal_file)
    title = title or title_from_body(proposal)
    loop_root = initialize_loop_files(workspace, title=title, proposal=proposal, force=force)
    write_last_run_workspace(last_run_path, workspace)
    typer.echo(f"loop: {loop_root}")
    typer.echo("next: hooky loop status")


@loop_app.command("status")
def loop_status(
    ctx: typer.Context,
    last_run_path: Annotated[Path, typer.Option(help="Path written by loop init/run; used when the current directory has no loop.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Show Karpathy-style loop state and durable file locations."""
    workspace = workspace_for_loop(ctx, last_run_path)
    ctx.obj["workspace"] = workspace
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    typer.echo(f"loop: {loop_dir(workspace)}")
    typer.echo(f"status: {state.get('status')}")
    typer.echo(f"current_attempt: {state.get('current_attempt') or 'none'}")
    typer.echo(f"contract_accepted: {str(bool(state.get('contract_accepted'))).lower()}")
    typer.echo("files:")
    typer.echo(f"  feature_list: {loop_feature_list_path(workspace)}")
    typer.echo(f"  progress: {loop_progress_path(workspace)}")
    typer.echo(f"  contract: {loop_contract_path(workspace)}")
    typer.echo(f"  log: {loop_log_path(workspace)}")
    attempts = state.get("attempts") if isinstance(state.get("attempts"), list) else []
    if attempts:
        typer.echo("attempts:")
        for attempt in attempts:
            if isinstance(attempt, dict):
                typer.echo(f"  {attempt.get('id')}: {attempt.get('status')}")


@loop_app.command("watch")
def loop_watch(
    ctx: typer.Context,
    path_only: Annotated[bool, typer.Option("--path", help="Only print the loop log path.")] = False,
    follow: Annotated[bool, typer.Option("--follow/--no-follow", "-f", help="Follow the loop log.")] = True,
    last_run_path: Annotated[Path, typer.Option(help="Path written by loop init/run; used when the current directory has no loop.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Show or follow .workflow/loop/log.md."""
    workspace = workspace_for_loop(ctx, last_run_path)
    ctx.obj["workspace"] = workspace
    ensure_loop_initialized(workspace)
    path = loop_log_path(workspace)
    if path_only:
        typer.echo(path)
        return
    if follow:
        follow_runtime_log(workspace, "loop", {}, path)
        return
    typer.echo(path.read_text(encoding="utf-8").rstrip())


def latest_loop_attempt_id(state: dict[str, Any]) -> str | None:
    if state.get("current_attempt"):
        return str(state["current_attempt"])
    attempts = state.get("attempts") if isinstance(state.get("attempts"), list) else []
    for attempt in reversed(attempts):
        if isinstance(attempt, dict) and attempt.get("id"):
            return str(attempt["id"])
    return None


def loop_debug_root(workspace: Path, state: dict[str, Any], attempt: str | None) -> Path:
    attempt_id = attempt or latest_loop_attempt_id(state)
    if attempt_id:
        return loop_attempt_dir(workspace, attempt_id) / "traces"
    return loop_dir(workspace)


def load_loop_transcript(root: Path) -> list[dict[str, Any]]:
    path = root / "runtime_transcript.json"
    if not path.exists():
        raise typer.BadParameter(f"runtime transcript not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise typer.BadParameter(f"runtime transcript is not a list: {path}")
    return [item for item in payload if isinstance(item, dict)]


def transcript_message(entry: dict[str, Any]) -> dict[str, Any]:
    message = entry.get("message")
    return message if isinstance(message, dict) else entry


def transcript_role(entry: dict[str, Any]) -> str:
    message = transcript_message(entry)
    return str(message.get("role") or entry.get("role") or "event")


def transcript_text(entry: dict[str, Any]) -> str:
    message = transcript_message(entry)
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(entry.get("message"), str):
        return str(entry["message"])
    return ""


def transcript_tool_calls(entry: dict[str, Any]) -> list[dict[str, Any]]:
    message = transcript_message(entry)
    calls = message.get("tool_calls")
    return calls if isinstance(calls, list) else []


@loop_app.command("runtime-log")
def loop_runtime_log(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt, or contract-level runtime if none exists.")] = None,
    follow: Annotated[bool, typer.Option("--follow/--no-follow", "-f", help="Follow the runtime event log.")] = False,
    tail_path: Annotated[bool, typer.Option("--tail-path", help="Only print the runtime event log path.")] = False,
    lines: Annotated[int, typer.Option(help="Number of lines to show when not following.")] = 80,
) -> None:
    """Show the live model/tool runtime log for the loop or an attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    path = root / "runtime_events.log"
    if tail_path:
        typer.echo(path)
        return
    if follow:
        follow_runtime_log(workspace, "loop", state, path)
        return
    if not path.exists():
        raise typer.BadParameter(f"runtime log not found: {path}")
    content = path.read_text(encoding="utf-8").splitlines()
    typer.echo("\n".join(content[-lines:]))


@loop_app.command("transcript")
def loop_transcript(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt, or contract-level runtime if none exists.")] = None,
    role: Annotated[str, typer.Option(help="Role filter: all, system, user, assistant, or tool.")] = "all",
    last: Annotated[int, typer.Option(help="Number of matching transcript entries to show.")] = 20,
    full: Annotated[bool, typer.Option("--full", help="Print full message text instead of a preview.")] = False,
) -> None:
    """Show actual persisted model conversation entries for loop debugging."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    entries = load_loop_transcript(root)
    allowed_roles = {"all", "system", "user", "assistant", "tool"}
    if role not in allowed_roles:
        raise typer.BadParameter("role must be one of: all, system, user, assistant, tool")
    selected = [entry for entry in entries if role == "all" or transcript_role(entry) == role]
    for index, entry in list(enumerate(selected, 1))[-last:]:
        entry_role = transcript_role(entry)
        calls = transcript_tool_calls(entry)
        kind = str(entry.get("kind") or "message")
        text = transcript_text(entry).strip()
        typer.echo(f"## {index}. {entry_role} kind={kind} tool_calls={len(calls)}")
        if calls:
            names = [
                str((call.get("function") or {}).get("name") or call.get("name") or "unknown")
                for call in calls
                if isinstance(call, dict)
            ]
            typer.echo("tools: " + ", ".join(names))
        if entry_role == "tool":
            typer.echo("tool: " + str(entry.get("name") or "unknown"))
            result = entry.get("result")
            typer.echo(agent_runtime.single_line(json.dumps(result, sort_keys=True) if isinstance(result, dict) else str(result), 1200))
        elif text:
            typer.echo(text if full else agent_runtime.single_line(text, 1200))
        else:
            typer.echo("[empty]")
        typer.echo("")


@loop_app.command("stall")
def loop_stall(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt.")] = None,
    last: Annotated[int, typer.Option(help="Number of recent assistant entries to inspect.")] = 20,
) -> None:
    """Summarize recent no-tool assistant responses and fake final_report text."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    entries = load_loop_transcript(root)
    assistants = [entry for entry in entries if transcript_role(entry) == "assistant"]
    recent = assistants[-last:]
    no_tool = [entry for entry in recent if not transcript_tool_calls(entry)]
    fake_final = [entry for entry in no_tool if "final_report" in transcript_text(entry)]
    trailing_no_tool = 0
    for entry in reversed(assistants):
        if transcript_tool_calls(entry):
            break
        trailing_no_tool += 1
    typer.echo(f"transcript: {root / 'runtime_transcript.json'}")
    typer.echo(f"assistant_entries_checked: {len(recent)}")
    typer.echo(f"no_tool_assistant_entries: {len(no_tool)}")
    typer.echo(f"fake_final_report_text_entries: {len(fake_final)}")
    typer.echo(f"trailing_no_tool_assistant_entries: {trailing_no_tool}")
    if no_tool:
        typer.echo("")
        typer.echo("recent no-tool assistant messages:")
        for entry in no_tool[-5:]:
            text = transcript_text(entry).strip()
            typer.echo("- " + (agent_runtime.single_line(text, 500) if text else "[empty]"))


def run_model_loop_once(
    workspace: Path,
    *,
    title: str | None,
    proposal: str,
    force: bool,
    last_run_path: Path,
) -> None:
    if not loop_state_path(workspace).exists() or force:
        initialize_loop_files(workspace, title=title, proposal=proposal, force=force)
    elif proposal:
        path = loop_contract_path(workspace)
        path.write_text(replace_markdown_section(path.read_text(encoding="utf-8"), "Proposal", proposal), encoding="utf-8")

    state = read_loop_state(workspace)
    planner_report, planner_usage = loop_agent.generate_planner_artifacts(
        working_folder=workspace,
        proposal=proposal or loop_contract_path(workspace).read_text(encoding="utf-8"),
        attempt_id=state.get("current_attempt"),
    )
    state["status"] = "proposal-written"
    state["contract_accepted"] = False
    state["last_action"] = "run:planner"
    state.setdefault("role_usage", {})["planner"] = planner_usage
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=str(planner_report.get("summary") or "Planner wrote contract proposal."))
    append_loop_log(workspace, "planner", "planner wrote contract", str(planner_report.get("summary") or ""))

    max_contract_rounds = int(os.environ.get("LOOP_CONTRACT_MAX_ROUNDS", "3"))
    review_feedback = ""
    accepted = False
    review = ""
    for round_number in range(1, max_contract_rounds + 1):
        generator_contract_report, generator_contract_usage = loop_agent.generate_generator_contract_artifacts(
            working_folder=workspace,
            attempt_id=state.get("current_attempt"),
            review_feedback=review_feedback,
        )
        state = read_loop_state(workspace)
        state["status"] = "contract-proposed"
        state["contract_accepted"] = False
        state["last_action"] = f"run:generator-contract:{round_number}"
        state.setdefault("role_usage", {})[f"generator_contract_round_{round_number}"] = generator_contract_usage
        write_loop_state(workspace, state)
        write_loop_progress(workspace, state, note=str(generator_contract_report.get("summary") or "Generator proposed contract."))
        append_loop_log(workspace, "generator", f"generator proposed contract round {round_number}", str(generator_contract_report.get("summary") or ""))

        evaluator_contract_report, evaluator_contract_usage = loop_agent.generate_evaluator_contract_artifacts(
            working_folder=workspace,
            attempt_id=state.get("current_attempt"),
        )
        state = read_loop_state(workspace)
        accepted = bool(evaluator_contract_report.get("accepted"))
        state["status"] = "contract-accepted" if accepted else "contract-rejected"
        state["contract_accepted"] = accepted
        state["last_action"] = f"run:evaluator-contract:{round_number}"
        state.setdefault("role_usage", {})[f"evaluator_contract_round_{round_number}"] = evaluator_contract_usage
        write_loop_state(workspace, state)
        review = str(evaluator_contract_report.get("review") or "")
        required_changes = evaluator_contract_report.get("required_changes") if isinstance(evaluator_contract_report.get("required_changes"), list) else []
        if required_changes:
            review_feedback = review + "\n\nRequired changes:\n" + "\n".join(f"- {item}" for item in required_changes)
        else:
            review_feedback = review
        write_loop_progress(workspace, state, note=f"Contract review {'accepted' if accepted else 'rejected'} round {round_number}. {review}")
        append_loop_log(workspace, "evaluator", f"contract {'accepted' if accepted else 'rejected'} round {round_number}", review_feedback)
        if accepted:
            break
    if not accepted:
        write_last_run_workspace(last_run_path, workspace)
        typer.echo("status: contract-rejected")
        typer.echo(f"rounds: {max_contract_rounds}")
        typer.echo(f"review: {review}")
        return

    max_attempt_rounds = int(os.environ.get("LOOP_ATTEMPT_MAX_ROUNDS", "3"))
    evaluator_feedback = ""
    final_attempt_id = ""
    final_report_path: Path | None = None
    for attempt_round in range(1, max_attempt_rounds + 1):
        state = read_loop_state(workspace)
        attempt_id, attempt_dir, state = start_loop_attempt_state(workspace, state)
        final_attempt_id = attempt_id
        state["last_action"] = "run:start-attempt"
        write_loop_state(workspace, state)
        write_loop_progress(workspace, state, note=f"Attempt {attempt_id} started.")
        append_loop_log(workspace, "attempt", f"attempt {attempt_id} started")

        generator_report, generator_usage = loop_agent.generate_generator_implementation_artifacts(
            working_folder=workspace,
            attempt_id=attempt_id,
            evaluator_feedback=evaluator_feedback,
        )
        write_json(attempt_dir / "generator_report.json", generator_report)
        state = read_loop_state(workspace)
        state.setdefault("role_usage", {})["generator_implementation"] = generator_usage
        state.setdefault("role_usage", {})[f"generator_implementation_{attempt_id}"] = generator_usage
        state["last_action"] = "run:generator-implement"
        write_loop_state(workspace, state)
        write_loop_progress(workspace, state, note=str(generator_report.get("summary") or "Generator completed implementation pass."))
        append_loop_log(workspace, "generator", f"attempt {attempt_id} implementation", str(generator_report.get("summary") or ""))

        try:
            evaluator_report, evaluator_usage = loop_agent.generate_evaluator_attempt_artifacts(
                working_folder=workspace,
                attempt_id=attempt_id,
            )
            evaluator_report = {
                "schema_version": 1,
                "attempt": attempt_id,
                "written_at": utc_now(),
                **evaluator_report,
            }
        except agent_runtime.AgentRunError as exc:
            evaluator_usage = exc.result.usage
            evaluator_report = fallback_evaluator_report_from_error(
                attempt_id=attempt_id,
                error=exc,
                generator_report=generator_report,
            )
        report_path = attempt_dir / "evaluator_report.json"
        final_report_path = report_path
        write_json(report_path, evaluator_report)
        state = read_loop_state(workspace)
        state.setdefault("role_usage", {})["evaluator_attempt"] = evaluator_usage
        state.setdefault("role_usage", {})[f"evaluator_attempt_{attempt_id}"] = evaluator_usage
        state = apply_loop_evaluator_report(
            workspace,
            state,
            attempt_id=attempt_id,
            report=evaluator_report,
            report_path=report_path,
            action="run:evaluator-attempt",
        )
        evaluator_feedback = format_evaluator_feedback(evaluator_report)
        recommendation = str(evaluator_report.get("recommendation"))
        status = str(evaluator_report.get("status"))
        if status == "pass" or recommendation in {"restart-contract", "stop"}:
            break
        if recommendation == "restart-attempt":
            if attempt_round >= max_attempt_rounds:
                break
            reset_note = reset_loop_attempt_workspace(workspace)
            append_loop_log(workspace, "loop-runner", f"attempt {attempt_id} reset", reset_note)
            continue
        if recommendation == "continue" and attempt_round < max_attempt_rounds:
            append_loop_log(workspace, "loop-runner", f"attempt {attempt_id} continuing", "Starting another generator/evaluator attempt with evaluator feedback.")
            continue
        break

    state = read_loop_state(workspace)
    write_last_run_workspace(last_run_path, workspace)
    typer.echo(f"attempt: {final_attempt_id}")
    typer.echo(f"status: {state['status']}")
    if final_report_path is not None:
        typer.echo(f"report: {final_report_path}")


@loop_app.command("run")
def loop_run(
    ctx: typer.Context,
    title: Annotated[str | None, typer.Option(help="Problem title used when initializing a new loop.")] = None,
    proposal: Annotated[str, typer.Option(help="Planner proposal text for contract.md.")] = "",
    criteria: Annotated[str, typer.Option(help="Dry-run generator-proposed done criteria.")] = "- Define done criteria explicitly.",
    review: Annotated[str, typer.Option(help="Dry-run evaluator contract review text.")] = "Contract criteria are accepted for this local run.",
    status: Annotated[str, typer.Option(help="Dry-run evaluator status: pass or fail.")] = "pass",
    recommendation: Annotated[str, typer.Option(help="Dry-run evaluator recommendation: continue, restart-attempt, restart-contract, or stop.")] = "continue",
    bottleneck: Annotated[str | None, typer.Option(help="Dry-run evaluator bottleneck.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Use deterministic local loop plumbing instead of model roles.")] = False,
    force: Annotated[bool, typer.Option(help="Reinitialize the loop before running.")] = False,
    last_run_path: Annotated[Path, typer.Option(help="Path used by loop status/watch to find the latest loop workspace.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Run the Karpathy-style loop suite through one attempt."""
    workspace = workspace_from_ctx(ctx)
    if not dry_run:
        run_model_loop_once(workspace, title=title, proposal=proposal, force=force, last_run_path=last_run_path)
        return
    if not loop_state_path(workspace).exists() or force:
        initialize_loop_files(workspace, title=title, proposal=proposal, force=force)
    elif proposal:
        path = loop_contract_path(workspace)
        path.write_text(replace_markdown_section(path.read_text(encoding="utf-8"), "Proposal", proposal), encoding="utf-8")
    if status not in {"pass", "fail"}:
        raise typer.BadParameter("status must be pass or fail")
    if recommendation not in {"continue", "restart-attempt", "restart-contract", "stop"}:
        raise typer.BadParameter("recommendation must be continue, restart-attempt, restart-contract, or stop")

    contract_path = loop_contract_path(workspace)
    contract_path.write_text(
        replace_markdown_section(contract_path.read_text(encoding="utf-8"), "Done Criteria", criteria),
        encoding="utf-8",
    )
    append_loop_log(workspace, "generator", "contract proposed", criteria)
    state = read_loop_state(workspace)
    state["status"] = "contract-accepted"
    state["contract_accepted"] = True
    state["last_action"] = "run:contract-accepted"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=f"Contract accepted. {review}")
    append_loop_log(workspace, "evaluator", "contract accepted", review)

    attempt_id = next_loop_attempt_id(state)
    attempt_dir = loop_attempt_dir(workspace, attempt_id)
    (attempt_dir / "traces").mkdir(parents=True, exist_ok=True)
    (attempt_dir / "otel").mkdir(parents=True, exist_ok=True)
    for role in ("planner", "generator", "evaluator"):
        append_jsonl(
            attempt_dir / "traces" / f"{role}.jsonl",
            {
                "timestamp": utc_now(),
                "attempt": attempt_id,
                "role": role,
                "kind": "run",
                "content": f"local loop run {role}",
            },
        )
    append_jsonl(
        attempt_dir / "otel/spans.jsonl",
        {
            "timestamp": utc_now(),
            "name": "attempt_started",
            "attributes": {"attempt": attempt_id, "decision": "continue"},
        },
    )
    state = read_loop_state(workspace)
    state.setdefault("attempts", []).append(
        {
            "id": attempt_id,
            "status": "running",
            "started_at": utc_now(),
            "path": attempt_dir.relative_to(workspace).as_posix(),
        }
    )
    state["current_attempt"] = attempt_id
    state["status"] = "attempt-running"
    state["last_action"] = "run:start-attempt"
    write_loop_state(workspace, state)
    append_loop_log(workspace, "attempt", f"attempt {attempt_id} started")

    report = {
        "schema_version": 1,
        "attempt": attempt_id,
        "written_at": utc_now(),
        "status": status,
        "recommendation": recommendation,
        "bottleneck": bottleneck,
        "findings": [],
        "score": 1.0 if status == "pass" else 0.0,
    }
    report_path = attempt_dir / "evaluator_report.json"
    write_json(report_path, report)
    attempt_status = "passed" if status == "pass" else "failed"
    if recommendation == "restart-attempt":
        attempt_status = "restarted"
    update_loop_attempt(
        state,
        attempt_id,
        status=attempt_status,
        completed_at=utc_now(),
        evaluator_report=report_path.relative_to(workspace).as_posix(),
        **({"bottleneck": bottleneck} if bottleneck else {}),
    )
    if recommendation == "restart-attempt":
        state["status"] = "restart-attempt"
    elif recommendation == "restart-contract":
        state["status"] = "restart-contract"
        state["contract_accepted"] = False
    elif status == "pass":
        state["status"] = "passed"
    elif recommendation == "stop":
        state["status"] = "stopped"
    else:
        state["status"] = "attempt-failed"
    state["current_attempt"] = None
    state["last_action"] = f"run:evaluator-report:{recommendation}"
    if bottleneck:
        state["bottleneck"] = bottleneck
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=f"Run completed with status={status}, recommendation={recommendation}.")
    append_loop_log(workspace, "evaluator", f"attempt {attempt_id} {status}", f"recommendation: {recommendation}")
    write_last_run_workspace(last_run_path, workspace)
    typer.echo(f"attempt: {attempt_id}")
    typer.echo(f"status: {state['status']}")
    typer.echo(f"report: {report_path}")


@loop_app.command("log")
def loop_log(
    ctx: typer.Context,
    op: Annotated[str, typer.Option(help="Operation label for the log heading.")] = "note",
    title: Annotated[str, typer.Option(help="Short log title.")] = "manual note",
    body: Annotated[str, typer.Option(help="Optional log body.")] = "",
) -> None:
    """Append an entry to .workflow/loop/log.md."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    append_loop_log(workspace, op, title, body)
    state = read_loop_state(workspace)
    state["last_action"] = op
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=title)
    typer.echo(f"log: {loop_log_path(workspace)}")


@loop_app.command("proposal")
def loop_proposal(
    ctx: typer.Context,
    proposal: Annotated[str | None, typer.Option(help="Problem proposal text.")] = None,
    proposal_file: Annotated[Path | None, typer.Option(help="Problem proposal Markdown file.")] = None,
) -> None:
    """Write the planner problem proposal into contract.md."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    proposal_text = read_body_arg_or_file(proposal, proposal_file)
    path = loop_contract_path(workspace)
    path.write_text(replace_markdown_section(path.read_text(encoding="utf-8"), "Proposal", proposal_text), encoding="utf-8")
    state = read_loop_state(workspace)
    state["status"] = "proposal-written"
    state["contract_accepted"] = False
    state["last_action"] = "proposal"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note="Problem proposal updated.")
    append_loop_log(workspace, "planner", "proposal updated")
    typer.echo(f"contract: {path}")


@loop_app.command("planner")
def loop_planner(
    ctx: typer.Context,
    proposal: Annotated[str, typer.Option(help="Problem proposal text for the planner.")] = "",
) -> None:
    """Run the real planner model role to write contract.md."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    report, usage = loop_agent.generate_planner_artifacts(
        working_folder=workspace,
        proposal=proposal or loop_contract_path(workspace).read_text(encoding="utf-8"),
        attempt_id=state.get("current_attempt"),
    )
    state["status"] = "proposal-written"
    state["contract_accepted"] = False
    state["last_action"] = "planner"
    state.setdefault("role_usage", {})["planner"] = usage
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=str(report.get("summary") or "Planner wrote contract proposal."))
    append_loop_log(workspace, "planner", "planner wrote contract", str(report.get("summary") or ""))
    typer.echo(f"contract: {loop_contract_path(workspace)}")
    typer.echo(f"summary: {report.get('summary')}")


@loop_app.command("propose-contract")
def loop_propose_contract(
    ctx: typer.Context,
    body: Annotated[str | None, typer.Option(help="Done criteria text.")] = None,
    body_file: Annotated[Path | None, typer.Option(help="Done criteria Markdown file.")] = None,
) -> None:
    """Write generator-proposed done criteria into contract.md."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    criteria = read_body_arg_or_file(body, body_file)
    path = loop_contract_path(workspace)
    path.write_text(replace_markdown_section(path.read_text(encoding="utf-8"), "Done Criteria", criteria), encoding="utf-8")
    state = read_loop_state(workspace)
    state["status"] = "contract-proposed"
    state["contract_accepted"] = False
    state["last_action"] = "propose-contract"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note="Done criteria proposed.")
    append_loop_log(workspace, "generator", "contract proposed")
    typer.echo(f"contract: {path}")


@loop_app.command("generator-contract")
def loop_generator_contract(ctx: typer.Context) -> None:
    """Run the real generator model role to propose done criteria."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    report, usage = loop_agent.generate_generator_contract_artifacts(
        working_folder=workspace,
        attempt_id=state.get("current_attempt"),
    )
    state["status"] = "contract-proposed"
    state["contract_accepted"] = False
    state["last_action"] = "generator-contract"
    state.setdefault("role_usage", {})["generator_contract"] = usage
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=str(report.get("summary") or "Generator proposed contract criteria."))
    append_loop_log(workspace, "generator", "generator proposed contract", str(report.get("summary") or ""))
    typer.echo(f"contract: {loop_contract_path(workspace)}")
    typer.echo(f"feature_list: {loop_feature_list_path(workspace)}")
    typer.echo(f"summary: {report.get('summary')}")


@loop_app.command("review-contract")
def loop_review_contract(
    ctx: typer.Context,
    status: Annotated[str, typer.Option(help="Review status: accepted or rejected.")] = "rejected",
    body: Annotated[str | None, typer.Option(help="Evaluator review text.")] = None,
    body_file: Annotated[Path | None, typer.Option(help="Evaluator review Markdown file.")] = None,
) -> None:
    """Record evaluator contract review in log.md and progress.md."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    if status not in {"accepted", "rejected"}:
        raise typer.BadParameter("status must be accepted or rejected")
    review = read_body_arg_or_file(body, body_file)
    state = read_loop_state(workspace)
    state["status"] = "contract-accepted" if status == "accepted" else "contract-rejected"
    state["contract_accepted"] = status == "accepted"
    state["last_action"] = f"review-contract:{status}"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=f"Contract review {status}. {review.strip()}")
    append_loop_log(workspace, "evaluator", f"contract {status}", review)
    typer.echo(f"contract_review: {status}")


@loop_app.command("evaluator-contract")
def loop_evaluator_contract(ctx: typer.Context) -> None:
    """Run the real evaluator model role to accept or reject the proposed contract."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    report, usage = loop_agent.generate_evaluator_contract_artifacts(
        working_folder=workspace,
        attempt_id=state.get("current_attempt"),
    )
    accepted = bool(report.get("accepted"))
    state["status"] = "contract-accepted" if accepted else "contract-rejected"
    state["contract_accepted"] = accepted
    state["last_action"] = "evaluator-contract"
    state.setdefault("role_usage", {})["evaluator_contract"] = usage
    write_loop_state(workspace, state)
    review = str(report.get("review") or "")
    required_changes = report.get("required_changes") if isinstance(report.get("required_changes"), list) else []
    note = f"Contract review {'accepted' if accepted else 'rejected'}. {review}"
    if required_changes:
        note += " Required changes: " + "; ".join(str(item) for item in required_changes)
    write_loop_progress(workspace, state, note=note)
    append_loop_log(workspace, "evaluator", f"contract {'accepted' if accepted else 'rejected'}", note)
    typer.echo(f"contract_review: {'accepted' if accepted else 'rejected'}")
    typer.echo(f"review: {review}")


@loop_app.command("accept-contract")
def loop_accept_contract(ctx: typer.Context) -> None:
    """Mark contract.md as accepted for implementation."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    state["status"] = "contract-accepted"
    state["contract_accepted"] = True
    state["last_action"] = "accept-contract"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note="Contract accepted.")
    append_loop_log(workspace, "contract", "contract accepted")
    typer.echo("contract_accepted: true")


@loop_app.command("start-attempt")
def loop_start_attempt(ctx: typer.Context) -> None:
    """Start a new implementation attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    attempt_id, attempt_dir, state = start_loop_attempt_state(workspace, state)
    state["last_action"] = "start-attempt"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=f"Attempt {attempt_id} started.")
    append_loop_log(workspace, "attempt", f"attempt {attempt_id} started")
    typer.echo(f"attempt: {attempt_id}")
    typer.echo(f"path: {attempt_dir}")


@loop_app.command("generator-implement")
def loop_generator_implement(ctx: typer.Context) -> None:
    """Run the real generator model role for the active implementation attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    report, usage = loop_agent.generate_generator_implementation_artifacts(
        working_folder=workspace,
        attempt_id=attempt_id,
    )
    report_path = loop_attempt_dir(workspace, attempt_id) / "generator_report.json"
    write_json(report_path, report)
    state.setdefault("role_usage", {})["generator_implementation"] = usage
    state["last_action"] = "generator-implement"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=str(report.get("summary") or "Generator completed implementation pass."))
    append_loop_log(workspace, "generator", f"attempt {attempt_id} implementation", str(report.get("summary") or ""))
    typer.echo(f"report: {report_path}")
    typer.echo(f"summary: {report.get('summary')}")


@loop_app.command("complete-attempt")
def loop_complete_attempt(
    ctx: typer.Context,
    result: Annotated[str, typer.Option(help="Evaluator result: pass or fail.")] = "fail",
    bottleneck: Annotated[str | None, typer.Option(help="Current bottleneck identified by evaluator.")] = None,
) -> None:
    """Complete the active attempt from evaluator evidence."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    if result not in {"pass", "fail"}:
        raise typer.BadParameter("result must be pass or fail")
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    attempt_updates: dict[str, Any] = {
        "status": "passed" if result == "pass" else "failed",
        "completed_at": utc_now(),
    }
    if bottleneck:
        attempt_updates["bottleneck"] = bottleneck
    update_loop_attempt(state, attempt_id, **attempt_updates)
    state["current_attempt"] = None
    state["status"] = "passed" if result == "pass" else "attempt-failed"
    state["last_action"] = f"complete-attempt:{result}"
    if bottleneck:
        state["bottleneck"] = bottleneck
    write_loop_state(workspace, state)
    note = f"Attempt {attempt_id} completed with result={result}."
    if bottleneck:
        note += f" Bottleneck: {bottleneck}."
    write_loop_progress(workspace, state, note=note)
    append_loop_log(workspace, "evaluation", f"attempt {attempt_id} {result}", note)
    typer.echo(f"attempt: {attempt_id}")
    typer.echo(f"result: {result}")


@loop_app.command("evaluator-attempt")
def loop_evaluator_attempt(ctx: typer.Context) -> None:
    """Run the real evaluator model role for the active implementation attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    report, usage = loop_agent.generate_evaluator_attempt_artifacts(
        working_folder=workspace,
        attempt_id=attempt_id,
    )
    report = {
        "schema_version": 1,
        "attempt": attempt_id,
        "written_at": utc_now(),
        **report,
    }
    report_path = loop_attempt_dir(workspace, attempt_id) / "evaluator_report.json"
    write_json(report_path, report)
    state.setdefault("role_usage", {})["evaluator_attempt"] = usage
    state = apply_loop_evaluator_report(
        workspace,
        state,
        attempt_id=attempt_id,
        report=report,
        report_path=report_path,
        action="evaluator-attempt",
    )
    typer.echo(f"report: {report_path}")
    typer.echo(f"recommendation: {report.get('recommendation')}")
    typer.echo(f"status: {state.get('status')}")


@loop_app.command("evaluator-report")
def loop_evaluator_report(
    ctx: typer.Context,
    status: Annotated[str, typer.Option(help="Evaluator status: pass or fail.")] = "fail",
    recommendation: Annotated[str, typer.Option(help="Recommended control action: continue, restart-attempt, restart-contract, or stop.")] = "continue",
    bottleneck: Annotated[str | None, typer.Option(help="Current bottleneck.")] = None,
    finding: Annotated[list[str] | None, typer.Option("--finding", help="Evaluator finding. Repeat for multiple findings.")] = None,
    score: Annotated[float | None, typer.Option(help="Optional subjective/objective score from 0.0 to 1.0.")] = None,
) -> None:
    """Write an evaluator report for the active attempt and apply its recommendation."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    if status not in {"pass", "fail"}:
        raise typer.BadParameter("status must be pass or fail")
    if recommendation not in {"continue", "restart-attempt", "restart-contract", "stop"}:
        raise typer.BadParameter("recommendation must be continue, restart-attempt, restart-contract, or stop")
    if score is not None and (score < 0 or score > 1):
        raise typer.BadParameter("score must be between 0.0 and 1.0")
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    report = {
        "schema_version": 1,
        "attempt": attempt_id,
        "written_at": utc_now(),
        "status": status,
        "recommendation": recommendation,
        "bottleneck": bottleneck,
        "findings": finding or [],
        "score": score,
    }
    report_path = loop_attempt_dir(workspace, attempt_id) / "evaluator_report.json"
    write_json(report_path, report)
    apply_loop_evaluator_report(
        workspace,
        state,
        attempt_id=attempt_id,
        report=report,
        report_path=report_path,
        action="evaluator-report",
    )
    typer.echo(f"report: {report_path}")
    typer.echo(f"recommendation: {recommendation}")


@loop_app.command("trace-event")
def loop_trace_event(
    ctx: typer.Context,
    role: Annotated[str, typer.Option(help="Model role: planner, generator, or evaluator.")],
    content: Annotated[str, typer.Option(help="Transcript content to append.")],
    kind: Annotated[str, typer.Option(help="Event kind, such as message, tool_call, or decision.")] = "message",
) -> None:
    """Append a grep-friendly JSONL trace event for the active attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    if role not in {"planner", "generator", "evaluator"}:
        raise typer.BadParameter("role must be planner, generator, or evaluator")
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    path = loop_attempt_dir(workspace, attempt_id) / "traces" / f"{role}.jsonl"
    payload = {
        "timestamp": utc_now(),
        "attempt": attempt_id,
        "role": role,
        "kind": kind,
        "content": content,
    }
    append_jsonl(path, payload)
    append_loop_log(workspace, "trace", f"{role} {kind}", f"attempt: {attempt_id}\ntrace: {path.relative_to(workspace).as_posix()}")
    typer.echo(f"trace: {path}")


@loop_app.command("otel-event")
def loop_otel_event(
    ctx: typer.Context,
    name: Annotated[str, typer.Option(help="OpenTelemetry-style event name.")],
    role: Annotated[str | None, typer.Option(help="Optional role associated with the event.")] = None,
    decision: Annotated[str | None, typer.Option(help="Optional loop decision.")] = None,
    cost: Annotated[float | None, typer.Option(help="Optional cost attribute.")] = None,
    tokens: Annotated[int | None, typer.Option(help="Optional token count attribute.")] = None,
) -> None:
    """Append a local OpenTelemetry-style event for the active attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    if role is not None and role not in {"planner", "generator", "evaluator"}:
        raise typer.BadParameter("role must be planner, generator, or evaluator")
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    path = loop_attempt_dir(workspace, attempt_id) / "otel" / "spans.jsonl"
    attributes: dict[str, Any] = {"attempt": attempt_id}
    if role:
        attributes["role"] = role
    if decision:
        attributes["decision"] = decision
    if cost is not None:
        attributes["cost"] = cost
    if tokens is not None:
        attributes["tokens"] = tokens
    payload = {
        "timestamp": utc_now(),
        "name": name,
        "attributes": attributes,
    }
    append_jsonl(path, payload)
    typer.echo(f"otel: {path}")


@loop_app.command("restart-attempt")
def loop_restart_attempt(
    ctx: typer.Context,
    reason: Annotated[str, typer.Option(help="Evaluator evidence for restart.")] = "bad trajectory",
) -> None:
    """Record a restart-attempt decision and clear the active attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    attempt_id = state.get("current_attempt")
    if attempt_id:
        for attempt in state.get("attempts", []):
            if isinstance(attempt, dict) and attempt.get("id") == attempt_id:
                attempt["status"] = "restarted"
                attempt["completed_at"] = utc_now()
                attempt["restart_reason"] = reason
                break
    state["current_attempt"] = None
    state["status"] = "restart-attempt"
    state["last_action"] = "restart-attempt"
    state["bottleneck"] = "bad_attempt"
    write_loop_state(workspace, state)
    note = f"Restart attempt requested. Reason: {reason}"
    write_loop_progress(workspace, state, note=note)
    append_loop_log(workspace, "restart-attempt", "attempt restart", note)
    typer.echo("restart-attempt: recorded")


@loop_app.command("restart-contract")
def loop_restart_contract(
    ctx: typer.Context,
    reason: Annotated[str, typer.Option(help="Reason the contract must be revised.")] = "contract is wrong",
) -> None:
    """Record a restart-contract decision."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    state["current_attempt"] = None
    state["status"] = "restart-contract"
    state["contract_accepted"] = False
    state["last_action"] = "restart-contract"
    state["bottleneck"] = "contract"
    write_loop_state(workspace, state)
    note = f"Restart contract requested. Reason: {reason}"
    write_loop_progress(workspace, state, note=note)
    append_loop_log(workspace, "restart-contract", "contract restart", note)
    typer.echo("restart-contract: recorded")


@run_app.command("spec")
def run_spec(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Run the Spec Agent."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    issue = state["issue"]
    set_stage_status(workspace, state, "spec", "running")
    try:
        with in_workspace(workspace):
            artifact_dir, _contract, usage = spec_agent.generate_spec_artifacts(
                issue_number=int(issue["number"]),
                title=issue["title"],
                body=issue["body"],
                author=issue.get("user", {}).get("login", "local"),
                generated_at=utc_now(),
                artifact_root=Path("docs/specs"),
                sidecar_root=Path(".workflow/artifacts/specs"),
                working_folder=Path("."),
            )
    except Exception as exc:
        set_stage_status(workspace, state, "spec", "failed", error=str(exc))
        raise
    state["artifacts"]["spec"] = {"artifact_dir": artifact_dir.as_posix(), "contract": (artifact_dir / "contract.json").as_posix(), "usage": usage}
    set_stage_status(workspace, state, "spec", "passed", contract=(artifact_dir / "contract.json").as_posix(), cost=usage.get("cost"))
    save_task_state(workspace, state)
    typer.echo(f"spec artifacts: {artifact_dir}")
    typer.echo("next: hooky approve spec")


@approve_app.command("spec")
def approve_spec(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Approve the current Spec Agent output."""
    approve_stage(ctx, "spec", "next: hooky run builder", task)


@approve_app.command("test")
def approve_test(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Approve the current Test Agent output."""
    approve_stage(ctx, "test", "next: hooky run builder", task)


def approve_stage(ctx: typer.Context, stage: str, next_message: str, task: str | None) -> None:
    workspace = workspace_from_ctx(ctx)
    state = load_task_state(workspace, task)
    if stage not in state.get("artifacts", {}):
        raise typer.BadParameter(f"cannot approve {stage}: stage has not run")
    state.setdefault("approvals", {})[stage] = {"approved_at": utc_now()}
    save_task_state(workspace, state)
    append_pipeline_event(workspace, "approval", task=state.get("task_id"), stage=stage, status="approved")
    typer.echo(f"approved: {stage}")
    typer.echo(next_message)


@run_app.command("test")
def run_test(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Run the Test Agent."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    require_approval(state, "spec")
    spec_contract = Path(state["artifacts"]["spec"]["contract"])
    set_stage_status(workspace, state, "test", "running")
    try:
        with in_workspace(workspace):
            approved_spec = spec_agent.read_json(spec_contract)
            report_dir, contract, usage = test_agent.generate_test_artifacts(
                approved_spec=approved_spec,
                spec_source=spec_contract.as_posix(),
                generated_at=utc_now(),
                working_folder=Path("."),
                project_root=Path("."),
                report_root=Path(".workflow/artifacts/test-agent"),
            )
    except Exception as exc:
        set_stage_status(workspace, state, "test", "failed", error=str(exc))
        raise
    state["artifacts"]["test"] = {
        "report_dir": report_dir.as_posix(),
        "contract": (report_dir / "contract.json").as_posix(),
        "test_files": [item["path"] for item in contract.get("test_files", [])],
        "fixtures": [item["path"] for item in contract.get("fixtures", [])],
        "usage": usage,
    }
    set_stage_status(workspace, state, "test", "passed", contract=(report_dir / "contract.json").as_posix(), report_dir=report_dir.as_posix(), cost=usage.get("cost"))
    save_task_state(workspace, state)
    typer.echo(f"test report: {report_dir}")
    typer.echo("next: hooky approve test")


@run_app.command("builder")
def run_builder(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Run the Builder Agent."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    require_approval(state, "spec")
    set_stage_status(workspace, state, "builder", "running")
    before_builder = snapshot_project_files(workspace)
    try:
        with in_workspace(workspace):
            generated_at = utc_now()
            report_dir, contract, usage = builder_agent.generate_build_artifacts(working_folder=Path("."), generated_at=generated_at)
    except Exception as exc:
        set_stage_status(workspace, state, "builder", "failed", error=str(exc))
        raise
    builder_contract_path = report_dir / "contract.json"
    proposal = create_change_proposal(
        workspace=workspace,
        state=state,
        builder_contract_path=builder_contract_path,
        generated_at=generated_at,
        before_snapshot=before_builder,
    )
    proposal_dir = change_proposal_dir(workspace, state["task_id"])
    state["artifacts"]["builder"] = {
        "report_dir": report_dir.as_posix(),
        "contract": builder_contract_path.as_posix(),
        "proposal_dir": display_workspace_path(proposal_dir, workspace),
        "proposal": display_workspace_path(proposal_dir / "proposal.json", workspace),
        "patch": display_workspace_path(proposal_dir / "patch.diff", workspace),
        "summary": display_workspace_path(proposal_dir / "summary.md", workspace),
        "hosted_pr_url": proposal["git"].get("hosted_pr_url"),
        "usage": usage,
    }
    builder_passed = contract.get("tests_passing") is True
    builder_status = "passed" if builder_passed else "failed"
    failure_summary = "; ".join(str(item) for item in contract.get("failures_remaining", [])[:3])
    set_stage_status(
        workspace,
        state,
        "builder",
        builder_status,
        contract=builder_contract_path.as_posix(),
        proposal=display_workspace_path(proposal_dir / "proposal.json", workspace),
        report_dir=report_dir.as_posix(),
        cost=usage.get("cost"),
        error=None if builder_passed else failure_summary or "Builder reported tests_passing=false",
    )
    save_task_state(workspace, state)
    typer.echo(f"builder proposal: {proposal_dir}")
    typer.echo(f"builder report: {report_dir}")
    if not builder_passed:
        raise RuntimeError(f"builder reported tests_passing=false: {failure_summary or 'see builder report'}")
    typer.echo("next: hooky run verifier")


@run_app.command("verifier")
def run_verifier(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Run the Verifier Agent."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    require_artifact(state, "builder")
    set_stage_status(workspace, state, "verifier", "running")
    try:
        with in_workspace(workspace):
            report_dir, contract, usage = verifier_agent.generate_verification_artifacts(working_folder=Path("."), generated_at=utc_now())
    except Exception as exc:
        set_stage_status(workspace, state, "verifier", "failed", error=str(exc))
        raise
    verifier_contract_path = report_dir / "contract.json"
    state["artifacts"]["verifier"] = {"report_dir": report_dir.as_posix(), "contract": verifier_contract_path.as_posix(), "usage": usage}
    verifier_passed = contract.get("status") == "pass"
    verifier_status = "passed" if verifier_passed else "failed"
    failure_summary = str(contract.get("summary") or "Verifier reported status!=pass")
    set_stage_status(
        workspace,
        state,
        "verifier",
        verifier_status,
        contract=verifier_contract_path.as_posix(),
        report_dir=report_dir.as_posix(),
        cost=usage.get("cost"),
        safe_to_open_pr=contract.get("safe_to_open_pr"),
        error=None if verifier_passed else failure_summary,
    )
    save_task_state(workspace, state)
    typer.echo(f"verifier report: {report_dir}")
    if not verifier_passed:
        raise RuntimeError(f"verifier reported status={contract.get('status')}: {failure_summary}")
    typer.echo("next: hooky run eval")


@run_app.command("eval")
def run_eval(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Run the Eval Agent."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    set_stage_status(workspace, state, "eval", "running")
    try:
        with in_workspace(workspace):
            report_dir, contract, usage = eval_agent.generate_eval_artifacts(working_folder=Path("."), generated_at=utc_now())
    except Exception as exc:
        set_stage_status(workspace, state, "eval", "failed", error=str(exc))
        raise
    eval_contract_path = report_dir / "contract.json"
    state["artifacts"]["eval"] = {"report_dir": report_dir.as_posix(), "contract": eval_contract_path.as_posix(), "usage": usage}
    eval_status = str(contract.get("status") or "fail")
    stage_status = "passed" if eval_status == "pass" else eval_status
    set_stage_status(
        workspace,
        state,
        "eval",
        stage_status,
        contract=eval_contract_path.as_posix(),
        report_dir=report_dir.as_posix(),
        cost=usage.get("cost"),
        safe_to_merge=contract.get("safe_to_merge"),
    )
    remediation_plan = maybe_create_remediation_plan(workspace, state, contract, eval_contract_path)
    save_task_state(workspace, state)
    typer.echo(f"eval report: {report_dir}")
    if remediation_plan is not None:
        typer.echo(f"remediation plan: {remediation_plan.relative_to(workspace).as_posix()}")
        typer.echo(f"next: uv run hooky -C {workspace.as_posix()} run remediation --auto-approve")
    else:
        typer.echo("next: hooky report")


def run_stage_sequence(ctx: typer.Context, *, start_stage: str, auto_approve: bool, task: str | None) -> list[str]:
    failures: list[str] = []
    stages = PIPELINE_STAGES[PIPELINE_STAGES.index(start_stage) :]

    if "spec" in stages:
        try:
            run_spec(ctx, task=task)
            if auto_approve:
                approve_stage(ctx, "spec", "running builder...", task)
        except Exception as exc:
            failures.append(f"spec failed: {exc}")

    if not failures and "builder" in stages:
        try:
            run_builder(ctx, task=task)
        except Exception as exc:
            failures.append(f"builder failed: {exc}")

    if not failures and "verifier" in stages:
        try:
            run_verifier(ctx, task=task)
        except Exception as exc:
            failures.append(f"verifier failed: {exc}")

    if "eval" in stages:
        try:
            run_eval(ctx, task=task)
        except Exception as exc:
            failures.append(f"eval failed: {exc}")

    return failures


@run_app.command("remediation")
def run_remediation(
    ctx: typer.Context,
    auto_approve: Annotated[bool, typer.Option(help="Automatically approve remediated spec gates.")] = False,
    task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None,
) -> None:
    """Resume the pipeline from the latest eval root-cause stage."""
    if not auto_approve:
        raise typer.BadParameter("run remediation requires --auto-approve. Use individual `hooky run ...` commands for human-gated flow.")
    workspace = workspace_from_ctx(ctx)
    state = load_task_state(workspace, task)
    plan_path = current_remediation_path(workspace)
    if not plan_path.exists():
        raise typer.BadParameter("no remediation plan exists. Run eval first.")
    plan = read_json(plan_path)
    start_stage = str(plan.get("resume_from_stage") or plan.get("root_cause_stage") or "")
    if start_stage == "test":
        start_stage = "builder"
    if start_stage not in PIPELINE_STAGES:
        raise typer.BadParameter(f"remediation plan has invalid resume stage: {start_stage or 'missing'}")
    recover_artifacts_for_resume(workspace, state, start_stage, auto_approve=auto_approve)
    state = load_task_state(workspace, task)
    set_pipeline_status(
        workspace,
        state,
        "running",
        remediation_plan=plan_path.relative_to(workspace).as_posix(),
        resume_from=start_stage,
        resume_from_phase=phase_for_stage(start_stage),
    )
    failures = run_stage_sequence(ctx, start_stage=start_stage, auto_approve=auto_approve, task=task)
    if failures:
        message = "; ".join(failures)
        state = load_task_state(workspace, task)
        set_pipeline_status(workspace, state, "failed", error=message)
        raise RuntimeError(message)
    state = load_task_state(workspace, task)
    if not pipeline_complete(state):
        missing = [
            stage
            for stage in PIPELINE_STAGES
            if state.get("stage_status", {}).get(stage, {}).get("status") != "passed"
        ]
        message = "pipeline cannot pass before stages pass: " + ", ".join(missing)
        set_pipeline_status(workspace, state, "failed", error=message)
        raise RuntimeError(message)
    set_pipeline_status(workspace, state, "passed")


@run_app.command("pipeline")
def run_pipeline(
    ctx: typer.Context,
    auto_approve: Annotated[bool, typer.Option(help="Automatically approve spec gate.")] = False,
    max_remediations: Annotated[int, typer.Option(help="Maximum automatic remediation attempts after Eval creates a remediation plan.")] = 2,
    task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None,
) -> None:
    """Run all stages for the current task."""
    if not auto_approve:
        raise typer.BadParameter("run pipeline requires --auto-approve. Use individual `hooky run ...` commands for human-gated flow.")
    workspace = workspace_from_ctx(ctx)
    state = load_task_state(workspace, task)
    set_pipeline_status(workspace, state, "running", max_remediations=max_remediations)
    run_pipeline_with_remediation_loop(ctx, workspace=workspace, task=task, auto_approve=auto_approve, max_remediations=max_remediations)


@app.command()
def start(
    ctx: typer.Context,
    auto_approve: Annotated[bool, typer.Option(help="Automatically approve spec gate.")] = True,
    max_remediations: Annotated[int, typer.Option(help="Maximum automatic remediation attempts after Eval creates a remediation plan.")] = 2,
    task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None,
    skill: Annotated[list[str] | None, typer.Option("--skill", help="Preselect an agent skill by name for this run. Repeat for multiple skills.")] = None,
    last_run_path: Annotated[Path, typer.Option(help="Path used by `hooky watch` to find the latest workspace.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Start the standard pipeline and register this workspace for `hooky watch`."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    if skill:
        os.environ["HOOKY_ACTIVE_SKILLS"] = ",".join(skill)
    write_last_run_workspace(last_run_path, workspace)
    typer.echo(f"workspace: {workspace}")
    if skill:
        typer.echo(f"skills: {', '.join(skill)}")
    typer.echo(f"watch: uv run hooky watch")
    run_pipeline(ctx, auto_approve=auto_approve, max_remediations=max_remediations, task=task)


@app.command()
def watch(
    ctx: typer.Context,
    stage: Annotated[str, typer.Argument(help="Stage to follow: pipeline, spec, builder, verifier, or eval.")] = "pipeline",
    task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None,
    last_run_path: Annotated[Path, typer.Option(help="Path written by `hooky start`.")] = DEFAULT_LAST_RUN_PATH,
    tail_path: Annotated[bool, typer.Option("--tail-path", help="Only print the append-only runtime log path.")] = False,
    follow: Annotated[bool, typer.Option("--follow/--no-follow", "-f", help="Follow the append-only runtime log.")] = True,
) -> None:
    """Follow the latest started workspace's runtime log."""
    workspace = workspace_from_last_run(last_run_path) or workspace_from_ctx(ctx)
    ctx.obj["workspace"] = workspace
    if not tail_path:
        typer.echo(f"workspace: {workspace}")
    trace(ctx, stage=stage, task=task, tail_path=tail_path, follow=follow)


def run_pipeline_with_remediation_loop(
    ctx: typer.Context,
    *,
    workspace: Path,
    task: str | None,
    auto_approve: bool,
    max_remediations: int,
) -> None:
    attempts = 0
    last_failures: list[str] = []
    failures = run_stage_sequence(ctx, start_stage="spec", auto_approve=auto_approve, task=task)
    last_failures = failures
    while True:
        state = load_task_state(workspace, task)
        if pipeline_complete(state):
            set_pipeline_status(workspace, state, "passed", remediation_attempts=attempts)
            return

        plan_path = current_remediation_path(workspace)
        if failures and not plan_path.exists():
            message = "; ".join(failures)
            set_pipeline_status(workspace, state, "failed", error=message, remediation_attempts=attempts)
            raise RuntimeError(message)
        if not plan_path.exists():
            fail_incomplete_pipeline(workspace, state, attempts, last_failures)

        if attempts >= max_remediations:
            missing = incomplete_pipeline_stages(state)
            message = (
                f"pipeline remediation limit reached ({max_remediations}) before stages pass"
                + (": " + ", ".join(missing) if missing else "")
            )
            set_pipeline_status(workspace, state, "failed", error=message, remediation_attempts=attempts)
            raise RuntimeError(message)

        plan = read_json(plan_path)
        start_stage = str(plan.get("resume_from_stage") or plan.get("root_cause_stage") or "")
        if start_stage not in PIPELINE_STAGES:
            message = f"remediation plan has invalid resume stage: {start_stage or 'missing'}"
            set_pipeline_status(workspace, state, "failed", error=message, remediation_attempts=attempts)
            raise RuntimeError(message)
        attempts += 1
        set_pipeline_status(
            workspace,
            state,
            "running",
            remediation_attempts=attempts,
            remediation_plan=plan_path.relative_to(workspace).as_posix(),
            resume_from=start_stage,
            resume_from_phase=phase_for_stage(start_stage),
        )
        result = run_remediation_subprocess(workspace=workspace, task=task, auto_approve=auto_approve)
        state = load_task_state(workspace, task)
        if pipeline_complete(state):
            set_pipeline_status(workspace, state, "passed", remediation_attempts=attempts)
            return
        if result.returncode != 0:
            last_failures = [f"remediation attempt {attempts} failed with exit code {result.returncode}"]
        else:
            last_failures = []


def run_remediation_subprocess(*, workspace: Path, task: str | None, auto_approve: bool) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        (SCRIPT_DIR / "hooky_cli.py").as_posix(),
        "-C",
        workspace.as_posix(),
        "run",
        "remediation",
    ]
    if auto_approve:
        command.append("--auto-approve")
    if task:
        command.extend(["--task", task])
    append_pipeline_event(
        workspace,
        "remediation_process",
        status="started",
        command=" ".join(shlex.quote(part) for part in command),
    )
    result = subprocess.run(command, cwd=REPO_ROOT, text=True)
    append_pipeline_event(
        workspace,
        "remediation_process",
        status="exited",
        returncode=result.returncode,
    )
    return result


def incomplete_pipeline_stages(state: dict[str, Any]) -> list[str]:
    return [
        stage
        for stage in PIPELINE_STAGES
        if state.get("stage_status", {}).get(stage, {}).get("status") != "passed"
    ]


def fail_incomplete_pipeline(workspace: Path, state: dict[str, Any], attempts: int, failures: list[str]) -> None:
    missing = incomplete_pipeline_stages(state)
    message = "pipeline cannot pass before stages pass: " + ", ".join(missing)
    if failures:
        message += "; failures: " + "; ".join(failures)
    set_pipeline_status(workspace, state, "failed", error=message, remediation_attempts=attempts)
    raise RuntimeError(message)


def next_action_for_state(state: dict[str, Any]) -> str:
    pipeline_state = state.get("pipeline_status") if isinstance(state.get("pipeline_status"), dict) else {}
    pipeline_status = pipeline_state.get("status") if isinstance(pipeline_state, dict) else None
    if pipeline_status == "running":
        return "uv run hooky watch"
    if pipeline_status == "passed":
        return "uv run hooky report"
    if pipeline_status in {"failed", "interrupted"}:
        return "uv run hooky status; inspect the trace shown above"
    statuses = state.get("stage_status") if isinstance(state.get("stage_status"), dict) else {}
    if not isinstance(statuses.get("spec"), dict) or statuses["spec"].get("status") != "passed":
        return "uv run hooky start"
    if "spec" not in state.get("approvals", {}):
        return "uv run hooky approve spec"
    return "uv run hooky start"


def require_approval(state: dict[str, Any], stage: str) -> None:
    if stage not in state.get("approvals", {}):
        raise typer.BadParameter(f"{stage} is not approved. Run `hooky approve {stage}` first.")


def require_artifact(state: dict[str, Any], stage: str) -> None:
    if stage not in state.get("artifacts", {}):
        raise typer.BadParameter(f"{stage} has not run yet.")


def runtime_log_dir(workspace: Path, stage: str, state: dict[str, Any]) -> Path:
    if stage == "pipeline":
        return workflow_dir(workspace)
    if stage == "spec":
        return workspace / ".workflow/artifacts/specs/_runtime"
    artifact = state.get("artifacts", {}).get(stage, {})
    if artifact.get("report_dir"):
        return workspace / artifact["report_dir"]
    defaults = {
        "test": ".workflow/artifacts/test-agent",
        "builder": ".workflow/artifacts/builder-agent",
        "verifier": ".workflow/artifacts/verifier-agent",
        "eval": ".workflow/artifacts/eval-agent",
    }
    if stage in defaults:
        return workspace / defaults[stage]
    raise typer.BadParameter(f"unknown stage: {stage}")


def stage_runtime_events_path(workspace: Path, stage: str, state: dict[str, Any]) -> str:
    return (runtime_log_dir(workspace, stage, state) / "runtime_events.log").relative_to(workspace).as_posix()


def follow_runtime_log(workspace: Path, stage: str, state: dict[str, Any], tail_file: Path) -> None:
    position = 0

    def read_new() -> bool:
        nonlocal position
        with tail_file.open("r", encoding="utf-8") as handle:
            handle.seek(position)
            chunk = handle.read()
            position = handle.tell()
        if chunk:
            typer.echo(chunk.rstrip("\n"))
            return True
        return False

    if not tail_file.exists():
        tail_file.parent.mkdir(parents=True, exist_ok=True)
        tail_file.write_text("", encoding="utf-8")
    read_new()
    try:
        while True:
            time.sleep(1)
            read_new()
    except KeyboardInterrupt:
        return


@app.command()
def status(
    ctx: typer.Context,
    task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None,
    last_run_path: Annotated[Path, typer.Option(help="Path written by `hooky start`; used when the current directory has no task.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Show the current task's pipeline state and artifact locations."""
    workspace = workspace_for_status(ctx, last_run_path)
    ctx.obj["workspace"] = workspace
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    refresh_interrupted_stages(workspace, state)
    typer.echo(f"workspace: {workspace}")
    typer.echo(f"task: {state['task_id']}")
    typer.echo(f"title: {state['title']}")
    pipeline_state = state.get("pipeline_status") if isinstance(state.get("pipeline_status"), dict) else {}
    pipeline_status = pipeline_state.get("status", "not-run") if isinstance(pipeline_state, dict) else "not-run"
    if pipeline_status == "not-run" and any(state.get("stage_status", {}).get(stage, {}).get("status") for stage in PIPELINE_STAGES):
        pipeline_status = "partial"
    typer.echo(f"pipeline: {pipeline_status}")
    if isinstance(pipeline_state, dict) and pipeline_state.get("error"):
        typer.echo(f"  error: {pipeline_state['error']}")
    if isinstance(pipeline_state, dict) and pipeline_state.get("trace"):
        typer.echo(f"  trace: {pipeline_state['trace']}")
    if isinstance(pipeline_state, dict) and pipeline_state.get("owner_pid"):
        typer.echo(f"  pid: {pipeline_state['owner_pid']}")
    last_event = last_nonempty_line(pipeline_log_path(workspace))
    if last_event:
        typer.echo(f"last_event: {last_event}")
    typer.echo(f"next: {next_action_for_state(state)}")
    typer.echo("phases:")
    phase_statuses = phase_statuses_from_state(state)
    for phase in PIPELINE_PHASES:
        phase_state = phase_statuses.get(phase, {})
        status_text = phase_state.get("status", "not-run")
        suffix_parts = []
        if phase_state.get("stage"):
            suffix_parts.append(f"stage={phase_state['stage']}")
        if phase_state.get("agent") and phase_state.get("agent") != phase_state.get("stage"):
            suffix_parts.append(f"agent={phase_state['agent']}")
        suffix = " " + " ".join(suffix_parts) if suffix_parts else ""
        typer.echo(f"  {phase}: {status_text}{suffix}")
        if phase_state.get("error"):
            typer.echo(f"    error: {phase_state['error']}")
    for stage in PIPELINE_STAGES:
        stage_state = state.get("stage_status", {}).get(stage, {})
        artifact = state.get("artifacts", {}).get(stage, {})
        approval = state.get("approvals", {}).get(stage)
        status_text = stage_state.get("status", "not-run")
        suffix = " approved" if approval else ""
        typer.echo(f"{stage}: {status_text}{suffix}")
        if stage_state.get("error"):
            typer.echo(f"  error: {stage_state['error']}")
        trace_path = stage_state.get("trace")
        if not trace_path and status_text in {"running", "interrupted"}:
            trace_path = stage_runtime_events_path(workspace, stage, state)
        if trace_path:
            typer.echo(f"  trace: {trace_path}")
        if stage_state.get("owner_pid"):
            typer.echo(f"  pid: {stage_state['owner_pid']}")
        for key in ["artifact_dir", "report_dir", "contract"]:
            if artifact.get(key):
                typer.echo(f"  {key}: {artifact[key]}")
        if stage == "builder":
            for key in ["proposal_dir", "proposal", "patch", "summary", "hosted_pr_url"]:
                if artifact.get(key):
                    typer.echo(f"  {key}: {artifact[key]}")
        if stage == "test":
            for path in artifact.get("test_files", []):
                typer.echo(f"  test_file: {path}")
            for path in artifact.get("fixtures", []):
                typer.echo(f"  fixture: {path}")


@app.command()
def trace(
    ctx: typer.Context,
    stage: Annotated[str, typer.Argument(help="Stage to inspect: pipeline, spec, test, builder, verifier, or eval.")],
    task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None,
    raw_path: Annotated[bool, typer.Option("--path", help="Only print the tool-call summary file path.")] = False,
    tail_path: Annotated[bool, typer.Option("--tail-path", help="Only print the append-only runtime log path.")] = False,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow the append-only runtime log.")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="Regenerate the summary from tool_events.json.")] = False,
) -> None:
    """Show a readable tool-call timeline for an agent stage."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    log_dir = runtime_log_dir(workspace, stage, state)
    summary_path = log_dir / "runtime_timeline.md"
    tail_file = log_dir / "runtime_events.log"
    if raw_path:
        typer.echo(tail_file if stage == "pipeline" else summary_path)
        return
    if tail_path:
        typer.echo(tail_file)
        return
    if follow:
        follow_runtime_log(workspace, stage, state, tail_file)
        return
    if stage == "pipeline":
        if not tail_file.exists():
            raise typer.BadParameter(f"pipeline event log not found: {tail_file}")
        typer.echo(tail_file.read_text(encoding="utf-8").rstrip())
        return
    metadata_path = log_dir / "runtime_metadata.json"
    if metadata_path.exists():
        metadata = read_json(metadata_path)
        typer.echo(f"stage: {stage}")
        typer.echo(f"status: {metadata.get('status', 'unknown')}")
        typer.echo(f"model: {metadata.get('variant_id') or metadata.get('model') or 'unknown'}")
        if metadata.get("error"):
            typer.echo(f"error: {metadata['error']}")
        usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
        events = metadata.get("events") if isinstance(metadata.get("events"), dict) else {}
        typer.echo(f"cost: {usage.get('cost', 0)}")
        typer.echo(f"tool_calls: {events.get('tool_calls', 0)}")
        typer.echo("")
    if summary_path.exists() and not refresh:
        typer.echo(summary_path.read_text(encoding="utf-8").rstrip())
        return
    transcript_path = log_dir / "runtime_transcript.json"
    if transcript_path.exists():
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        if isinstance(transcript, list):
            rendered = agent_runtime.render_runtime_timeline_markdown(transcript)
            summary_path.write_text(rendered, encoding="utf-8")
            typer.echo(rendered.rstrip())
            return
    events_path = log_dir / "tool_events.json"
    if not events_path.exists():
        raise typer.BadParameter(f"tool-call log not found: {events_path}")
    events = json.loads(events_path.read_text(encoding="utf-8"))
    if not isinstance(events, list):
        raise typer.BadParameter(f"tool-call log is not a list: {events_path}")
    rendered = agent_runtime.render_tool_calls_markdown(events)
    summary_path.write_text(rendered, encoding="utf-8")
    typer.echo(rendered.rstrip())


@app.command()
def report(ctx: typer.Context, output: Annotated[Path | None, typer.Option(help="HTML output path.")] = None) -> None:
    """Generate the HTML eval health report for this workspace."""
    workspace = workspace_from_ctx(ctx)
    output_path = output if output and output.is_absolute() else workspace / (output or Path(".workflow/eval-runs/report.html"))
    with in_workspace(workspace):
        reports = generate_eval_report.discover_reports(Path(".workflow/eval-runs"))
        selected_models = generate_eval_report.load_selected_models()
        latest = generate_eval_report.latest_reports_by_stage(reports)
        warnings = generate_eval_report.health_warnings(latest, selected_models, datetime.now(timezone.utc))
        status = generate_eval_report.overall_status(latest, selected_models)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(generate_eval_report.render_html(reports, latest, selected_models, warnings, status), encoding="utf-8")
    typer.echo(f"report: {output_path}")


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check workspace and environment readiness."""
    workspace = workspace_from_ctx(ctx)
    provider = os.environ.get("WEB_SEARCH_PROVIDER")
    checks = {
        "workspace": workspace.exists(),
        "agents": (workspace / ".workflow/agents").exists(),
        "OPENROUTER_API_KEY": bool(os.environ.get("OPENROUTER_API_KEY")),
        "current_task": bool(read_global_state(workspace).get("current_task")),
    }
    failed = False
    for name, ok in checks.items():
        typer.echo(f"{'ok' if ok else 'missing'}  {name}")
        failed = failed or not ok
    if provider:
        typer.echo(f"ok  WEB_SEARCH_PROVIDER={provider}")
        if provider == "tavily":
            tavily_ok = bool(os.environ.get("TAVILY_API_KEY"))
            typer.echo(f"{'ok' if tavily_ok else 'missing'}  TAVILY_API_KEY")
            failed = failed or not tavily_ok
    else:
        typer.echo("missing  WEB_SEARCH_PROVIDER (web_search tool will return a structured error)")
    if failed:
        raise typer.Exit(1)


def cli() -> None:
    app()


if __name__ == "__main__":
    cli()
