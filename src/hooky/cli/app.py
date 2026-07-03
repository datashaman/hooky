"""Typer app instances, global CLI constants, and the root callback."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, TypeVar

import typer

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]

app = typer.Typer(help="Run the Hooky loop pipeline.", no_args_is_help=True)
T = TypeVar("T")
skills_app = typer.Typer(help="Inspect available agent skills.", no_args_is_help=True)
app.add_typer(skills_app, name="skills")
evidence_app = typer.Typer(help="Capture and inspect system-owned evidence reports.", no_args_is_help=True)
app.add_typer(evidence_app, name="evidence")

# These constants live in hooky.cli.paths alongside the other run-key/
# workspace helpers; re-imported here because the root callback needs them.
from hooky.cli.paths import (  # noqa: E402
    RUN_KEY_ENV,
    normalize_run_key,
)


@app.callback()
def main(
    ctx: typer.Context,
    workspace: Annotated[Path, typer.Option("--workspace", "-C", help="Workspace directory. Defaults to current directory.")] = Path("."),
    run_key: Annotated[str | None, typer.Option("--run-key", help="Durable loop run key under .hooky/runs/<key>.")] = None,
) -> None:
    resolved = workspace.resolve()
    if run_key:
        run_key = normalize_run_key(run_key)
        os.environ[RUN_KEY_ENV] = run_key
    ctx.obj = {"workspace": resolved, "run_key": run_key}


def cli() -> None:
    app()
