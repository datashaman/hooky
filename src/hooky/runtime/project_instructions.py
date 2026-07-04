"""Fold a project's AGENTS.md into the native executor's role system prompts, giving
`native` parity with codex/claude, which already read AGENTS.md themselves."""

from __future__ import annotations

from pathlib import Path

PROJECT_INSTRUCTIONS_FILENAME = "AGENTS.md"
MAX_PROJECT_INSTRUCTIONS_CHARS = 8000


def project_instructions_prompt_addendum(working_folder: Path) -> str:
    path = working_folder / PROJECT_INSTRUCTIONS_FILENAME
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""
    if not content:
        return ""
    if len(content) > MAX_PROJECT_INSTRUCTIONS_CHARS:
        content = content[:MAX_PROJECT_INSTRUCTIONS_CHARS] + "\n...(truncated)"
    return f"\n\n## Project Instructions (from {PROJECT_INSTRUCTIONS_FILENAME})\n\n{content}"


def compose_system_prompt(system: str, working_folder: Path) -> str:
    addendum = project_instructions_prompt_addendum(working_folder)
    return system + addendum if addendum else system
