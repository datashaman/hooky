"""`hooky mcp-serve`: the stdio MCP server external executors (codex, claude) attach to.

This is spawned by codex/claude themselves per role invocation, per the MCP config Hooky writes
to executor_dir in loop_executor.run_external_executor; it is not meant to be run by hand.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from hooky.cli.app import app
from hooky.runtime.mcp_server import main as run_mcp_server


@app.command("mcp-serve", hidden=True)
def mcp_serve(
    config: Annotated[Path, typer.Option("--config", help="Path to the MCP server config JSON file written by the external executor wiring.")],
) -> None:
    run_mcp_server(config)
