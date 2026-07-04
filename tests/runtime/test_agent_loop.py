from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hooky.runtime import (
    ADVISOR_TOOL_TYPE,
    ToolRuntime,
    image_input_message,
    model_request_deadline_seconds,
    skill_catalog_message,
    tools_for_completion,
)
from hooky.shared import agent_skills


class AgentLoopTests(unittest.TestCase):
    def test_model_request_deadline_respects_remaining_stage_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_TIMEOUT_MS": "120000"}):
            runtime = ToolRuntime(working_folder=Path(tmp), final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=180)

            self.assertEqual(model_request_deadline_seconds(runtime, elapsed_seconds=175.2), 4)

    def test_skill_catalog_message_lists_available_skills_by_name_and_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_path = root / ".agents/skills/example/SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text(
                "---\nname: example\ndescription: Example skill.\n---\n\n# Example\n",
                encoding="utf-8",
            )
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                skills=agent_skills.discover_skills(root),
            )

            message = skill_catalog_message(runtime)

            self.assertIsNotNone(message)
            self.assertEqual(message["role"], "user")
            self.assertIn("example", message["content"])
            self.assertIn("Example skill.", message["content"])
            self.assertIn("activate_skill", message["content"])

    def test_skill_catalog_message_is_none_without_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            self.assertIsNone(skill_catalog_message(runtime))

    def test_image_input_message_uses_data_url_content_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "shot.png"
            image.write_bytes(b"png-bytes")
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            message = image_input_message(runtime, [{"path": "shot.png", "label": "Screenshot"}], "Inspect this.")

            self.assertIsNotNone(message)
            content = message["content"]
            self.assertEqual(content[0]["type"], "text")
            self.assertEqual(content[1]["type"], "image_url")
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_tools_for_completion_strips_advisor_tool_for_ollama_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOOKY_ADVISOR_MODEL": "anthropic/claude-opus-latest"}, clear=True):
            runtime = ToolRuntime(working_folder=Path(tmp), final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            openrouter_types = [tool.get("type") for tool in tools_for_completion(runtime, "anthropic/claude-3.5")]
            ollama_types = [tool.get("type") for tool in tools_for_completion(runtime, "ollama/gpt-oss:20b")]

            self.assertIn(ADVISOR_TOOL_TYPE, openrouter_types)
            self.assertNotIn(ADVISOR_TOOL_TYPE, ollama_types)


if __name__ == "__main__":
    unittest.main()
