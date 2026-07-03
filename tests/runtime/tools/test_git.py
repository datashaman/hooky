from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from hooky.runtime import ToolRuntime


class GitToolsTests(unittest.TestCase):
    def test_git_status_and_diff_are_read_only_structured_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "tracked.txt").write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, text=True, capture_output=True)
            (root / "tracked.txt").write_text("after\n", encoding="utf-8")
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            status = runtime.git_status({})
            diff = runtime.git_diff({"path": "tracked.txt"})
            show = runtime.git_show({"ref": "HEAD", "path": "tracked.txt"})

            self.assertTrue(status["ok"])
            self.assertIn("tracked.txt", status["stdout"])
            self.assertIn("-before", diff["stdout"])
            self.assertIn("before", show["stdout"])


if __name__ == "__main__":
    unittest.main()
