#!/usr/bin/env python3
"""Facade over :mod:`hooky.cli` — preserved for backward compatibility.

The Typer CLI used to live entirely in this one 2600-line file. It has since
been split into cohesive submodules under ``hooky/cli/`` (app, paths,
loop_state, validation, transcript, and per-area ``commands_*`` modules).
This module re-exports the full public API so existing imports
(``import hooky.hooky_cli`` / ``from hooky import hooky_cli`` /
``hooky_cli.app`` / ``hooky_cli.agent_runtime.foo`` /
``mock.patch.object(hooky_cli.loop_agent, "foo", ...)``) keep working
unchanged.
"""

from __future__ import annotations

from hooky.cli import *  # noqa: F401,F403
from hooky.cli import __all__ as _cli_all

__all__ = list(_cli_all)


if __name__ == "__main__":
    cli()
