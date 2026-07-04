from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hooky.runtime import ADVISOR_TOOL_TYPE, ToolRuntime, available_tool_names


class ToolRuntimeTests(unittest.TestCase):
    def test_enabled_tools_rejects_hidden_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                enabled_tools=["final_report"],
            )

            self.assertEqual(
                [tool["function"]["name"] for tool in runtime.tools()],
                ["final_report"],
            )
            result = runtime.run_tool("read_files", {"paths": ["README.md"]})

            self.assertFalse(result["ok"])
            self.assertIn("tool is disabled", result["error"])

    def test_runtime_dispatch_accepts_canonicalized_tool_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text("{}\n", encoding="utf-8")
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.run_tool("read_files.json", {"paths": ["package.json"]})

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["files"][0]["content"], "{}\n")

    def test_tool_schema_order_matches_available_tool_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )

            schema_names = [tool["function"]["name"] for tool in runtime.tools()]

            self.assertEqual(schema_names, available_tool_names())

    def test_advisor_tool_is_included_when_configured_and_unrestricted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOOKY_ADVISOR_MODEL": "anthropic/claude-opus-latest"}, clear=True):
            runtime = ToolRuntime(working_folder=Path(tmp), final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            types = [tool.get("type") for tool in runtime.tools()]

            self.assertIn(ADVISOR_TOOL_TYPE, types)

    def test_advisor_tool_is_excluded_when_enabled_tools_restricts_the_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOOKY_ADVISOR_MODEL": "anthropic/claude-opus-latest"}, clear=True):
            runtime = ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                enabled_tools=["final_report"],
            )

            types = [tool.get("type") for tool in runtime.tools()]

            self.assertNotIn(ADVISOR_TOOL_TYPE, types)

    def test_advisor_tool_is_absent_when_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            runtime = ToolRuntime(working_folder=Path(tmp), final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            types = [tool.get("type") for tool in runtime.tools()]

            self.assertNotIn(ADVISOR_TOOL_TYPE, types)


if __name__ == "__main__":
    unittest.main()
