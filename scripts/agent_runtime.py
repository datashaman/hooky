#!/usr/bin/env python3
"""Minimal OpenRouter tool-loop runtime for local SDLC agents."""

from __future__ import annotations

import fnmatch
import base64
import hashlib
import json
import mimetypes
import os
import re
import signal
import shlex
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import agent_skills


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]
FinalValidator = Callable[[dict[str, Any]], None]
DEFAULT_RUNTIME_DIR = ".hooky/runs/local"
RUNTIME_DIR_ENV = "HOOKY_RUN_DIR"
DISPOSABLE_RUNTIME_DIR_NAMES = {
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    "__pycache__",
    "coverage",
    "test-results",
}


def runtime_dir() -> str:
    raw = os.environ.get(RUNTIME_DIR_ENV, DEFAULT_RUNTIME_DIR).strip().strip("/")
    return raw or DEFAULT_RUNTIME_DIR


def runtime_path(root: Path, *parts: str) -> Path:
    return root / runtime_dir() / Path(*parts)


def openrouter_request_options() -> dict[str, Any]:
    options: dict[str, Any] = {"provider": {"require_parameters": True}}
    reasoning = os.environ.get("OPENROUTER_REASONING")
    if reasoning:
        options["reasoning"] = json.loads(reasoning)
    return options


def model_provider(model: str) -> str:
    return "ollama" if model.startswith("ollama/") else "openrouter"


def provider_model_name(model: str) -> str:
    return model.removeprefix("ollama/")


def model_request_options(model: str) -> dict[str, Any]:
    if model_provider(model) == "ollama":
        return ollama_request_options()
    return openrouter_request_options()


def ollama_request_options() -> dict[str, Any]:
    options: dict[str, Any] = {}
    reasoning = os.environ.get("OLLAMA_THINK")
    if reasoning:
        options["think"] = reasoning
    return options


def structured_response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


class ModelFunctionCall:
    def __init__(self, payload: dict[str, Any]):
        self.name = str(payload.get("name") or "")
        self.arguments = payload.get("arguments") if isinstance(payload.get("arguments"), str) else json.dumps(payload.get("arguments") or {})

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {"name": self.name, "arguments": self.arguments}


class ModelToolCall:
    def __init__(self, payload: dict[str, Any], index: int):
        self.id = str(payload.get("id") or f"tool-{index}")
        self.type = str(payload.get("type") or "function")
        function_payload = payload.get("function") if isinstance(payload.get("function"), dict) else {}
        self.function = ModelFunctionCall(function_payload)

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "function": self.function.model_dump()}


class ModelMessage:
    def __init__(self, payload: dict[str, Any]):
        self.role = str(payload.get("role") or "assistant")
        self.content = payload.get("content")
        tool_calls = payload.get("tool_calls") if isinstance(payload.get("tool_calls"), list) else []
        self.tool_calls = [ModelToolCall(item, index) for index, item in enumerate(tool_calls, 1) if isinstance(item, dict)]

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            payload["content"] = self.content
        if self.tool_calls:
            payload["tool_calls"] = [call.model_dump() for call in self.tool_calls]
        return payload


class ModelChoice:
    def __init__(self, payload: dict[str, Any]):
        message_payload = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        self.message = ModelMessage(message_payload)


class ModelCompletion:
    def __init__(self, payload: dict[str, Any]):
        choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
        self.choices = [ModelChoice(item) for item in choices if isinstance(item, dict)]
        self.usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        if not self.choices:
            self.choices = [ModelChoice({"message": {"role": "assistant", "content": ""}})]


class OllamaChat:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def chat_completions_url(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    def send(self, **kwargs: Any) -> ModelCompletion:
        model = provider_model_name(str(kwargs["model"]))
        payload: dict[str, Any] = {
            "model": model,
            "messages": kwargs.get("messages") or [],
            "stream": False,
        }
        if kwargs.get("tools"):
            payload["tools"] = kwargs["tools"]
        if kwargs.get("tool_choice"):
            payload["tool_choice"] = kwargs["tool_choice"]
        if kwargs.get("response_format"):
            payload["response_format"] = kwargs["response_format"]
        for option in ("max_tokens", "temperature", "top_p", "seed", "stop"):
            if kwargs.get(option) is not None:
                payload[option] = kwargs[option]
        request = urllib.request.Request(
            self.chat_completions_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = max(1, openrouter_timeout_ms() // 1000)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama chat request failed HTTP {exc.code}: {detail[:2000]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama chat request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Ollama chat request timed out after {timeout}s") from exc
        return ModelCompletion(data)


class OllamaClient:
    def __init__(self, base_url: str):
        self.chat = OllamaChat(base_url)

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def model_client(model: str) -> Any:
    if model_provider(model) == "ollama":
        return OllamaClient(os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"))
    from openrouter import OpenRouter

    return OpenRouter(api_key=os.environ["OPENROUTER_API_KEY"], timeout_ms=openrouter_timeout_ms())


def model_credentials_available(model: str) -> bool:
    if model_provider(model) == "ollama":
        return True
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def model_credentials_error(model: str, role_name: str) -> str:
    if model_provider(model) == "ollama":
        return f"Ollama is required for {role_name}; ensure `ollama serve` is running and OLLAMA_MODEL is installed"
    return f"OPENROUTER_API_KEY is required; {role_name} has no non-AI path"


def response_usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return usage.model_dump(exclude_none=True)
    if isinstance(usage, dict):
        return {key: value for key, value in usage.items() if value is not None}
    return {
        key: value
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost")
        if (value := getattr(usage, key, None)) is not None
    }


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
    write_validator: Callable[[Path, str], None] | None = None
    context_window_tokens: int | None = None
    compaction_threshold: float = 0.65
    compaction_keep_recent_messages: int = 16
    compaction_prompt_path: Path = Path(".hooky/prompts/compaction.md")
    live_log_root: Path | None = None
    live_event_log_paths: list[Path] = field(default_factory=list)
    live_event_prefix: str = ""
    heartbeat_seconds: int = 20
    no_tool_response_limit: int = 5
    write_enabled: bool = True
    read_blocked_prefixes: list[str] = field(default_factory=lambda: [".hooky"])
    read_allowed_prefixes: list[str] = field(default_factory=lambda: [f"{runtime_dir()}/tool-results"])
    write_allowed_prefixes: list[str] = field(default_factory=list)
    write_blocked_prefixes: list[str] = field(default_factory=lambda: [".hooky"])
    write_blocked_names: list[str] = field(default_factory=list)
    bash_blocked_substrings: list[str] = field(default_factory=list)
    bash_command_validator: Callable[[str], str | None] | None = None
    bash_protected_prefixes: list[str] = field(default_factory=list)
    todo_items: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    managed_processes: dict[str, subprocess.Popen[str]] = field(default_factory=dict)
    next_process_id: int = 1
    initial_image_paths: list[Path] = field(default_factory=list)
    pending_image_inputs: list[dict[str, str]] = field(default_factory=list)
    extension_seconds_used: int = 0
    extension_requests_used: int = 0
    max_extension_seconds: int = 0
    max_extension_requests: int = 0
    post_success_grace_seconds_used: int = 0
    max_post_success_grace_seconds: int = 0
    skills: list[agent_skills.AgentSkill] = field(default_factory=list)
    activated_skill_names: set[str] = field(default_factory=set)
    preselected_skill_names: list[str] = field(default_factory=list)
    enabled_tools: list[str] | None = None
    read_generation: int = 0
    read_observations: dict[str, dict[str, Any]] = field(default_factory=dict)

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
        if os.environ.get("AGENT_HEARTBEAT_SECONDS"):
            self.heartbeat_seconds = int(os.environ["AGENT_HEARTBEAT_SECONDS"])
        if os.environ.get("AGENT_NO_TOOL_RESPONSE_LIMIT"):
            self.no_tool_response_limit = int(os.environ["AGENT_NO_TOOL_RESPONSE_LIMIT"])
        if os.environ.get("AGENT_MAX_EXTENSION_SECONDS"):
            self.max_extension_seconds = int(os.environ["AGENT_MAX_EXTENSION_SECONDS"])
        if os.environ.get("AGENT_MAX_EXTENSION_REQUESTS"):
            self.max_extension_requests = int(os.environ["AGENT_MAX_EXTENSION_REQUESTS"])
        if os.environ.get("AGENT_POST_SUCCESS_GRACE_SECONDS"):
            self.max_post_success_grace_seconds = int(os.environ["AGENT_POST_SUCCESS_GRACE_SECONDS"])
        if os.environ.get("HOOKY_ACTIVE_SKILLS") and not self.preselected_skill_names:
            self.preselected_skill_names = [
                item.strip()
                for item in os.environ["HOOKY_ACTIVE_SKILLS"].split(",")
                if item.strip()
            ]

    def tools(self) -> list[dict[str, Any]]:
        tools = [
            tool_schema("read_file", "Read a UTF-8 text file from the working folder.", {"path": string_schema()}, ["path"]),
            tool_schema(
                "read_file_excerpt",
                "Read selected lines from a UTF-8 text file in the working folder.",
                {
                    "path": string_schema(),
                    "start_line": integer_schema(default=1, minimum=1),
                    "max_lines": integer_schema(default=120, minimum=1, maximum=500),
                },
                ["path"],
            ),
            tool_schema(
                "read_many_files",
                "Read multiple UTF-8 text files from the working folder with per-file truncation.",
                {
                    "paths": string_array_schema(),
                    "max_bytes_per_file": integer_schema(default=12000, minimum=1, maximum=50000),
                },
                ["paths"],
            ),
            tool_schema("write_file", "Write a UTF-8 text file inside the working folder.", {"path": string_schema(), "content": string_schema()}, ["path", "content"]),
            tool_schema("list_files", "List direct children of a directory in the working folder.", {"path": string_schema(default=".")}, []),
            tool_schema("find_files", "Find files by glob pattern inside the working folder.", {"pattern": string_schema(), "path": string_schema(default=".")}, ["pattern"]),
            tool_schema("search_files", "Search UTF-8 files for a literal string.", {"pattern": string_schema(), "path": string_schema(default=".")}, ["pattern"]),
            tool_schema(
                "detect_project_environment",
                "Detect language/package manager hints, scripts, lockfiles, and likely test commands.",
                {},
                [],
            ),
            tool_schema(
                "run_tests",
                "Run a project test command and return structured pass/fail evidence with full output saved to an artifact.",
                {
                    "command": string_schema(default=""),
                    "test_file": string_schema(default=""),
                    "test_name": string_schema(default=""),
                    "list_only": {"type": "boolean", "default": False},
                    "timeout_seconds": integer_schema(default=120, minimum=1, maximum=600),
                },
                [],
            ),
            tool_schema(
                "latest_test_failure_context",
                "Write and return a concise, file-backed diagnostic bundle for the latest failed run_tests call.",
                {
                    "max_output_bytes": integer_schema(default=8000, minimum=1000, maximum=50000),
                    "max_artifact_bytes": integer_schema(default=12000, minimum=1000, maximum=50000),
                    "max_artifacts": integer_schema(default=3, minimum=0, maximum=10),
                },
                [],
            ),
            tool_schema(
                "capture_visual_snapshot",
                "Capture a browser screenshot for visual verification and return layout metrics.",
                {
                    "url": string_schema(default=""),
                    "wait_selector": string_schema(default="body"),
                    "viewport_width": integer_schema(default=1280, minimum=320, maximum=3840),
                    "viewport_height": integer_schema(default=900, minimum=240, maximum=2160),
                    "full_page": {"type": "boolean", "default": True},
                    "timeout_seconds": integer_schema(default=30, minimum=1, maximum=120),
                },
                ["url"],
            ),
            tool_schema(
                "append_evidence_note",
                "Append a human-readable note to the system-owned evidence report for this run or attempt.",
                {
                    "title": string_schema(default="Evidence note"),
                    "body": string_schema(default=""),
                },
                [],
            ),
            tool_schema(
                "append_evidence_command",
                "Run a bounded shell command, save its real output, and append it to the system-owned evidence report.",
                {
                    "command": string_schema(),
                    "title": string_schema(default="Command evidence"),
                    "timeout_seconds": integer_schema(default=120, minimum=1, maximum=600),
                },
                ["command"],
            ),
            tool_schema(
                "append_evidence_screenshot",
                "Capture a browser screenshot and append the image plus metrics to the system-owned evidence report.",
                {
                    "url": string_schema(default=""),
                    "title": string_schema(default="Visual evidence"),
                    "wait_selector": string_schema(default="body"),
                    "viewport_width": integer_schema(default=1280, minimum=320, maximum=3840),
                    "viewport_height": integer_schema(default=900, minimum=240, maximum=2160),
                    "full_page": {"type": "boolean", "default": True},
                    "timeout_seconds": integer_schema(default=30, minimum=1, maximum=120),
                },
                ["url"],
            ),
            tool_schema("git_status", "Read git working-tree status without modifying files.", {}, []),
            tool_schema(
                "git_diff",
                "Read git diff output without modifying files.",
                {"path": string_schema(default=""), "staged": {"type": "boolean", "default": False}, "max_bytes": integer_schema(default=20000, minimum=1, maximum=100000)},
                [],
            ),
            tool_schema(
                "git_show",
                "Read a file or object from git without modifying files.",
                {"ref": string_schema(default="HEAD"), "path": string_schema(default=""), "max_bytes": integer_schema(default=20000, minimum=1, maximum=100000)},
                [],
            ),
            tool_schema("bash", "Run a shell command in the working folder with a timeout.", {"command": string_schema()}, ["command"]),
            tool_schema(
                "start_process",
                (
                    "Start a long-running local process in the working folder, such as a development server. "
                    "Use the returned url/ports fields for follow-up browser calls; requested_ports are only the ports requested by the command."
                ),
                {
                    "command": string_schema(),
                    "name": string_schema(default="process"),
                    "port": integer_schema(default=0, minimum=0, maximum=65535),
                    "auto_allocate_port": {"type": "boolean", "default": False},
                    "wait_for_url": string_schema(default=""),
                    "wait_seconds": integer_schema(default=3, minimum=0, maximum=60),
                },
                ["command"],
            ),
            tool_schema(
                "read_process",
                "Read recent stdout/stderr output from a managed long-running process.",
                {"process_id": string_schema(), "max_bytes": integer_schema(default=8000, minimum=1, maximum=20000)},
                ["process_id"],
            ),
            tool_schema("stop_process", "Stop a managed long-running process.", {"process_id": string_schema()}, ["process_id"]),
            tool_schema("list_processes", "List managed long-running processes.", {}, []),
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
            tool_schema(
                "request_time_extension",
                "Request a bounded runtime extension when recent tool evidence shows useful progress and a concrete next step remains.",
                {
                    "requested_seconds": integer_schema(default=180, minimum=30, maximum=600),
                    "reason": string_schema(),
                    "current_status": string_schema(),
                    "next_step": string_schema(),
                },
                ["reason", "current_status", "next_step"],
            ),
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
        if self.skills:
            tools.insert(
                -3,
                tool_schema(
                    "activate_skill",
                    "Load one available agent skill's SKILL.md instructions and list its optional resources.",
                    {"name": string_schema()},
                    ["name"],
                ),
            )
            tools.insert(
                -3,
                tool_schema(
                    "read_skill_resource",
                    "Read a file resource from an already activated skill directory.",
                    {
                        "name": string_schema(),
                        "path": string_schema(),
                        "max_bytes": integer_schema(default=20000, minimum=1, maximum=100000),
                    },
                    ["name", "path"],
                ),
            )
        if self.enabled_tools is not None:
            enabled = set(self.enabled_tools)
            tools = [
                tool
                for tool in tools
                if tool.get("function", {}).get("name") in enabled
            ]
        return tools

    def run_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handlers = self.tool_handlers()
        name = canonical_tool_name(name, handlers.keys())
        if self.enabled_tools is not None and name not in set(self.enabled_tools):
            return {"ok": False, "error": f"tool is disabled for this agent: {name}"}
        if name != "request_time_extension" and time.monotonic() - self.started_at > self.max_seconds:
            raise TimeoutError(f"agent runtime exceeded {self.max_seconds}s")
        if name not in handlers:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            return handlers[name](args)
        except Exception as exc:  # noqa: BLE001 - tool errors should feed back to the model.
            return {"ok": False, "error": str(exc)}

    def canonical_tool_name(self, name: str) -> str:
        return canonical_tool_name(name, self.tool_handlers().keys())

    def tool_handlers(self) -> dict[str, ToolHandler]:
        return {
            "read_file": self.read_file,
            "read_file_excerpt": self.read_file_excerpt,
            "read_many_files": self.read_many_files,
            "write_file": self.write_file,
            "list_files": self.list_files,
            "find_files": self.find_files,
            "search_files": self.search_files,
            "detect_project_environment": self.detect_project_environment,
            "run_tests": self.run_tests,
            "latest_test_failure_context": self.latest_test_failure_context,
            "capture_visual_snapshot": self.capture_visual_snapshot,
            "append_evidence_note": self.append_evidence_note,
            "append_evidence_command": self.append_evidence_command,
            "append_evidence_screenshot": self.append_evidence_screenshot,
            "git_status": self.git_status,
            "git_diff": self.git_diff,
            "git_show": self.git_show,
            "bash": self.bash,
            "start_process": self.start_process,
            "read_process": self.read_process,
            "stop_process": self.stop_process,
            "list_processes": self.list_processes,
            "web_search": self.web_search,
            "fetch_url": self.fetch_url,
            "request_time_extension": self.request_time_extension,
            "activate_skill": self.activate_skill,
            "read_skill_resource": self.read_skill_resource,
            "todo_read": self.todo_read,
            "todo_write": self.todo_write,
            "final_report": self.finish,
        }

    def skill_by_name(self, name: str) -> agent_skills.AgentSkill:
        for skill in self.skills:
            if skill.name == name:
                return skill
        raise ValueError(f"unknown skill: {name}")

    def activate_skill(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        if not name:
            raise ValueError("skill name is required")
        skill = self.skill_by_name(name)
        already_active = skill.name in self.activated_skill_names
        self.activated_skill_names.add(skill.name)
        resources = agent_skills.skill_resources(skill)
        return {
            "ok": True,
            "name": skill.name,
            "description": skill.description,
            "skill_path": skill.path.as_posix(),
            "body": skill.body,
            "resources": resources,
            "already_active": already_active,
        }

    def read_skill_resource(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        resource_path = str(args.get("path") or "").strip()
        if not name or not resource_path:
            raise ValueError("name and path are required")
        if name not in self.activated_skill_names:
            raise ValueError(f"activate skill before reading resources: {name}")
        skill = self.skill_by_name(name)
        path = agent_skills.resolve_skill_resource(skill, resource_path)
        max_bytes = int(args.get("max_bytes") or 20000)
        content = read_text_prefix(path, max_bytes)
        return {
            "ok": True,
            "name": skill.name,
            "path": resource_path,
            "bytes": path.stat().st_size,
            "content": content,
            "truncated": path.stat().st_size > len(content.encode("utf-8")),
        }

    def request_time_extension(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.max_extension_seconds <= 0 or self.max_extension_requests <= 0:
            return {"ok": False, "granted": False, "error": "time extensions are disabled for this agent"}
        if self.extension_requests_used >= self.max_extension_requests:
            return {"ok": False, "granted": False, "error": "time extension request limit reached"}
        if self.extension_seconds_used >= self.max_extension_seconds:
            return {"ok": False, "granted": False, "error": "time extension budget exhausted"}
        reason = str(args.get("reason") or "").strip()
        current_status = str(args.get("current_status") or "").strip()
        next_step = str(args.get("next_step") or "").strip()
        if not reason or not current_status or not next_step:
            return {"ok": False, "granted": False, "error": "reason, current_status, and next_step are required"}
        elapsed = time.monotonic() - self.started_at
        if elapsed < self.max_seconds * 0.75:
            return {"ok": False, "granted": False, "error": "extension requests are only available near the runtime deadline"}
        if not self.recent_progress_evidence():
            return {"ok": False, "granted": False, "error": "no recent progress evidence from tool results"}
        requested = int(args.get("requested_seconds") or 180)
        remaining_budget = self.max_extension_seconds - self.extension_seconds_used
        granted = max(0, min(requested, remaining_budget))
        if granted <= 0:
            return {"ok": False, "granted": False, "error": "time extension budget exhausted"}
        self.max_seconds += granted
        self.extension_seconds_used += granted
        self.extension_requests_used += 1
        return {
            "ok": True,
            "granted": True,
            "added_seconds": granted,
            "max_seconds": self.max_seconds,
            "extension_requests_used": self.extension_requests_used,
            "extension_seconds_used": self.extension_seconds_used,
        }

    def recent_progress_evidence(self) -> bool:
        recent_events = self.tool_events[-12:]
        saw_write = False
        saw_test = False
        saw_successful_tool = False
        for event in recent_events:
            result = event.get("result") or {}
            if result.get("ok"):
                saw_successful_tool = True
            if event.get("name") == "write_file" and result.get("ok"):
                saw_write = True
            if event.get("name") == "run_tests":
                saw_test = True
        return saw_successful_tool and (saw_write or saw_test)

    def grant_post_success_grace(self, result: dict[str, Any]) -> dict[str, Any] | None:
        if self.max_post_success_grace_seconds <= 0:
            return None
        if not result.get("ok") or result.get("passed") is not True:
            return None
        remaining_budget = self.max_post_success_grace_seconds - self.post_success_grace_seconds_used
        if remaining_budget <= 0:
            return None
        elapsed = time.monotonic() - self.started_at
        target_deadline = elapsed + remaining_budget
        if target_deadline <= self.max_seconds:
            return None
        added_seconds = int(target_deadline - self.max_seconds)
        if added_seconds <= 0:
            return None
        self.max_seconds += added_seconds
        self.post_success_grace_seconds_used += added_seconds
        return {
            "added_seconds": added_seconds,
            "max_seconds": self.max_seconds,
            "post_success_grace_seconds_used": self.post_success_grace_seconds_used,
        }

    def resolve_path(self, value: str) -> Path:
        path = (self.working_folder / value).resolve()
        if path != self.working_folder and self.working_folder not in path.parents:
            raise ValueError(f"path escapes working folder: {value}")
        return path

    def read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(str(args["path"]))
        self.validate_read_path(path)
        content = path.read_text(encoding="utf-8")
        self.record_read_observation(path, content)
        return {"ok": True, "path": relative_to(path, self.working_folder), "content": content}

    def read_file_excerpt(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(str(args["path"]))
        self.validate_read_path(path)
        start_line = int(args.get("start_line") or 1)
        max_lines = int(args.get("max_lines") or 120)
        lines = path.read_text(encoding="utf-8").splitlines()
        start_index = max(start_line - 1, 0)
        excerpt = lines[start_index : start_index + max_lines]
        return {
            "ok": True,
            "path": relative_to(path, self.working_folder),
            "start_line": start_index + 1,
            "end_line": start_index + len(excerpt),
            "total_lines": len(lines),
            "content": "\n".join(excerpt),
            "truncated": start_index + max_lines < len(lines),
        }

    def read_many_files(self, args: dict[str, Any]) -> dict[str, Any]:
        max_bytes = int(args.get("max_bytes_per_file") or 12000)
        files = []
        for raw_path in list(args.get("paths") or [])[:50]:
            try:
                path = self.resolve_path(str(raw_path))
                self.validate_read_path(path)
                content = read_text_prefix(path, max_bytes)
                files.append(
                    {
                        "path": relative_to(path, self.working_folder),
                        "content": content,
                        "bytes": path.stat().st_size,
                        "truncated": path.stat().st_size > len(content.encode("utf-8")),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - preserve batch reads when one file is absent.
                files.append({"path": str(raw_path), "error": str(exc)})
        return {"ok": True, "files": files, "truncated": len(list(args.get("paths") or [])) > 50}

    def is_read_blocked(self, path: Path) -> bool:
        relative = relative_to(path.resolve(), self.working_folder)
        parts = Path(relative).parts
        for prefix in self.read_allowed_prefixes:
            prefix_parts = Path(prefix).parts
            if parts[: len(prefix_parts)] == prefix_parts:
                return False
        for prefix in self.read_blocked_prefixes:
            prefix_parts = Path(prefix).parts
            if parts[: len(prefix_parts)] == prefix_parts:
                return True
        return False

    def validate_read_path(self, path: Path) -> None:
        if self.is_read_blocked(path):
            raise FileNotFoundError(f"path not found: {relative_to(path, self.working_folder)}")

    def write_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(str(args["path"]))
        self.validate_write_path(path)
        self.validate_write_has_current_read(path)
        content = str(args["content"])
        if self.write_validator:
            self.write_validator(path, content)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.record_read_observation(path, content)
        return {"ok": True, "path": relative_to(path, self.working_folder), "bytes": path.stat().st_size}

    def record_read_observation(self, path: Path, content: str) -> None:
        relative = relative_to(path.resolve(), self.working_folder)
        self.read_observations[relative] = {
            "generation": self.read_generation,
            "sha256": content_sha256(content),
            "size": len(content.encode("utf-8")),
        }

    def advance_read_generation(self) -> None:
        self.read_generation += 1
        self.read_observations.clear()

    def validate_write_has_current_read(self, path: Path) -> None:
        if not path.exists() or self.is_write_allowed_prefix_path(path):
            return
        relative = relative_to(path.resolve(), self.working_folder)
        observation = self.read_observations.get(relative)
        if not observation or observation.get("generation") != self.read_generation:
            raise ValueError(f"write_file blocked: {relative} was not read in the current uncompacted context. Call read_file first.")
        current_hash = file_sha256(path)
        if current_hash != observation.get("sha256"):
            raise ValueError(f"write_file blocked: {relative} changed since it was read. Call read_file again before writing.")

    def validate_write_path(self, path: Path) -> None:
        if not self.write_enabled:
            raise ValueError("write_file is disabled for this agent; finish with final_report instead")
        relative = relative_to(path, self.working_folder)
        parts = Path(relative).parts
        if self.write_allowed_prefixes:
            allowed = False
            for prefix in self.write_allowed_prefixes:
                prefix_parts = Path(prefix).parts
                if parts[: len(prefix_parts)] == prefix_parts:
                    allowed = True
                    break
            if not allowed:
                raise ValueError(f"agent is not allowed to write outside allowed paths: {relative}")
        for prefix in self.write_blocked_prefixes:
            prefix_parts = Path(prefix).parts
            if parts[: len(prefix_parts)] == prefix_parts:
                raise FileNotFoundError(f"path not found: {relative}")
        if path.name in set(self.write_blocked_names):
            raise ValueError(f"agent is not allowed to write system-managed file: {relative}")

    def is_write_allowed_prefix_path(self, path: Path) -> bool:
        if not self.write_allowed_prefixes:
            return False
        relative = relative_to(path.resolve(), self.working_folder)
        parts = Path(relative).parts
        for prefix in self.write_allowed_prefixes:
            prefix_parts = Path(prefix).parts
            if parts[: len(prefix_parts)] == prefix_parts:
                return True
        return False

    def list_files(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(str(args.get("path") or "."))
        self.validate_read_path(path)
        entries = []
        for child in sorted(path.iterdir()):
            if self.is_read_blocked(child):
                continue
            entries.append({"path": relative_to(child, self.working_folder), "type": "dir" if child.is_dir() else "file"})
        return {"ok": True, "entries": entries}

    def find_files(self, args: dict[str, Any]) -> dict[str, Any]:
        root = self.resolve_path(str(args.get("path") or "."))
        self.validate_read_path(root)
        pattern = str(args["pattern"])
        matches = []
        for path in sorted(root.rglob("*")):
            relative = relative_to(path, self.working_folder)
            if (
                path.is_file()
                and not self.is_read_blocked(path)
                and (fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(relative, pattern))
            ):
                matches.append(relative)
        return {"ok": True, "matches": matches[:500], "truncated": len(matches) > 500}

    def search_files(self, args: dict[str, Any]) -> dict[str, Any]:
        root = self.resolve_path(str(args.get("path") or "."))
        self.validate_read_path(root)
        pattern = str(args["pattern"])
        matches = []
        for path in sorted(root.rglob("*")):
            if self.is_read_blocked(path) or not path.is_file() or path.stat().st_size > 1_000_000:
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

    def detect_project_environment(self, _args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, **detect_project_environment(self.working_folder)}

    def run_tests(self, args: dict[str, Any]) -> dict[str, Any]:
        environment = detect_project_environment(self.working_folder)
        command = str(args.get("command") or "").strip()
        if not command:
            command = default_test_command(environment, bool(args.get("list_only")))
        test_file = str(args.get("test_file") or "").strip()
        test_name = str(args.get("test_name") or "").strip()
        list_only = bool(args.get("list_only"))
        if test_file and test_file not in command:
            command = append_shell_arg(command, test_file)
        if test_name:
            command = append_test_name_filter(command, test_name)
        if list_only and " --list" not in command and "playwright test" in command:
            command += " --list"
        if self.bash_command_validator:
            violation = self.bash_command_validator(command)
            if violation:
                return {"ok": False, "error": f"test command blocked by agent policy: {violation}", "command": command}
        before = snapshot_protected_paths(self.working_folder, self.bash_protected_prefixes)
        timeout_seconds = int(args.get("timeout_seconds") or self.bash_timeout_seconds)
        started = utc_timestamp()
        completed, timed_out = run_shell_command(command, cwd=self.working_folder, timeout_seconds=timeout_seconds)
        ended = utc_timestamp()
        protected_changes = protected_path_changes(self.working_folder, self.bash_protected_prefixes, before)
        output = normalize_subprocess_output(completed.stdout) + normalize_subprocess_output(completed.stderr)
        output_path = write_tool_result_artifact(self.working_folder, "test-runs", output)
        result = {
            "ok": completed.returncode == 0 and not timed_out and not protected_changes,
            "command": command,
            "returncode": completed.returncode,
            "timed_out": timed_out,
            "started_at": started,
            "ended_at": ended,
            "output_path": output_path,
            "output_tail": output[-8000:],
            "summary": parse_test_output(output, completed.returncode),
        }
        if protected_changes:
            restore_protected_paths(self.working_folder, before, protected_changes)
            result["ok"] = False
            result["error"] = "test command modified protected paths; changes were reverted: " + ", ".join(protected_changes[:20])
        return result

    def latest_test_failure_context(self, args: dict[str, Any]) -> dict[str, Any]:
        max_output_bytes = int(args.get("max_output_bytes") or 8000)
        max_artifact_bytes = int(args.get("max_artifact_bytes") or 12000)
        max_artifacts = int(args.get("max_artifacts") or 3)
        event = latest_failed_run_tests_event(self.tool_events)
        if event is None:
            return {"ok": False, "error": "no failed run_tests event is available"}
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        output_path = str(result.get("output_path") or "")
        output = ""
        if output_path:
            try:
                resolved_output = self.resolve_path(output_path)
                self.validate_read_path(resolved_output)
                output = read_tail(resolved_output, max_output_bytes)
            except Exception:
                output = str(result.get("output_tail") or "")[-max_output_bytes:]
        else:
            output = str(result.get("output_tail") or "")[-max_output_bytes:]
        artifacts = latest_error_context_artifacts(self.working_folder, max_artifacts, max_artifact_bytes)
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        failed_tests = summary.get("failed_tests") if isinstance(summary.get("failed_tests"), list) else []
        bundle = render_failure_context_bundle(
            command=str(result.get("command") or ""),
            returncode=result.get("returncode"),
            timed_out=bool(result.get("timed_out")),
            output_path=output_path,
            failed_tests=[str(item) for item in failed_tests],
            output_tail=output,
            artifacts=artifacts,
        )
        context_path = write_tool_result_artifact(self.working_folder, "failure-context", bundle)
        return {
            "ok": True,
            "context_path": context_path,
            "command": result.get("command"),
            "returncode": result.get("returncode"),
            "timed_out": result.get("timed_out"),
            "output_path": output_path,
            "failed_tests": failed_tests[:10],
            "artifacts": [
                {"path": item["path"], "bytes": item["bytes"], "truncated": item["truncated"]}
                for item in artifacts
            ],
            "content": bundle[: max_output_bytes + max_artifact_bytes],
            "truncated": len(bundle.encode("utf-8")) > max_output_bytes + max_artifact_bytes,
        }

    def capture_visual_snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        if not url:
            raise ValueError("capture_visual_snapshot requires a URL; start a dev server first when needed")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("capture_visual_snapshot URL must be http or https")
        viewport_width = int(args.get("viewport_width") or 1280)
        viewport_height = int(args.get("viewport_height") or 900)
        wait_selector = str(args.get("wait_selector") or "body")
        full_page = bool(args.get("full_page", True))
        timeout_seconds = int(args.get("timeout_seconds") or 30)
        output_dir = runtime_path(self.working_folder, "tool-results", "visual-snapshots")
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ%f")[:22]
        screenshot_path = output_dir / f"{stamp}.png"
        script_path = output_dir / f"{stamp}.cjs"
        script_path.write_text(visual_snapshot_script(), encoding="utf-8")
        completed = subprocess.run(
            [
                "node",
                script_path.as_posix(),
                url,
                screenshot_path.as_posix(),
                str(viewport_width),
                str(viewport_height),
                wait_selector,
                "1" if full_page else "0",
            ],
            cwd=self.working_folder,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        if completed.returncode != 0:
            return {
                "ok": False,
                "url": url,
                "returncode": completed.returncode,
                "error": single_line(stderr or stdout or "visual snapshot command failed", 1000),
                "screenshot_path": relative_to(screenshot_path, self.working_folder) if screenshot_path.exists() else "",
            }
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return {
                "ok": False,
                "url": url,
                "returncode": completed.returncode,
                "error": f"visual snapshot returned invalid JSON: {exc}",
                "stdout": single_line(stdout, 1000),
                "stderr": single_line(stderr, 1000),
            }
        payload["ok"] = True
        payload["screenshot_path"] = relative_to(screenshot_path, self.working_folder)
        payload["script_path"] = relative_to(script_path, self.working_folder)
        self.queue_image_input(screenshot_path, f"Visual snapshot for {url} at {viewport_width}x{viewport_height}")
        if stderr:
            payload["stderr"] = single_line(stderr, 1000)
        return payload

    def evidence_base_dir(self) -> Path:
        if self.live_log_root and self.live_log_root.name == "traces":
            return self.live_log_root.resolve().parent
        return runtime_path(self.working_folder)

    def evidence_report_path(self) -> Path:
        return self.evidence_base_dir() / "evidence.md"

    def append_evidence_note(self, args: dict[str, Any]) -> dict[str, Any]:
        title = str(args.get("title") or "Evidence note").strip() or "Evidence note"
        body = str(args.get("body") or "").strip()
        path = append_evidence_note_file(
            self.working_folder,
            self.evidence_base_dir(),
            title=title,
            body=body,
        )
        return {"ok": True, "evidence_path": relative_to(path, self.working_folder)}

    def append_evidence_command(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ValueError("command is required")
        long_running_violation = long_running_bash_violation(command)
        if long_running_violation:
            return {"ok": False, "captured": False, "error": long_running_violation, "command": command}
        lowered = command.lower()
        if ".hooky" in lowered and self.read_blocked_prefixes:
            return {"ok": False, "captured": False, "error": "command references a path that is not available to this agent", "command": command}
        for blocked in self.bash_blocked_substrings:
            if blocked.lower() in lowered:
                return {"ok": False, "captured": False, "error": f"command blocked by agent policy: {blocked}", "command": command}
        if self.bash_command_validator:
            violation = self.bash_command_validator(command)
            if violation:
                return {"ok": False, "error": f"command blocked by agent policy: {violation}", "command": command}
        timeout_seconds = int(args.get("timeout_seconds") or self.bash_timeout_seconds)
        title = str(args.get("title") or "Command evidence").strip() or "Command evidence"
        before = snapshot_protected_paths(self.working_folder, self.bash_protected_prefixes)
        started = utc_timestamp()
        completed, timed_out = run_shell_command(command, cwd=self.working_folder, timeout_seconds=timeout_seconds)
        ended = utc_timestamp()
        protected_changes = protected_path_changes(self.working_folder, self.bash_protected_prefixes, before)
        output = normalize_subprocess_output(completed.stdout) + normalize_subprocess_output(completed.stderr)
        command_output_path = write_evidence_command_output(
            self.working_folder,
            self.evidence_base_dir(),
            command=command,
            output=output,
        )
        evidence_path = append_evidence_command_file(
            self.working_folder,
            self.evidence_base_dir(),
            title=title,
            command=command,
            returncode=completed.returncode,
            timed_out=timed_out,
            started_at=started,
            ended_at=ended,
            output_path=command_output_path,
            output_tail=output[-4000:],
            protected_changes=protected_changes,
        )
        if protected_changes:
            restore_protected_paths(self.working_folder, before, protected_changes)
        return {
            "ok": completed.returncode == 0 and not timed_out and not protected_changes,
            "captured": True,
            "command": command,
            "returncode": completed.returncode,
            "timed_out": timed_out,
            "started_at": started,
            "ended_at": ended,
            "evidence_path": relative_to(evidence_path, self.working_folder),
            "output_path": relative_to(command_output_path, self.working_folder),
            "output_tail": output[-4000:],
            **({"error": "command modified protected paths; changes were reverted: " + ", ".join(protected_changes[:20])} if protected_changes else {}),
        }

    def append_evidence_screenshot(self, args: dict[str, Any]) -> dict[str, Any]:
        title = str(args.get("title") or "Visual evidence").strip() or "Visual evidence"
        snapshot = self.capture_visual_snapshot(args)
        evidence_path = append_evidence_screenshot_file(
            self.working_folder,
            self.evidence_base_dir(),
            title=title,
            url=str(args.get("url") or ""),
            snapshot=snapshot,
        )
        return {
            **snapshot,
            "evidence_path": relative_to(evidence_path, self.working_folder),
        }

    def queue_image_input(self, path: Path, label: str) -> None:
        resolved = path if path.is_absolute() else self.working_folder / path
        if not resolved.exists() or not resolved.is_file():
            return
        self.pending_image_inputs.append(
            {
                "path": relative_to(resolved, self.working_folder),
                "label": label,
            }
        )

    def git_status(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self.run_git(["status", "--short"])

    def git_diff(self, args: dict[str, Any]) -> dict[str, Any]:
        command = ["diff"]
        if bool(args.get("staged")):
            command.append("--cached")
        path = str(args.get("path") or "").strip()
        if path:
            resolved = self.resolve_path(path)
            self.validate_read_path(resolved)
            command.extend(["--", path])
        return self.run_git(command, max_bytes=int(args.get("max_bytes") or 20000))

    def git_show(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = str(args.get("ref") or "HEAD")
        path = str(args.get("path") or "").strip()
        if path:
            resolved = self.resolve_path(path)
            self.validate_read_path(resolved)
            spec = f"{ref}:{path}"
        else:
            spec = ref
        return self.run_git(["show", "--no-ext-diff", spec], max_bytes=int(args.get("max_bytes") or 20000))

    def run_git(self, command: list[str], max_bytes: int = 20000) -> dict[str, Any]:
        completed = subprocess.run(
            ["git", "--no-pager", *command],
            cwd=self.working_folder,
            text=True,
            capture_output=True,
            timeout=30,
        )
        stdout = sanitize_output_for_read_policy(completed.stdout[-max_bytes:], self.read_blocked_prefixes)
        stderr = sanitize_output_for_read_policy(completed.stderr[-max_bytes:], self.read_blocked_prefixes)
        return {"ok": completed.returncode == 0, "returncode": completed.returncode, "stdout": stdout, "stderr": stderr}

    def bash(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args["command"])
        lowered = command.lower()
        long_running_violation = long_running_bash_violation(command)
        if long_running_violation:
            return {"ok": False, "error": long_running_violation}
        if ".hooky" in lowered and self.read_blocked_prefixes:
            return {"ok": False, "error": "bash command references a path that is not available to this agent"}
        for blocked in self.bash_blocked_substrings:
            if blocked.lower() in lowered:
                return {"ok": False, "error": f"bash command blocked by agent policy: {blocked}"}
        if self.bash_command_validator:
            violation = self.bash_command_validator(command)
            if violation:
                return {"ok": False, "error": f"bash command blocked by agent policy: {violation}"}
        before = snapshot_protected_paths(self.working_folder, self.bash_protected_prefixes)
        completed, timed_out = run_shell_command(command, cwd=self.working_folder, timeout_seconds=self.bash_timeout_seconds)
        protected_changes = protected_path_changes(self.working_folder, self.bash_protected_prefixes, before)
        if protected_changes:
            restore_protected_paths(self.working_folder, before, protected_changes)
            return {
                "ok": False,
                "returncode": completed.returncode,
                "stdout": completed.stdout[-8000:],
                "stderr": completed.stderr[-8000:],
                "error": "bash command modified protected paths; changes were reverted: " + ", ".join(protected_changes[:20]),
            }
        result = {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-8000:],
            "stderr": completed.stderr[-8000:],
        }
        if timed_out:
            result["error"] = f"Command '{command}' timed out after {self.bash_timeout_seconds} seconds; process group was terminated"
        return result

    def start_process(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args["command"])
        wait_for_url = str(args.get("wait_for_url") or "").strip()
        requested_ports = set(requested_ports_from_command(command + " " + wait_for_url))
        explicit_port = int(args.get("port") or 0)
        if explicit_port:
            requested_ports.add(explicit_port)
        allocated_port = None
        env = os.environ.copy()
        if bool(args.get("auto_allocate_port")):
            allocated_port = explicit_port if explicit_port else allocate_tcp_port()
            requested_ports.add(allocated_port)
            env["PORT"] = str(allocated_port)
            env["HOOKY_PORT"] = str(allocated_port)
        requested_ports_list = sorted(requested_ports)
        busy_ports = [port for port in requested_ports_list if tcp_port_is_listening(port)]
        if busy_ports:
            return {
                "ok": False,
                "error": "requested port already in use: " + ", ".join(str(port) for port in busy_ports),
                "command": command,
                "requested_ports": requested_ports_list,
                "allocated_port": allocated_port,
                "ports": [],
                "listeners": [],
            }
        process_id = f"proc-{self.next_process_id}"
        self.next_process_id += 1
        log_path = self.working_folder / ".hooky" / "managed-processes" / f"{process_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=self.working_folder,
            shell=True,
            text=True,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        process._hooky_log_handle = log_handle  # type: ignore[attr-defined]
        process._hooky_log_path = log_path  # type: ignore[attr-defined]
        process._hooky_command = command  # type: ignore[attr-defined]
        process._hooky_name = str(args.get("name") or "process")  # type: ignore[attr-defined]
        process._hooky_requested_ports = requested_ports_list  # type: ignore[attr-defined]
        process._hooky_allocated_port = allocated_port  # type: ignore[attr-defined]
        self.managed_processes[process_id] = process
        wait_seconds = int(args.get("wait_seconds") or 0)
        ready = False
        if wait_for_url:
            ready = wait_for_http_url(wait_for_url, wait_seconds)
        elif wait_seconds > 0:
            time.sleep(wait_seconds)
        listeners = process_listeners(process.pid)
        ports = sorted({int(item["port"]) for item in listeners})
        url = process_url_from_ports(ports)
        return {
            "ok": process.poll() is None,
            "process_id": process_id,
            "pid": process.pid,
            "name": process._hooky_name,  # type: ignore[attr-defined]
            "command": command,
            "requested_ports": requested_ports_list,
            "allocated_port": allocated_port,
            "ports": ports,
            "url": url,
            "listeners": listeners,
            "log_path": relative_to(log_path, self.working_folder),
            "ready": ready if wait_for_url else None,
            "returncode": process.poll(),
            "output": read_tail(log_path, 4000),
        }

    def read_process(self, args: dict[str, Any]) -> dict[str, Any]:
        process = self.require_process(str(args["process_id"]))
        log_path = process._hooky_log_path  # type: ignore[attr-defined]
        listeners = process_listeners(process.pid)
        ports = sorted({int(item["port"]) for item in listeners})
        return {
            "ok": True,
            "process_id": str(args["process_id"]),
            "running": process.poll() is None,
            "returncode": process.poll(),
            "ports": ports,
            "url": process_url_from_ports(ports),
            "listeners": listeners,
            "output": read_tail(log_path, int(args.get("max_bytes") or 8000)),
        }

    def stop_process(self, args: dict[str, Any]) -> dict[str, Any]:
        process_id = str(args["process_id"])
        process = self.require_process(process_id)
        stopped = stop_managed_process(process)
        self.managed_processes.pop(process_id, None)
        return {
            "ok": True,
            "process_id": process_id,
            "stopped": stopped,
            "returncode": process.poll(),
            "output": read_tail(process._hooky_log_path, 4000),  # type: ignore[attr-defined]
        }

    def list_processes(self, _args: dict[str, Any]) -> dict[str, Any]:
        processes = []
        for process_id, process in self.managed_processes.items():
            listeners = process_listeners(process.pid)
            ports = sorted({int(item["port"]) for item in listeners})
            processes.append(
                {
                    "process_id": process_id,
                    "pid": process.pid,
                    "name": process._hooky_name,  # type: ignore[attr-defined]
                    "command": process._hooky_command,  # type: ignore[attr-defined]
                    "running": process.poll() is None,
                    "returncode": process.poll(),
                    "requested_ports": list(getattr(process, "_hooky_requested_ports", requested_ports_from_command(process._hooky_command))),  # type: ignore[attr-defined]
                    "allocated_port": getattr(process, "_hooky_allocated_port", None),
                    "ports": ports,
                    "url": process_url_from_ports(ports),
                    "listeners": listeners,
                    "log_path": relative_to(process._hooky_log_path, self.working_folder),  # type: ignore[attr-defined]
                }
            )
        return {
            "ok": True,
            "processes": processes,
        }

    def require_process(self, process_id: str) -> subprocess.Popen[str]:
        process = self.managed_processes.get(process_id)
        if process is None:
            raise ValueError(f"unknown managed process: {process_id}")
        return process

    def cleanup_processes(self) -> None:
        for process_id, process in list(self.managed_processes.items()):
            stop_managed_process(process)
            self.managed_processes.pop(process_id, None)

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


def visual_snapshot_script() -> str:
    return r"""
const fs = require('fs');

async function loadPlaywright() {
  try {
    return require('playwright');
  } catch (firstError) {
    try {
      return require('@playwright/test');
    } catch (_secondError) {
      throw firstError;
    }
  }
}

(async () => {
  const [url, screenshotPath, widthRaw, heightRaw, waitSelector, fullPageRaw] = process.argv.slice(2);
  const width = Number(widthRaw || 1280);
  const height = Number(heightRaw || 900);
  const fullPage = fullPageRaw === '1';
  const consoleMessages = [];
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width, height } });
  page.on('console', message => {
    if (['error', 'warning'].includes(message.type())) {
      consoleMessages.push({ type: message.type(), text: message.text().slice(0, 500) });
    }
  });
  page.on('pageerror', error => {
    consoleMessages.push({ type: 'pageerror', text: String(error.message || error).slice(0, 500) });
  });
  await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });
  if (waitSelector) {
    await page.waitForSelector(waitSelector, { timeout: 10000 });
  }
  const metrics = await page.evaluate(() => {
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    const doc = document.documentElement;
    const body = document.body;
    const visibleElements = [];
    const interactiveElements = [];
    const textBlocks = [];
    const selectors = 'a,button,input,textarea,select,[role="button"],[role="link"],[tabindex]';
    function isVisible(element, rect, style) {
      return rect.width > 0 && rect.height > 0 &&
        style.visibility !== 'hidden' &&
        style.display !== 'none' &&
        Number(style.opacity || '1') > 0;
    }
    function asRect(rect) {
      return {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        right: Math.round(rect.right),
        bottom: Math.round(rect.bottom),
      };
    }
    for (const element of Array.from(document.body.querySelectorAll('*'))) {
      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      if (!isVisible(element, rect, style)) continue;
      const tag = element.tagName.toLowerCase();
      const item = {
        tag,
        id: element.id || '',
        className: typeof element.className === 'string' ? element.className.slice(0, 120) : '',
        role: element.getAttribute('role') || '',
        text: (element.innerText || element.getAttribute('aria-label') || element.getAttribute('placeholder') || '').replace(/\s+/g, ' ').trim().slice(0, 160),
        rect: asRect(rect),
      };
      visibleElements.push(item);
      if (element.matches(selectors)) interactiveElements.push(item);
      if (item.text && rect.width > 10 && rect.height > 10) textBlocks.push(item);
    }
    const rects = visibleElements.map(item => item.rect).filter(rect => rect.width > 0 && rect.height > 0);
    const clippedElements = visibleElements.filter(item =>
      item.rect.x < 0 ||
      item.rect.y < 0 ||
      item.rect.right > viewport.width ||
      item.rect.bottom > viewport.height
    );
    const intersectionArea = rects.reduce((total, rect) => {
      const width = Math.max(0, Math.min(rect.right, viewport.width) - Math.max(rect.x, 0));
      const height = Math.max(0, Math.min(rect.bottom, viewport.height) - Math.max(rect.y, 0));
      return total + width * height;
    }, 0);
    let bounds = null;
    if (rects.length) {
      bounds = {
        x: Math.min(...rects.map(rect => rect.x)),
        y: Math.min(...rects.map(rect => rect.y)),
        right: Math.max(...rects.map(rect => rect.right)),
        bottom: Math.max(...rects.map(rect => rect.bottom)),
      };
      bounds.width = bounds.right - bounds.x;
      bounds.height = bounds.bottom - bounds.y;
    }
    const viewportArea = viewport.width * viewport.height;
    const boundsArea = bounds ? Math.max(bounds.width, 0) * Math.max(bounds.height, 0) : 0;
    const center = bounds ? {
      x: Math.round(bounds.x + bounds.width / 2),
      y: Math.round(bounds.y + bounds.height / 2),
      offsetX: Math.round(bounds.x + bounds.width / 2 - viewport.width / 2),
      offsetY: Math.round(bounds.y + bounds.height / 2 - viewport.height / 2),
    } : null;
    const headings = Array.from(document.querySelectorAll('h1,h2,h3,[role="heading"]')).map(element => ({
      text: (element.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 160),
      tag: element.tagName.toLowerCase(),
      role: element.getAttribute('role') || '',
      rect: asRect(element.getBoundingClientRect()),
    }));
    function intersectionRatio(a, b) {
      const width = Math.max(0, Math.min(a.right, b.right) - Math.max(a.x, b.x));
      const height = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y));
      const area = width * height;
      const smallest = Math.max(1, Math.min(a.width * a.height, b.width * b.height));
      return Number((area / smallest).toFixed(4));
    }
    const headingInteractiveOverlaps = [];
    for (const heading of headings) {
      for (const interactive of interactiveElements) {
        const ratio = intersectionRatio(heading.rect, interactive.rect);
        if (ratio > 0.01) {
          headingInteractiveOverlaps.push({
            headingText: heading.text,
            headingTag: heading.tag,
            headingRect: heading.rect,
            interactiveText: interactive.text,
            interactiveTag: interactive.tag,
            interactiveRole: interactive.role,
            interactiveClassName: interactive.className,
            interactiveRect: interactive.rect,
            overlapRatio: ratio,
          });
        }
      }
    }
    return {
      title: document.title,
      location: window.location.href,
      viewport,
      document: {
        scrollWidth: doc.scrollWidth,
        scrollHeight: doc.scrollHeight,
        bodyTextLength: (body.innerText || '').length,
      },
      contentBounds: bounds,
      contentCenter: center,
      viewportCoverage: Number((boundsArea / viewportArea).toFixed(4)),
      visiblePaintCoverage: Number((intersectionArea / viewportArea).toFixed(4)),
      topGapRatio: bounds ? Number((Math.max(bounds.y, 0) / viewport.height).toFixed(4)) : 1,
      leftGapRatio: bounds ? Number((Math.max(bounds.x, 0) / viewport.width).toFixed(4)) : 1,
      horizontalOverflow: doc.scrollWidth > viewport.width + 2,
      verticalOverflow: doc.scrollHeight > viewport.height + 2,
      clippedElementCount: clippedElements.length,
      headingInteractiveOverlapCount: headingInteractiveOverlaps.length,
      visibleElementCount: visibleElements.length,
      interactiveElementCount: interactiveElements.length,
      headings: headings.slice(0, 12),
      sampleHeadingInteractiveOverlaps: headingInteractiveOverlaps.slice(0, 20),
      sampleClippedElements: clippedElements.slice(0, 20),
      sampleVisibleElements: visibleElements.slice(0, 40),
      sampleInteractiveElements: interactiveElements.slice(0, 30),
      sampleTextBlocks: textBlocks.slice(0, 30),
    };
  });
  await page.screenshot({ path: screenshotPath, fullPage });
  await browser.close();
  console.log(JSON.stringify({
    url,
    screenshotBytes: fs.statSync(screenshotPath).size,
    consoleMessages,
    metrics,
  }));
})().catch(error => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""


def run_tool_agent(
    *,
    model: str,
    system: str,
    user: str,
    runtime: ToolRuntime,
) -> AgentRunResult:
    transcript: list[dict[str, Any]] = []
    tool_events: list[dict[str, Any]] = []
    compaction_events: list[dict[str, Any]] = []
    pre_compaction_archives: list[dict[str, Any]] = []
    total_usage: dict[str, Any] = {"cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    started_at = utc_timestamp()
    soft_deadline_sent = False
    consecutive_no_tool_responses = 0
    messages: list[Any] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    transcript.extend(
        [
            {
                "role": "system",
                "message": system,
                "started_at": utc_timestamp(),
                "ended_at": utc_timestamp(),
            },
            {
                "role": "user",
                "kind": "initial",
                "message": user,
                "started_at": utc_timestamp(),
                "ended_at": utc_timestamp(),
            },
        ]
    )
    preselected_skill_message = preselected_skills_message(runtime)
    if preselected_skill_message:
        messages.append(preselected_skill_message)
        transcript.append(
            {
                "role": "skill_activation",
                "message": preselected_skill_message["content"],
                "started_at": utc_timestamp(),
                "ended_at": utc_timestamp(),
            }
        )
    if runtime.initial_image_paths:
        initial_images = [
            {"path": relative_to((path if path.is_absolute() else runtime.working_folder / path), runtime.working_folder), "label": "Initial visual evidence"}
            for path in runtime.initial_image_paths
        ]
        image_message = image_input_message(runtime, initial_images, "Initial visual evidence attached for inspection.")
        if image_message:
            messages.append(image_message)
            transcript.append(
                {
                    "role": "image_input",
                    "message": "Initial visual evidence attached for inspection.",
                    "images": initial_images,
                    "started_at": utc_timestamp(),
                    "ended_at": utc_timestamp(),
                }
            )

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
    stop_heartbeat = threading.Event()
    heartbeat_thread = start_heartbeat_thread(runtime, model, total_usage, stop_heartbeat)
    try:
        with model_client(model) as client:
            while runtime.final_report is None:
                elapsed_seconds = time.monotonic() - runtime.started_at
                if elapsed_seconds > runtime.max_seconds:
                    raise AgentRunError(f"agent runtime exceeded {runtime.max_seconds}s", current_result())
                if float(total_usage.get("cost") or 0) > runtime.max_cost_usd:
                    raise AgentRunError(f"agent runtime exceeded ${runtime.max_cost_usd:.4f} cost budget", current_result())
                remaining_seconds = max(0, int(runtime.max_seconds - elapsed_seconds))
                if not soft_deadline_sent and runtime.max_seconds >= 60 and elapsed_seconds >= runtime.max_seconds * 0.85:
                    soft_deadline_sent = True
                    warning = (
                        f"Runtime soft deadline: about {remaining_seconds}s remain before the hard timeout. "
                        "If recent tool results show useful progress and one concrete next step remains, you may call "
                        "request_time_extension with the failing tests, current status, and next command. Otherwise, "
                        "call final_report now with current status, concrete failures, and next steps instead of "
                        "starting another long debugging cycle."
                    )
                    messages.append({"role": "user", "content": warning})
                    transcript.append(
                        {
                            "role": "user",
                            "kind": "soft_deadline",
                            "message": warning,
                            "started_at": utc_timestamp(),
                            "ended_at": utc_timestamp(),
                        }
                    )
                    append_live_event(runtime, f"{utc_timestamp()} runtime_notice kind=soft_deadline remaining_seconds={remaining_seconds}")
                    flush_live_log()

                request_seconds = model_request_deadline_seconds(runtime, elapsed_seconds)
                try:
                    with LocalDeadline(request_seconds, f"agent model request exceeded {request_seconds}s"):
                        messages, compaction_event, pre_compaction_archive = maybe_compact_messages(client, model, runtime, messages)
                except TimeoutError as exc:
                    raise AgentRunError(str(exc), current_result()) from exc
                if pre_compaction_archive:
                    pre_compaction_archives.append(pre_compaction_archive)
                    transcript.append({"role": "pre_compaction", **pre_compaction_archive})
                    flush_live_log()
                if compaction_event:
                    runtime.advance_read_generation()
                    usage = compaction_event.get("usage") or {}
                    accumulate_usage(total_usage, usage)
                    compaction_events.append(compaction_event)
                    transcript.append({"role": "compaction", **compaction_event})
                    flush_live_log()
                    if float(total_usage.get("cost") or 0) > runtime.max_cost_usd:
                        raise AgentRunError(f"agent runtime exceeded ${runtime.max_cost_usd:.4f} cost budget after compaction", current_result())

                if runtime.pending_image_inputs:
                    attachments = list(runtime.pending_image_inputs)
                    runtime.pending_image_inputs.clear()
                    image_message = image_input_message(runtime, attachments, "Visual evidence attached. Inspect the image pixels directly before continuing.")
                    if image_message:
                        messages.append(image_message)
                        transcript.append(
                            {
                                "role": "image_input",
                                "images": [{"path": item["path"], "label": item.get("label", "")} for item in attachments],
                                "started_at": utc_timestamp(),
                                "ended_at": utc_timestamp(),
                            }
                        )
                        append_live_event(runtime, f"{utc_timestamp()} image_input count={len(attachments)} paths={','.join(item['path'] for item in attachments)}")
                        flush_live_log()

                assistant_started_at = utc_timestamp()
                assistant_start = time.monotonic()
                request_seconds = model_request_deadline_seconds(runtime, time.monotonic() - runtime.started_at)
                try:
                    with LocalDeadline(request_seconds, f"agent model request exceeded {request_seconds}s"):
                        completion = client.chat.send(
                            model=model,
                            messages=messages,
                            tools=runtime.tools(),
                            tool_choice="auto",
                            **model_request_options(model),
                        )
                except TimeoutError as exc:
                    raise AgentRunError(str(exc), current_result()) from exc
                assistant_ended_at = utc_timestamp()
                assistant_duration_ms = round((time.monotonic() - assistant_start) * 1000, 2)
                usage = response_usage(completion)
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
                    assistant_text = assistant_message_text(message_payload)
                    text_actions = extract_text_tool_actions(assistant_text)
                    if text_actions:
                        recovered_results: list[dict[str, Any]] = []
                        for action in text_actions:
                            raw_name = str(action["name"])
                            name = runtime.canonical_tool_name(raw_name)
                            tool_started_at = utc_timestamp()
                            tool_start = time.monotonic()
                            args = dict(action["arguments"])
                            result = runtime.run_tool(name, args)
                            event = {
                                "tool_call_id": f"text-tool-{len(tool_events) + 1}",
                                "name": name,
                                "arguments": args,
                                "result": result,
                                "started_at": tool_started_at,
                                "ended_at": utc_timestamp(),
                                "duration_ms": round((time.monotonic() - tool_start) * 1000, 2),
                                "source": "assistant_text",
                            }
                            if raw_name != name:
                                event["raw_name"] = raw_name
                            tool_events.append(event)
                            runtime.tool_events.append(event)
                            transcript.append({"role": "tool", **event})
                            append_live_event(runtime, format_runtime_event_line(transcript[-1]))
                            recovered_results.append({"name": name, "result": result})
                            if name == "final_report" and runtime.final_report is not None:
                                break
                        transcript.append(
                            {
                                "role": "runtime_notice",
                                "kind": "recovered_text_tool_calls",
                                "message": f"Recovered {len(recovered_results)} tool call(s) from assistant text.",
                                "started_at": utc_timestamp(),
                                "ended_at": utc_timestamp(),
                            }
                        )
                        append_live_event(runtime, format_runtime_event_line(transcript[-1]))
                        flush_live_log()
                        if runtime.final_report is not None:
                            break
                        messages.append(
                            {
                                "role": "user",
                                "content": "Recovered text tool call results:\n" + json.dumps(recovered_results, sort_keys=True),
                            }
                        )
                        continue
                    recovered_report = recover_text_final_report(runtime, assistant_text)
                    if recovered_report is not None:
                        transcript.append(
                            {
                                "role": "runtime_notice",
                                "kind": "recovered_text_final_report",
                                "message": "Recovered final_report from assistant text because no tool call was emitted.",
                                "started_at": utc_timestamp(),
                                "ended_at": utc_timestamp(),
                            }
                        )
                        append_live_event(runtime, format_runtime_event_line(transcript[-1]))
                        flush_live_log()
                        break
                    consecutive_no_tool_responses += 1
                    if runtime.no_tool_response_limit > 0 and consecutive_no_tool_responses > runtime.no_tool_response_limit:
                        raise AgentRunError(
                            f"agent produced {consecutive_no_tool_responses} consecutive assistant messages without tool calls",
                            current_result(),
                        )
                    prompt = "Continue by using the available tools. Finish only by calling final_report."
                    messages.append({"role": "user", "content": prompt})
                    transcript.append(
                        {
                            "role": "user",
                            "kind": "no_tool_calls",
                            "message": prompt,
                            "started_at": utc_timestamp(),
                            "ended_at": utc_timestamp(),
                        }
                    )
                    append_live_event(runtime, format_runtime_event_line(transcript[-1]))
                    flush_live_log()
                    continue
                consecutive_no_tool_responses = 0

                for tool_call in tool_calls:
                    raw_name = tool_call.function.name
                    name = runtime.canonical_tool_name(raw_name)
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
                    if raw_name != name:
                        event["raw_name"] = raw_name
                    tool_events.append(event)
                    runtime.tool_events.append(event)
                    transcript.append({"role": "tool", **event})
                    append_live_event(runtime, format_runtime_event_line(transcript[-1]))
                    if name == "run_tests":
                        grace = runtime.grant_post_success_grace(result)
                        if grace:
                            notice = (
                                "Post-success grace: tests passed near the runtime deadline. "
                                f"Added {grace['added_seconds']}s for final todo/reporting."
                            )
                            transcript.append(
                                {
                                    "role": "runtime_notice",
                                    "message": notice,
                                    "started_at": utc_timestamp(),
                                    "ended_at": utc_timestamp(),
                                }
                            )
                            append_live_event(
                                runtime,
                                (
                                    f"{utc_timestamp()} runtime_notice kind=post_success_grace "
                                    f"added_seconds={grace['added_seconds']} max_seconds={int(grace['max_seconds'])}"
                                ),
                            )
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
    finally:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1)
        runtime.cleanup_processes()

    append_live_event(runtime, f"{utc_timestamp()} run success tool_calls={len(tool_events)} cost=${float(total_usage.get('cost') or 0):.8f}")
    flush_live_log(status="success")
    return current_result()


def assistant_message_text(message_payload: dict[str, Any]) -> str:
    content = message_payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
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


def start_heartbeat_thread(
    runtime: ToolRuntime,
    model: str,
    total_usage: dict[str, Any],
    stop_event: threading.Event,
) -> threading.Thread | None:
    if runtime.heartbeat_seconds <= 0:
        return None

    def emit_heartbeats() -> None:
        while not stop_event.wait(runtime.heartbeat_seconds):
            elapsed = int(time.monotonic() - runtime.started_at)
            append_live_event(runtime, f"{utc_timestamp()} heartbeat source=local model={model} elapsed_seconds={elapsed}")

    thread = threading.Thread(target=emit_heartbeats, name="hooky-agent-heartbeat", daemon=True)
    thread.start()
    return thread


def preselected_skills_message(runtime: ToolRuntime) -> dict[str, Any] | None:
    if not runtime.preselected_skill_names:
        return None
    activated = []
    errors = []
    for name in runtime.preselected_skill_names:
        try:
            activated.append(runtime.activate_skill({"name": name}))
        except Exception as exc:  # noqa: BLE001 - invalid operator-selected skills should be visible.
            errors.append({"name": name, "error": str(exc)})
    if not activated and not errors:
        return None
    lines = ["Preselected Agent Skills loaded by the harness:"]
    for result in activated:
        lines.append(f"\n--- {result['name']} ({result['skill_path']}) ---")
        if result.get("description"):
            lines.append(str(result["description"]))
        lines.append(str(result["body"]))
        resources = result.get("resources") if isinstance(result.get("resources"), list) else []
        if resources:
            lines.append("\nResources available via read_skill_resource:")
            for resource in resources[:50]:
                if isinstance(resource, dict):
                    lines.append(f"- {resource.get('path')} ({resource.get('bytes')} bytes)")
    if errors:
        lines.append("\nSkill activation errors:")
        for error in errors:
            lines.append(f"- {error['name']}: {error['error']}")
    return {"role": "user", "content": "\n".join(lines)}


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
    compaction_model = os.environ.get("COMPACTION_MODEL", model)
    compaction_messages = [
        {"role": "system", "content": compaction_system_prompt(runtime)},
        {"role": "user", "content": prompt},
    ]
    compaction_kwargs = {
        "model": compaction_model,
        "messages": compaction_messages,
        "response_format": structured_response_format("context_compaction", compaction_schema()),
        **model_request_options(compaction_model),
    }
    if model_provider(compaction_model) == model_provider(model):
        completion = client.chat.send(**compaction_kwargs)
    else:
        with model_client(compaction_model) as compaction_client:
            completion = compaction_client.chat.send(**compaction_kwargs)
    content = completion.choices[0].message.content
    if not content:
        return messages, None, archive
    payload = json.loads(content)
    runtime.anchored_summary = payload["summary"]
    summary_messages = [
        {
            "role": "user",
            "content": "Anchored context summary for continuing this agent run:\n\n" + runtime.anchored_summary,
        }
    ]
    active_skill_message = activated_skills_compaction_message(runtime)
    if active_skill_message:
        summary_messages.append(active_skill_message)
    compacted = prefix + summary_messages + recent
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
        "usage": response_usage(completion),
    }
    return compacted, event, archive


def activated_skills_compaction_message(runtime: ToolRuntime) -> dict[str, Any] | None:
    if not runtime.activated_skill_names:
        return None
    lines = ["Active Agent Skills that remain loaded after context compaction:"]
    for name in sorted(runtime.activated_skill_names):
        try:
            result = runtime.activate_skill({"name": name})
        except Exception:
            continue
        lines.append(f"\n--- {result['name']} ({result['skill_path']}) ---")
        if result.get("description"):
            lines.append(str(result["description"]))
        lines.append(str(result["body"]))
        resources = result.get("resources") if isinstance(result.get("resources"), list) else []
        if resources:
            lines.append("\nResources available via read_skill_resource:")
            for resource in resources[:50]:
                if isinstance(resource, dict):
                    lines.append(f"- {resource.get('path')} ({resource.get('bytes')} bytes)")
    return {"role": "user", "content": "\n".join(lines)}


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
    write_runtime_invocation_archive(
        report_root,
        transcript,
        tool_events,
        compaction_events,
        pre_compaction_archives,
        metadata or {"schema_version": 1, "written_at": utc_timestamp()},
    )


def write_runtime_invocation_archive(
    report_root: Path,
    transcript: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    compaction_events: list[dict[str, Any]],
    pre_compaction_archives: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> None:
    agent_name = safe_path_segment(str(metadata.get("agent_name") or "agent"))
    timestamp = safe_path_segment(str(metadata.get("started_at") or metadata.get("written_at") or utc_timestamp()))
    archive_root = report_root / "invocations" / f"{timestamp}-{agent_name}"
    archive_root.mkdir(parents=True, exist_ok=True)
    (archive_root / "runtime_transcript.json").write_text(json.dumps(transcript, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (archive_root / "tool_events.json").write_text(json.dumps(tool_events, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (archive_root / "tool_calls.md").write_text(render_tool_calls_markdown(tool_events), encoding="utf-8")
    (archive_root / "runtime_timeline.md").write_text(render_runtime_timeline_markdown(transcript), encoding="utf-8")
    (archive_root / "runtime_events.snapshot.log").write_text(render_runtime_events_log(transcript), encoding="utf-8")
    (archive_root / "compaction_events.json").write_text(json.dumps(compaction_events, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (archive_root / "pre_compaction_archives.json").write_text(json.dumps(pre_compaction_archives, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (archive_root / "runtime_metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_path_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-") or "unknown"


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
    lines = [
        format_runtime_event_line(item)
        for item in transcript
        if item.get("role") in {"system", "user", "assistant", "tool", "runtime_notice", "compaction", "pre_compaction"}
    ]
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
            display_tool_call_name(str(call.get("function", {}).get("name")))
            for call in tool_calls
            if isinstance(call, dict) and isinstance(call.get("function"), dict) and call.get("function", {}).get("name")
        ]
        assistant_text = assistant_message_text(message)
        return (
            f"{timestamp} assistant duration={duration} cost=${float(usage.get('cost') or 0):.8f} "
            f"tokens={int(usage.get('total_tokens') or 0)} tool_calls={len(tool_calls)}"
            + (f" tools={','.join(names)}" if names else "")
            + (f" message={quote_value(single_line(assistant_text, 320), 320)}" if assistant_text else "")
        )
    if role == "tool":
        name = str(item.get("name") or "unknown")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        status = "ok" if result.get("ok") is True else "error" if result.get("ok") is False else "unknown"
        return f"{timestamp} tool name={name} status={status} duration={duration} {tail_detail(name, arguments, result)}".rstrip()
    if role in {"system", "user", "runtime_notice"}:
        kind = str(item.get("kind") or "message")
        message = single_line(str(item.get("message") or ""), 180)
        return f"{timestamp} {role} kind={kind} message={quote_value(message, 180)}"
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
    if name == "run_tests":
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        detail = (
            f"command={quote_value(str(result.get('command') or ''), 180)} "
            f"returncode={result.get('returncode')} passed={summary.get('passed')} "
            f"failed={summary.get('failed')} output_path={quote_value(str(result.get('output_path') or ''), 180)}"
        )
        failed_tests = summary.get("failed_tests") if isinstance(summary.get("failed_tests"), list) else []
        if failed_tests:
            detail += f" failing={quote_value(single_line('; '.join(str(item) for item in failed_tests[:3]), 180), 180)}"
        return detail
    if name == "latest_test_failure_context":
        failed_tests = result.get("failed_tests") if isinstance(result.get("failed_tests"), list) else []
        detail = (
            f"context_path={quote_value(str(result.get('context_path') or ''), 180)} "
            f"failed_tests={len(failed_tests)} artifacts={len(result.get('artifacts') or [])}"
        )
        if failed_tests:
            detail += f" failing={quote_value(single_line('; '.join(str(item) for item in failed_tests[:2]), 180), 180)}"
        return detail
    if name == "capture_visual_snapshot":
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        detail = (
            f"url={quote_value(str(result.get('url') or arguments.get('url') or ''), 180)} "
            f"screenshot_path={quote_value(str(result.get('screenshot_path') or ''), 180)} "
            f"coverage={metrics.get('viewportCoverage')} top_gap={metrics.get('topGapRatio')} "
            f"elements={metrics.get('visibleElementCount')}"
        )
        if metrics.get("headingInteractiveOverlapCount"):
            detail += f" heading_control_overlaps={metrics.get('headingInteractiveOverlapCount')}"
        console_messages = result.get("consoleMessages") if isinstance(result.get("consoleMessages"), list) else []
        if console_messages:
            detail += f" console_messages={len(console_messages)}"
        return detail
    if name in {"append_evidence_note", "append_evidence_command", "append_evidence_screenshot"}:
        detail = f"evidence_path={quote_value(str(result.get('evidence_path') or ''), 180)}"
        if result.get("command"):
            detail += f" command={quote_value(str(result.get('command') or ''), 180)} returncode={result.get('returncode')}"
        if result.get("screenshot_path"):
            detail += f" screenshot_path={quote_value(str(result.get('screenshot_path') or ''), 180)}"
        if result.get("output_path"):
            detail += f" output_path={quote_value(str(result.get('output_path') or ''), 180)}"
        return detail
    if name == "detect_project_environment":
        return (
            f"package_manager={quote_value(str(result.get('package_manager') or ''), 80)} "
            f"scripts={len(result.get('scripts') or {})} test_commands={len(result.get('test_commands') or [])}"
        )
    if name in {"git_status", "git_diff", "git_show"}:
        stdout = single_line(str(result.get("stdout") or ""), 180)
        detail = f"returncode={result.get('returncode')}"
        if stdout:
            detail += f" stdout={quote_value(stdout, 180)}"
        return detail
    if name == "start_process":
        ports = ",".join(str(port) for port in result.get("ports") or [])
        requested_ports = ",".join(str(port) for port in result.get("requested_ports") or [])
        detail = (
            f"process_id={quote_value(str(result.get('process_id') or ''), 80)} "
            f"pid={result.get('pid')} ready={result.get('ready')} "
            f"command={quote_value(str(arguments.get('command') or ''), 180)}"
        )
        if requested_ports:
            detail += f" requested_ports={quote_value(requested_ports, 80)}"
        if result.get("allocated_port"):
            detail += f" allocated_port={result.get('allocated_port')}"
        if ports:
            detail += f" ports={quote_value(ports, 80)}"
        if result.get("url"):
            detail += f" url={quote_value(str(result.get('url')), 120)}"
        output = single_line(str(result.get("output") or ""), 180)
        if output:
            detail += f" output={quote_value(output, 180)}"
        return detail
    if name == "read_process":
        output = single_line(str(result.get("output") or ""), 180)
        detail = (
            f"process_id={quote_value(str(arguments.get('process_id') or ''), 80)} "
            f"running={result.get('running')} returncode={result.get('returncode')}"
        )
        if output:
            detail += f" output={quote_value(output, 180)}"
        return detail
    if name == "stop_process":
        return (
            f"process_id={quote_value(str(arguments.get('process_id') or ''), 80)} "
            f"stopped={result.get('stopped')} returncode={result.get('returncode')}"
        )
    if name == "list_processes":
        processes = result.get("processes") if isinstance(result.get("processes"), list) else []
        running = sum(1 for item in processes if isinstance(item, dict) and item.get("running") is True)
        ports = sorted({str(port) for item in processes if isinstance(item, dict) for port in item.get("ports") or []})
        detail = f"processes={len(processes)} running={running}"
        if ports:
            detail += f" ports={quote_value(','.join(ports), 120)}"
        return detail
    if name == "activate_skill":
        resources = result.get("resources") if isinstance(result.get("resources"), list) else []
        return f"name={quote_value(str(result.get('name') or arguments.get('name') or ''), 120)} resources={len(resources)} already_active={result.get('already_active')}"
    if name == "read_skill_resource":
        return f"name={quote_value(str(result.get('name') or arguments.get('name') or ''), 120)} path={quote_value(str(result.get('path') or arguments.get('path') or ''), 180)} bytes={result.get('bytes')}"
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
    if name == "search_files":
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


def canonical_tool_name(name: str, valid_names: Any) -> str:
    valid = set(str(item) for item in valid_names)
    if name in valid:
        return name
    for separator in ("<|channel|>", "."):
        if separator in name:
            candidate = name.split(separator, 1)[0]
            if candidate in valid:
                return candidate
    return name


def display_tool_call_name(name: str) -> str:
    for separator in ("<|channel|>", "."):
        if separator in name:
            return name.split(separator, 1)[0]
    return name


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
    value = item.get("text") or item.get("description") or item.get("content") or item.get("task") or item.get("title")
    if value:
        return str(value)
    return compact_json(item, 180)


def render_runtime_timeline_markdown(transcript: list[dict[str, Any]]) -> str:
    lines = ["# Runtime Timeline", ""]
    items = [
        item
        for item in transcript
        if item.get("role") in {"system", "user", "assistant", "tool", "runtime_notice", "compaction", "pre_compaction"}
    ]
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
                display_tool_call_name(str(call.get("function", {}).get("name")))
                for call in tool_calls
                if isinstance(call, dict) and isinstance(call.get("function"), dict) and call.get("function", {}).get("name")
            ]
            if names:
                details.append("tools requested: " + ", ".join(str(name) for name in names))
            lines.extend(f"- {detail}" for detail in details)
            text = assistant_message_text(message).strip()
            if text:
                lines.extend(["", "Assistant message:", "", "```text", text, "```"])
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
        elif role in {"system", "user", "runtime_notice"}:
            kind = str(item.get("kind") or "message")
            lines.append(f"## {index}. `{role}` `{kind}`")
            details = summarize_tool_timing(item)
            if item.get("message"):
                details.append("message: " + single_line(str(item["message"]), 500))
            lines.extend(f"- {detail}" for detail in details)
            lines.append("")
        else:
            lines.append(f"## {index}. `{role}`")
            details = summarize_tool_timing(item)
            lines.extend(f"- {detail}" for detail in details)
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
    elif name == "search_files":
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
    elif name == "run_tests":
        summary.append(f"command: `{result.get('command')}`")
        summary.append(f"returncode: {result.get('returncode')}")
        summary.append(f"timed out: {result.get('timed_out')}")
        summary.append(f"output path: `{result.get('output_path')}`")
        parsed = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        summary.append(f"passed: {parsed.get('passed')}")
        summary.append(f"failed: {parsed.get('failed')}")
        failed_tests = parsed.get("failed_tests") if isinstance(parsed.get("failed_tests"), list) else []
        for failed in failed_tests[:8]:
            summary.append("failed test: " + single_line(str(failed), 300))
        output_tail = str(result.get("output_tail") or "").strip()
        if output_tail:
            summary.append("output tail: " + single_line(output_tail, 500))
    elif name == "latest_test_failure_context":
        summary.append(f"context path: `{result.get('context_path')}`")
        summary.append(f"command: `{result.get('command')}`")
        summary.append(f"returncode: {result.get('returncode')}")
        failed_tests = result.get("failed_tests") if isinstance(result.get("failed_tests"), list) else []
        for failed in failed_tests[:8]:
            summary.append("failed test: " + single_line(str(failed), 300))
        artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), list) else []
        for artifact in artifacts[:8]:
            if isinstance(artifact, dict):
                summary.append(f"artifact: `{artifact.get('path')}`")
    elif name == "capture_visual_snapshot":
        summary.append(f"url: `{result.get('url') or arguments.get('url')}`")
        summary.append(f"screenshot path: `{result.get('screenshot_path')}`")
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        summary.append(f"viewport coverage: {metrics.get('viewportCoverage')}")
        summary.append(f"top gap ratio: {metrics.get('topGapRatio')}")
        summary.append(f"left gap ratio: {metrics.get('leftGapRatio')}")
        summary.append(f"visible elements: {metrics.get('visibleElementCount')}")
        if metrics.get("headingInteractiveOverlapCount"):
            summary.append(f"heading/control overlaps: {metrics.get('headingInteractiveOverlapCount')}")
        console_messages = result.get("consoleMessages") if isinstance(result.get("consoleMessages"), list) else []
        if console_messages:
            summary.append("console messages: " + str(len(console_messages)))
    elif name in {"append_evidence_note", "append_evidence_command", "append_evidence_screenshot"}:
        summary.append(f"evidence path: `{result.get('evidence_path')}`")
        if result.get("command"):
            summary.append(f"command: `{result.get('command')}`")
            summary.append(f"returncode: {result.get('returncode')}")
            summary.append(f"output path: `{result.get('output_path')}`")
        if result.get("screenshot_path"):
            summary.append(f"screenshot path: `{result.get('screenshot_path')}`")
        output_tail = str(result.get("output_tail") or "").strip()
        if output_tail:
            summary.append("output tail: " + single_line(output_tail, 500))
    elif name == "detect_project_environment":
        summary.append(f"package manager: {result.get('package_manager')}")
        summary.append("lockfiles: " + ", ".join(str(item) for item in result.get("lockfiles") or []))
        test_commands = result.get("test_commands") if isinstance(result.get("test_commands"), list) else []
        for command in test_commands[:8]:
            summary.append("test command: `" + str(command) + "`")
    elif name in {"git_status", "git_diff", "git_show"}:
        summary.append(f"returncode: {result.get('returncode')}")
        stdout = str(result.get("stdout") or "").strip()
        stderr = str(result.get("stderr") or "").strip()
        if stdout:
            summary.append("stdout: " + single_line(stdout, 500))
        if stderr:
            summary.append("stderr: " + single_line(stderr, 500))
    elif name == "start_process":
        summary.append(f"process id: `{result.get('process_id')}`")
        summary.append(f"pid: {result.get('pid')}")
        summary.append(f"ready: {result.get('ready')}")
        if result.get("requested_ports"):
            summary.append("requested ports: " + ", ".join(str(port) for port in result.get("requested_ports") or []))
        if result.get("allocated_port"):
            summary.append(f"allocated port: {result.get('allocated_port')}")
        if result.get("ports"):
            summary.append("listening ports: " + ", ".join(str(port) for port in result.get("ports") or []))
        if result.get("url"):
            summary.append(f"url: {result.get('url')}")
        summary.append(f"log path: `{result.get('log_path')}`")
        output = str(result.get("output") or "").strip()
        if output:
            summary.append("output: " + single_line(output, 500))
    elif name == "read_process":
        summary.append(f"process id: `{arguments.get('process_id')}`")
        summary.append(f"running: {result.get('running')}")
        summary.append(f"returncode: {result.get('returncode')}")
        if result.get("ports"):
            summary.append("listening ports: " + ", ".join(str(port) for port in result.get("ports") or []))
        if result.get("url"):
            summary.append(f"url: {result.get('url')}")
        output = str(result.get("output") or "").strip()
        if output:
            summary.append("output: " + single_line(output, 500))
    elif name == "stop_process":
        summary.append(f"process id: `{arguments.get('process_id')}`")
        summary.append(f"stopped: {result.get('stopped')}")
        summary.append(f"returncode: {result.get('returncode')}")
    elif name == "list_processes":
        processes = result.get("processes") if isinstance(result.get("processes"), list) else []
        summary.append(f"processes: {len(processes)}")
        for process in processes[:8]:
            if isinstance(process, dict):
                summary.append(
                    "process: "
                    + single_line(
                        f"{process.get('process_id')} pid={process.get('pid')} running={process.get('running')} command={process.get('command')}",
                        220,
                    )
                )
                if process.get("ports"):
                    summary.append("ports: " + ", ".join(str(port) for port in process.get("ports") or []))
                if process.get("allocated_port"):
                    summary.append(f"allocated port: {process.get('allocated_port')}")
                if process.get("url"):
                    summary.append(f"url: {process.get('url')}")
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
    elif name == "activate_skill":
        summary.append(f"skill: `{result.get('name') or arguments.get('name')}`")
        summary.append(f"description: {result.get('description') or ''}")
        summary.append(f"skill path: `{result.get('skill_path')}`")
        resources = result.get("resources") if isinstance(result.get("resources"), list) else []
        summary.append(f"resources: {len(resources)}")
        for resource in resources[:12]:
            if isinstance(resource, dict):
                summary.append(f"resource: `{resource.get('path')}` ({resource.get('bytes')} bytes)")
    elif name == "read_skill_resource":
        content = str(result.get("content") or "")
        summary.append(f"skill: `{result.get('name') or arguments.get('name')}`")
        summary.append(f"path: `{result.get('path') or arguments.get('path')}`")
        summary.append(f"bytes read: {len(content.encode('utf-8'))}")
    elif name == "final_report":
        summary.append("final report submitted")
    return summary


def display_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name in {"read_file", "write_file", "fetch_url", "read_skill_resource"}:
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


def model_request_deadline_seconds(runtime: ToolRuntime, elapsed_seconds: float) -> int:
    remaining = max(1, int(runtime.max_seconds - elapsed_seconds))
    configured = max(1, openrouter_timeout_ms() // 1000)
    return max(1, min(configured, remaining))


class LocalDeadline:
    def __init__(self, seconds: int, message: str) -> None:
        self.seconds = seconds
        self.message = message
        self.previous_handler: Any = None
        self.previous_timer: tuple[float, float] = (0.0, 0.0)

    def __enter__(self) -> "LocalDeadline":
        self.previous_handler = signal.getsignal(signal.SIGALRM)
        self.previous_timer = signal.getitimer(signal.ITIMER_REAL)
        signal.signal(signal.SIGALRM, self._raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.previous_handler)
        if self.previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *self.previous_timer)

    def _raise_timeout(self, _signum: int, _frame: Any) -> None:
        raise TimeoutError(self.message)


def image_input_message(runtime: ToolRuntime, images: list[dict[str, str]], text: str) -> dict[str, Any] | None:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    attached: list[str] = []
    for image in images:
        relative = str(image.get("path") or "").strip()
        if not relative:
            continue
        try:
            path = runtime.resolve_path(relative)
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_data_url(path),
                    },
                }
            )
            label = str(image.get("label") or relative)
            content[0]["text"] += f"\n- {label}: {relative}"
            attached.append(relative)
        except (OSError, ValueError):
            continue
    if not attached:
        return None
    return {"role": "user", "content": content}


def image_data_url(path: Path) -> str:
    data = path.read_bytes()
    max_bytes = int(os.environ.get("AGENT_IMAGE_INPUT_MAX_BYTES", "5000000"))
    if len(data) > max_bytes:
        raise ValueError(f"image is too large to attach: {relative_or_name(path)} ({len(data)} bytes)")
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    if mime_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise ValueError(f"unsupported image type for model input: {mime_type}")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def relative_or_name(path: Path) -> str:
    return path.as_posix()


def available_tool_names() -> list[str]:
    return [
        "read_file",
        "read_file_excerpt",
        "read_many_files",
        "write_file",
        "list_files",
        "search_files",
        "find_files",
        "detect_project_environment",
        "run_tests",
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
        "activate_skill",
        "read_skill_resource",
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


def wait_for_http_url(url: str, wait_seconds: int) -> bool:
    deadline = time.monotonic() + max(wait_seconds, 0)
    while time.monotonic() <= deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 500:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def read_tail(path: Path, max_bytes: int) -> str:
    if not path.exists():
        return ""
    max_bytes = max(1, max_bytes)
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(size - max_bytes, 0))
        return handle.read(max_bytes).decode("utf-8", errors="replace")


def read_text_prefix(path: Path, max_bytes: int) -> str:
    with path.open("rb") as handle:
        return handle.read(max(1, max_bytes)).decode("utf-8", errors="replace")


def detect_project_environment(root: Path) -> dict[str, Any]:
    lockfiles = [
        name
        for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "uv.lock", "requirements.txt", "Cargo.lock", "go.sum")
        if (root / name).exists()
    ]
    package_json_path = root / "package.json"
    package_json: dict[str, Any] = {}
    scripts: dict[str, str] = {}
    package_manager = ""
    if package_json_path.exists():
        try:
            package_json = json.loads(package_json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            package_json = {}
        scripts = {str(key): str(value) for key, value in (package_json.get("scripts") or {}).items()}
        package_manager = package_manager_from_package_json(package_json)
    if not package_manager:
        package_manager = package_manager_from_lockfiles(lockfiles)
    test_commands = likely_test_commands(root, package_manager, scripts)
    return {
        "package_manager": package_manager,
        "lockfiles": lockfiles,
        "scripts": scripts,
        "test_commands": test_commands,
        "has_package_json": package_json_path.exists(),
        "languages": language_hints(root),
    }


def package_manager_from_package_json(package_json: dict[str, Any]) -> str:
    raw = str(package_json.get("packageManager") or "")
    if raw.startswith("pnpm@"):
        return "pnpm"
    if raw.startswith("yarn@"):
        return "yarn"
    if raw.startswith("bun@"):
        return "bun"
    if raw.startswith("npm@"):
        return "npm"
    return ""


def package_manager_from_lockfiles(lockfiles: list[str]) -> str:
    if "pnpm-lock.yaml" in lockfiles:
        return "pnpm"
    if "yarn.lock" in lockfiles:
        return "yarn"
    if "bun.lockb" in lockfiles:
        return "bun"
    if "package-lock.json" in lockfiles:
        return "npm"
    if "uv.lock" in lockfiles:
        return "uv"
    if "Cargo.lock" in lockfiles:
        return "cargo"
    return "npm" if lockfiles and any(name.startswith("package") for name in lockfiles) else ""


def likely_test_commands(root: Path, package_manager: str, scripts: dict[str, str]) -> list[str]:
    commands: list[str] = []
    if scripts.get("test"):
        commands.append(f"{package_manager or 'npm'} test")
    if (root / "playwright.config.js").exists() or (root / "playwright.config.cjs").exists() or (root / "playwright.config.mjs").exists():
        runner = "npx"
        if package_manager == "pnpm":
            runner = "pnpm exec"
        elif package_manager == "yarn":
            runner = "yarn"
        elif package_manager == "bun":
            runner = "bunx"
        commands.append(f"{runner} playwright test")
    if (root / "pytest.ini").exists() or (root / "tests").exists() and any((root / "tests").glob("test_*.py")):
        commands.append("pytest")
    if (root / "Cargo.toml").exists():
        commands.append("cargo test")
    if (root / "go.mod").exists():
        commands.append("go test ./...")
    return dedupe_strings(commands)


def language_hints(root: Path) -> list[str]:
    hints = []
    markers = {
        "javascript": ["package.json"],
        "python": ["pyproject.toml", "requirements.txt"],
        "rust": ["Cargo.toml"],
        "go": ["go.mod"],
        "php": ["composer.json"],
        "ruby": ["Gemfile"],
    }
    for language, names in markers.items():
        if any((root / name).exists() for name in names):
            hints.append(language)
    return hints


def default_test_command(environment: dict[str, Any], list_only: bool) -> str:
    commands = environment.get("test_commands") if isinstance(environment.get("test_commands"), list) else []
    if commands:
        command = str(commands[0])
    else:
        package_manager = str(environment.get("package_manager") or "npm")
        command = f"{package_manager} test"
    if list_only and "playwright test" in command and " --list" not in command:
        command += " --list"
    return command


def append_shell_arg(command: str, value: str) -> str:
    return command + " " + shlex.quote(value)


def append_test_name_filter(command: str, test_name: str) -> str:
    if "playwright test" in command:
        return command + " -g " + shlex.quote(test_name)
    if "pytest" in command:
        return command + " -k " + shlex.quote(test_name)
    return command


def normalize_subprocess_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_shell_command(command: str, *, cwd: Path, timeout_seconds: int) -> tuple[subprocess.CompletedProcess[str], bool]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return subprocess.CompletedProcess(command, process.returncode, stdout=stdout or "", stderr=stderr or ""), False
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        return (
            subprocess.CompletedProcess(
                command,
                124,
                stdout=normalize_subprocess_output(stdout or exc.stdout),
                stderr=normalize_subprocess_output(stderr or exc.stderr),
            ),
            True,
        )


def long_running_bash_violation(command: str) -> str | None:
    lowered = " ".join(command.lower().split())
    if shell_backgrounds_process(command):
        return "bash command starts a background process; use start_process/read_process/stop_process instead"
    server_patterns = [
        "npm run dev",
        "npm run preview",
        "yarn dev",
        "yarn preview",
        "pnpm dev",
        "pnpm preview",
        "bun dev",
        "vite --host",
        "vite preview",
        "next dev",
        "python -m http.server",
        "python3 -m http.server",
        "rails server",
        "flask run",
        "uvicorn ",
    ]
    if any(pattern in lowered for pattern in server_patterns) and not any(help_flag in lowered for help_flag in (" --help", " -h")):
        return "bash command appears to start a long-running server; use start_process/read_process/stop_process instead"
    return None


def shell_backgrounds_process(command: str) -> bool:
    for index, char in enumerate(command):
        if char != "&":
            continue
        previous_char = command[index - 1] if index > 0 else ""
        next_char = command[index + 1] if index + 1 < len(command) else ""
        if previous_char == "&" or next_char == "&":
            continue
        if previous_char in {">", "<"}:
            continue
        return True
    return False


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_tool_result_artifact(root: Path, category: str, content: str) -> str:
    directory = runtime_path(root, "tool-results", category)
    directory.mkdir(parents=True, exist_ok=True)
    filename = utc_timestamp().replace(":", "").replace("+", "Z") + ".log"
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return relative_to(path, root)


def evidence_assets_dir(evidence_base_dir: Path, category: str) -> Path:
    path = evidence_base_dir / "evidence" / category
    path.mkdir(parents=True, exist_ok=True)
    return path


def evidence_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ%f")[:22]


def ensure_evidence_report(root: Path, evidence_base_dir: Path) -> Path:
    path = evidence_base_dir / "evidence.md"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Evidence\n\n"
            "Generated by Hooky evidence tools. Command output and screenshots in this report are captured by tools, not hand-authored by the model.\n\n",
            encoding="utf-8",
        )
    return path


def append_evidence_section(root: Path, evidence_base_dir: Path, title: str, body: str) -> Path:
    path = ensure_evidence_report(root, evidence_base_dir)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"## {title}\n\n")
        handle.write(f"- captured_at: {utc_timestamp()}\n\n")
        if body.strip():
            handle.write(body.rstrip() + "\n\n")
    return path


def fenced(value: str, language: str = "") -> str:
    fence = "```"
    while fence in value:
        fence += "`"
    info = language.strip()
    return f"{fence}{info}\n{value.rstrip()}\n{fence}"


def append_evidence_note_file(root: Path, evidence_base_dir: Path, *, title: str, body: str) -> Path:
    return append_evidence_section(root, evidence_base_dir, title, body)


def write_evidence_command_output(root: Path, evidence_base_dir: Path, *, command: str, output: str) -> Path:
    directory = evidence_assets_dir(evidence_base_dir, "command-output")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", command.strip()).strip(".-").lower()[:48] or "command"
    path = directory / f"{evidence_stamp()}-{slug}.log"
    path.write_text(output, encoding="utf-8")
    return path


def append_evidence_command_file(
    root: Path,
    evidence_base_dir: Path,
    *,
    title: str,
    command: str,
    returncode: int,
    timed_out: bool,
    started_at: str,
    ended_at: str,
    output_path: Path,
    output_tail: str,
    protected_changes: list[str],
) -> Path:
    lines = [
        f"- command: `{command}`",
        f"- returncode: {returncode}",
        f"- timed_out: {str(timed_out).lower()}",
        f"- started_at: {started_at}",
        f"- ended_at: {ended_at}",
        f"- output: `{relative_to(output_path, root)}`",
    ]
    if protected_changes:
        lines.append("- protected_changes_reverted: " + ", ".join(protected_changes[:20]))
    if output_tail.strip():
        lines.extend(["", fenced(output_tail[-4000:], "text")])
    return append_evidence_section(root, evidence_base_dir, title, "\n".join(lines))


def append_evidence_screenshot_file(root: Path, evidence_base_dir: Path, *, title: str, url: str, snapshot: dict[str, Any]) -> Path:
    screenshot = str(snapshot.get("screenshot_path") or "")
    lines = [
        f"- url: `{url}`",
        f"- ok: {str(bool(snapshot.get('ok'))).lower()}",
    ]
    if screenshot:
        lines.append(f"- screenshot: `{screenshot}`")
        lines.extend(["", f"![{title}]({screenshot})"])
    if snapshot.get("error"):
        lines.append(f"- error: {snapshot.get('error')}")
    metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
    if metrics:
        lines.extend(
            [
                "",
                "- metrics:",
                f"  - viewportCoverage: {metrics.get('viewportCoverage')}",
                f"  - topGapRatio: {metrics.get('topGapRatio')}",
                f"  - leftGapRatio: {metrics.get('leftGapRatio')}",
                f"  - visibleElementCount: {metrics.get('visibleElementCount')}",
                f"  - headingInteractiveOverlapCount: {metrics.get('headingInteractiveOverlapCount')}",
            ]
        )
    return append_evidence_section(root, evidence_base_dir, title, "\n".join(lines))


def parse_test_output(output: str, returncode: int) -> dict[str, Any]:
    failed_tests = extract_failed_test_names(output)
    counts = parse_test_counts(output)
    passed = returncode == 0
    return {
        "passed": passed,
        "failed": not passed,
        "failed_tests": failed_tests[:50],
        "counts": counts,
        "truncated": len(failed_tests) > 50,
    }


def latest_failed_run_tests_event(tool_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(tool_events):
        if event.get("name") != "run_tests":
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if result.get("ok") is False:
            return event
    return None


def latest_error_context_artifacts(root: Path, max_artifacts: int, max_bytes: int) -> list[dict[str, Any]]:
    if max_artifacts <= 0:
        return []
    candidates = sorted(
        (path for path in (root / "test-results").rglob("error-context.md") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    artifacts: list[dict[str, Any]] = []
    for path in candidates[:max_artifacts]:
        content = read_text_prefix(path, max_bytes)
        artifacts.append(
            {
                "path": relative_to(path, root),
                "bytes": path.stat().st_size,
                "content": content,
                "truncated": path.stat().st_size > len(content.encode("utf-8")),
            }
        )
    return artifacts


def render_failure_context_bundle(
    *,
    command: str,
    returncode: Any,
    timed_out: bool,
    output_path: str,
    failed_tests: list[str],
    output_tail: str,
    artifacts: list[dict[str, Any]],
) -> str:
    lines = [
        "# Latest Test Failure Context",
        "",
        "## Command",
        "",
        f"- command: `{command}`",
        f"- returncode: {returncode}",
        f"- timed_out: {timed_out}",
        f"- output_path: `{output_path}`",
        "",
        "## Failed Tests",
        "",
    ]
    if failed_tests:
        lines.extend(f"- {single_line(item, 500)}" for item in failed_tests[:20])
    else:
        lines.append("- No individual failed test names were parsed from output.")
    lines.extend(["", "## Output Tail", "", "```text", output_tail.strip(), "```", ""])
    if artifacts:
        lines.extend(["## Related Error Context Artifacts", ""])
        for artifact in artifacts:
            lines.extend(
                [
                    f"### `{artifact['path']}`",
                    "",
                    f"- bytes: {artifact['bytes']}",
                    f"- truncated: {artifact['truncated']}",
                    "",
                    "```markdown",
                    str(artifact.get("content") or "").strip(),
                    "```",
                    "",
                ]
            )
    else:
        lines.extend(["## Related Error Context Artifacts", "", "No `test-results/**/error-context.md` artifacts were found.", ""])
    return "\n".join(lines)


def extract_failed_test_names(output: str) -> list[str]:
    names: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if re.match(r"^\d+\)\s+", stripped):
            names.append(re.sub(r"\s+", " ", stripped))
        elif "›" in stripped and ("failed" in stripped.lower() or re.search(r"^\d+\)", stripped)):
            names.append(re.sub(r"\s+", " ", stripped))
    return dedupe_strings(names)


def parse_test_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in ("failed", "passed", "skipped", "timed out"):
        match = re.search(rf"(\d+)\s+{re.escape(key)}", output, flags=re.IGNORECASE)
        if match:
            counts[key.replace(" ", "_")] = int(match.group(1))
    return counts


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


def ensure_git_baseline(workspace: Path, message: str = "Initial workspace baseline") -> None:
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    git_run(workspace, ["init"], check=True)
    ensure_git_identity(workspace)
    git_run(workspace, ["add", "-A"], check=True)
    if git_has_head(workspace):
        if git_staged_changes(workspace):
            git_run(workspace, ["commit", "-m", message], check=True)
        return
    if git_staged_changes(workspace):
        git_run(workspace, ["commit", "-m", message], check=True)
    else:
        git_run(workspace, ["commit", "--allow-empty", "-m", message], check=True)


def ensure_git_identity(workspace: Path) -> None:
    if git_run(workspace, ["config", "user.email"], check=False).returncode != 0:
        git_run(workspace, ["config", "user.email", "hooky@example.local"], check=True)
    if git_run(workspace, ["config", "user.name"], check=False).returncode != 0:
        git_run(workspace, ["config", "user.name", "Hooky"], check=True)


def git_has_head(workspace: Path) -> bool:
    return git_run(workspace, ["rev-parse", "--verify", "HEAD"], check=False).returncode == 0


def git_staged_changes(workspace: Path) -> bool:
    return git_run(workspace, ["diff", "--cached", "--quiet"], check=False).returncode == 1


def git_run(workspace: Path, args: list[str], check: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def stop_managed_process(process: subprocess.Popen[str]) -> bool:
    if process.poll() is not None:
        close_process_log(process)
        return False
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        close_process_log(process)
        return False
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    close_process_log(process)
    return True


def requested_ports_from_command(command: str) -> list[int]:
    ports: set[int] = set()
    tokens = shell_tokens_for_ports(command)
    for index, token in enumerate(tokens):
        if token in {"--port", "-p"} and index + 1 < len(tokens):
            add_port(ports, tokens[index + 1])
            continue
        for prefix in ("--port=", "-p=", "PORT=", "port="):
            if token.startswith(prefix):
                add_port(ports, token[len(prefix) :])
                break
    for match in re.finditer(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])[:/](\d{2,5})", command):
        add_port(ports, match.group(1))
    for match in re.finditer(r"(?<![\w.:-]):(\d{2,5})(?!\d)", command):
        add_port(ports, match.group(1))
    return sorted(ports)


def process_url_from_ports(ports: list[int]) -> str | None:
    if not ports:
        return None
    return f"http://127.0.0.1:{ports[0]}"


def shell_tokens_for_ports(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def add_port(ports: set[int], value: str) -> None:
    try:
        port = int(str(value).strip())
    except ValueError:
        return
    if 1 <= port <= 65535:
        ports.add(port)


def tcp_port_is_listening(port: int) -> bool:
    try:
        completed = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            text=True,
            capture_output=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


def allocate_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def process_tree_pids(pid: int) -> set[int]:
    pids = {pid}
    try:
        completed = subprocess.run(["pgrep", "-P", str(pid)], text=True, capture_output=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return pids
    if completed.returncode not in {0, 1}:
        return pids
    for line in completed.stdout.splitlines():
        try:
            child = int(line.strip())
        except ValueError:
            continue
        if child not in pids:
            pids.update(process_tree_pids(child))
    return pids


def process_listeners(pid: int) -> list[dict[str, Any]]:
    listeners: list[dict[str, Any]] = []
    for candidate_pid in sorted(process_tree_pids(pid)):
        try:
            completed = subprocess.run(
                ["lsof", "-nP", "-a", "-p", str(candidate_pid), "-iTCP", "-sTCP:LISTEN"],
                text=True,
                capture_output=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode != 0:
            continue
        for line in completed.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 9:
                continue
            port = listener_port_from_name(parts[-2] if parts[-1] == "(LISTEN)" else parts[-1])
            if port is None:
                continue
            listeners.append(
                {
                    "pid": candidate_pid,
                    "command": parts[0],
                    "host": listener_host_from_name(parts[-2] if parts[-1] == "(LISTEN)" else parts[-1]),
                    "port": port,
                }
            )
    return dedupe_listeners(listeners)


def listener_port_from_name(name: str) -> int | None:
    match = re.search(r":(\d+)(?:\s|$)", name)
    if not match:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else None


def listener_host_from_name(name: str) -> str:
    return name.rsplit(":", 1)[0]


def dedupe_listeners(listeners: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, Any, Any]] = set()
    deduped: list[dict[str, Any]] = []
    for listener in listeners:
        key = (listener.get("pid"), listener.get("host"), listener.get("port"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(listener)
    return deduped


def close_process_log(process: subprocess.Popen[str]) -> None:
    handle = getattr(process, "_hooky_log_handle", None)
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        pass


def snapshot_protected_paths(root: Path, prefixes: list[str]) -> dict[str, bytes | None]:
    root = root.resolve()
    snapshot: dict[str, bytes | None] = {}
    for prefix in prefixes:
        protected = (root / prefix).resolve()
        if protected.is_file():
            snapshot[relative_to(protected, root)] = protected.read_bytes()
        elif protected.is_dir():
            for path in sorted(protected.rglob("*")):
                if path.is_file():
                    snapshot[relative_to(path, root)] = path.read_bytes()
        else:
            snapshot[str(Path(prefix))] = None
    return snapshot


def protected_path_changes(root: Path, prefixes: list[str], before: dict[str, bytes | None]) -> list[str]:
    if not prefixes:
        return []
    root = root.resolve()
    after = snapshot_protected_paths(root, prefixes)
    changes: list[str] = []
    before_keys = set(before)
    after_keys = set(after)
    for path in sorted(after_keys - before_keys):
        if is_disposable_runtime_output(path):
            continue
        if after[path] is not None:
            changes.append(path)
    for path in sorted(before_keys - after_keys):
        if is_disposable_runtime_output(path):
            continue
        if before[path] is not None:
            changes.append(path)
    for path in sorted(before_keys & after_keys):
        if is_disposable_runtime_output(path):
            continue
        if before[path] != after[path]:
            changes.append(path)
    return changes


def restore_protected_paths(root: Path, before: dict[str, bytes | None], changes: list[str]) -> None:
    root = root.resolve()
    for relative in changes:
        path = (root / relative).resolve()
        if path != root and root not in path.parents:
            continue
        content = before.get(relative)
        if content is None:
            if path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def is_disposable_runtime_output(relative: str) -> bool:
    for part in Path(relative).parts:
        if part in DISPOSABLE_RUNTIME_DIR_NAMES:
            return True
        if part.endswith("-report") or part.endswith("-reports"):
            return True
    return False


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
