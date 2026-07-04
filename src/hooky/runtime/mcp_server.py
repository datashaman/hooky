"""MCP stdio server exposing a curated subset of ToolRuntime tools to external executors.

The native executor drives ToolRuntime in-process. External executors (codex, claude) run as
separate CLI processes and cannot share that Python object, so this module lets them reach the
same evidence/verification tools (run_tests, capture_visual_snapshot, interact_and_snapshot, ...)
over MCP instead. Each call is dispatched through ToolRuntime.run_tool, the same path the native
loop uses, and appended to a JSONL events file so the external-executor caller can fold the calls
back into the attempt's tool_events.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from hooky.runtime.rendering import utc_timestamp
from hooky.runtime.tool_runtime import ToolRuntime

MCP_TOOL_NAMES: tuple[str, ...] = (
    "detect_project_environment",
    "run_tests",
    "run_lint",
    "latest_test_failure_context",
    "capture_visual_snapshot",
    "interact_and_snapshot",
    "append_evidence_note",
    "append_evidence_command",
    "append_evidence_screenshot",
    "append_evidence_interaction",
)


@dataclass
class McpServerConfig:
    working_folder: str
    events_path: str
    max_seconds: int = 600
    bash_timeout_seconds: int = 120
    read_allowed_prefixes: list[str] = field(default_factory=list)
    read_blocked_prefixes: list[str] = field(default_factory=lambda: [".hooky"])
    bash_protected_prefixes: list[str] = field(default_factory=list)
    bash_blocked_substrings: list[str] = field(default_factory=list)
    live_log_root: str | None = None
    live_event_prefix: str = ""

    def to_json_file(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @classmethod
    def from_json_file(cls, path: Path) -> McpServerConfig:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)


def build_runtime(config: McpServerConfig) -> ToolRuntime:
    return ToolRuntime(
        working_folder=Path(config.working_folder),
        final_report_schema={"type": "object"},
        max_cost_usd=0.0,
        max_seconds=config.max_seconds,
        bash_timeout_seconds=config.bash_timeout_seconds,
        read_allowed_prefixes=list(config.read_allowed_prefixes),
        read_blocked_prefixes=list(config.read_blocked_prefixes),
        bash_protected_prefixes=list(config.bash_protected_prefixes),
        bash_blocked_substrings=list(config.bash_blocked_substrings),
        live_log_root=Path(config.live_log_root) if config.live_log_root else None,
        live_event_prefix=config.live_event_prefix,
        enabled_tools=list(MCP_TOOL_NAMES),
        write_enabled=False,
        skills=[],
    )


def append_mcp_event(config: McpServerConfig, *, name: str, arguments: dict[str, Any], result: dict[str, Any], started_at: str, ended_at: str, duration_ms: float) -> None:
    event = {
        "tool_call_id": f"mcp-{uuid.uuid4().hex[:12]}",
        "name": name,
        "arguments": arguments,
        "result": result,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": round(duration_ms, 2),
        "source": "mcp",
    }
    with Path(config.events_path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def build_server(config: McpServerConfig) -> Server:
    runtime = build_runtime(config)
    schemas = {tool["function"]["name"]: tool["function"] for tool in runtime.tools() if tool.get("function", {}).get("name") in MCP_TOOL_NAMES}

    server: Server = Server("hooky")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                description=str(schema.get("description") or ""),
                inputSchema=schema.get("parameters") or {"type": "object", "properties": {}},
            )
            for name, schema in schemas.items()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in MCP_TOOL_NAMES:
            raise ValueError(f"unknown tool: {name}")
        started_at = utc_timestamp()
        started = time.monotonic()
        result = runtime.run_tool(name, arguments)
        ended_at = utc_timestamp()
        append_mcp_event(
            config,
            name=name,
            arguments=arguments,
            result=result,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=(time.monotonic() - started) * 1000,
        )
        return result

    return server


async def run_stdio(config: McpServerConfig) -> None:
    server = build_server(config)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(config_path: Path) -> None:
    config = McpServerConfig.from_json_file(config_path)
    asyncio.run(run_stdio(config))
