"""Markdown/JSON extraction, compaction prompts, and text utilities."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hooky.runtime.tool_runtime import ToolRuntime


def assistant_message_text(message_payload: dict[str, Any]) -> str:
    content = message_payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if content_part_is_reasoning(item):
                    continue
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return ""


def content_part_is_reasoning(item: dict[str, Any]) -> bool:
    part_type = str(item.get("type") or item.get("channel") or "").lower()
    return part_type in {"reasoning", "analysis", "thinking", "chain_of_thought"}


def assistant_reasoning_trace(message_payload: dict[str, Any]) -> dict[str, Any] | None:
    entries: list[dict[str, str]] = []
    for key, source in (
        ("reasoning", "message.reasoning"),
        ("analysis", "message.analysis"),
        ("thinking", "message.thinking"),
    ):
        value = message_payload.get(key)
        text = reasoning_value_text(value)
        if text:
            entries.append({"source": source, "content": text})
    content = message_payload.get("content")
    if isinstance(content, list):
        for index, item in enumerate(content):
            if not isinstance(item, dict) or not content_part_is_reasoning(item):
                continue
            text = reasoning_value_text(item.get("text") or item.get("content") or item.get("reasoning"))
            if text:
                entries.append({"source": f"message.content[{index}]", "content": text})
    if not entries:
        return None
    return {
        "entries": entries,
        "chars": sum(len(entry["content"]) for entry in entries),
        "visible_by_default": False,
    }


def reasoning_trace_text(reasoning: dict[str, Any] | None) -> str:
    if not reasoning:
        return ""
    entries = reasoning.get("entries")
    if not isinstance(entries, list):
        return ""
    parts = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("content"):
            parts.append(str(entry["content"]))
    return "\n".join(parts)


def reasoning_value_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [reasoning_value_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "reasoning", "summary"):
            text = reasoning_value_text(value.get(key))
            if text:
                return text
        return json.dumps(value, sort_keys=True)
    return ""


def extract_json_object_from_text(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def recover_text_final_report(runtime: ToolRuntime, text: str) -> dict[str, Any] | None:
    payload = extract_json_object_from_text(text)
    if payload is None:
        payload = extract_markdown_final_report(text)
    if payload is None:
        return None
    actions = text_tool_actions_from_payload(payload)
    for action in actions:
        if action.get("name") == "final_report" and isinstance(action.get("arguments"), dict):
            payload = action["arguments"]
            break
    try:
        runtime.finish(payload)
    except Exception:
        return None
    return payload


def extract_markdown_final_report(text: str) -> dict[str, Any] | None:
    if "final_report" not in text:
        return None
    payload: dict[str, Any] = {}
    accepted_match = re.search(r"\bAccepted\s*:\s*\**(true|false)\**", text, re.IGNORECASE)
    if accepted_match:
        payload["accepted"] = accepted_match.group(1).lower() == "true"
        payload["status"] = "done"

    review = extract_markdown_section(text, "review")
    if review:
        payload["review"] = review

    required_changes = extract_markdown_required_changes(text)
    if required_changes:
        payload["required_changes"] = required_changes
    elif "accepted" in payload:
        payload["required_changes"] = []

    return payload if payload else None


def extract_markdown_section(text: str, heading: str) -> str:
    return cleanup_markdown_text(extract_markdown_section_raw(text, heading))


def extract_markdown_section_raw(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?:^|\n)\s*(?:#+\s*)?(?:\*\*)?{re.escape(heading)}(?:\*\*)?\s*:?\s*(.*?)(?=\n\s*(?:#+\s*)?(?:\*\*)?[A-Za-z_ ]+(?:\*\*)?\s*:?\s*(?:\n|\Z)|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return ""
    return match.group(1).strip()


def extract_markdown_required_changes(text: str) -> list[str]:
    section = extract_markdown_section_raw(text, "required_changes") or extract_markdown_section_raw(text, "required changes")
    if not section:
        return []
    changes: list[str] = []
    current: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        numbered = re.match(r"^(?:[-*]\s*)?\d+[.)]\s+(.*)$", stripped)
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if numbered:
            if current:
                changes.append(cleanup_markdown_text(" ".join(current)))
            current = [numbered.group(1)]
        elif bullet and current:
            current.append(bullet.group(1))
        elif stripped and current:
            current.append(stripped)
    if current:
        changes.append(cleanup_markdown_text(" ".join(current)))
    if changes:
        return [change for change in changes if change]
    return [cleanup_markdown_text(section)] if section.strip() else []


def cleanup_markdown_text(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = text.replace("**", "").replace("__", "").strip()
    return re.sub(r"\s+", " ", text)


def text_tool_actions_from_payload(payload: Any) -> list[dict[str, Any]]:
    raw_actions: Any
    if isinstance(payload, list):
        raw_actions = payload
    elif isinstance(payload, dict) and isinstance(payload.get("content"), list):
        raw_actions = payload["content"]
    elif isinstance(payload, dict) and payload.get("name"):
        raw_actions = [payload]
    else:
        return []
    actions: list[dict[str, Any]] = []
    for item in raw_actions:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        arguments = item.get("arguments")
        if isinstance(name, str) and isinstance(arguments, dict):
            actions.append({"name": name, "arguments": arguments})
    return actions


def extract_text_tool_actions(text: str) -> list[dict[str, Any]]:
    payload = extract_json_object_from_text(text)
    if payload is None:
        return []
    return text_tool_actions_from_payload(payload)


def compaction_system_prompt(runtime: ToolRuntime) -> str:
    if runtime.compaction_prompt_path.exists():
        return runtime.compaction_prompt_path.read_text(encoding="utf-8")
    return "You are an anchored context summarization assistant. Summarize only the supplied older context, preserve exact paths and identifiers, and do not answer the task."


def compaction_user_prompt(previous_summary: str, older_messages: list[Any]) -> str:
    return (
        "Return JSON with a single `summary` string.\n\n"
        "Required structure inside summary:\n"
        "- Objective\n"
        "- Current state\n"
        "- Decisions and constraints\n"
        "- Files and artifacts\n"
        "- Tool results and failures\n"
        "- Todo state\n"
        "- Next relevant actions\n\n"
        "Previous summary:\n" + (previous_summary or "None.") + "\n\nOlder context to compact:\n```json\n" + json.dumps(older_messages, indent=2, sort_keys=True, default=str) + "\n```"
    )


def compaction_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary"],
        "properties": {
            "summary": {"type": "string"},
        },
    }


def trim_leading_tool_messages(messages: list[Any]) -> list[Any]:
    trimmed = list(messages)
    while trimmed and isinstance(trimmed[0], dict) and trimmed[0].get("role") == "tool":
        trimmed.pop(0)
    return trimmed


def estimate_tokens(value: Any) -> int:
    return max(1, len(json.dumps(value, sort_keys=True, default=str)) // 4)


def quote_value(value: str, max_chars: int) -> str:
    return '"' + single_line(value, max_chars).replace('"', '\\"') + '"'


def compact_json(value: Any, max_chars: int) -> str:
    return single_line(json.dumps(value, sort_keys=True, default=str), max_chars)


def single_line(value: str, max_chars: int) -> str:
    cleaned = " ".join(value.split())
    return cleaned if len(cleaned) <= max_chars else cleaned[: max_chars - 3] + "..."


def apply_line_edits(content: str, edits: list[Any], label: str) -> str:
    lines = content.splitlines(keepends=True)
    line_count = len(lines)
    normalized: list[tuple[int, int, str, int, int]] = []
    insertion_points: set[int] = set()
    for index, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            raise ValueError(f"edit {index} for {label} must be an object")
        start_line = int(edit["start_line"])
        end_line = int(edit["end_line"])
        replacement = str(edit.get("replacement", ""))
        if end_line < start_line - 1:
            raise ValueError(f"edit {index} for {label} has end_line before insertion point")
        if start_line > line_count + 1:
            raise ValueError(f"edit {index} for {label} starts after end of file")
        if end_line > line_count:
            raise ValueError(f"edit {index} for {label} ends after end of file")
        start_index = start_line - 1
        end_index = end_line
        if start_index == end_index:
            if start_index in insertion_points:
                raise ValueError(f"edit {index} for {label} duplicates insertion point")
            insertion_points.add(start_index)
        normalized.append((start_index, end_index, replacement, start_line, end_line))

    previous_start: int | None = None
    previous_end: int | None = None
    for start_index, end_index, _replacement, start_line, end_line in sorted(normalized, key=lambda item: (item[0], item[1])):
        if previous_start is not None and start_index < int(previous_end):
            raise ValueError(f"line edits overlap in {label} near lines {start_line}-{end_line}")
        if previous_start is not None and start_index == previous_start and end_index != start_index:
            raise ValueError(f"line edits overlap in {label} near lines {start_line}-{end_line}")
        previous_start = start_index
        previous_end = end_index

    updated = list(lines)
    for start_index, end_index, replacement, _start_line, _end_line in sorted(normalized, key=lambda item: (item[0], item[1]), reverse=True):
        updated[start_index:end_index] = replacement.splitlines(keepends=True)
    return "".join(updated)


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def sanitize_output_for_read_policy(output: str, blocked_prefixes: list[str]) -> str:
    if not blocked_prefixes:
        return output
    lines = []
    for line in output.splitlines():
        if any(prefix and prefix in line for prefix in blocked_prefixes):
            continue
        lines.append(line)
    return "\n".join(lines)
