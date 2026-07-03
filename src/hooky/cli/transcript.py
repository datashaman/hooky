"""Transcript loading/formatting and context-usage stats for loop debugging commands."""

from __future__ import annotations

import json

from pathlib import Path
from typing import Any

import typer

from hooky import runtime

from hooky.cli.loop_state import latest_loop_attempt_id
from hooky.cli.paths import loop_attempt_dir, loop_dir, normalize_run_key, selected_run_key


def loop_debug_root(workspace: Path, state: dict[str, Any], attempt: str | None) -> Path:
    attempt_id = attempt or latest_loop_attempt_id(state)
    if attempt_id:
        return loop_attempt_dir(workspace, attempt_id) / "traces"
    return loop_dir(workspace)


def evidence_base_dir_for_cli(workspace: Path, state: dict[str, Any], attempt: str | None) -> Path:
    attempt_id = attempt or latest_loop_attempt_id(state)
    if attempt_id:
        path = loop_attempt_dir(workspace, attempt_id)
        if not path.exists():
            raise typer.BadParameter(f"attempt does not exist: {attempt_id}")
        return path
    return loop_dir(workspace)


def evidence_runtime_for_cli(workspace: Path, state: dict[str, Any], attempt: str | None) -> runtime.ToolRuntime:
    base_dir = evidence_base_dir_for_cli(workspace, state, attempt)
    live_root = base_dir / "traces" if base_dir.name != normalize_run_key(selected_run_key(workspace)) else None
    return runtime.ToolRuntime(
        working_folder=workspace,
        final_report_schema={"type": "object", "properties": {}, "additionalProperties": True},
        max_cost_usd=0,
        max_seconds=600,
        live_log_root=live_root,
    )


def evidence_report_path_for_cli(workspace: Path, state: dict[str, Any], attempt: str | None) -> Path:
    return evidence_base_dir_for_cli(workspace, state, attempt) / "evidence.md"


def load_loop_transcript(root: Path) -> list[dict[str, Any]]:
    path = root / "runtime_transcript.json"
    if not path.exists():
        raise typer.BadParameter(f"runtime transcript not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise typer.BadParameter(f"runtime transcript is not a list: {path}")
    return [item for item in payload if isinstance(item, dict)]


def transcript_message(entry: dict[str, Any]) -> dict[str, Any]:
    message = entry.get("message")
    return message if isinstance(message, dict) else entry


def transcript_role(entry: dict[str, Any]) -> str:
    message = transcript_message(entry)
    return str(message.get("role") or entry.get("role") or "event")


def transcript_text(entry: dict[str, Any]) -> str:
    message = transcript_message(entry)
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if runtime.content_part_is_reasoning(item):
                    continue
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(entry.get("message"), str):
        return str(entry["message"])
    return ""


def transcript_reasoning_text(entry: dict[str, Any]) -> str:
    reasoning = entry.get("reasoning")
    if not isinstance(reasoning, dict):
        return ""
    return runtime.reasoning_trace_text(reasoning)


def transcript_tool_calls(entry: dict[str, Any]) -> list[dict[str, Any]]:
    message = transcript_message(entry)
    calls = message.get("tool_calls")
    return calls if isinstance(calls, list) else []


def load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def loop_context_stats(root: Path) -> dict[str, Any]:
    transcript = load_json_list(root / "runtime_transcript.json")
    tool_events = load_json_list(root / "tool_events.json")
    compactions = load_json_list(root / "compaction_events.json")
    archives = load_json_list(root / "pre_compaction_archives.json")
    assistants = [entry for entry in transcript if transcript_role(entry) == "assistant"]
    no_tool = [entry for entry in assistants if not transcript_tool_calls(entry)]
    trailing_no_tool = 0
    for entry in reversed(assistants):
        if transcript_tool_calls(entry):
            break
        trailing_no_tool += 1
    archived_messages = 0
    for archive in archives:
        older = archive.get("older_messages")
        if isinstance(older, list):
            archived_messages += len(older)
    return {
        "transcript_entries": len(transcript),
        "assistant_entries": len(assistants),
        "no_tool_assistant_entries": len(no_tool),
        "trailing_no_tool_assistant_entries": trailing_no_tool,
        "tool_events": len(tool_events),
        "compactions": len(compactions),
        "pre_compaction_archives": len(archives),
        "archived_older_messages": archived_messages,
        "tool_schema_order": runtime.available_tool_names(),
        "files": {
            "runtime_transcript": root / "runtime_transcript.json",
            "tool_events": root / "tool_events.json",
            "compaction_events": root / "compaction_events.json",
            "pre_compaction_archives": root / "pre_compaction_archives.json",
        },
    }
