from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_skills  # noqa: E402


class AgentSkillsTests(unittest.TestCase):
    def test_discovers_project_skill_with_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_path = root / ".agents/skills/example/SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text(
                "---\nname: example\ndescription: Example skill.\n---\n\n# Example\n\nDo the thing.\n",
                encoding="utf-8",
            )

            skills = {skill.name: skill for skill in agent_skills.discover_skills(root)}

            self.assertIn("example", skills)
            self.assertEqual(skills["example"].description, "Example skill.")
            self.assertIn("Do the thing.", agent_skills.skill_context(list(skills.values()), ["example"]))

    def test_bundled_visual_ui_review_skill_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills = {skill.name: skill for skill in agent_skills.discover_skills(Path(tmp))}

            self.assertIn("visual-ui-review", skills)
            self.assertIn("browser UI", skills["visual-ui-review"].description)


if __name__ == "__main__":
    unittest.main()
