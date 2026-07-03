from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from hooky import cli


class EvidenceCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / ".hooky").mkdir(parents=True)
        os.environ.pop("HOOKY_RUN_KEY", None)
        os.environ.pop("HOOKY_RUN_DIR", None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_evidence_cli_captures_note_and_command_output(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])

        init = runner.invoke(cli.app, ["-C", str(self.workspace), "evidence", "init"])
        note = runner.invoke(
            cli.app,
            ["-C", str(self.workspace), "evidence", "note", "Manual check", "--body", "Reviewed the proposal."],
        )
        command = runner.invoke(
            cli.app,
            ["-C", str(self.workspace), "evidence", "exec", "printf cli-evidence", "--title", "CLI command"],
        )
        show = runner.invoke(cli.app, ["-C", str(self.workspace), "evidence", "show"])

        self.assertEqual(init.exit_code, 0, init.output)
        self.assertEqual(note.exit_code, 0, note.output)
        self.assertEqual(command.exit_code, 0, command.output)
        self.assertEqual(show.exit_code, 0, show.output)
        self.assertIn(".hooky/runs/local/evidence.md", init.output)
        self.assertIn("Manual check", show.output)
        self.assertIn("Reviewed the proposal.", show.output)
        self.assertIn("CLI command", show.output)
        self.assertIn("cli-evidence", show.output)
        self.assertTrue((self.workspace / ".hooky/runs/local/evidence/command-output").exists())


if __name__ == "__main__":
    unittest.main()
