from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from hooky.runtime import (
    ensure_git_baseline,
    protected_path_changes,
    snapshot_protected_paths,
)


class GitTests(unittest.TestCase):
    def test_disposable_runtime_output_under_protected_path_is_ignored_and_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            before = snapshot_protected_paths(root, ["tests"])

            report = root / "tests" / "some-tool-report" / "data" / "result.md"
            report.parent.mkdir(parents=True)
            report.write_text("generated report", encoding="utf-8")

            changes = protected_path_changes(root, ["tests"], before)

            self.assertEqual(changes, [])
            self.assertTrue(report.exists())

    def test_source_change_under_protected_path_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "tests" / "example.spec"
            source.parent.mkdir()
            source.write_text("before", encoding="utf-8")
            before = snapshot_protected_paths(root, ["tests"])

            source.write_text("after", encoding="utf-8")

            self.assertEqual(protected_path_changes(root, ["tests"], before), ["tests/example.spec"])

    def test_ensure_git_baseline_commits_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "existing.txt").write_text("baseline\n", encoding="utf-8")

            ensure_git_baseline(root)

            head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root, check=False, text=True, capture_output=True)
            show = subprocess.run(["git", "show", "HEAD:existing.txt"], cwd=root, check=False, text=True, capture_output=True)
            status = subprocess.run(["git", "status", "--short"], cwd=root, check=False, text=True, capture_output=True)
            self.assertEqual(head.returncode, 0)
            self.assertEqual(show.stdout, "baseline\n")
            self.assertEqual(status.stdout, "")


if __name__ == "__main__":
    unittest.main()
