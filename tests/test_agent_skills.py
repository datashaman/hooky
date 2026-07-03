from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hooky import agent_skills


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

    def test_discovers_external_skill_root_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as external:
            skill_path = Path(external) / "skills-sh-example/SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text(
                "---\nname: skills-sh-example\ndescription: External skill.\n---\n\n# External\n\nUse this.\n",
                encoding="utf-8",
            )

            with mock.patch.dict("os.environ", {"HOOKY_SKILL_ROOTS": external}):
                skills = {skill.name: skill for skill in agent_skills.discover_skills(Path(tmp))}

            self.assertIn("skills-sh-example", skills)

    def test_parses_multiline_yaml_frontmatter_description(self) -> None:
        metadata, body = agent_skills.parse_frontmatter("---\nname: example\ndescription: >\n  First sentence.\n  Second sentence.\n---\n\n# Body\n")

        self.assertEqual(metadata["description"], "First sentence. Second sentence.")
        self.assertEqual(body, "\n# Body\n")


if __name__ == "__main__":
    unittest.main()
