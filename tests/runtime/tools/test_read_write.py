from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hooky.runtime import ToolRuntime


class ReadWriteToolsTests(unittest.TestCase):
    def test_write_files_requires_current_uncompacted_read_of_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src/App.jsx"
            source.parent.mkdir()
            source.write_text("before", encoding="utf-8")
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )

            with self.assertRaisesRegex(ValueError, "was not read in the current uncompacted context"):
                runtime.write_files({"files": [{"path": "src/App.jsx", "content": "after"}]})

            runtime.read_files({"paths": ["src/App.jsx"]})
            result = runtime.write_files({"files": [{"path": "src/App.jsx", "content": "after"}]})

            self.assertTrue(result["ok"])
            self.assertEqual(source.read_text(encoding="utf-8"), "after")

    def test_write_files_records_agent_written_content_as_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src/App.jsx"
            source.parent.mkdir()
            source.write_text("before", encoding="utf-8")
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )

            runtime.read_files({"paths": ["src/App.jsx"]})
            first = runtime.write_files({"files": [{"path": "src/App.jsx", "content": "after"}]})
            second = runtime.write_files({"files": [{"path": "src/App.jsx", "content": "after again"}]})

            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertEqual(source.read_text(encoding="utf-8"), "after again")

    def test_write_files_requires_reread_when_existing_file_changed_after_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src/App.jsx"
            source.parent.mkdir()
            source.write_text("before", encoding="utf-8")
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )

            runtime.read_files({"paths": ["src/App.jsx"]})
            source.write_text("changed elsewhere", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "changed since it was read"):
                runtime.write_files({"files": [{"path": "src/App.jsx", "content": "after"}]})

    def test_write_files_is_atomic_when_one_existing_file_is_not_currently_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "src/first.jsx"
            second = root / "src/second.jsx"
            first.parent.mkdir()
            first.write_text("first-before", encoding="utf-8")
            second.write_text("second-before", encoding="utf-8")
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )

            runtime.read_files({"paths": ["src/first.jsx"]})

            with self.assertRaisesRegex(ValueError, "second.jsx was not read"):
                runtime.write_files(
                    {
                        "files": [
                            {"path": "src/first.jsx", "content": "first-after"},
                            {"path": "src/second.jsx", "content": "second-after"},
                        ]
                    }
                )

            self.assertEqual(first.read_text(encoding="utf-8"), "first-before")
            self.assertEqual(second.read_text(encoding="utf-8"), "second-before")

    def test_edit_files_requires_current_uncompacted_read_of_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src/App.jsx"
            source.parent.mkdir()
            source.write_text("one\ntwo\nthree\n", encoding="utf-8")
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )

            with self.assertRaisesRegex(ValueError, "was not read in the current uncompacted context"):
                runtime.edit_files(
                    {
                        "files": [
                            {
                                "path": "src/App.jsx",
                                "edits": [{"start_line": 2, "end_line": 2, "replacement": "TWO\n"}],
                            }
                        ]
                    }
                )

            runtime.read_files({"paths": ["src/App.jsx"]})
            result = runtime.edit_files(
                {
                    "files": [
                        {
                            "path": "src/App.jsx",
                            "edits": [
                                {"start_line": 2, "end_line": 2, "replacement": "TWO\n"},
                                {"start_line": 4, "end_line": 3, "replacement": "four\n"},
                            ],
                        }
                    ]
                }
            )

            self.assertTrue(result["ok"])
            self.assertEqual(source.read_text(encoding="utf-8"), "one\nTWO\nthree\nfour\n")

    def test_edit_files_is_atomic_when_one_edit_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "src/first.txt"
            second = root / "src/second.txt"
            first.parent.mkdir()
            first.write_text("one\n", encoding="utf-8")
            second.write_text("alpha\n", encoding="utf-8")
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )

            runtime.read_files({"paths": ["src/first.txt", "src/second.txt"]})

            with self.assertRaisesRegex(ValueError, "ends after end of file"):
                runtime.edit_files(
                    {
                        "files": [
                            {
                                "path": "src/first.txt",
                                "edits": [{"start_line": 1, "end_line": 1, "replacement": "ONE\n"}],
                            },
                            {
                                "path": "src/second.txt",
                                "edits": [{"start_line": 2, "end_line": 2, "replacement": "beta\n"}],
                            },
                        ]
                    }
                )

            self.assertEqual(first.read_text(encoding="utf-8"), "one\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "alpha\n")

    def test_edit_files_rejects_overlapping_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src/App.jsx"
            source.parent.mkdir()
            source.write_text("one\ntwo\nthree\n", encoding="utf-8")
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )

            runtime.read_files({"paths": ["src/App.jsx"]})

            with self.assertRaisesRegex(ValueError, "overlap"):
                runtime.edit_files(
                    {
                        "files": [
                            {
                                "path": "src/App.jsx",
                                "edits": [
                                    {"start_line": 1, "end_line": 2, "replacement": "changed\n"},
                                    {"start_line": 2, "end_line": 3, "replacement": "also changed\n"},
                                ],
                            }
                        ]
                    }
                )

    def test_compaction_invalidates_read_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src/App.jsx"
            source.parent.mkdir()
            source.write_text("before", encoding="utf-8")
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )

            runtime.read_files({"paths": ["src/App.jsx"]})
            runtime.advance_read_generation()

            with self.assertRaisesRegex(ValueError, "current uncompacted context"):
                runtime.write_files({"files": [{"path": "src/App.jsx", "content": "after"}]})

    def test_write_allowed_prefixes_remain_available_for_system_artifact_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / ".hooky/runs/local/contract.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("before", encoding="utf-8")
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                write_allowed_prefixes=[".hooky/runs/local/contract.md"],
                write_blocked_prefixes=[],
            )

            result = runtime.write_files({"files": [{"path": ".hooky/runs/local/contract.md", "content": "after"}]})

            self.assertTrue(result["ok"])
            self.assertEqual(artifact.read_text(encoding="utf-8"), "after")

    def test_read_file_excerpt_and_read_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
            (root / "b.txt").write_text("alpha\nbeta\n", encoding="utf-8")
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            excerpt = runtime.read_file_excerpt({"path": "a.txt", "start_line": 2, "max_lines": 2})
            many = runtime.read_files({"paths": ["a.txt", "b.txt"], "max_bytes_per_file": 20})

            self.assertEqual(excerpt["content"], "two\nthree")
            self.assertEqual(excerpt["start_line"], 2)
            self.assertEqual(len(many["files"]), 2)

    def test_read_files_reports_per_file_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("one\n", encoding="utf-8")
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.read_files({"paths": ["a.txt", "missing.txt"]})

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["files"][0]["content"], "one\n")
            self.assertEqual(result["files"][1]["path"], "missing.txt")
            self.assertIn("error", result["files"][1])

    def test_find_files_matches_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src/styles.css").write_text("body {}\n", encoding="utf-8")
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.find_files({"pattern": "src/styles.css"})

            self.assertEqual(result["matches"], ["src/styles.css"])

    def test_search_files_searches_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src/app.js").write_text("class Mirror {}\nconst material = new Mirror();\n", encoding="utf-8")
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.search_files({"pattern": "Mirror"})

            self.assertTrue(result["ok"])
            self.assertEqual([item["line"] for item in result["matches"]], [1, 2])

    def test_tool_result_artifacts_are_readable_without_exposing_workflow_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_path = root / ".hooky/runs/local/tool-results/test-runs/result.log"
            result_path.parent.mkdir(parents=True)
            result_path.write_text("line one\nline two\n", encoding="utf-8")
            (root / ".hooky/artifacts/state.json").parent.mkdir(parents=True)
            (root / ".hooky/artifacts/state.json").write_text("{}", encoding="utf-8")
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            excerpt = runtime.read_file_excerpt({"path": ".hooky/runs/local/tool-results/test-runs/result.log", "start_line": 2, "max_lines": 1})
            root_listing = runtime.list_files({"path": "."})

            self.assertEqual(excerpt["content"], "line two")
            self.assertNotIn(".hooky", [item["path"] for item in root_listing["entries"]])
            blocked_read = runtime.read_files({"paths": [".hooky/artifacts/state.json"]})
            self.assertEqual(blocked_read["files"][0]["error"], "path not found: .hooky/artifacts/state.json")
            with self.assertRaises(FileNotFoundError):
                runtime.write_files({"files": [{"path": ".hooky/runs/local/tool-results/test-runs/new.log", "content": "nope"}]})

    def test_list_files_hides_custom_read_blocked_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "App.jsx").write_text("export default function App() {}", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "framework.js").write_text("internal", encoding="utf-8")
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                read_blocked_prefixes=[".hooky", "node_modules"],
            )

            result = runtime.list_files({"path": "."})

            self.assertTrue(result["ok"])
            paths = {entry["path"] for entry in result["entries"]}
            self.assertIn("src", paths)
            self.assertNotIn("node_modules", paths)

    def test_read_files_rejects_custom_read_blocked_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "node_modules").mkdir()
            (root / "node_modules" / "framework.js").write_text("internal", encoding="utf-8")
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                read_blocked_prefixes=[".hooky", "node_modules"],
            )

            result = runtime.run_tool("read_files", {"paths": ["node_modules/framework.js"]})

            self.assertTrue(result["ok"])
            self.assertIn("path not found", result["files"][0]["error"])


if __name__ == "__main__":
    unittest.main()
