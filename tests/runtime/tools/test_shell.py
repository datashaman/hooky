from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hooky.runtime import ToolRuntime


class ShellToolsTests(unittest.TestCase):
    def test_bash_rejects_long_running_server_and_background_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )

            server = runtime.bash({"command": "npm run dev -- --port 5173"})
            background = runtime.bash({"command": "npx vite preview --port 5173 & echo $!"})
            help_command = runtime.bash({"command": "printf ok && npx vite --help >/dev/null 2>&1 || true"})

            self.assertFalse(server["ok"])
            self.assertIn("start_process", server["error"])
            self.assertFalse(background["ok"])
            self.assertIn("background process", background["error"])
            self.assertTrue(help_command["ok"])

    def test_detect_project_environment_finds_package_manager_and_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                '{"packageManager":"pnpm@9.0.0","scripts":{"test":"vitest"}}',
                encoding="utf-8",
            )
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
            (root / "playwright.config.cjs").write_text("module.exports = {}\n", encoding="utf-8")
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.detect_project_environment({})

            self.assertEqual(result["package_manager"], "pnpm")
            self.assertIn("pnpm test", result["test_commands"])
            self.assertIn("pnpm exec playwright test", result["test_commands"])

    def test_detect_project_environment_finds_lint_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"scripts":{"lint":"eslint ."}}', encoding="utf-8")
            (root / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.detect_project_environment({})

            self.assertIn("npm run lint", result["lint_commands"])
            self.assertIn("ruff check .", result["lint_commands"])

    def test_run_lint_saves_output_and_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.run_lint({"command": "python3 -c \"print('bad style'); raise SystemExit(1)\"", "timeout_seconds": 10})

            self.assertFalse(result["ok"])
            self.assertEqual(result["returncode"], 1)
            self.assertTrue((root / result["output_path"]).exists())
            self.assertIn("lint-runs", result["output_path"])

    def test_run_lint_without_command_or_detection_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ToolRuntime(working_folder=Path(tmp), final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.run_lint({})

            self.assertFalse(result["ok"])
            self.assertIn("no lint command", result["error"])

    def test_run_tests_saves_output_and_extracts_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.run_tests(
                {
                    "command": "python3 -c \"print('1) tests/example.spec.js:1:1 › suite › fails'); raise SystemExit(1)\"",
                    "timeout_seconds": 10,
                }
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["returncode"], 1)
            self.assertIn("tests/example.spec.js", result["summary"]["failed_tests"][0])
            self.assertTrue((root / result["output_path"]).exists())

    def test_latest_test_failure_context_writes_bundle_with_error_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            error_context = root / "test-results" / "example-failure" / "error-context.md"
            error_context.parent.mkdir(parents=True)
            error_context.write_text("# Page snapshot\n\nbutton: Save\n", encoding="utf-8")
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            test_result = runtime.run_tests(
                {
                    "command": "python3 -c \"print('1) tests/example.spec.js:1:1 › suite › fails'); raise SystemExit(1)\"",
                    "timeout_seconds": 10,
                }
            )
            runtime.tool_events.append({"name": "run_tests", "result": test_result})

            result = runtime.latest_test_failure_context({})

            self.assertTrue(result["ok"])
            self.assertIn("tests/example.spec.js", result["failed_tests"][0])
            self.assertEqual(result["artifacts"][0]["path"], "test-results/example-failure/error-context.md")
            self.assertIn("Page snapshot", result["content"])
            self.assertTrue((root / result["context_path"]).exists())
            self.assertIn("failure-context", result["context_path"])

    def test_latest_test_failure_context_reports_missing_failed_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ToolRuntime(working_folder=Path(tmp), final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.latest_test_failure_context({})

            self.assertFalse(result["ok"])
            self.assertIn("no failed run_tests", result["error"])

    def test_capture_visual_snapshot_saves_artifact_and_returns_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            def fake_run(command, cwd, text, capture_output, timeout):
                screenshot_path = Path(command[3])
                screenshot_path.write_bytes(b"png")
                payload = {
                    "url": command[2],
                    "screenshotBytes": 3,
                    "consoleMessages": [],
                    "metrics": {
                        "viewportCoverage": 0.05,
                        "visiblePaintCoverage": 0.05,
                        "topGapRatio": 0.2,
                        "clippedElementCount": 1,
                        "headingInteractiveOverlapCount": 1,
                        "sampleHeadingInteractiveOverlaps": [{"headingText": "Create project", "interactiveText": "Project name", "overlapRatio": 0.35}],
                        "visibleElementCount": 4,
                    },
                }
                return subprocess.CompletedProcess(command, 0, stdout=json_dumps(payload), stderr="")

            with mock.patch.object(subprocess, "run", side_effect=fake_run):
                result = runtime.capture_visual_snapshot({"url": "http://127.0.0.1:4173", "wait_selector": ".app"})

            self.assertTrue(result["ok"])
            self.assertEqual(result["metrics"]["clippedElementCount"], 1)
            self.assertEqual(result["metrics"]["headingInteractiveOverlapCount"], 1)
            self.assertIn("visual-snapshots", result["screenshot_path"])
            self.assertTrue((root / result["screenshot_path"]).exists())
            self.assertEqual(runtime.pending_image_inputs[0]["path"], result["screenshot_path"])

    def test_interact_and_snapshot_requires_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ToolRuntime(working_folder=Path(tmp), final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            with self.assertRaises(ValueError):
                runtime.interact_and_snapshot({"url": "http://127.0.0.1:4173", "actions": []})

    def test_interact_and_snapshot_runs_actions_and_reports_step_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            def fake_run(command, cwd, text, capture_output, timeout):
                screenshot_path = Path(command[3])
                screenshot_path.write_bytes(b"png")
                actions_path = Path(command[8])
                self.assertTrue(actions_path.exists())
                payload = {
                    "url": command[2],
                    "screenshotBytes": 3,
                    "consoleMessages": [],
                    "metrics": {"viewportCoverage": 0.4, "visibleElementCount": 6},
                    "steps": [
                        {"action": "click", "selector": "#open-form", "value": "", "ok": True},
                        {"action": "fill", "selector": "#missing", "value": "hello", "ok": False, "error": "selector not found"},
                    ],
                }
                return subprocess.CompletedProcess(command, 0, stdout=json_dumps(payload), stderr="")

            with mock.patch.object(subprocess, "run", side_effect=fake_run):
                result = runtime.interact_and_snapshot(
                    {
                        "url": "http://127.0.0.1:4173",
                        "actions": [
                            {"action": "click", "selector": "#open-form"},
                            {"action": "fill", "selector": "#missing", "value": "hello"},
                        ],
                    }
                )

            self.assertFalse(result["ok"])
            self.assertEqual(len(result["steps"]), 2)
            self.assertFalse(result["steps"][1]["ok"])
            self.assertIn("visual-snapshots", result["screenshot_path"])
            self.assertTrue((root / result["screenshot_path"]).exists())
            self.assertEqual(runtime.pending_image_inputs[0]["path"], result["screenshot_path"])


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value)


if __name__ == "__main__":
    unittest.main()
