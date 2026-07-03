"""Top-level run/start/watch/status/trace/report/doctor commands and the model-loop driver."""

from __future__ import annotations

import os
import sys

from pathlib import Path
from typing import Annotated, Callable

import typer

from hooky import agent_runtime
from hooky import loop_agent
from hooky import loop_executor

from hooky.cli.app import DEFAULT_LAST_RUN_PATH, T, app
from hooky.cli.commands_inspect import loop_harness_review
from hooky.cli.commands_setup import follow_runtime_log, loop_status, loop_watch
from hooky.cli.loop_state import (
    append_jsonl,
    append_loop_log,
    apply_loop_evaluator_report,
    ensure_loop_initialized,
    fallback_evaluator_report_from_error,
    format_evaluator_feedback,
    initialize_loop_files,
    loop_attempt_dir,
    next_loop_attempt_id,
    read_loop_state,
    record_loop_transition,
    reset_loop_attempt_workspace,
    start_loop_attempt_state,
    update_loop_attempt,
    write_loop_progress,
    write_loop_state,
)
from hooky.cli.paths import (
    ensure_workspace_ready,
    loop_contract_path,
    loop_dir,
    loop_state_path,
    replace_markdown_section,
    resolve_workspace_path,
    set_current_run_key,
    temporary_executor,
    title_from_body,
    utc_now,
    workspace_for_loop,
    workspace_from_ctx,
    write_json,
    write_last_run_workspace,
)
from hooky.cli.transcript import loop_debug_root


def run_model_role_with_retries(
    workspace: Path,
    label: str,
    call: Callable[[], T],
    *,
    on_retry: Callable[[int, agent_runtime.AgentRunError], None] | None = None,
) -> T:
    max_retries = int(os.environ.get("LOOP_MODEL_ROLE_RETRIES", "1"))
    attempt = 0
    while True:
        try:
            return call()
        except agent_runtime.AgentRunError as exc:
            if attempt >= max_retries:
                raise
            attempt += 1
            append_loop_log(
                workspace,
                "loop-runner",
                f"retry {label}",
                f"Retry {attempt}/{max_retries} after model role error: {exc}",
            )
            if on_retry is not None:
                on_retry(attempt, exc)


def run_model_loop_once(
    workspace: Path,
    *,
    title: str | None,
    proposal: str,
    force: bool,
    last_run_path: Path,
    executor: str | None = None,
) -> None:
    with temporary_executor(executor):
        _run_model_loop_once(workspace, title=title, proposal=proposal, force=force, last_run_path=last_run_path)


def _run_model_loop_once(
    workspace: Path,
    *,
    title: str | None,
    proposal: str,
    force: bool,
    last_run_path: Path,
) -> None:
    ensure_workspace_ready(workspace)
    if not loop_state_path(workspace).exists() or force:
        initialize_loop_files(workspace, title=title, proposal=proposal, force=force)
    elif proposal:
        path = loop_contract_path(workspace)
        path.write_text(replace_markdown_section(path.read_text(encoding="utf-8"), "Proposal", proposal), encoding="utf-8")

    state = read_loop_state(workspace)
    planner_report, planner_usage = run_model_role_with_retries(
        workspace,
        "planner",
        lambda: loop_agent.generate_planner_artifacts(
            working_folder=workspace,
            proposal=proposal or loop_contract_path(workspace).read_text(encoding="utf-8"),
            attempt_id=state.get("current_attempt"),
        ),
    )
    state["status"] = "proposal-written"
    state["contract_accepted"] = False
    state["last_action"] = "run:planner"
    state.setdefault("role_usage", {})["planner"] = planner_usage
    record_loop_transition(
        workspace,
        state,
        note=str(planner_report.get("summary") or "Planner wrote contract proposal."),
        log_op="planner",
        log_title="planner wrote contract",
        log_body=str(planner_report.get("summary") or ""),
    )

    max_contract_rounds = int(os.environ.get("LOOP_CONTRACT_MAX_ROUNDS", "5"))
    review_feedback = ""
    accepted = False
    review = ""
    for round_number in range(1, max_contract_rounds + 1):
        generator_contract_report, generator_contract_usage = run_model_role_with_retries(
            workspace,
            f"generator-contract round {round_number}",
            lambda: loop_agent.generate_generator_contract_artifacts(
                working_folder=workspace,
                attempt_id=state.get("current_attempt"),
                review_feedback=review_feedback,
            ),
        )
        state = read_loop_state(workspace)
        state["status"] = "contract-proposed"
        state["contract_accepted"] = False
        state["last_action"] = f"run:generator-contract:{round_number}"
        state.setdefault("role_usage", {})[f"generator_contract_round_{round_number}"] = generator_contract_usage
        record_loop_transition(
            workspace,
            state,
            note=str(generator_contract_report.get("summary") or "Generator proposed contract."),
            log_op="generator",
            log_title=f"generator proposed contract round {round_number}",
            log_body=str(generator_contract_report.get("summary") or ""),
        )

        evaluator_contract_report, evaluator_contract_usage = run_model_role_with_retries(
            workspace,
            f"evaluator-contract round {round_number}",
            lambda: loop_agent.generate_evaluator_contract_artifacts(
                working_folder=workspace,
                attempt_id=state.get("current_attempt"),
            ),
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
        raise typer.Exit(1)

    max_attempt_rounds = int(os.environ.get("LOOP_ATTEMPT_MAX_ROUNDS", "3"))
    evaluator_feedback = ""
    final_attempt_id = ""
    final_report_path: Path | None = None
    for attempt_round in range(1, max_attempt_rounds + 1):
        state = read_loop_state(workspace)
        attempt_id, attempt_dir, state = start_loop_attempt_state(workspace, state)
        final_attempt_id = attempt_id
        state["last_action"] = "run:start-attempt"
        record_loop_transition(workspace, state, note=f"Attempt {attempt_id} started.", log_op="attempt", log_title=f"attempt {attempt_id} started")

        generator_report, generator_usage = run_model_role_with_retries(
            workspace,
            f"generator-implementation attempt {attempt_id}",
            lambda: loop_agent.generate_generator_implementation_artifacts(
                working_folder=workspace,
                attempt_id=attempt_id,
                evaluator_feedback=evaluator_feedback,
            ),
            on_retry=lambda retry, _exc: append_loop_log(
                workspace,
                "loop-runner",
                f"attempt {attempt_id} generator retry reset",
                reset_loop_attempt_workspace(workspace),
            ),
        )
        write_json(attempt_dir / "generator_report.json", generator_report)
        state = read_loop_state(workspace)
        state.setdefault("role_usage", {})["generator_implementation"] = generator_usage
        state.setdefault("role_usage", {})[f"generator_implementation_{attempt_id}"] = generator_usage
        state["last_action"] = "run:generator-implement"
        record_loop_transition(
            workspace,
            state,
            note=str(generator_report.get("summary") or "Generator completed implementation pass."),
            log_op="generator",
            log_title=f"attempt {attempt_id} implementation",
            log_body=str(generator_report.get("summary") or ""),
        )

        try:
            evaluator_report, evaluator_usage = run_model_role_with_retries(
                workspace,
                f"evaluator-attempt {attempt_id}",
                lambda: loop_agent.generate_evaluator_attempt_artifacts(
                    working_folder=workspace,
                    attempt_id=attempt_id,
                ),
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
    if state["status"] != "passed":
        raise typer.Exit(1)


def loop_run(
    ctx: typer.Context,
    title: Annotated[str | None, typer.Option(help="Problem title used when initializing a new loop.")] = None,
    proposal: Annotated[str, typer.Option(help="Planner proposal text for contract.md.")] = "",
    proposal_file: Annotated[Path | None, typer.Option(help="Problem proposal Markdown file.")] = None,
    criteria: Annotated[str, typer.Option(help="Dry-run generator-proposed done criteria.", hidden=True)] = "- Define done criteria explicitly.",
    review: Annotated[str, typer.Option(help="Dry-run evaluator contract review text.", hidden=True)] = "Contract criteria are accepted for this local run.",
    status: Annotated[str, typer.Option(help="Dry-run evaluator status: pass or fail.", hidden=True)] = "pass",
    recommendation: Annotated[str, typer.Option(help="Dry-run evaluator recommendation: continue, restart-attempt, restart-contract, or stop.", hidden=True)] = "continue",
    bottleneck: Annotated[str | None, typer.Option(help="Dry-run evaluator bottleneck.", hidden=True)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Use deterministic local loop plumbing instead of model roles.")] = False,
    executor: Annotated[str | None, typer.Option(help="Role executor: native, shell, codex, or claude. Defaults to HOOKY_EXECUTOR or native.")] = None,
    force: Annotated[bool, typer.Option(help="Reinitialize the loop before running.")] = False,
    run_key: Annotated[str | None, typer.Option(help="Durable loop run key under .hooky/runs/<key>.")] = None,
    last_run_path: Annotated[Path, typer.Option(help="Path used by loop status/watch to find the latest loop workspace.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Run the Karpathy-style loop suite through one attempt."""
    workspace = workspace_from_ctx(ctx)
    if run_key:
        set_current_run_key(workspace, run_key)
    ensure_workspace_ready(workspace)
    if proposal_file is not None:
        proposal = resolve_workspace_path(workspace, proposal_file).read_text(encoding="utf-8")
    elif not proposal and not sys.stdin.isatty():
        proposal = sys.stdin.read().strip()
    title = title or title_from_body(proposal)
    if not dry_run:
        run_model_loop_once(workspace, title=title, proposal=proposal, force=force, last_run_path=last_run_path, executor=executor)
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
    record_loop_transition(workspace, state, note=f"Contract accepted. {review}", log_op="evaluator", log_title="contract accepted", log_body=review)

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
    record_loop_transition(
        workspace,
        state,
        note=f"Run completed with status={status}, recommendation={recommendation}.",
        log_op="evaluator",
        log_title=f"attempt {attempt_id} {status}",
        log_body=f"recommendation: {recommendation}",
    )
    write_last_run_workspace(last_run_path, workspace)
    typer.echo(f"attempt: {attempt_id}")
    typer.echo(f"status: {state['status']}")
    typer.echo(f"report: {report_path}")
    if state["status"] != "passed":
        raise typer.Exit(1)


run_default = app.command("run")(loop_run)


@app.command()
def start(
    ctx: typer.Context,
    title: Annotated[str | None, typer.Option(help="Problem title used when initializing a new loop.")] = None,
    proposal: Annotated[str, typer.Option(help="Planner proposal text for contract.md.")] = "",
    proposal_file: Annotated[Path | None, typer.Option(help="Problem proposal Markdown file.")] = None,
    force: Annotated[bool, typer.Option(help="Reinitialize the loop before running.")] = False,
    executor: Annotated[str | None, typer.Option(help="Role executor: native, shell, codex, or claude. Defaults to HOOKY_EXECUTOR or native.")] = None,
    skill: Annotated[list[str] | None, typer.Option("--skill", help="Preselect an agent skill by name for this run. Repeat for multiple skills.")] = None,
    run_key: Annotated[str | None, typer.Option(help="Durable loop run key under .hooky/runs/<key>.")] = None,
    last_run_path: Annotated[Path, typer.Option(help="Path used by `hooky watch` to find the latest workspace.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Start the loop pipeline and register this workspace for `hooky watch`."""
    workspace = workspace_from_ctx(ctx)
    if run_key:
        set_current_run_key(workspace, run_key)
    if skill:
        os.environ["HOOKY_ACTIVE_SKILLS"] = ",".join(skill)
    proposal_text = proposal
    if proposal_file is not None:
        proposal_text = resolve_workspace_path(workspace, proposal_file).read_text(encoding="utf-8")
    write_last_run_workspace(last_run_path, workspace)
    typer.echo(f"workspace: {workspace}")
    if skill:
        typer.echo(f"skills: {', '.join(skill)}")
    if executor:
        typer.echo(f"executor: {loop_executor.selected_executor(executor)}")
    typer.echo("watch: hooky watch")
    run_model_loop_once(workspace, title=title, proposal=proposal_text, force=force, last_run_path=last_run_path, executor=executor)


@app.command()
def watch(
    ctx: typer.Context,
    last_run_path: Annotated[Path, typer.Option(help="Path written by `hooky start`.")] = DEFAULT_LAST_RUN_PATH,
    path_only: Annotated[bool, typer.Option("--path", help="Only print the loop log path.")] = False,
    follow: Annotated[bool, typer.Option("--follow/--no-follow", "-f", help="Follow the append-only runtime log.")] = True,
) -> None:
    """Follow the latest started loop log."""
    loop_watch(ctx, path_only=path_only, follow=follow, last_run_path=last_run_path)


@app.command()
def status(
    ctx: typer.Context,
    last_run_path: Annotated[Path, typer.Option(help="Path written by `hooky start`; used when the current directory has no loop.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Show the current loop state and durable file locations."""
    loop_status(ctx, last_run_path=last_run_path)


@app.command()
def trace(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt.")] = None,
    raw_path: Annotated[bool, typer.Option("--path", help="Only print the runtime log path.")] = False,
    tail_path: Annotated[bool, typer.Option("--tail-path", help="Only print the append-only runtime log path.")] = False,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow the append-only runtime log.")] = False,
    last_run_path: Annotated[Path, typer.Option(help="Path written by `hooky start`; used when the current directory has no loop.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Show or follow the loop runtime log for the active/latest attempt."""
    workspace = workspace_for_loop(ctx, last_run_path)
    ctx.obj["workspace"] = workspace
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    tail_file = root / "runtime_events.log"
    if raw_path:
        typer.echo(tail_file)
        return
    if tail_path:
        typer.echo(tail_file)
        return
    if follow:
        follow_runtime_log(workspace, "loop", state, tail_file)
        return
    if not tail_file.exists():
        raise typer.BadParameter(f"loop runtime log not found: {tail_file}")
    typer.echo(tail_file.read_text(encoding="utf-8").rstrip())


@app.command()
def report(ctx: typer.Context, output: Annotated[Path | None, typer.Option(help="HTML output path.")] = None) -> None:
    """Write a loop harness review report."""
    if output is not None:
        raise typer.BadParameter("HTML report output is not supported. Use `hooky report`.")
    loop_harness_review(ctx)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check loop workspace and environment readiness."""
    workspace = workspace_from_ctx(ctx)
    provider = os.environ.get("WEB_SEARCH_PROVIDER")
    checks = {
        "workspace": workspace.exists(),
        "loop": loop_dir(workspace).exists(),
        "OPENROUTER_API_KEY": bool(os.environ.get("OPENROUTER_API_KEY")),
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
