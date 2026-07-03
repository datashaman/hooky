"""Facade over :mod:`hooky.roles` — preserved for backward compatibility.

The Karpathy-style loop role runners used to live entirely in this one file.
They have since been split into cohesive submodules under ``hooky/roles/``
(models, runner, schemas, validators, prompts, generators). This module
re-exports the full public API so existing imports (``import hooky.loop_agent``
/ ``from hooky import loop_agent`` / ``loop_agent.selected_model()``) keep
working unchanged.
"""

from __future__ import annotations

from hooky.roles import *  # noqa: F401,F403
from hooky.roles import __all__ as _roles_all

__all__ = list(_roles_all)
