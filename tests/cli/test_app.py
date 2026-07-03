from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from hooky import cli


class AppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / ".hooky").mkdir(parents=True)
        os.environ.pop("HOOKY_RUN_KEY", None)
        os.environ.pop("HOOKY_RUN_DIR", None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_removed_pipeline_commands_are_not_registered(self) -> None:
        help_result = CliRunner().invoke(cli.app, ["--help"])

        self.assertEqual(help_result.exit_code, 0, help_result.output)
        self.assertNotIn(" task ", help_result.output)
        self.assertNotIn(" approve ", help_result.output)

        loop_result = CliRunner().invoke(cli.app, ["loop"])
        self.assertNotEqual(loop_result.exit_code, 0)
        pipeline_result = CliRunner().invoke(cli.app, ["-C", str(self.workspace), "run", "pipeline"])
        self.assertNotEqual(pipeline_result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
