from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from hooky import cli
from hooky.runtime import git_run


class SetupCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / ".hooky").mkdir(parents=True)
        os.environ.pop("HOOKY_RUN_KEY", None)
        os.environ.pop("HOOKY_RUN_DIR", None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_init_creates_git_baseline_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "package.json").write_text('{"scripts":{}}\n', encoding="utf-8")

            result = CliRunner().invoke(cli.app, ["-C", str(workspace), "init"])

            self.assertEqual(result.exit_code, 0, result.output)
            head = git_run(workspace, ["rev-parse", "--verify", "HEAD"], check=False)
            show = git_run(workspace, ["show", "HEAD:package.json"], check=False)
            status = git_run(workspace, ["status", "--short", "--", ".", ":!.hooky"], check=False)
            self.assertEqual(head.returncode, 0)
            self.assertEqual(show.stdout, '{"scripts":{}}\n')
            self.assertEqual(status.stdout, "")

    def test_skills_list_shows_workspace_skill(self) -> None:
        skill_path = self.workspace / ".agents/skills/example/SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text(
            "---\nname: example\ndescription: Workspace skill.\n---\n\n# Example\n",
            encoding="utf-8",
        )

        result = CliRunner().invoke(cli.app, ["-C", str(self.workspace), "skills", "list"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("example - Workspace skill.", result.output)

    def test_loop_init_creates_durable_state_files(self) -> None:
        result = CliRunner().invoke(
            cli.app,
            ["-C", str(self.workspace), "init", "--title", "Build a todo app"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((self.workspace / ".hooky/runs/local/feature_list.json").exists())
        self.assertTrue((self.workspace / ".hooky/runs/local/progress.md").exists())
        self.assertTrue((self.workspace / ".hooky/runs/local/contract.md").exists())
        self.assertTrue((self.workspace / ".hooky/runs/local/log.md").exists())
        self.assertTrue((self.workspace / ".hooky/runs/local/proposal.md").exists())
        contract = (self.workspace / ".hooky/runs/local/contract.md").read_text(encoding="utf-8")
        self.assertIn("Build a todo app", contract)
        proposal = (self.workspace / ".hooky/runs/local/proposal.md").read_text(encoding="utf-8")
        self.assertIn("Build a todo app", proposal)
        feature_list = cli.read_json(self.workspace / ".hooky/runs/local/feature_list.json")
        self.assertEqual(feature_list["features"], [])
        log = (self.workspace / ".hooky/runs/local/log.md").read_text(encoding="utf-8")
        self.assertRegex(log, r"(?m)^## \d{4}-\d{2}-\d{2}$")
        self.assertRegex(log, r"(?m)^- \d{2}:\d{2}:\d{2}Z init \| loop initialized$")
        self.assertRegex(log, r"(?m)^  - workspace: ")

    def test_loop_init_reads_proposal_from_stdin_and_derives_title(self) -> None:
        result = CliRunner().invoke(
            cli.app,
            ["-C", str(self.workspace), "init"],
            input="Build a todo app\n\nUsers can add and complete todos.\n",
        )

        self.assertEqual(result.exit_code, 0, result.output)
        contract = (self.workspace / ".hooky/runs/local/contract.md").read_text(encoding="utf-8")
        self.assertIn("# Build a todo app", contract)
        self.assertIn("Users can add and complete todos.", contract)

    def test_loop_init_derives_title_from_proposal_file_when_title_missing(self) -> None:
        body_file = self.workspace / "proposal.md"
        body_file.write_text("Build a calendar\n\nUsers can add events.\n", encoding="utf-8")

        result = CliRunner().invoke(
            cli.app,
            ["-C", str(self.workspace), "init", "--proposal-file", str(body_file)],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        contract = (self.workspace / ".hooky/runs/local/contract.md").read_text(encoding="utf-8")
        self.assertIn("# Build a calendar", contract)
        self.assertIn("Users can add events.", contract)


if __name__ == "__main__":
    unittest.main()
