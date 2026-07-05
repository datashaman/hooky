from __future__ import annotations

import json
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

    def test_read_loop_state_passes_when_state_and_progress_agree(self) -> None:
        cli.initialize_loop_files(self.workspace, title="demo", proposal="do the thing")

        state = cli.read_loop_state(self.workspace)

        self.assertEqual(state["status"], "initialized")

    def test_read_loop_state_raises_on_current_attempt_mismatch(self) -> None:
        cli.initialize_loop_files(self.workspace, title="demo", proposal="do the thing")
        state = cli.read_loop_state(self.workspace)
        state["current_attempt"] = "007"
        cli.write_json(cli.loop_state_path(self.workspace), state)
        # progress.md still says "current_attempt: none"

        with self.assertRaises(cli.LoopStateConsistencyError) as ctx:
            cli.read_loop_state(self.workspace)

        self.assertIn("current_attempt", str(ctx.exception))
        self.assertIn("007", str(ctx.exception))

    def test_read_loop_state_raises_on_status_mismatch(self) -> None:
        cli.initialize_loop_files(self.workspace, title="demo", proposal="do the thing")
        state = cli.read_loop_state(self.workspace)
        state["status"] = "attempt-running"
        cli.write_json(cli.loop_state_path(self.workspace), state)
        # progress.md still says "status: initialized"

        with self.assertRaises(cli.LoopStateConsistencyError) as ctx:
            cli.read_loop_state(self.workspace)

        self.assertIn("attempt-running", str(ctx.exception))
        self.assertIn("initialized", str(ctx.exception))

    def test_read_loop_state_raises_on_truncated_state_json(self) -> None:
        cli.initialize_loop_files(self.workspace, title="demo", proposal="do the thing")
        # Simulate a process killed mid-write: state.json truncated to a partial write.
        cli.loop_state_path(self.workspace).write_text('{"schema_version": 1, "status": "attempt-r', encoding="utf-8")

        with self.assertRaises(json.JSONDecodeError):
            cli.read_loop_state(self.workspace)


if __name__ == "__main__":
    unittest.main()
