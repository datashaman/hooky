"""Facade over :mod:`hooky.runtime` — preserved for backward compatibility.

The runtime used to live entirely in this one file. It has since been split
into cohesive submodules under ``hooky/runtime/`` (models, schemas, text,
rendering, git, project_env, process, evidence, search, tool_runtime,
agent_loop). This module re-exports the full public API so existing imports
(``import hooky.agent_runtime`` / ``from hooky import agent_runtime`` /
``agent_runtime.ToolRuntime``) keep working unchanged.
"""

from __future__ import annotations

from hooky.runtime import *  # noqa: F401,F403
from hooky.runtime import __all__ as _runtime_all

__all__ = list(_runtime_all)
