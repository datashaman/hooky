#!/usr/bin/env python3
"""Minimal OpenRouter tool-loop runtime for local SDLC agents."""

from __future__ import annotations

import fnmatch
import base64
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

import spec_agent


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]
FinalValidator = Callable[[dict[str, Any]], None]
DISPOSABLE_RUNTIME_DIR_NAMES = {
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    "__pycache__",
    "coverage",
    "test-results",
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
    compaction_prompt_path: Path = Path(".workflow/agents/common/static/compaction.md")
    live_log_root: Path | None = None
    live_event_log_paths: list[Path] = field(default_factory=list)
    live_event_prefix: str = ""
    heartbeat_seconds: int = 20
    write_enabled: bool = True
    read_blocked_prefixes: list[str] = field(default_factory=lambda: [".workflow"])
    read_allowed_prefixes: list[str] = field(default_factory=lambda: [".workflow/tool-results"])
    write_allowed_prefixes: list[str] = field(default_factory=list)
    write_blocked_prefixes: list[str] = field(default_factory=lambda: [".workflow"])
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

    def tools(self) -> list[dict[str, Any]]:
        return [
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
            tool_schema("grep_files", "Search UTF-8 files for a literal string.", {"pattern": string_schema(), "path": string_schema(default=".")}, ["pattern"]),
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
                "Start a long-running local process in the working folder, such as a development server.",
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
            "read_file_excerpt": self.read_file_excerpt,
            "read_many_files": self.read_many_files,
            "write_file": self.write_file,
            "list_files": self.list_files,
            "find_files": self.find_files,
            "grep_files": self.grep_files,
            "detect_project_environment": self.detect_project_environment,
            "run_tests": self.run_tests,
            "capture_visual_snapshot": self.capture_visual_snapshot,
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
        self.validate_read_path(path)
        return {"ok": True, "path": relative_to(path, self.working_folder), "content": path.read_text(encoding="utf-8")}

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
        content = str(args["content"])
        if self.write_validator:
            self.write_validator(path, content)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "path": relative_to(path, self.working_folder), "bytes": path.stat().st_size}

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
        matches = [
            relative_to(path, self.working_folder)
            for path in sorted(root.rglob("*"))
            if path.is_file() and not self.is_read_blocked(path) and fnmatch.fnmatch(path.name, pattern)
        ]
        return {"ok": True, "matches": matches[:500], "truncated": len(matches) > 500}

    def grep_files(self, args: dict[str, Any]) -> dict[str, Any]:
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
        try:
            completed = subprocess.run(
                command,
                cwd=self.working_folder,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
            )
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            completed = subprocess.CompletedProcess(command, 124, stdout=exc.stdout or "", stderr=exc.stderr or "")
            timed_out = True
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
        output_dir = self.working_folder / ".workflow" / "tool-results" / "visual-snapshots"
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
        if ".workflow" in lowered and self.read_blocked_prefixes:
            return {"ok": False, "error": "bash command references a path that is not available to this agent"}
        for blocked in self.bash_blocked_substrings:
            if blocked.lower() in lowered:
                return {"ok": False, "error": f"bash command blocked by agent policy: {blocked}"}
        if self.bash_command_validator:
            violation = self.bash_command_validator(command)
            if violation:
                return {"ok": False, "error": f"bash command blocked by agent policy: {violation}"}
        before = snapshot_protected_paths(self.working_folder, self.bash_protected_prefixes)
        completed = subprocess.run(
            command,
            cwd=self.working_folder,
            shell=True,
            text=True,
            capture_output=True,
            timeout=self.bash_timeout_seconds,
        )
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
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-8000:],
            "stderr": completed.stderr[-8000:],
        }

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
        log_path = self.working_folder / ".workflow" / "managed-processes" / f"{process_id}.log"
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
        return {
            "ok": process.poll() is None,
            "process_id": process_id,
            "pid": process.pid,
            "name": process._hooky_name,  # type: ignore[attr-defined]
            "command": command,
            "requested_ports": requested_ports_list,
            "allocated_port": allocated_port,
            "ports": ports,
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
        return {
            "ok": True,
            "process_id": str(args["process_id"]),
            "running": process.poll() is None,
            "returncode": process.poll(),
            "ports": sorted({int(item["port"]) for item in listeners}),
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
                    "ports": sorted({int(item["port"]) for item in listeners}),
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
      rect: asRect(element.getBoundingClientRect()),
    }));
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
      visibleElementCount: visibleElements.length,
      interactiveElementCount: interactiveElements.length,
      headings: headings.slice(0, 12),
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
    from openrouter import OpenRouter

    messages: list[Any] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if runtime.initial_image_paths:
        initial_images = [
            {"path": relative_to((path if path.is_absolute() else runtime.working_folder / path), runtime.working_folder), "label": "Initial visual evidence"}
            for path in runtime.initial_image_paths
        ]
        image_message = image_input_message(runtime, initial_images, "Initial visual evidence attached for inspection.")
        if image_message:
            messages.append(image_message)
    transcript: list[dict[str, Any]] = []
    tool_events: list[dict[str, Any]] = []
    compaction_events: list[dict[str, Any]] = []
    pre_compaction_archives: list[dict[str, Any]] = []
    total_usage: dict[str, Any] = {"cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    started_at = utc_timestamp()
    soft_deadline_sent = False

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
        with OpenRouter(api_key=os.environ["OPENROUTER_API_KEY"], timeout_ms=openrouter_timeout_ms()) as client:
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
                        "If the task is not complete, call final_report now with current status, concrete failures, "
                        "and next steps instead of starting another long debugging cycle."
                    )
                    messages.append({"role": "user", "content": warning})
                    transcript.append(
                        {
                            "role": "runtime_notice",
                            "message": warning,
                            "started_at": utc_timestamp(),
                            "ended_at": utc_timestamp(),
                        }
                    )
                    append_live_event(runtime, f"{utc_timestamp()} runtime_notice kind=soft_deadline remaining_seconds={remaining_seconds}")
                    flush_live_log()

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
                    runtime.tool_events.append(event)
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
    finally:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1)
        runtime.cleanup_processes()

    append_live_event(runtime, f"{utc_timestamp()} run success tool_calls={len(tool_events)} cost=${float(total_usage.get('cost') or 0):.8f}")
    flush_live_log(status="success")
    return current_result()


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
    if name == "capture_visual_snapshot":
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        detail = (
            f"url={quote_value(str(result.get('url') or arguments.get('url') or ''), 180)} "
            f"screenshot_path={quote_value(str(result.get('screenshot_path') or ''), 180)} "
            f"coverage={metrics.get('viewportCoverage')} top_gap={metrics.get('topGapRatio')} "
            f"elements={metrics.get('visibleElementCount')}"
        )
        console_messages = result.get("consoleMessages") if isinstance(result.get("consoleMessages"), list) else []
        if console_messages:
            detail += f" console_messages={len(console_messages)}"
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
    value = item.get("text") or item.get("description") or item.get("content") or item.get("task") or item.get("title")
    if value:
        return str(value)
    return compact_json(item, 180)


def render_runtime_timeline_markdown(transcript: list[dict[str, Any]]) -> str:
    lines = ["# Runtime Timeline", ""]
    items = [item for item in transcript if item.get("role") in {"assistant", "tool", "runtime_notice", "compaction", "pre_compaction"}]
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
            details = summarize_tool_timing(item)
            if role == "runtime_notice" and item.get("message"):
                details.append("message: " + single_line(str(item["message"]), 500))
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
    elif name == "capture_visual_snapshot":
        summary.append(f"url: `{result.get('url') or arguments.get('url')}`")
        summary.append(f"screenshot path: `{result.get('screenshot_path')}`")
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        summary.append(f"viewport coverage: {metrics.get('viewportCoverage')}")
        summary.append(f"top gap ratio: {metrics.get('topGapRatio')}")
        summary.append(f"left gap ratio: {metrics.get('leftGapRatio')}")
        summary.append(f"visible elements: {metrics.get('visibleElementCount')}")
        console_messages = result.get("consoleMessages") if isinstance(result.get("consoleMessages"), list) else []
        if console_messages:
            summary.append("console messages: " + str(len(console_messages)))
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
        "grep_files",
        "find_files",
        "detect_project_environment",
        "run_tests",
        "capture_visual_snapshot",
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


def write_tool_result_artifact(root: Path, category: str, content: str) -> str:
    directory = root / ".workflow" / "tool-results" / category
    directory.mkdir(parents=True, exist_ok=True)
    filename = utc_timestamp().replace(":", "").replace("+", "Z") + ".log"
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return relative_to(path, root)


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


def read_remediation_context(working_folder: Path, stage: str) -> dict[str, Any] | None:
    path = Path(working_folder) / ".workflow/artifacts/remediation/current.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("root_cause_stage") != stage:
        return None
    return payload


def read_task_state(working_folder: Path) -> dict[str, Any]:
    workflow_state_path = Path(working_folder) / ".workflow/state.json"
    if not workflow_state_path.exists():
        return {}
    try:
        workflow_state = json.loads(workflow_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    current_task = workflow_state.get("current_task")
    if not current_task:
        return {}
    task_state_path = Path(working_folder) / ".workflow/tasks" / str(current_task) / "state.json"
    if not task_state_path.exists():
        return {}
    try:
        return json.loads(task_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def upstream_evidence_context(working_folder: Path) -> dict[str, Any]:
    root = Path(working_folder)
    state = read_task_state(root)
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    stages: dict[str, Any] = {}
    for stage in ("spec", "test", "builder", "verifier", "eval"):
        artifact = artifacts.get(stage) if isinstance(artifacts.get(stage), dict) else {}
        stage_payload: dict[str, Any] = {
            "artifacts": {
                key: value
                for key, value in artifact.items()
                if isinstance(value, (str, list, dict, int, float, bool)) or value is None
            },
            "runtime": runtime_evidence_files(root, stage, artifact),
        }
        stages[stage] = stage_payload
    return {
        "instruction": "Use these explicit artifact and runtime evidence paths before guessing filenames or listing directories.",
        "stages": stages,
    }


def runtime_evidence_files(root: Path, stage: str, artifact: dict[str, Any]) -> dict[str, str]:
    candidates: list[Path] = []
    report_dir = artifact.get("report_dir")
    if isinstance(report_dir, str) and report_dir:
        candidates.append(root / report_dir)
    if stage == "spec":
        candidates.append(root / ".workflow/artifacts/specs/_runtime")
    elif stage == "test":
        candidates.append(root / ".workflow/artifacts/test-agent")
    elif stage == "builder":
        candidates.append(root / ".workflow/artifacts/builder-agent")
    elif stage == "verifier":
        candidates.append(root / ".workflow/artifacts/verifier-agent")
    elif stage == "eval":
        candidates.append(root / ".workflow/artifacts/eval-agent")

    result: dict[str, str] = {}
    names = {
        "events": "runtime_events.log",
        "timeline": "runtime_timeline.md",
        "tool_events": "tool_events.json",
        "tool_calls": "tool_calls.md",
        "metadata": "runtime_metadata.json",
        "transcript": "runtime_transcript.json",
        "context_snapshot": "context_snapshot.md",
        "dynamic_context": "dynamic_context.json",
    }
    for directory in candidates:
        for key, filename in names.items():
            path = directory / filename
            if key not in result and path.exists():
                result[key] = relative_to(path.resolve(), root.resolve())
    return result


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
