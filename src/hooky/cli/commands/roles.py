"""Hidden per-role commands: planner/generator/evaluator steps, trace/otel events, restarts."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from hooky import roles

from hooky.cli.app import app
from hooky.cli.loop_state import (
    active_loop_attempt,
    append_jsonl,
    append_loop_log,
    apply_loop_evaluator_report,
    ensure_loop_initialized,
    loop_attempt_dir,
    read_loop_state,
    record_loop_transition,
    start_loop_attempt_state,
    update_loop_attempt,
    write_loop_progress,
    write_loop_state,
)
from hooky.cli.paths import (
    loop_contract_path,
    loop_feature_list_path,
    loop_log_path,
    loop_proposal_path,
    read_body_arg_or_file,
    replace_markdown_section,
    utc_now,
    workspace_from_ctx,
    write_json,
)


@app.command("log", hidden=True)
def loop_log(
    ctx: typer.Context,
    op: Annotated[str, typer.Option(help="Operation label for the log heading.")] = "note",
    title: Annotated[str, typer.Option(help="Short log title.")] = "manual note",
    body: Annotated[str, typer.Option(help="Optional log body.")] = "",
) -> None:
    """Append an entry to the selected run's log.md."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    state["last_action"] = op
    record_loop_transition(workspace, state, note=title, log_op=op, log_title=title, log_body=body)
    typer.echo(f"log: {loop_log_path(workspace)}")


@app.command("proposal", hidden=True)
def loop_proposal(
    ctx: typer.Context,
    proposal: Annotated[str | None, typer.Option(help="Problem proposal text.")] = None,
    proposal_file: Annotated[Path | None, typer.Option(help="Problem proposal Markdown file.")] = None,
) -> None:
    """Write the planner problem proposal into contract.md."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    proposal_text = read_body_arg_or_file(proposal, proposal_file)
    loop_proposal_path(workspace).write_text(proposal_text.strip() + ("\n" if proposal_text.strip() else ""), encoding="utf-8")
    path = loop_contract_path(workspace)
    path.write_text(replace_markdown_section(path.read_text(encoding="utf-8"), "Proposal", proposal_text), encoding="utf-8")
    state = read_loop_state(workspace)
    state["status"] = "proposal-written"
    state["contract_accepted"] = False
    state["last_action"] = "proposal"
    record_loop_transition(workspace, state, note="Problem proposal updated.", log_op="planner", log_title="proposal updated")
    typer.echo(f"contract: {path}")


@app.command("planner", hidden=True)
def loop_planner(
    ctx: typer.Context,
    proposal: Annotated[str, typer.Option(help="Problem proposal text for the planner.")] = "",
) -> None:
    """Run the real planner model role to write contract.md."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    report, usage = roles.generate_planner_artifacts(
        working_folder=workspace,
        proposal=proposal or loop_contract_path(workspace).read_text(encoding="utf-8"),
        attempt_id=state.get("current_attempt"),
    )
    state["status"] = "proposal-written"
    state["contract_accepted"] = False
    state["last_action"] = "planner"
    state.setdefault("role_usage", {})["planner"] = usage
    record_loop_transition(
        workspace,
        state,
        note=str(report.get("summary") or "Planner wrote contract proposal."),
        log_op="planner",
        log_title="planner wrote contract",
        log_body=str(report.get("summary") or ""),
    )
    typer.echo(f"contract: {loop_contract_path(workspace)}")
    typer.echo(f"summary: {report.get('summary')}")


@app.command("propose-contract", hidden=True)
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
    record_loop_transition(workspace, state, note="Done criteria proposed.", log_op="generator", log_title="contract proposed")
    typer.echo(f"contract: {path}")


@app.command("generator-contract", hidden=True)
def loop_generator_contract(ctx: typer.Context) -> None:
    """Run the real generator model role to propose done criteria."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    report, usage = roles.generate_generator_contract_artifacts(
        working_folder=workspace,
        attempt_id=state.get("current_attempt"),
    )
    state["status"] = "contract-proposed"
    state["contract_accepted"] = False
    state["last_action"] = "generator-contract"
    state.setdefault("role_usage", {})["generator_contract"] = usage
    record_loop_transition(
        workspace,
        state,
        note=str(report.get("summary") or "Generator proposed contract criteria."),
        log_op="generator",
        log_title="generator proposed contract",
        log_body=str(report.get("summary") or ""),
    )
    typer.echo(f"contract: {loop_contract_path(workspace)}")
    typer.echo(f"feature_list: {loop_feature_list_path(workspace)}")
    typer.echo(f"summary: {report.get('summary')}")


@app.command("review-contract", hidden=True)
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
    record_loop_transition(
        workspace,
        state,
        note=f"Contract review {status}. {review.strip()}",
        log_op="evaluator",
        log_title=f"contract {status}",
        log_body=review,
    )
    typer.echo(f"contract_review: {status}")


@app.command("evaluator-contract", hidden=True)
def loop_evaluator_contract(ctx: typer.Context) -> None:
    """Run the real evaluator model role to accept or reject the proposed contract."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    report, usage = roles.generate_evaluator_contract_artifacts(
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


@app.command("accept-contract", hidden=True)
def loop_accept_contract(ctx: typer.Context) -> None:
    """Mark contract.md as accepted for implementation."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    state["status"] = "contract-accepted"
    state["contract_accepted"] = True
    state["last_action"] = "accept-contract"
    record_loop_transition(workspace, state, note="Contract accepted.", log_op="contract", log_title="contract accepted")
    typer.echo("contract_accepted: true")


@app.command("start-attempt", hidden=True)
def loop_start_attempt(ctx: typer.Context) -> None:
    """Start a new implementation attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    attempt_id, attempt_dir, state = start_loop_attempt_state(workspace, state)
    state["last_action"] = "start-attempt"
    record_loop_transition(workspace, state, note=f"Attempt {attempt_id} started.", log_op="attempt", log_title=f"attempt {attempt_id} started")
    typer.echo(f"attempt: {attempt_id}")
    typer.echo(f"path: {attempt_dir}")


@app.command("generator-implement", hidden=True)
def loop_generator_implement(ctx: typer.Context) -> None:
    """Run the real generator model role for the active implementation attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    report, usage = roles.generate_generator_implementation_artifacts(
        working_folder=workspace,
        attempt_id=attempt_id,
    )
    report_path = loop_attempt_dir(workspace, attempt_id) / "generator_report.json"
    write_json(report_path, report)
    state.setdefault("role_usage", {})["generator_implementation"] = usage
    state["last_action"] = "generator-implement"
    record_loop_transition(
        workspace,
        state,
        note=str(report.get("summary") or "Generator completed implementation pass."),
        log_op="generator",
        log_title=f"attempt {attempt_id} implementation",
        log_body=str(report.get("summary") or ""),
    )
    typer.echo(f"report: {report_path}")
    typer.echo(f"summary: {report.get('summary')}")


@app.command("complete-attempt", hidden=True)
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
    note = f"Attempt {attempt_id} completed with result={result}."
    if bottleneck:
        note += f" Bottleneck: {bottleneck}."
    record_loop_transition(workspace, state, note=note, log_op="evaluation", log_title=f"attempt {attempt_id} {result}", log_body=note)
    typer.echo(f"attempt: {attempt_id}")
    typer.echo(f"result: {result}")


@app.command("evaluator-attempt", hidden=True)
def loop_evaluator_attempt(ctx: typer.Context) -> None:
    """Run the real evaluator model role for the active implementation attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    report, usage = roles.generate_evaluator_attempt_artifacts(
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


@app.command("evaluator-report", hidden=True)
def loop_evaluator_report(
    ctx: typer.Context,
    status: Annotated[str, typer.Option(help="Evaluator status: pass or fail.")] = "fail",
    recommendation: Annotated[str, typer.Option(help="Recommended control action: continue, restart-attempt, restart-contract, or stop.")] = "continue",
    bottleneck: Annotated[str, typer.Option(help="Current bottleneck. Use none_visible_after_trace_review only after trace/artifact review.")] = "none_visible_after_trace_review",
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
    if not bottleneck.strip():
        raise typer.BadParameter("bottleneck must be non-empty")
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


@app.command("trace-event", hidden=True)
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


@app.command("otel-event", hidden=True)
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


@app.command("restart-attempt", hidden=True)
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
    note = f"Restart attempt requested. Reason: {reason}"
    record_loop_transition(workspace, state, note=note, log_op="restart-attempt", log_title="attempt restart", log_body=note)
    typer.echo("restart-attempt: recorded")


@app.command("restart-contract", hidden=True)
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
    note = f"Restart contract requested. Reason: {reason}"
    record_loop_transition(workspace, state, note=note, log_op="restart-contract", log_title="contract restart", log_body=note)
    typer.echo("restart-contract: recorded")
