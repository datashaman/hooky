"""Workspace/skills setup and loop init/status/watch commands."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any

import typer

from hooky import agent_skills
from hooky.cli.app import app, skills_app
from hooky.cli.loop_state import ensure_loop_initialized, initialize_loop_files, read_loop_state
from hooky.cli.paths import (
    DEFAULT_LAST_RUN_PATH,
    ensure_workspace_ready,
    loop_contract_path,
    loop_dir,
    loop_feature_list_path,
    loop_log_path,
    loop_progress_path,
    read_optional_proposal_file_or_stdin,
    selected_run_key,
    set_current_run_key,
    title_from_body,
    workspace_for_loop,
    workspace_from_ctx,
    write_last_run_workspace,
)


@app.command()
def init(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option(help="Overwrite existing Hooky runtime files.")] = False,
    git: Annotated[bool, typer.Option("--git/--no-git", help="Initialize a local git repository when the workspace is not already a worktree.")] = True,
    title: Annotated[str | None, typer.Option(help="Problem title for loop contract.md.")] = None,
    proposal_file: Annotated[Path | None, typer.Option(help="Optional problem proposal Markdown file.")] = None,
    run_key: Annotated[str | None, typer.Option(help="Durable loop run key under .hooky/runs/<key>.")] = None,
    last_run_path: Annotated[Path, typer.Option(help="Path used by status/watch to find the latest loop workspace.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Initialize a workspace with loop runtime context."""
    workspace = workspace_from_ctx(ctx)
    if run_key:
        set_current_run_key(workspace, run_key)
    ensure_workspace_ready(workspace, git=git)
    proposal = read_optional_proposal_file_or_stdin(proposal_file)
    title = title or title_from_body(proposal)
    loop_root = initialize_loop_files(workspace, title=title, proposal=proposal, force=force)
    write_last_run_workspace(last_run_path, workspace)
    typer.echo(f"initialized: {workspace}")
    typer.echo(f"loop: {loop_root}")


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
    typer.echo("next: hooky status")


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
    typer.echo(f"run_key: {selected_run_key(workspace)}")
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


def loop_watch(
    ctx: typer.Context,
    path_only: Annotated[bool, typer.Option("--path", help="Only print the loop log path.")] = False,
    follow: Annotated[bool, typer.Option("--follow/--no-follow", "-f", help="Follow the loop log.")] = True,
    last_run_path: Annotated[Path, typer.Option(help="Path written by loop init/run; used when the current directory has no loop.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Show or follow the selected run's log.md."""
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
