#!/usr/bin/env python3
"""Minimal OpenRouter tool-loop runtime for local SDLC agents."""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import spec_agent


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]
FinalValidator = Callable[[dict[str, Any]], None]


@dataclass
class AgentRunResult:
    final_report: dict[str, Any] | None
    usage: dict[str, Any]
    transcript: list[dict[str, Any]]
    tool_events: list[dict[str, Any]]
    compaction_events: list[dict[str, Any]]
    pre_compaction_archives: list[dict[str, Any]]
    started_at: str
    ended_at: str


class AgentRunError(RuntimeError):
    def __init__(self, message: str, result: AgentRunResult):
        super().__init__(message)
        self.result = result


@dataclass
class ToolRuntime:
    working_folder: Path
    final_report_schema: dict[str, Any]
    max_cost_usd: float
    max_seconds: int
    bash_timeout_seconds: int = 30
    final_validator: FinalValidator | None = None
    context_window_tokens: int | None = None
    compaction_threshold: float = 0.65
    compaction_keep_recent_messages: int = 16
    compaction_prompt_path: Path = Path(".workflow/agents/common/static/compaction.md")
    live_log_root: Path | None = None
    live_event_log_paths: list[Path] = field(default_factory=list)
    live_event_prefix: str = ""
    write_enabled: bool = True
    write_blocked_prefixes: list[str] = field(default_factory=lambda: [".workflow"])
    write_blocked_names: list[str] = field(default_factory=list)
    bash_blocked_substrings: list[str] = field(default_factory=list)
    todo_items: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.working_folder = Path(self.working_folder).resolve()
        if self.context_window_tokens is None:
            raw_context_window = os.environ.get("OPENROUTER_CONTEXT_LENGTH")
            self.context_window_tokens = int(raw_context_window) if raw_context_window else None
        if os.environ.get("AGENT_COMPACTION_THRESHOLD"):
            self.compaction_threshold = float(os.environ["AGENT_COMPACTION_THRESHOLD"])
        if os.environ.get("AGENT_COMPACTION_KEEP_RECENT_MESSAGES"):
            self.compaction_keep_recent_messages = int(os.environ["AGENT_COMPACTION_KEEP_RECENT_MESSAGES"])
        self.compaction_prompt_path = Path(self.compaction_prompt_path)
        self.started_at = time.monotonic()
        self.final_report: dict[str, Any] | None = None
        self.anchored_summary = ""

    def tools(self) -> list[dict[str, Any]]:
        return [
            tool_schema("read_file", "Read a UTF-8 text file from the working folder.", {"path": string_schema()}, ["path"]),
            tool_schema("write_file", "Write a UTF-8 text file inside the working folder.", {"path": string_schema(), "content": string_schema()}, ["path", "content"]),
            tool_schema("list_files", "List direct children of a directory in the working folder.", {"path": string_schema(default=".")}, []),
            tool_schema("find_files", "Find files by glob pattern inside the working folder.", {"pattern": string_schema(), "path": string_schema(default=".")}, ["pattern"]),
            tool_schema("grep_files", "Search UTF-8 files for a literal string.", {"pattern": string_schema(), "path": string_schema(default=".")}, ["pattern"]),
            tool_schema("bash", "Run a shell command in the working folder with a timeout.", {"command": string_schema()}, ["command"]),
            tool_schema(
                "web_search",
                "Search the web for source material and return normalized result metadata.",
                {
                    "query": string_schema(),
                    "max_results": integer_schema(default=5, minimum=1, maximum=10),
                    "include_domains": string_array_schema(),
                    "exclude_domains": string_array_schema(),
                },
                ["query"],
            ),
            tool_schema("fetch_url", "Fetch UTF-8 text content from an http or https URL.", {"url": string_schema()}, ["url"]),
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
            "web_search": self.web_search,
            "fetch_url": self.fetch_url,
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
        self.validate_write_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args["content"]), encoding="utf-8")
        return {"ok": True, "path": relative_to(path, self.working_folder), "bytes": path.stat().st_size}

    def validate_write_path(self, path: Path) -> None:
        if not self.write_enabled:
            raise ValueError("write_file is disabled for this agent; finish with final_report instead")
        relative = relative_to(path, self.working_folder)
        parts = Path(relative).parts
        for prefix in self.write_blocked_prefixes:
            prefix_parts = Path(prefix).parts
            if parts[: len(prefix_parts)] == prefix_parts:
                raise ValueError(f"agent is not allowed to write system-managed path: {relative}")
        if path.name in set(self.write_blocked_names):
            raise ValueError(f"agent is not allowed to write system-managed file: {relative}")

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
        command = str(args["command"])
        lowered = command.lower()
        for blocked in self.bash_blocked_substrings:
            if blocked.lower() in lowered:
                return {"ok": False, "error": f"bash command blocked by agent policy: {blocked}"}
        completed = subprocess.run(
            command,
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

    def fetch_url(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args["url"])
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("fetch_url requires an http or https URL")
        request = urllib.request.Request(url, headers={"User-Agent": "hooky-agent-runtime/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(1_000_001)
                truncated = len(raw) > 1_000_000
                content = raw[:1_000_000].decode("utf-8", errors="replace")
                return {
                    "ok": True,
                    "url": url,
                    "status": response.status,
                    "content_type": response.headers.get("content-type", ""),
                    "content": content,
                    "truncated": truncated,
                }
        except urllib.error.HTTPError as exc:
            return {"ok": False, "url": url, "status": exc.code, "error": exc.reason}
        except urllib.error.URLError as exc:
            return {"ok": False, "url": url, "error": str(exc.reason)}

    def web_search(self, args: dict[str, Any]) -> dict[str, Any]:
        provider = os.environ.get("WEB_SEARCH_PROVIDER", "").strip().lower()
        if not provider:
            return {"ok": False, "error": "WEB_SEARCH_PROVIDER is not configured"}
        if provider != "tavily":
            return {"ok": False, "error": f"unsupported WEB_SEARCH_PROVIDER: {provider}"}
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            return {"ok": False, "provider": "tavily", "error": "TAVILY_API_KEY is not configured"}
        return tavily_search(
            api_key=api_key,
            query=str(args["query"]),
            max_results=int(args.get("max_results") or 5),
            include_domains=list(args.get("include_domains") or []),
            exclude_domains=list(args.get("exclude_domains") or []),
        )

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
    compaction_events: list[dict[str, Any]] = []
    pre_compaction_archives: list[dict[str, Any]] = []
    total_usage: dict[str, Any] = {"cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    started_at = utc_timestamp()

    def current_result() -> AgentRunResult:
        return AgentRunResult(
            runtime.final_report,
            total_usage,
            transcript,
            tool_events,
            compaction_events,
            pre_compaction_archives,
            started_at,
            utc_timestamp(),
        )

    def flush_live_log(status: str = "running", error: str | None = None) -> None:
        if not runtime.live_log_root:
            return
        result = current_result()
        write_runtime_log(
            runtime.live_log_root,
            result.transcript,
            result.tool_events,
            result.compaction_events,
            result.pre_compaction_archives,
            metadata={
                "schema_version": 1,
                "status": status,
                "model": model,
                "started_at": result.started_at,
                "ended_at": result.ended_at,
                "written_at": utc_timestamp(),
                "error": error,
                "final_report_present": result.final_report is not None,
                "usage": result.usage,
                "events": {
                    "tool_calls": len(result.tool_events),
                    "compactions": len(result.compaction_events),
                    "pre_compaction_archives": len(result.pre_compaction_archives),
                },
            },
        )

    append_live_event(runtime, f"{utc_timestamp()} run start model={model}")
    flush_live_log()
    try:
        with OpenRouter(api_key=os.environ["OPENROUTER_API_KEY"], timeout_ms=openrouter_timeout_ms()) as client:
            while runtime.final_report is None:
                if time.monotonic() - runtime.started_at > runtime.max_seconds:
                    raise AgentRunError(f"agent runtime exceeded {runtime.max_seconds}s", current_result())
                if float(total_usage.get("cost") or 0) > runtime.max_cost_usd:
                    raise AgentRunError(f"agent runtime exceeded ${runtime.max_cost_usd:.4f} cost budget", current_result())

                messages, compaction_event, pre_compaction_archive = maybe_compact_messages(client, model, runtime, messages)
                if pre_compaction_archive:
                    pre_compaction_archives.append(pre_compaction_archive)
                    transcript.append({"role": "pre_compaction", **pre_compaction_archive})
                    flush_live_log()
                if compaction_event:
                    usage = compaction_event.get("usage") or {}
                    accumulate_usage(total_usage, usage)
                    compaction_events.append(compaction_event)
                    transcript.append({"role": "compaction", **compaction_event})
                    flush_live_log()
                    if float(total_usage.get("cost") or 0) > runtime.max_cost_usd:
                        raise AgentRunError(f"agent runtime exceeded ${runtime.max_cost_usd:.4f} cost budget after compaction", current_result())

                assistant_started_at = utc_timestamp()
                assistant_start = time.monotonic()
                completion = client.chat.send(
                    model=model,
                    messages=messages,
                    tools=runtime.tools(),
                    tool_choice="auto",
                    **spec_agent.openrouter_request_options(),
                )
                assistant_ended_at = utc_timestamp()
                assistant_duration_ms = round((time.monotonic() - assistant_start) * 1000, 2)
                usage = spec_agent.response_usage(completion)
                accumulate_usage(total_usage, usage)
                message = completion.choices[0].message
                message_payload = message.model_dump(exclude_none=True) if hasattr(message, "model_dump") else message
                messages.append(message_payload)
                transcript.append(
                    {
                        "role": "assistant",
                        "message": message_payload,
                        "usage": usage,
                        "started_at": assistant_started_at,
                        "ended_at": assistant_ended_at,
                        "duration_ms": assistant_duration_ms,
                    }
                )
                append_live_event(runtime, format_runtime_event_line(transcript[-1]))
                flush_live_log()

                tool_calls = getattr(message, "tool_calls", None) or []
                if not tool_calls:
                    messages.append({"role": "user", "content": "Continue by using the available tools. Finish only by calling final_report."})
                    continue

                for tool_call in tool_calls:
                    name = tool_call.function.name
                    tool_started_at = utc_timestamp()
                    tool_start = time.monotonic()
                    try:
                        args = json.loads(tool_call.function.arguments or "{}")
                    except json.JSONDecodeError as exc:
                        args = {}
                        result = {"ok": False, "error": f"invalid JSON tool arguments: {exc}"}
                    else:
                        result = runtime.run_tool(name, args)
                    event = {
                        "tool_call_id": tool_call.id,
                        "name": name,
                        "arguments": args,
                        "result": result,
                        "started_at": tool_started_at,
                        "ended_at": utc_timestamp(),
                        "duration_ms": round((time.monotonic() - tool_start) * 1000, 2),
                    }
                    tool_events.append(event)
                    transcript.append({"role": "tool", **event})
                    append_live_event(runtime, format_runtime_event_line(transcript[-1]))
                    flush_live_log()
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": name,
                            "content": json.dumps(result, sort_keys=True),
                        }
                    )
    except AgentRunError as exc:
        append_live_event(runtime, f"{utc_timestamp()} run error error={single_line(str(exc), 300)}")
        flush_live_log(status="error", error=str(exc))
        raise
    except Exception as exc:
        append_live_event(runtime, f"{utc_timestamp()} run error error={single_line(str(exc), 300)}")
        flush_live_log(status="error", error=str(exc))
        raise AgentRunError(str(exc), current_result()) from exc

    append_live_event(runtime, f"{utc_timestamp()} run success tool_calls={len(tool_events)} cost=${float(total_usage.get('cost') or 0):.8f}")
    flush_live_log(status="success")
    return current_result()


def maybe_compact_messages(
    client: Any,
    model: str,
    runtime: ToolRuntime,
    messages: list[Any],
) -> tuple[list[Any], dict[str, Any] | None, dict[str, Any] | None]:
    if not runtime.context_window_tokens:
        return messages, None, None
    estimated_tokens = estimate_tokens(messages)
    threshold_tokens = int(runtime.context_window_tokens * runtime.compaction_threshold)
    if estimated_tokens < threshold_tokens:
        return messages, None, None
    if len(messages) <= runtime.compaction_keep_recent_messages + 2:
        return messages, None, None

    keep_count = max(2, runtime.compaction_keep_recent_messages)
    prefix = messages[:2]
    older = messages[2:-keep_count]
    recent = trim_leading_tool_messages(messages[-keep_count:])
    if not older:
        return messages, None, None

    archive = {
        "reason": "pre_compaction_archive",
        "context_window_tokens": runtime.context_window_tokens,
        "threshold": runtime.compaction_threshold,
        "before_estimated_tokens": estimated_tokens,
        "older_messages_count": len(older),
        "recent_messages_count": len(recent),
        "older_messages": older,
    }

    previous_summary = runtime.anchored_summary
    prompt = compaction_user_prompt(previous_summary, older)
    completion = client.chat.send(
        model=os.environ.get("COMPACTION_MODEL", model),
        messages=[
            {"role": "system", "content": compaction_system_prompt(runtime)},
            {"role": "user", "content": prompt},
        ],
        response_format=spec_agent.structured_response_format("context_compaction", compaction_schema()),
        **spec_agent.openrouter_request_options(),
    )
    content = completion.choices[0].message.content
    if not content:
        return messages, None, archive
    payload = json.loads(content)
    runtime.anchored_summary = payload["summary"]
    summary_message = {
        "role": "user",
        "content": "Anchored context summary for continuing this agent run:\n\n" + runtime.anchored_summary,
    }
    compacted = prefix + [summary_message] + recent
    after_tokens = estimate_tokens(compacted)
    event = {
        "reason": "context_threshold",
        "context_window_tokens": runtime.context_window_tokens,
        "threshold": runtime.compaction_threshold,
        "before_estimated_tokens": estimated_tokens,
        "after_estimated_tokens": after_tokens,
        "older_messages_compacted": len(older),
        "recent_messages_kept": len(recent),
        "summary_chars": len(runtime.anchored_summary),
        "usage": spec_agent.response_usage(completion),
    }
    return compacted, event, archive


def compaction_system_prompt(runtime: ToolRuntime) -> str:
    if runtime.compaction_prompt_path.exists():
        return runtime.compaction_prompt_path.read_text(encoding="utf-8")
    return (
        "You are an anchored context summarization assistant. Summarize only the supplied "
        "older context, preserve exact paths and identifiers, and do not answer the task."
    )


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
        "Previous summary:\n"
        + (previous_summary or "None.")
        + "\n\nOlder context to compact:\n```json\n"
        + json.dumps(older_messages, indent=2, sort_keys=True, default=str)
        + "\n```"
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


def accumulate_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        total[key] = int(total.get(key) or 0) + int(usage.get(key) or 0)
    total["cost"] = round(float(total.get("cost") or 0) + float(usage.get("cost") or 0), 8)


def write_runtime_log(
    report_root: Path,
    transcript: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    compaction_events: list[dict[str, Any]],
    pre_compaction_archives: list[dict[str, Any]],
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    report_root.mkdir(parents=True, exist_ok=True)
    (report_root / "runtime_transcript.json").write_text(json.dumps(transcript, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (report_root / "tool_events.json").write_text(json.dumps(tool_events, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (report_root / "tool_calls.md").write_text(render_tool_calls_markdown(tool_events), encoding="utf-8")
    (report_root / "runtime_timeline.md").write_text(render_runtime_timeline_markdown(transcript), encoding="utf-8")
    (report_root / "runtime_events.snapshot.log").write_text(render_runtime_events_log(transcript), encoding="utf-8")
    (report_root / "compaction_events.json").write_text(json.dumps(compaction_events, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (report_root / "pre_compaction_archives.json").write_text(json.dumps(pre_compaction_archives, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (report_root / "runtime_metadata.json").write_text(
        json.dumps(metadata or {"schema_version": 1, "written_at": utc_timestamp()}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def append_live_event(runtime: ToolRuntime, line: str) -> None:
    targets: list[tuple[Path, str]] = []
    if runtime.live_log_root is not None:
        targets.append((runtime.live_log_root / "runtime_events.log", ""))
    for path in runtime.live_event_log_paths:
        targets.append((path, runtime.live_event_prefix))
    for path, prefix in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(prefix_event_line(line.rstrip(), prefix) + "\n")


def prefix_event_line(line: str, prefix: str) -> str:
    if not prefix:
        return line
    timestamp, sep, rest = line.partition(" ")
    if not sep:
        return prefix + line
    return f"{timestamp} {prefix}{rest}"


def render_runtime_events_log(transcript: list[dict[str, Any]]) -> str:
    lines = [format_runtime_event_line(item) for item in transcript if item.get("role") in {"assistant", "tool", "compaction", "pre_compaction"}]
    return "\n".join(line for line in lines if line) + ("\n" if lines else "")


def format_runtime_event_line(item: dict[str, Any]) -> str:
    role = str(item.get("role") or "event")
    timestamp = str(item.get("ended_at") or item.get("started_at") or utc_timestamp())
    duration = format_duration(item.get("duration_ms"))
    if role == "assistant":
        message = item.get("message") if isinstance(item.get("message"), dict) else {}
        tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        names = [
            str(call.get("function", {}).get("name"))
            for call in tool_calls
            if isinstance(call, dict) and isinstance(call.get("function"), dict) and call.get("function", {}).get("name")
        ]
        return (
            f"{timestamp} assistant duration={duration} cost=${float(usage.get('cost') or 0):.8f} "
            f"tokens={int(usage.get('total_tokens') or 0)} tool_calls={len(tool_calls)}"
            + (f" tools={','.join(names)}" if names else "")
        )
    if role == "tool":
        name = str(item.get("name") or "unknown")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        status = "ok" if result.get("ok") is True else "error" if result.get("ok") is False else "unknown"
        return f"{timestamp} tool name={name} status={status} duration={duration} {tail_detail(name, arguments, result)}".rstrip()
    return f"{timestamp} {role}"


def tail_detail(name: str, arguments: dict[str, Any], result: dict[str, Any]) -> str:
    if result.get("error"):
        return "error=" + quote_value(str(result["error"]), 220)
    if name in {"read_file", "write_file"}:
        path = result.get("path") or arguments.get("path")
        size_key = "bytes_written" if name == "write_file" else "bytes_read"
        size_value = result.get("bytes") if name == "write_file" else len(str(result.get("content") or "").encode("utf-8"))
        return f"path={quote_value(str(path), 180)} {size_key}={size_value}"
    if name == "bash":
        stdout = single_line(str(result.get("stdout") or ""), 180)
        stderr = single_line(str(result.get("stderr") or ""), 180)
        detail = f"command={quote_value(str(arguments.get('command') or ''), 180)} returncode={result.get('returncode')}"
        if stdout:
            detail += f" stdout={quote_value(stdout, 180)}"
        if stderr:
            detail += f" stderr={quote_value(stderr, 180)}"
        return detail
    if name in {"list_files", "find_files"}:
        entries = result.get("entries") if name == "list_files" else result.get("matches")
        count = len(entries) if isinstance(entries, list) else 0
        path = arguments.get("path")
        pattern = arguments.get("pattern")
        parts = [f"count={count}"]
        if path:
            parts.append(f"path={quote_value(str(path), 120)}")
        if pattern:
            parts.append(f"pattern={quote_value(str(pattern), 120)}")
        return " ".join(parts)
    if name == "grep_files":
        matches = result.get("matches") if isinstance(result.get("matches"), list) else []
        return f"matches={len(matches)} pattern={quote_value(str(arguments.get('pattern') or ''), 120)}"
    if name in {"todo_read", "todo_write"}:
        items = result.get("items") if isinstance(result.get("items"), list) else []
        detail = f"items={len(items)}"
        if name == "todo_write":
            active = active_todo_label(items)
            if active:
                detail += f" active={quote_value(active, 180)}"
        return detail
    if name == "web_search":
        results = result.get("results") if isinstance(result.get("results"), list) else []
        return f"results={len(results)} query={quote_value(str(arguments.get('query') or ''), 160)}"
    if name == "fetch_url":
        return f"url={quote_value(str(result.get('url') or arguments.get('url') or ''), 180)} status={result.get('status')}"
    if name == "final_report":
        return "submitted=true"
    return ""


def format_duration(value: Any) -> str:
    if value is None:
        return "unknown"
    duration_ms = float(value)
    if duration_ms >= 1000:
        return f"{duration_ms / 1000:.2f}s"
    return f"{duration_ms:.2f}ms"


def quote_value(value: str, max_chars: int) -> str:
    return '"' + single_line(value, max_chars).replace('"', '\\"') + '"'


def active_todo_label(items: list[Any]) -> str | None:
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or item.get("state") or "").lower()
        if status in {"active", "in_progress", "in-progress", "doing"}:
            return todo_label(item)
    for item in items:
        if isinstance(item, dict) and item.get("completed") is False:
            return todo_label(item)
    return None


def todo_label(item: dict[str, Any]) -> str:
    value = item.get("description") or item.get("content") or item.get("task") or item.get("title")
    if value:
        return str(value)
    return compact_json(item, 180)


def render_runtime_timeline_markdown(transcript: list[dict[str, Any]]) -> str:
    lines = ["# Runtime Timeline", ""]
    items = [item for item in transcript if item.get("role") in {"assistant", "tool", "compaction", "pre_compaction"}]
    if not items:
        lines.append("No runtime events recorded.")
        lines.append("")
        return "\n".join(lines)
    for index, item in enumerate(items, 1):
        role = item.get("role")
        if role == "assistant":
            message = item.get("message") if isinstance(item.get("message"), dict) else {}
            tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
            usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
            lines.append(f"## {index}. `assistant`")
            timing = summarize_tool_timing(item)
            details = timing + [
                f"tool calls requested: {len(tool_calls)}",
                f"cost: {usage.get('cost', 0)}",
                f"tokens: {usage.get('total_tokens', 0)}",
            ]
            names = [
                call.get("function", {}).get("name")
                for call in tool_calls
                if isinstance(call, dict) and isinstance(call.get("function"), dict)
            ]
            if names:
                details.append("tools requested: " + ", ".join(str(name) for name in names))
            lines.extend(f"- {detail}" for detail in details)
            lines.append("")
        elif role == "tool":
            name = str(item.get("name") or "unknown")
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            status = "ok" if result.get("ok") is True else "error" if result.get("ok") is False else "unknown"
            arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            lines.append(f"## {index}. `tool:{name}` `{status}`")
            details = summarize_tool_timing(item) + summarize_tool_event(name, arguments, result)
            lines.extend(f"- {detail}" for detail in details)
            lines.append("")
        else:
            lines.append(f"## {index}. `{role}`")
            lines.extend(f"- {detail}" for detail in summarize_tool_timing(item))
            lines.append("")
    return "\n".join(lines)


def render_tool_calls_markdown(tool_events: list[dict[str, Any]]) -> str:
    lines = ["# Tool Calls", ""]
    if not tool_events:
        lines.append("No tool calls recorded.")
        lines.append("")
        return "\n".join(lines)
    for index, event in enumerate(tool_events, 1):
        name = str(event.get("name") or "unknown")
        arguments = event.get("arguments") if isinstance(event.get("arguments"), dict) else {}
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        status = "ok" if result.get("ok") is True else "error" if result.get("ok") is False else "unknown"
        lines.append(f"## {index}. `{name}` `{status}`")
        summary = summarize_tool_event(name, arguments, result)
        timing = summarize_tool_timing(event)
        if timing:
            summary = timing + summary
        if summary:
            lines.append("")
            lines.extend(f"- {item}" for item in summary)
        lines.append("")
    return "\n".join(lines)


def summarize_tool_timing(event: dict[str, Any]) -> list[str]:
    timing: list[str] = []
    if event.get("started_at"):
        timing.append(f"started: {event['started_at']}")
    if event.get("ended_at"):
        timing.append(f"ended: {event['ended_at']}")
    if event.get("duration_ms") is not None:
        timing.append(f"duration: {event['duration_ms']}ms")
    return timing


def summarize_tool_event(name: str, arguments: dict[str, Any], result: dict[str, Any]) -> list[str]:
    summary: list[str] = []
    display_args = display_tool_arguments(name, arguments)
    if display_args:
        summary.append("args: " + compact_json(display_args, 300))
    if result.get("error"):
        summary.append("error: " + str(result["error"])[:500])
    if name == "read_file":
        content = str(result.get("content") or "")
        summary.append(f"path: `{result.get('path') or arguments.get('path')}`")
        summary.append(f"bytes read: {len(content.encode('utf-8'))}")
    elif name == "write_file":
        summary.append(f"path: `{result.get('path') or arguments.get('path')}`")
        summary.append(f"bytes written: {result.get('bytes', len(str(arguments.get('content') or '').encode('utf-8')))}")
    elif name == "list_files":
        entries = result.get("entries") if isinstance(result.get("entries"), list) else []
        summary.append(f"entries: {len(entries)}")
        if entries:
            summary.append("sample: " + ", ".join(str(item.get("path")) for item in entries[:12]))
    elif name == "find_files":
        matches = result.get("matches") if isinstance(result.get("matches"), list) else []
        summary.append(f"matches: {len(matches)}")
        if matches:
            summary.append("sample: " + ", ".join(str(item) for item in matches[:12]))
    elif name == "grep_files":
        matches = result.get("matches") if isinstance(result.get("matches"), list) else []
        summary.append(f"matches: {len(matches)}")
        if matches:
            summary.append("sample: " + "; ".join(f"{item.get('path')}:{item.get('line')}" for item in matches[:8] if isinstance(item, dict)))
    elif name == "bash":
        summary.append(f"returncode: {result.get('returncode')}")
        stdout = str(result.get("stdout") or "").strip()
        stderr = str(result.get("stderr") or "").strip()
        if stdout:
            summary.append("stdout: " + single_line(stdout, 500))
        if stderr:
            summary.append("stderr: " + single_line(stderr, 500))
    elif name == "todo_read":
        items = result.get("items") if isinstance(result.get("items"), list) else []
        summary.append(f"todo items: {len(items)}")
    elif name == "todo_write":
        items = result.get("items") if isinstance(result.get("items"), list) else []
        summary.append(f"todo items: {len(items)}")
        for item in items[:8]:
            if isinstance(item, dict):
                label = item.get("content") or item.get("task") or item.get("title") or compact_json(item, 120)
                status = item.get("status") or item.get("state")
                summary.append(f"todo: {status + ' ' if status else ''}{single_line(str(label), 160)}")
    elif name == "web_search":
        results = result.get("results") if isinstance(result.get("results"), list) else []
        summary.append(f"results: {len(results)}")
        for item in results[:5]:
            if isinstance(item, dict):
                summary.append(f"result: {single_line(str(item.get('title') or ''), 120)} {item.get('url') or ''}")
    elif name == "fetch_url":
        content = str(result.get("content") or "")
        summary.append(f"url: `{result.get('url') or arguments.get('url')}`")
        summary.append(f"status: {result.get('status')}")
        summary.append(f"bytes read: {len(content.encode('utf-8'))}")
    elif name == "final_report":
        summary.append("final report submitted")
    return summary


def display_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name in {"read_file", "write_file", "fetch_url"}:
        return {key: value for key, value in arguments.items() if key != "content"}
    if name == "todo_write":
        return {}
    return arguments


def compact_json(value: Any, max_chars: int) -> str:
    return single_line(json.dumps(value, sort_keys=True, default=str), max_chars)


def single_line(value: str, max_chars: int) -> str:
    cleaned = " ".join(value.split())
    return cleaned if len(cleaned) <= max_chars else cleaned[: max_chars - 3] + "..."


def build_runtime_metadata(
    agent_name: str,
    model: str,
    selected_model: dict[str, Any],
    result: AgentRunResult,
    *,
    status: str = "success",
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "agent_name": agent_name,
        "model": model,
        "variant_id": selected_model.get("variant_id", model),
        "reasoning_request": selected_model.get("reasoning_request"),
        "started_at": result.started_at,
        "ended_at": result.ended_at,
        "written_at": utc_timestamp(),
        "error": error,
        "final_report_present": result.final_report is not None,
        "usage": result.usage,
        "events": {
            "tool_calls": len(result.tool_events),
            "compactions": len(result.compaction_events),
            "pre_compaction_archives": len(result.pre_compaction_archives),
        },
    }


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def openrouter_timeout_ms() -> int:
    return int(os.environ.get("OPENROUTER_TIMEOUT_MS", "120000"))


def available_tool_names() -> list[str]:
    return [
        "read_file",
        "write_file",
        "list_files",
        "grep_files",
        "find_files",
        "bash",
        "web_search",
        "fetch_url",
        "todo_read",
        "todo_write",
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


def tavily_search(
    *,
    api_key: str,
    query: str,
    max_results: int,
    include_domains: list[str],
    exclude_domains: list[str],
) -> dict[str, Any]:
    max_results = min(max(max_results, 1), 10)
    payload: dict[str, Any] = {
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
    }
    if include_domains:
        payload["include_domains"] = include_domains
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains
    request = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "hooky-agent-runtime/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "provider": "tavily", "query": query, "status": exc.code, "error": detail[:2000]}
    except urllib.error.URLError as exc:
        return {"ok": False, "provider": "tavily", "query": query, "error": str(exc.reason)}
    results = []
    for item in data.get("results") or []:
        results.append(
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": item.get("content") or "",
                "content": None,
                "score": item.get("score"),
            }
        )
    return {
        "ok": True,
        "provider": "tavily",
        "query": query,
        "results": results,
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


def relative_to(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()
