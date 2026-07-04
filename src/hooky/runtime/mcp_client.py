"""MCP client bridge: lets the native (in-process) executor call external MCP servers
declared in a workspace's .mcp.json, the same file codex/claude already read.

The MCP Python SDK is asyncio-native; Hooky's tool runtime is synchronous. Each server
connection runs on a dedicated background thread's event loop, driven by a single
persistent request-queue task (not one-shot run_coroutine_threadsafe calls per operation)
because anyio's cancel scopes are task-affine: stdio_client/ClientSession must be entered
and exited from the same asyncio Task, so connect/call/close all happen inside one
long-lived driver coroutine.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MCP_CONFIG_FILENAME = ".mcp.json"
CONNECT_TIMEOUT_SECONDS = 30
CALL_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    command: str
    args: list[str]
    env: dict[str, str] | None


def discover_mcp_server_specs(working_folder: Path) -> list[McpServerSpec]:
    config_path = working_folder / MCP_CONFIG_FILENAME
    if not config_path.exists():
        return []
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    specs: list[McpServerSpec] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            continue
        args = entry.get("args")
        env = entry.get("env")
        specs.append(
            McpServerSpec(
                name=str(name),
                command=command,
                args=[str(item) for item in args] if isinstance(args, list) else [],
                env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else None,
            )
        )
    return specs


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    return f"mcp__{server_name}__{tool_name}"


class _LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="hooky-mcp-client")
        self.thread.start()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


class _ServerSession:
    def __init__(self, loop_thread: _LoopThread, spec: McpServerSpec) -> None:
        self.loop_thread = loop_thread
        self.spec = spec
        self._requests: asyncio.Queue[Any] | None = None
        self._error: BaseException | None = None
        ready = threading.Event()
        self._driver_future = asyncio.run_coroutine_threadsafe(self._driver(ready), loop_thread.loop)
        ready.wait(timeout=CONNECT_TIMEOUT_SECONDS)
        if not ready.is_set():
            raise TimeoutError(f"mcp server '{spec.name}' did not start within {CONNECT_TIMEOUT_SECONDS}s")
        if self._error is not None:
            raise self._error

    async def _driver(self, ready: threading.Event) -> None:
        self._requests = asyncio.Queue()
        params = StdioServerParameters(command=self.spec.command, args=self.spec.args, env=self.spec.env)
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                ready.set()
                while True:
                    item = await self._requests.get()
                    if item is None:
                        break
                    coro_factory, fut = item
                    try:
                        result = await coro_factory(session)
                        fut.set_result(result)
                    except Exception as exc:  # noqa: BLE001 - fed back to the caller, not raised here
                        fut.set_exception(exc)
        except Exception as exc:  # noqa: BLE001 - connection/startup failure, surfaced to __init__
            self._error = exc
            ready.set()

    def _submit(self, coro_factory: Any) -> Any:
        assert self._requests is not None
        fut: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self.loop_thread.loop.call_soon_threadsafe(self._requests.put_nowait, (coro_factory, fut))
        return fut.result(timeout=CALL_TIMEOUT_SECONDS)

    def list_tools(self) -> Any:
        return self._submit(lambda session: session.list_tools())

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        return self._submit(lambda session: session.call_tool(tool_name, arguments))

    def close(self) -> None:
        if self._requests is None:
            return
        self.loop_thread.loop.call_soon_threadsafe(self._requests.put_nowait, None)
        try:
            self._driver_future.result(timeout=CONNECT_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


def _mcp_content_to_result(content_result: Any) -> dict[str, Any]:
    structured = getattr(content_result, "structuredContent", None)
    if isinstance(structured, dict):
        result = dict(structured)
        result.setdefault("ok", not bool(getattr(content_result, "isError", False)))
        return result
    texts = []
    for block in getattr(content_result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            texts.append(text)
    return {"ok": not bool(getattr(content_result, "isError", False)), "content": "\n".join(texts)}


class McpClientManager:
    """Owns one background thread + persistent session per configured MCP server for the
    lifetime of a single ToolRuntime/role invocation."""

    def __init__(self, working_folder: Path, reserved_tool_names: set[str]) -> None:
        self._loop_thread: _LoopThread | None = None
        self._sessions: dict[str, _ServerSession] = {}
        self._schemas_by_name: dict[str, dict[str, Any]] = {}
        self._tool_owner: dict[str, tuple[str, str]] = {}
        self._warnings: list[str] = []
        self._connected = False
        self._reserved_tool_names = reserved_tool_names
        self._working_folder = working_folder

    def _ensure_connected(self) -> None:
        if self._connected:
            return
        self._connected = True
        specs = discover_mcp_server_specs(self._working_folder)
        if not specs:
            return
        self._loop_thread = _LoopThread()
        for spec in specs:
            try:
                session = _ServerSession(self._loop_thread, spec)
                tools = session.list_tools()
            except Exception as exc:  # noqa: BLE001 - one bad server shouldn't break the role
                self._warnings.append(f"mcp server '{spec.name}' failed to start: {exc}")
                continue
            self._sessions[spec.name] = session
            for tool in tools.tools:
                name = mcp_tool_name(spec.name, tool.name)
                if name in self._schemas_by_name or name in self._reserved_tool_names:
                    self._warnings.append(f"mcp tool '{name}' skipped: name collision")
                    continue
                schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {"type": "object", "properties": {}}
                self._schemas_by_name[name] = {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": tool.description or f"MCP tool {tool.name} from server {spec.name}",
                        "parameters": schema,
                    },
                }
                self._tool_owner[name] = (spec.name, tool.name)

    def tool_schemas(self) -> list[dict[str, Any]]:
        self._ensure_connected()
        return list(self._schemas_by_name.values())

    def tool_handlers(self) -> dict[str, Any]:
        self._ensure_connected()
        return {name: self._make_handler(name) for name in self._schemas_by_name}

    def _make_handler(self, name: str) -> Any:
        server_name, tool_name = self._tool_owner[name]

        def handler(args: dict[str, Any]) -> dict[str, Any]:
            session = self._sessions[server_name]
            try:
                result = session.call_tool(tool_name, args)
            except Exception as exc:  # noqa: BLE001 - tool errors feed back to the model
                return {"ok": False, "error": str(exc)}
            return _mcp_content_to_result(result)

        return handler

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    def close(self) -> None:
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()
        if self._loop_thread is not None:
            self._loop_thread.stop()
            self._loop_thread = None
