from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from hooky.runtime import mcp_client
from hooky.runtime.tool_runtime import ToolRuntime

FIXTURE_SERVER = """
import asyncio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("fixture")
call_count = {"n": 0}

@server.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="echo",
            description="echo text",
            inputSchema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        )
    ]

@server.call_tool()
async def call_tool(name, arguments):
    call_count["n"] += 1
    return {"ok": True, "text": arguments.get("text"), "call_count": call_count["n"]}

async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())

asyncio.run(main())
"""


def write_fixture_server(root: Path) -> Path:
    script = root / "fixture_mcp_server.py"
    script.write_text(FIXTURE_SERVER, encoding="utf-8")
    return script


def write_mcp_config(root: Path, script: Path, *, server_name: str = "fixture") -> None:
    config = {"mcpServers": {server_name: {"command": sys.executable, "args": [script.as_posix()]}}}
    (root / ".mcp.json").write_text(json.dumps(config), encoding="utf-8")


class DiscoverMcpServerSpecsTests(unittest.TestCase):
    def test_returns_empty_when_no_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(mcp_client.discover_mcp_server_specs(Path(tmp)), [])

    def test_parses_valid_config_and_skips_malformed_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "mcpServers": {
                    "good": {"command": "hooky", "args": ["mcp-serve"], "env": {"X": "1"}},
                    "missing_command": {"args": ["a"]},
                    "not_a_dict": "oops",
                }
            }
            (root / ".mcp.json").write_text(json.dumps(config), encoding="utf-8")

            specs = mcp_client.discover_mcp_server_specs(root)

            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0].name, "good")
            self.assertEqual(specs[0].command, "hooky")
            self.assertEqual(specs[0].args, ["mcp-serve"])
            self.assertEqual(specs[0].env, {"X": "1"})

    def test_returns_empty_for_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".mcp.json").write_text("not json", encoding="utf-8")

            self.assertEqual(mcp_client.discover_mcp_server_specs(root), [])


class McpToolNameTests(unittest.TestCase):
    def test_uses_double_underscore_server_tool_convention(self) -> None:
        self.assertEqual(mcp_client.mcp_tool_name("fixture", "echo"), "mcp__fixture__echo")


class McpClientManagerEndToEndTests(unittest.TestCase):
    def test_discovers_calls_and_tears_down_a_real_stdio_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_fixture_server(root)
            write_mcp_config(root, script)

            manager = mcp_client.McpClientManager(root, reserved_tool_names=set())
            schemas = manager.tool_schemas()
            self.assertEqual(len(schemas), 1)
            self.assertEqual(schemas[0]["function"]["name"], "mcp__fixture__echo")

            handlers = manager.tool_handlers()
            self.assertIn("mcp__fixture__echo", handlers)

            result1 = handlers["mcp__fixture__echo"]({"text": "first"})
            result2 = handlers["mcp__fixture__echo"]({"text": "second"})

            self.assertEqual(result1, {"ok": True, "text": "first", "call_count": 1})
            self.assertEqual(result2, {"ok": True, "text": "second", "call_count": 2})

            manager.close()

    def test_skips_tool_names_colliding_with_reserved_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_fixture_server(root)
            write_mcp_config(root, script)

            manager = mcp_client.McpClientManager(root, reserved_tool_names={"mcp__fixture__echo"})
            schemas = manager.tool_schemas()

            self.assertEqual(schemas, [])
            self.assertTrue(any("collision" in warning for warning in manager.warnings))
            manager.close()

    def test_bad_server_command_produces_a_warning_and_no_tools_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"mcpServers": {"broken": {"command": "definitely-not-a-real-executable-xyz", "args": []}}}
            (root / ".mcp.json").write_text(json.dumps(config), encoding="utf-8")

            manager = mcp_client.McpClientManager(root, reserved_tool_names=set())
            schemas = manager.tool_schemas()

            self.assertEqual(schemas, [])
            self.assertTrue(any("broken" in warning for warning in manager.warnings))
            manager.close()


class ToolRuntimeMcpIntegrationTests(unittest.TestCase):
    def test_tools_and_tool_handlers_include_mcp_tools_when_mcp_json_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_fixture_server(root)
            write_mcp_config(root, script)
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=0.01,
                max_seconds=10,
            )

            names = {tool["function"]["name"] for tool in runtime.tools()}
            self.assertIn("mcp__fixture__echo", names)

            result = runtime.run_tool("mcp__fixture__echo", {"text": "hi"})
            self.assertEqual(result, {"ok": True, "text": "hi", "call_count": 1})

            runtime.close_mcp_clients()

    def test_enabled_tools_allowlist_still_governs_mcp_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_fixture_server(root)
            write_mcp_config(root, script)
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=0.01,
                max_seconds=10,
                enabled_tools=["final_report"],
            )

            names = {tool["function"]["name"] for tool in runtime.tools()}
            self.assertNotIn("mcp__fixture__echo", names)

            result = runtime.run_tool("mcp__fixture__echo", {"text": "hi"})
            self.assertEqual(result, {"ok": False, "error": "tool is disabled for this agent: mcp__fixture__echo"})

            runtime.close_mcp_clients()

    def test_no_mcp_json_means_no_overhead_and_no_extra_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=0.01,
                max_seconds=10,
            )

            names = {tool["function"]["name"] for tool in runtime.tools()}
            self.assertFalse(any(name.startswith("mcp__") for name in names))

            runtime.close_mcp_clients()


if __name__ == "__main__":
    unittest.main()
