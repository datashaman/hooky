from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from hooky import cli


class LoopStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / ".hooky").mkdir(parents=True)
        os.environ.pop("HOOKY_RUN_KEY", None)
        os.environ.pop("HOOKY_RUN_DIR", None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reset_loop_attempt_workspace_removes_untracked_files_but_preserves_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "src").mkdir()
            (workspace / "src/App.jsx").write_text("before\n", encoding="utf-8")
            (workspace / ".hooky/runs/local").mkdir(parents=True)
            (workspace / ".hooky/runs/local/log.md").write_text("log\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(workspace), "config", "user.email", "test@example.local"], check=True)
            subprocess.run(["git", "-C", str(workspace), "config", "user.name", "Hooky Test"], check=True)
            subprocess.run(["git", "-C", str(workspace), "add", "src/App.jsx"], check=True)
            subprocess.run(["git", "-C", str(workspace), "commit", "-q", "-m", "baseline"], check=True)

            (workspace / "src/App.jsx").write_text("after\n", encoding="utf-8")
            (workspace / "tests").mkdir()
            (workspace / "tests/stale.spec.js").write_text("stale\n", encoding="utf-8")

            note = cli.reset_loop_attempt_workspace(workspace)

            self.assertIn("HEAD is now at", note)
            self.assertEqual((workspace / "src/App.jsx").read_text(encoding="utf-8"), "before\n")
            self.assertFalse((workspace / "tests/stale.spec.js").exists())
            self.assertTrue((workspace / ".hooky/runs/local/log.md").exists())


if __name__ == "__main__":
    unittest.main()
