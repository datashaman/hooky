"""JSON schema builders for tool definitions and structured responses."""

from __future__ import annotations

from typing import Any


def structured_response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


def available_tool_names() -> list[str]:
    return [
        "read_files",
        "read_file_excerpt",
        "write_files",
        "edit_files",
        "list_files",
        "find_files",
        "search_files",
        "detect_project_environment",
        "run_tests",
        "run_lint",
        "latest_test_failure_context",
        "capture_visual_snapshot",
        "append_evidence_note",
        "append_evidence_command",
        "append_evidence_screenshot",
        "git_status",
        "git_diff",
        "git_show",
        "bash",
        "start_process",
        "read_process",
        "stop_process",
        "list_processes",
        "web_search",
        "fetch_url",
        "request_time_extension",
        "todo_read",
        "todo_write",
        "final_report",
    ]


def tool_schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def string_schema(default: str | None = None) -> dict[str, Any]:
    schema = {"type": "string"}
    if default is not None:
        schema["default"] = default
    return schema


def integer_schema(default: int | None = None, minimum: int | None = None, maximum: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer"}
    if default is not None:
        schema["default"] = default
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


def string_array_schema() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "default": []}
