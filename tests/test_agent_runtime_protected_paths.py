from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys
import socket
import subprocess
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_runtime  # noqa: E402


class ProtectedPathTests(unittest.TestCase):
    def test_disposable_runtime_output_under_protected_path_is_ignored_and_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            before = agent_runtime.snapshot_protected_paths(root, ["tests"])

            report = root / "tests" / "some-tool-report" / "data" / "result.md"
            report.parent.mkdir(parents=True)
            report.write_text("generated report", encoding="utf-8")

            changes = agent_runtime.protected_path_changes(root, ["tests"], before)

            self.assertEqual(changes, [])
            self.assertTrue(report.exists())

    def test_source_change_under_protected_path_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "tests" / "example.spec"
            source.parent.mkdir()
            source.write_text("before", encoding="utf-8")
            before = agent_runtime.snapshot_protected_paths(root, ["tests"])

            source.write_text("after", encoding="utf-8")

            self.assertEqual(agent_runtime.protected_path_changes(root, ["tests"], before), ["tests/example.spec"])

    def test_remediation_context_is_stage_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".workflow/artifacts/remediation/current.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"root_cause_stage": "builder", "findings": ["fix builder output"]}', encoding="utf-8")

            self.assertEqual(
                agent_runtime.read_remediation_context(root, "builder"),
                {"root_cause_stage": "builder", "findings": ["fix builder output"]},
            )
            self.assertIsNone(agent_runtime.read_remediation_context(root, "test"))

    def test_upstream_evidence_context_exposes_artifacts_and_runtime_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_state = {
                "task_id": "issue-1",
                "artifacts": {
                    "builder": {
                        "report_dir": ".workflow/artifacts/builder-agent",
                        "contract": ".workflow/artifacts/builder-agent/contract.json",
                    }
                },
            }
            (root / ".workflow/tasks/issue-1").mkdir(parents=True)
            (root / ".workflow/state.json").write_text('{"current_task": "issue-1"}', encoding="utf-8")
            (root / ".workflow/tasks/issue-1/state.json").write_text(json_dumps(task_state), encoding="utf-8")
            runtime = root / ".workflow/artifacts/builder-agent"
            runtime.mkdir(parents=True)
            (runtime / "runtime_events.log").write_text("event\n", encoding="utf-8")
            (runtime / "tool_events.json").write_text("[]\n", encoding="utf-8")

            context = agent_runtime.upstream_evidence_context(root)

            self.assertEqual(
                context["stages"]["builder"]["artifacts"]["contract"],
                ".workflow/artifacts/builder-agent/contract.json",
            )
            self.assertEqual(
                context["stages"]["builder"]["runtime"]["events"],
                ".workflow/artifacts/builder-agent/runtime_events.log",
            )
            self.assertEqual(
                context["stages"]["builder"]["runtime"]["tool_events"],
                ".workflow/artifacts/builder-agent/tool_events.json",
            )

    def test_requested_ports_are_parsed_from_common_explicit_forms(self) -> None:
        self.assertEqual(agent_runtime.requested_ports_from_command("npm run dev -- --port 5173"), [5173])
        self.assertEqual(agent_runtime.requested_ports_from_command("PORT=4173 npm start"), [4173])
        self.assertEqual(agent_runtime.requested_ports_from_command("serve http://127.0.0.1:8080"), [8080])

    def test_start_process_fails_when_requested_port_is_busy(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = int(sock.getsockname()[1])
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            runtime = agent_runtime.ToolRuntime(working_folder=Path(tmp.name), final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.start_process({"command": f"PORT={port} python3 -m http.server"})

            self.assertFalse(result["ok"])
            self.assertIn(port, result["requested_ports"])
            self.assertIn("already in use", result["error"])

    def test_start_and_list_processes_include_detected_ports(self) -> None:
        if not lsof_available():
            self.skipTest("lsof is required for listener detection")
        with tempfile.TemporaryDirectory() as tmp:
            port = free_tcp_port()
            runtime = agent_runtime.ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )
            command = (
                f"PORT={port} python3 -c \""
                "import os, socket, time; "
                "s=socket.socket(); "
                "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
                "s.bind(('127.0.0.1', int(os.environ['PORT']))); "
                "s.listen(1); "
                "time.sleep(60)"
                "\""
            )

            result = runtime.start_process({"command": command, "wait_seconds": 1})
            try:
                self.assertTrue(result["ok"], result)
                self.assertIn(port, result["requested_ports"])
                self.assertIn(port, result["ports"])
                listed = runtime.list_processes({})["processes"]
                self.assertEqual(len(listed), 1)
                self.assertIn(port, listed[0]["ports"])
            finally:
                runtime.cleanup_processes()

    def test_start_process_can_auto_allocate_port_env(self) -> None:
        if not lsof_available():
            self.skipTest("lsof is required for listener detection")
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agent_runtime.ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )
            command = (
                "python3 -c \""
                "import os, socket, time; "
                "s=socket.socket(); "
                "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
                "s.bind(('127.0.0.1', int(os.environ['PORT']))); "
                "s.listen(1); "
                "time.sleep(60)"
                "\""
            )

            result = runtime.start_process({"command": command, "auto_allocate_port": True, "wait_seconds": 1})
            try:
                self.assertTrue(result["ok"], result)
                self.assertIsInstance(result["allocated_port"], int)
                self.assertIn(result["allocated_port"], result["requested_ports"])
                self.assertIn(result["allocated_port"], result["ports"])
            finally:
                runtime.cleanup_processes()

    def test_runtime_timeline_includes_runtime_notices(self) -> None:
        rendered = agent_runtime.render_runtime_timeline_markdown(
            [
                {
                    "role": "runtime_notice",
                    "message": "Runtime soft deadline: about 30s remain.",
                    "started_at": "2026-06-29T00:00:00+00:00",
                    "ended_at": "2026-06-29T00:00:00+00:00",
                }
            ]
        )

        self.assertIn("runtime_notice", rendered)
        self.assertIn("Runtime soft deadline", rendered)

    def test_read_file_excerpt_and_many_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
            (root / "b.txt").write_text("alpha\nbeta\n", encoding="utf-8")
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            excerpt = runtime.read_file_excerpt({"path": "a.txt", "start_line": 2, "max_lines": 2})
            many = runtime.read_many_files({"paths": ["a.txt", "b.txt"], "max_bytes_per_file": 20})

            self.assertEqual(excerpt["content"], "two\nthree")
            self.assertEqual(excerpt["start_line"], 2)
            self.assertEqual(len(many["files"]), 2)

    def test_tool_result_artifacts_are_readable_without_exposing_workflow_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_path = root / ".workflow/tool-results/test-runs/result.log"
            result_path.parent.mkdir(parents=True)
            result_path.write_text("line one\nline two\n", encoding="utf-8")
            (root / ".workflow/artifacts/state.json").parent.mkdir(parents=True)
            (root / ".workflow/artifacts/state.json").write_text("{}", encoding="utf-8")
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            excerpt = runtime.read_file_excerpt(
                {"path": ".workflow/tool-results/test-runs/result.log", "start_line": 2, "max_lines": 1}
            )
            root_listing = runtime.list_files({"path": "."})

            self.assertEqual(excerpt["content"], "line two")
            self.assertNotIn(".workflow", [item["path"] for item in root_listing["entries"]])
            with self.assertRaises(FileNotFoundError):
                runtime.read_file({"path": ".workflow/artifacts/state.json"})
            with self.assertRaises(FileNotFoundError):
                runtime.write_file({"path": ".workflow/tool-results/test-runs/new.log", "content": "nope"})

    def test_todo_text_is_used_for_active_log_label(self) -> None:
        self.assertEqual(
            agent_runtime.active_todo_label([{"id": 1, "status": "in_progress", "text": "Analyze existing implementation"}]),
            "Analyze existing implementation",
        )

    def test_malformed_tool_names_are_canonicalized(self) -> None:
        valid = {"read_file", "write_file", "final_report"}

        self.assertEqual(agent_runtime.canonical_tool_name("write_file<|channel|>commentary", valid), "write_file")
        self.assertEqual(agent_runtime.canonical_tool_name("read_file.json", valid), "read_file")
        self.assertEqual(agent_runtime.canonical_tool_name("missing_tool.json", valid), "missing_tool.json")

    def test_runtime_dispatch_accepts_canonicalized_tool_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text("{}\n", encoding="utf-8")
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.run_tool("read_file.json", {"path": "package.json"})

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["content"], "{}\n")

    def test_detect_project_environment_finds_package_manager_and_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                '{"packageManager":"pnpm@9.0.0","scripts":{"test":"vitest"}}',
                encoding="utf-8",
            )
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
            (root / "playwright.config.cjs").write_text("module.exports = {}\n", encoding="utf-8")
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.detect_project_environment({})

            self.assertEqual(result["package_manager"], "pnpm")
            self.assertIn("pnpm test", result["test_commands"])
            self.assertIn("pnpm exec playwright test", result["test_commands"])

    def test_run_tests_saves_output_and_extracts_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

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

    def test_capture_visual_snapshot_saves_artifact_and_returns_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

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
                        "visibleElementCount": 4,
                    },
                }
                return subprocess.CompletedProcess(command, 0, stdout=json_dumps(payload), stderr="")

            with mock.patch.object(subprocess, "run", side_effect=fake_run):
                result = runtime.capture_visual_snapshot({"url": "http://127.0.0.1:4173", "wait_selector": ".app"})

            self.assertTrue(result["ok"])
            self.assertEqual(result["metrics"]["clippedElementCount"], 1)
            self.assertIn("visual-snapshots", result["screenshot_path"])
            self.assertTrue((root / result["screenshot_path"]).exists())
            self.assertEqual(runtime.pending_image_inputs[0]["path"], result["screenshot_path"])

    def test_image_input_message_uses_data_url_content_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "shot.png"
            image.write_bytes(b"png-bytes")
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            message = agent_runtime.image_input_message(runtime, [{"path": "shot.png", "label": "Screenshot"}], "Inspect this.")

            self.assertIsNotNone(message)
            content = message["content"]
            self.assertEqual(content[0]["type"], "text")
            self.assertEqual(content[1]["type"], "image_url")
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_available_tools_include_visual_snapshot(self) -> None:
        self.assertIn("capture_visual_snapshot", agent_runtime.available_tool_names())

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
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            status = runtime.git_status({})
            diff = runtime.git_diff({"path": "tracked.txt"})
            show = runtime.git_show({"ref": "HEAD", "path": "tracked.txt"})

            self.assertTrue(status["ok"])
            self.assertIn("tracked.txt", status["stdout"])
            self.assertIn("-before", diff["stdout"])
            self.assertIn("before", show["stdout"])

    def test_ensure_git_baseline_commits_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "existing.txt").write_text("baseline\n", encoding="utf-8")

            agent_runtime.ensure_git_baseline(root)

            head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root, check=False, text=True, capture_output=True)
            show = subprocess.run(["git", "show", "HEAD:existing.txt"], cwd=root, check=False, text=True, capture_output=True)
            status = subprocess.run(["git", "status", "--short"], cwd=root, check=False, text=True, capture_output=True)
            self.assertEqual(head.returncode, 0)
            self.assertEqual(show.stdout, "baseline\n")
            self.assertEqual(status.stdout, "")


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value)


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def lsof_available() -> bool:
    import shutil

    return shutil.which("lsof") is not None


if __name__ == "__main__":
    unittest.main()
