#!/usr/bin/env python3
"""Typer CLI for running Hooky task pipelines."""

from __future__ import annotations

import json
import os
import shutil
import sys
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
import eval_agent
import generate_eval_report
import spec_agent
import test_agent
import verifier_agent


app = typer.Typer(help="Run the Hooky agentic SDLC pipeline.", no_args_is_help=True)
task_app = typer.Typer(help="Create, inspect, and switch tasks.", no_args_is_help=True)
run_app = typer.Typer(help="Run pipeline stages for the current task.", no_args_is_help=True)
approve_app = typer.Typer(help="Record human approval gates.", no_args_is_help=True)
app.add_typer(task_app, name="task")
app.add_typer(run_app, name="run")
app.add_typer(approve_app, name="approve")


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
    state.setdefault("stage_status", {})[stage] = {
        "status": status,
        "updated_at": utc_now(),
        **extra,
    }
    save_task_state(workspace, state)


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


@app.command()
def init(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option(help="Overwrite existing Hooky runtime files.")] = False,
) -> None:
    """Initialize a workspace with Hooky runtime context."""
    workspace = workspace_from_ctx(ctx)
    workspace.mkdir(parents=True, exist_ok=True)
    copy_missing(repo_file(".workflow/agents"), workspace / ".workflow/agents", force=force)
    copy_missing(repo_file("AGENTS.md"), workspace / "AGENTS.md", force=force)
    (workspace / ".workflow/tasks").mkdir(parents=True, exist_ok=True)
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
    body_text = resolve_workspace_path(workspace, body_file).read_text(encoding="utf-8") if body_file else body or ""
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
    set_stage_status(workspace, state, "spec", "passed", contract=(artifact_dir / "contract.json").as_posix())
    save_task_state(workspace, state)
    typer.echo(f"spec artifacts: {artifact_dir}")
    typer.echo("next: hooky approve spec")


@approve_app.command("spec")
def approve_spec(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Approve the current Spec Agent output."""
    approve_stage(ctx, "spec", "next: hooky run test", task)


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
    set_stage_status(workspace, state, "test", "passed", contract=(report_dir / "contract.json").as_posix())
    save_task_state(workspace, state)
    typer.echo(f"test report: {report_dir}")
    typer.echo("next: hooky approve test")


@run_app.command("builder")
def run_builder(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Run the Builder Agent."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    require_approval(state, "test")
    set_stage_status(workspace, state, "builder", "running")
    try:
        with in_workspace(workspace):
            report_dir, _contract, usage = builder_agent.generate_build_artifacts(working_folder=Path("."), generated_at=utc_now())
    except Exception as exc:
        set_stage_status(workspace, state, "builder", "failed", error=str(exc))
        raise
    state["artifacts"]["builder"] = {"report_dir": report_dir.as_posix(), "contract": (report_dir / "contract.json").as_posix(), "usage": usage}
    set_stage_status(workspace, state, "builder", "passed", contract=(report_dir / "contract.json").as_posix())
    save_task_state(workspace, state)
    typer.echo(f"builder report: {report_dir}")
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
            report_dir, _contract, usage = verifier_agent.generate_verification_artifacts(working_folder=Path("."), generated_at=utc_now())
    except Exception as exc:
        set_stage_status(workspace, state, "verifier", "failed", error=str(exc))
        raise
    state["artifacts"]["verifier"] = {"report_dir": report_dir.as_posix(), "contract": (report_dir / "contract.json").as_posix(), "usage": usage}
    set_stage_status(workspace, state, "verifier", "passed", contract=(report_dir / "contract.json").as_posix())
    save_task_state(workspace, state)
    typer.echo(f"verifier report: {report_dir}")
    typer.echo("next: hooky run eval")


@run_app.command("eval")
def run_eval(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Run the Eval Agent."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    require_artifact(state, "verifier")
    set_stage_status(workspace, state, "eval", "running")
    try:
        with in_workspace(workspace):
            report_dir, _contract, usage = eval_agent.generate_eval_artifacts(working_folder=Path("."), generated_at=utc_now())
    except Exception as exc:
        set_stage_status(workspace, state, "eval", "failed", error=str(exc))
        raise
    state["artifacts"]["eval"] = {"report_dir": report_dir.as_posix(), "contract": (report_dir / "contract.json").as_posix(), "usage": usage}
    set_stage_status(workspace, state, "eval", "passed", contract=(report_dir / "contract.json").as_posix())
    save_task_state(workspace, state)
    typer.echo(f"eval report: {report_dir}")
    typer.echo("next: hooky report")


@run_app.command("pipeline")
def run_pipeline(
    ctx: typer.Context,
    auto_approve: Annotated[bool, typer.Option(help="Automatically approve spec and test gates.")] = False,
    task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None,
) -> None:
    """Run all stages for the current task."""
    if not auto_approve:
        raise typer.BadParameter("run pipeline requires --auto-approve. Use individual `hooky run ...` commands for human-gated flow.")
    run_spec(ctx, task=task)
    approve_stage(ctx, "spec", "running test...", task)
    run_test(ctx, task=task)
    approve_stage(ctx, "test", "running builder...", task)
    run_builder(ctx, task=task)
    run_verifier(ctx, task=task)
    run_eval(ctx, task=task)


def require_approval(state: dict[str, Any], stage: str) -> None:
    if stage not in state.get("approvals", {}):
        raise typer.BadParameter(f"{stage} is not approved. Run `hooky approve {stage}` first.")


def require_artifact(state: dict[str, Any], stage: str) -> None:
    if stage not in state.get("artifacts", {}):
        raise typer.BadParameter(f"{stage} has not run yet.")


def runtime_log_dir(workspace: Path, stage: str, state: dict[str, Any]) -> Path:
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


@app.command()
def status(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Show the current task's pipeline state and artifact locations."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    typer.echo(f"task: {state['task_id']}")
    typer.echo(f"title: {state['title']}")
    for stage in ["spec", "test", "builder", "verifier", "eval"]:
        stage_state = state.get("stage_status", {}).get(stage, {})
        artifact = state.get("artifacts", {}).get(stage, {})
        approval = state.get("approvals", {}).get(stage)
        status_text = stage_state.get("status", "not-run")
        suffix = " approved" if approval else ""
        typer.echo(f"{stage}: {status_text}{suffix}")
        if stage_state.get("error"):
            typer.echo(f"  error: {stage_state['error']}")
        for key in ["artifact_dir", "report_dir", "contract"]:
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
    stage: Annotated[str, typer.Argument(help="Stage to inspect: spec, test, builder, verifier, or eval.")],
    task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None,
    raw_path: Annotated[bool, typer.Option("--path", help="Only print the tool-call summary file path.")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="Regenerate the summary from tool_events.json.")] = False,
) -> None:
    """Show a readable tool-call timeline for an agent stage."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    log_dir = runtime_log_dir(workspace, stage, state)
    summary_path = log_dir / "runtime_timeline.md"
    if raw_path:
        typer.echo(summary_path)
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
