from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hooky import cli


class AtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_write_is_readable_immediately_and_matches_input(self) -> None:
        target = self.workspace / "sub" / "state.json"

        cli.atomic_write_text(target, "hello world\n")

        self.assertEqual(target.read_text(encoding="utf-8"), "hello world\n")

    def test_no_leftover_temp_file_after_write(self) -> None:
        target = self.workspace / "progress.md"

        cli.atomic_write_text(target, "content\n")

        entries = sorted(p.name for p in self.workspace.iterdir())
        self.assertEqual(entries, ["progress.md"])

    def test_overwrite_replaces_prior_contents_with_no_leftovers(self) -> None:
        target = self.workspace / "contract.md"
        cli.atomic_write_text(target, "first\n")

        cli.atomic_write_text(target, "second\n")

        self.assertEqual(target.read_text(encoding="utf-8"), "second\n")
        entries = sorted(p.name for p in self.workspace.iterdir())
        self.assertEqual(entries, ["contract.md"])

    def test_crash_before_rename_leaves_original_target_intact(self) -> None:
        target = self.workspace / "state.json"
        target.write_text("original\n", encoding="utf-8")

        with mock.patch("os.replace", side_effect=OSError("simulated crash before rename")):
            with self.assertRaises(OSError):
                cli.atomic_write_text(target, "new-content\n")

        self.assertEqual(target.read_text(encoding="utf-8"), "original\n")
        tmp_files = [p for p in self.workspace.iterdir() if p.name != "state.json"]
        self.assertEqual(len(tmp_files), 1)
        self.assertTrue(tmp_files[0].name.endswith(".tmp"))

    def test_crash_during_write_leaves_no_leftover_and_original_intact(self) -> None:
        target = self.workspace / "state.json"
        target.write_text("original\n", encoding="utf-8")

        with mock.patch("os.fsync", side_effect=OSError("simulated crash while fsyncing")):
            with self.assertRaises(OSError):
                cli.atomic_write_text(target, "new-content\n")

        self.assertEqual(target.read_text(encoding="utf-8"), "original\n")
        entries = sorted(p.name for p in self.workspace.iterdir())
        self.assertEqual(entries, ["state.json"])

    def test_write_json_uses_atomic_helper_and_leaves_no_leftovers(self) -> None:
        target = self.workspace / "feature_list.json"

        cli.write_json(target, {"schema_version": 1, "features": []})

        self.assertEqual(cli.read_json(target), {"schema_version": 1, "features": []})
        entries = sorted(p.name for p in self.workspace.iterdir())
        self.assertEqual(entries, ["feature_list.json"])


if __name__ == "__main__":
    unittest.main()
