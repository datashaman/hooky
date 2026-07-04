from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp import types

from hooky.runtime import mcp_server


class McpServerConfigTests(unittest.TestCase):
    def test_config_round_trips_through_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = mcp_server.McpServerConfig(
                working_folder=root.as_posix(),
                events_path=(root / "events.jsonl").as_posix(),
                max_seconds=120,
                bash_timeout_seconds=30,
                read_allowed_prefixes=[".hooky"],
                bash_protected_prefixes=[".git"],
                live_log_root=(root / ".hooky/runs/local/attempts/1/traces").as_posix(),
                live_event_prefix="role=generator ",
            )
            config_path = root / "config.json"

            config.to_json_file(config_path)
            loaded = mcp_server.McpServerConfig.from_json_file(config_path)

            self.assertEqual(loaded, config)


class McpServerToolDispatchTests(unittest.TestCase):
    def test_build_runtime_restricts_enabled_tools_to_the_curated_subset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = mcp_server.McpServerConfig(working_folder=root.as_posix(), events_path=(root / "events.jsonl").as_posix())

            runtime = mcp_server.build_runtime(config)

            self.assertEqual(set(runtime.enabled_tools or []), set(mcp_server.MCP_TOOL_NAMES))
            self.assertFalse(runtime.write_enabled)

    def test_list_tools_exposes_only_curated_tools_with_matching_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = mcp_server.McpServerConfig(working_folder=root.as_posix(), events_path=(root / "events.jsonl").as_posix())
            server = mcp_server.build_server(config)

            list_tools_handler = server.request_handlers[types.ListToolsRequest]
            result = asyncio.run(list_tools_handler(mock.Mock(params=None)))

            names = {tool.name for tool in result.root.tools}
            self.assertEqual(names, set(mcp_server.MCP_TOOL_NAMES))
            self.assertNotIn("write_files", names)
            self.assertNotIn("final_report", names)

    def test_call_tool_dispatches_through_runtime_and_appends_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / "events.jsonl"
            events_path.write_text("", encoding="utf-8")
            config = mcp_server.McpServerConfig(working_folder=root.as_posix(), events_path=events_path.as_posix())
            server = mcp_server.build_server(config)

            handler = server.request_handlers[types.CallToolRequest]
            request = types.CallToolRequest(method="tools/call", params={"name": "detect_project_environment", "arguments": {}})

            response = asyncio.run(handler(request))

            self.assertFalse(response.root.isError)
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["name"], "detect_project_environment")
            self.assertTrue(events[0]["result"]["ok"])
            self.assertEqual(events[0]["source"], "mcp")


if __name__ == "__main__":
    unittest.main()
