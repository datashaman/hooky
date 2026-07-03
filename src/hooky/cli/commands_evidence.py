"""System-owned evidence report commands (path/init/note/exec/screenshot/show)."""

from __future__ import annotations

from typing import Annotated

import typer

from hooky.runtime import ensure_evidence_report

from hooky.cli.app import evidence_app
from hooky.cli.loop_state import ensure_loop_initialized, read_loop_state
from hooky.cli.paths import workspace_from_ctx
from hooky.cli.transcript import evidence_base_dir_for_cli, evidence_report_path_for_cli, evidence_runtime_for_cli


@evidence_app.command("path")
def evidence_path(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to latest attempt, or run-level evidence if no attempt exists.")] = None,
) -> None:
    """Print the selected evidence report path."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    typer.echo(evidence_report_path_for_cli(workspace, state, attempt))


@evidence_app.command("init")
def evidence_init(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to latest attempt, or run-level evidence if no attempt exists.")] = None,
) -> None:
    """Create the selected evidence report if it does not exist."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    base_dir = evidence_base_dir_for_cli(workspace, state, attempt)
    path = ensure_evidence_report(workspace, base_dir)
    typer.echo(f"evidence: {path}")


@evidence_app.command("note")
def evidence_note(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="Evidence section title.")],
    body: Annotated[str, typer.Option(help="Optional Markdown body.")] = "",
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to latest attempt, or run-level evidence if no attempt exists.")] = None,
) -> None:
    """Append a note to the selected evidence report."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    runtime = evidence_runtime_for_cli(workspace, state, attempt)
    result = runtime.append_evidence_note({"title": title, "body": body})
    typer.echo(f"evidence: {workspace / result['evidence_path']}")


@evidence_app.command("exec")
def evidence_exec(
    ctx: typer.Context,
    command: Annotated[str, typer.Argument(help="Shell command to run and capture.")],
    title: Annotated[str, typer.Option(help="Evidence section title.")] = "Command evidence",
    timeout_seconds: Annotated[int, typer.Option(help="Command timeout in seconds.")] = 120,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to latest attempt, or run-level evidence if no attempt exists.")] = None,
) -> None:
    """Run a command, save real output, and append it to the evidence report."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    runtime = evidence_runtime_for_cli(workspace, state, attempt)
    result = runtime.append_evidence_command({"title": title, "command": command, "timeout_seconds": timeout_seconds})
    typer.echo(f"evidence: {workspace / result['evidence_path']}")
    typer.echo(f"output: {workspace / result['output_path']}")
    typer.echo(f"returncode: {result.get('returncode')}")
    if result.get("timed_out"):
        typer.echo("timed_out: true")
    if result.get("error"):
        raise typer.BadParameter(str(result["error"]))
    if result.get("ok") is False:
        raise typer.Exit(1)


@evidence_app.command("screenshot")
def evidence_screenshot(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="HTTP(S) URL to capture.")],
    title: Annotated[str, typer.Option(help="Evidence section title.")] = "Visual evidence",
    wait_selector: Annotated[str, typer.Option(help="Selector to wait for before capture.")] = "body",
    viewport_width: Annotated[int, typer.Option(help="Viewport width.")] = 1280,
    viewport_height: Annotated[int, typer.Option(help="Viewport height.")] = 900,
    full_page: Annotated[bool, typer.Option("--full-page/--viewport-only", help="Capture full page instead of viewport only.")] = True,
    timeout_seconds: Annotated[int, typer.Option(help="Screenshot timeout in seconds.")] = 30,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to latest attempt, or run-level evidence if no attempt exists.")] = None,
) -> None:
    """Capture a browser screenshot and append it to the evidence report."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    runtime = evidence_runtime_for_cli(workspace, state, attempt)
    result = runtime.append_evidence_screenshot(
        {
            "title": title,
            "url": url,
            "wait_selector": wait_selector,
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
            "full_page": full_page,
            "timeout_seconds": timeout_seconds,
        }
    )
    typer.echo(f"evidence: {workspace / result['evidence_path']}")
    if result.get("screenshot_path"):
        typer.echo(f"screenshot: {workspace / result['screenshot_path']}")
    if result.get("error"):
        raise typer.BadParameter(str(result["error"]))


@evidence_app.command("show")
def evidence_show(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to latest attempt, or run-level evidence if no attempt exists.")] = None,
) -> None:
    """Print the selected evidence report."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    path = evidence_report_path_for_cli(workspace, state, attempt)
    if not path.exists():
        raise typer.BadParameter(f"evidence report not found: {path}")
    typer.echo(path.read_text(encoding="utf-8").rstrip())
