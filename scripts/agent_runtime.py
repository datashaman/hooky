#!/usr/bin/env python3
"""Minimal OpenRouter tool-loop runtime for local SDLC agents."""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import spec_agent


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]
FinalValidator = Callable[[dict[str, Any]], None]


@dataclass
class AgentRunResult:
    final_report: dict[str, Any]
    usage: dict[str, Any]
    transcript: list[dict[str, Any]]
    tool_events: list[dict[str, Any]]


@dataclass
class ToolRuntime:
    working_folder: Path
    final_report_schema: dict[str, Any]
    max_cost_usd: float
    max_seconds: int
    bash_timeout_seconds: int = 30
    final_validator: FinalValidator | None = None
    todo_items: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.working_folder = Path(self.working_folder).resolve()
        self.started_at = time.monotonic()
        self.final_report: dict[str, Any] | None = None

    def tools(self) -> list[dict[str, Any]]:
        return [
            tool_schema("read_file", "Read a UTF-8 text file from the working folder.", {"path": string_schema()}, ["path"]),
            tool_schema("write_file", "Write a UTF-8 text file inside the working folder.", {"path": string_schema(), "content": string_schema()}, ["path", "content"]),
            tool_schema("list_files", "List direct children of a directory in the working folder.", {"path": string_schema(default=".")}, []),
            tool_schema("find_files", "Find files by glob pattern inside the working folder.", {"pattern": string_schema(), "path": string_schema(default=".")}, ["pattern"]),
            tool_schema("grep_files", "Search UTF-8 files for a literal string.", {"pattern": string_schema(), "path": string_schema(default=".")}, ["pattern"]),
            tool_schema("bash", "Run a shell command in the working folder with a timeout.", {"command": string_schema()}, ["command"]),
            tool_schema("todo_read", "Read the current todo list.", {}, []),
            tool_schema("todo_write", "Replace the current todo list.", {"items": {"type": "array", "items": {"type": "object", "additionalProperties": True}}}, ["items"]),
            {
                "type": "function",
                "function": {
                    "name": "final_report",
                    "description": "Finish the agent run with the required typed output contract.",
                    "parameters": self.final_report_schema,
                },
            },
        ]

    def run_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if time.monotonic() - self.started_at > self.max_seconds:
            raise TimeoutError(f"agent runtime exceeded {self.max_seconds}s")
        handlers: dict[str, ToolHandler] = {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "list_files": self.list_files,
            "find_files": self.find_files,
            "grep_files": self.grep_files,
            "bash": self.bash,
            "todo_read": self.todo_read,
            "todo_write": self.todo_write,
            "final_report": self.finish,
        }
        if name not in handlers:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            return handlers[name](args)
        except Exception as exc:  # noqa: BLE001 - tool errors should feed back to the model.
            return {"ok": False, "error": str(exc)}

    def resolve_path(self, value: str) -> Path:
        path = (self.working_folder / value).resolve()
        if path != self.working_folder and self.working_folder not in path.parents:
            raise ValueError(f"path escapes working folder: {value}")
        return path

    def read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(str(args["path"]))
        return {"ok": True, "path": relative_to(path, self.working_folder), "content": path.read_text(encoding="utf-8")}

    def write_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(str(args["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args["content"]), encoding="utf-8")
        return {"ok": True, "path": relative_to(path, self.working_folder), "bytes": path.stat().st_size}

    def list_files(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(str(args.get("path") or "."))
        entries = []
        for child in sorted(path.iterdir()):
            entries.append({"path": relative_to(child, self.working_folder), "type": "dir" if child.is_dir() else "file"})
        return {"ok": True, "entries": entries}

    def find_files(self, args: dict[str, Any]) -> dict[str, Any]:
        root = self.resolve_path(str(args.get("path") or "."))
        pattern = str(args["pattern"])
        matches = [
            relative_to(path, self.working_folder)
            for path in sorted(root.rglob("*"))
            if path.is_file() and fnmatch.fnmatch(path.name, pattern)
        ]
        return {"ok": True, "matches": matches[:500], "truncated": len(matches) > 500}

    def grep_files(self, args: dict[str, Any]) -> dict[str, Any]:
        root = self.resolve_path(str(args.get("path") or "."))
        pattern = str(args["pattern"])
        matches = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.stat().st_size > 1_000_000:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for index, line in enumerate(lines, 1):
                if pattern in line:
                    matches.append({"path": relative_to(path, self.working_folder), "line": index, "text": line[:500]})
                    if len(matches) >= 500:
                        return {"ok": True, "matches": matches, "truncated": True}
        return {"ok": True, "matches": matches, "truncated": False}

    def bash(self, args: dict[str, Any]) -> dict[str, Any]:
        completed = subprocess.run(
            str(args["command"]),
            cwd=self.working_folder,
            shell=True,
            text=True,
            capture_output=True,
            timeout=self.bash_timeout_seconds,
        )
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-8000:],
            "stderr": completed.stderr[-8000:],
        }

    def todo_read(self, _args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "items": self.todo_items}

    def todo_write(self, args: dict[str, Any]) -> dict[str, Any]:
        self.todo_items = list(args.get("items") or [])
        return {"ok": True, "items": self.todo_items}

    def finish(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.final_validator:
            self.final_validator(args)
        self.final_report = args
        return {"ok": True, "final_report_received": True}


def run_tool_agent(
    *,
    model: str,
    system: str,
    user: str,
    runtime: ToolRuntime,
) -> AgentRunResult:
    from openrouter import OpenRouter

    messages: list[Any] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    transcript: list[dict[str, Any]] = []
    tool_events: list[dict[str, Any]] = []
    total_usage: dict[str, Any] = {"cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    with OpenRouter(api_key=os.environ["OPENROUTER_API_KEY"], timeout_ms=openrouter_timeout_ms()) as client:
        while runtime.final_report is None:
            if time.monotonic() - runtime.started_at > runtime.max_seconds:
                raise TimeoutError(f"agent runtime exceeded {runtime.max_seconds}s")
            if float(total_usage.get("cost") or 0) > runtime.max_cost_usd:
                raise RuntimeError(f"agent runtime exceeded ${runtime.max_cost_usd:.4f} cost budget")

            completion = client.chat.send(
                model=model,
                messages=messages,
                tools=runtime.tools(),
                tool_choice="auto",
                **spec_agent.openrouter_request_options(),
            )
            usage = spec_agent.response_usage(completion)
            accumulate_usage(total_usage, usage)
            message = completion.choices[0].message
            message_payload = message.model_dump(exclude_none=True) if hasattr(message, "model_dump") else message
            messages.append(message_payload)
            transcript.append({"role": "assistant", "message": message_payload, "usage": usage})

            tool_calls = getattr(message, "tool_calls", None) or []
            if not tool_calls:
                messages.append({"role": "user", "content": "Continue by using the available tools. Finish only by calling final_report."})
                continue

            for tool_call in tool_calls:
                name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    args = {}
                    result = {"ok": False, "error": f"invalid JSON tool arguments: {exc}"}
                else:
                    result = runtime.run_tool(name, args)
                event = {"tool_call_id": tool_call.id, "name": name, "arguments": args, "result": result}
                tool_events.append(event)
                transcript.append({"role": "tool", **event})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": name,
                        "content": json.dumps(result, sort_keys=True),
                    }
                )

    return AgentRunResult(runtime.final_report, total_usage, transcript, tool_events)


def accumulate_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        total[key] = int(total.get(key) or 0) + int(usage.get(key) or 0)
    total["cost"] = round(float(total.get("cost") or 0) + float(usage.get("cost") or 0), 8)


def openrouter_timeout_ms() -> int:
    return int(os.environ.get("OPENROUTER_TIMEOUT_MS", "120000"))


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


def relative_to(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()
