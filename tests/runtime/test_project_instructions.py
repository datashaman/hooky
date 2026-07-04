from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hooky.runtime.project_instructions import MAX_PROJECT_INSTRUCTIONS_CHARS, compose_system_prompt, project_instructions_prompt_addendum


class ProjectInstructionsPromptAddendumTests(unittest.TestCase):
    def test_returns_empty_string_when_no_agents_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(project_instructions_prompt_addendum(Path(tmp)), "")

    def test_returns_empty_string_when_agents_md_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("   \n", encoding="utf-8")

            self.assertEqual(project_instructions_prompt_addendum(root), "")

    def test_wraps_agents_md_content_with_a_distinguishing_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("Always use pnpm. Never touch legacy/.", encoding="utf-8")

            addendum = project_instructions_prompt_addendum(root)

            self.assertIn("## Project Instructions (from AGENTS.md)", addendum)
            self.assertIn("Always use pnpm. Never touch legacy/.", addendum)

    def test_truncates_oversized_agents_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("x" * (MAX_PROJECT_INSTRUCTIONS_CHARS + 500), encoding="utf-8")

            addendum = project_instructions_prompt_addendum(root)

            self.assertIn("(truncated)", addendum)
            self.assertLessEqual(len(addendum), MAX_PROJECT_INSTRUCTIONS_CHARS + 200)


class ComposeSystemPromptTests(unittest.TestCase):
    def test_returns_base_prompt_unchanged_when_no_agents_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(compose_system_prompt("base prompt", Path(tmp)), "base prompt")

    def test_appends_addendum_when_agents_md_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("Project-specific rule.", encoding="utf-8")

            composed = compose_system_prompt("base prompt", root)

            self.assertTrue(composed.startswith("base prompt"))
            self.assertIn("Project-specific rule.", composed)


if __name__ == "__main__":
    unittest.main()
