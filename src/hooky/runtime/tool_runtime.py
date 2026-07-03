"""The ToolRuntime dataclass: tool dispatch and the agent-facing tool surface."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hooky.runtime.models import runtime_dir
from hooky.runtime.rendering import canonical_tool_name
from hooky.runtime.schemas import integer_schema, string_array_schema, string_schema, tool_schema
from hooky.runtime.tools.evidence import EvidenceToolsMixin
from hooky.runtime.tools.git import GitToolsMixin
from hooky.runtime.tools.process import ProcessToolsMixin
from hooky.runtime.tools.read_write import ReadWriteToolsMixin
from hooky.runtime.tools.shell import ShellToolsMixin
from hooky.shared import agent_skills

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]
FinalValidator = Callable[[dict[str, Any]], None]


@dataclass
class ToolRuntime(
    ReadWriteToolsMixin,
    GitToolsMixin,
    ProcessToolsMixin,
    EvidenceToolsMixin,
    ShellToolsMixin,
):
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
            self.preselected_skill_names = [item.strip() for item in os.environ["HOOKY_ACTIVE_SKILLS"].split(",") if item.strip()]

    def tools(self) -> list[dict[str, Any]]:
        tools = [
            tool_schema(
                "read_files",
                "Read one or more UTF-8 text files from the working folder with per-file truncation.",
                {
                    "paths": string_array_schema(),
                    "max_bytes_per_file": integer_schema(default=12000, minimum=1, maximum=50000),
                },
                ["paths"],
            ),
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
                "write_files",
                "Write one or more UTF-8 text files inside the working folder. Existing files must be read first in the current uncompacted context.",
                {
                    "files": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 50,
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": string_schema(),
                                "content": string_schema(),
                            },
                            "required": ["path", "content"],
                            "additionalProperties": False,
                        },
                    },
                },
                ["files"],
            ),
            tool_schema(
                "edit_files",
                "Apply line-oriented edits to one or more existing UTF-8 text files. Existing files must be read first in the current uncompacted context. Edits are validated as a batch before any file is written.",
                {
                    "files": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 50,
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": string_schema(),
                                "edits": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 100,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "start_line": integer_schema(minimum=1),
                                            "end_line": integer_schema(minimum=0),
                                            "replacement": string_schema(),
                                        },
                                        "required": ["start_line", "end_line", "replacement"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "required": ["path", "edits"],
                            "additionalProperties": False,
                        },
                    },
                },
                ["files"],
            ),
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
            tools = [tool for tool in tools if tool.get("function", {}).get("name") in enabled]
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
            "read_files": self.read_files,
            "read_file_excerpt": self.read_file_excerpt,
            "write_files": self.write_files,
            "edit_files": self.edit_files,
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
