from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
import sys
import os
import socket
import subprocess
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_runtime  # noqa: E402
import agent_skills  # noqa: E402


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

    def test_requested_ports_are_parsed_from_common_explicit_forms(self) -> None:
        self.assertEqual(agent_runtime.requested_ports_from_command("npm run dev -- --port 5173"), [5173])
        self.assertEqual(agent_runtime.requested_ports_from_command("PORT=4173 npm start"), [4173])
        self.assertEqual(agent_runtime.requested_ports_from_command("serve http://127.0.0.1:8080"), [8080])

    def test_bash_rejects_long_running_server_and_background_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agent_runtime.ToolRuntime(
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

    def test_write_files_requires_current_uncompacted_read_of_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src/App.jsx"
            source.parent.mkdir()
            source.write_text("before", encoding="utf-8")
            runtime = agent_runtime.ToolRuntime(
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
            runtime = agent_runtime.ToolRuntime(
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
            runtime = agent_runtime.ToolRuntime(
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
            runtime = agent_runtime.ToolRuntime(
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
            runtime = agent_runtime.ToolRuntime(
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
            runtime = agent_runtime.ToolRuntime(
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
            runtime = agent_runtime.ToolRuntime(
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
            runtime = agent_runtime.ToolRuntime(
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
            runtime = agent_runtime.ToolRuntime(
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

    def test_timed_out_shell_command_terminates_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "marker"
            command = (
                "python3 -c \""
                "import pathlib, subprocess, time; "
                f"p=pathlib.Path({str(marker)!r}); "
                "subprocess.Popen(['python3','-c',"
                "'import pathlib,time; time.sleep(2); pathlib.Path(%r).write_text(\\\"leaked\\\")' % str(p)]); "
                "time.sleep(5)"
                "\""
            )

            completed, timed_out = agent_runtime.run_shell_command(command, cwd=root, timeout_seconds=1)
            time.sleep(2.5)

            self.assertTrue(timed_out)
            self.assertEqual(completed.returncode, 124)
            self.assertFalse(marker.exists())

    def test_model_request_deadline_respects_remaining_stage_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_TIMEOUT_MS": "120000"}):
            runtime = agent_runtime.ToolRuntime(working_folder=Path(tmp), final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=180)

            self.assertEqual(agent_runtime.model_request_deadline_seconds(runtime, elapsed_seconds=175.2), 4)

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
                self.assertEqual(result["url"], f"http://127.0.0.1:{port}")
                listed = runtime.list_processes({})["processes"]
                self.assertEqual(len(listed), 1)
                self.assertIn(port, listed[0]["ports"])
                self.assertEqual(listed[0]["url"], f"http://127.0.0.1:{port}")
                read = runtime.read_process({"process_id": result["process_id"]})
                self.assertEqual(read["url"], f"http://127.0.0.1:{port}")
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
                    "role": "user",
                    "kind": "no_tool_calls",
                    "message": "Runtime soft deadline: about 30s remain.",
                    "started_at": "2026-06-29T00:00:00+00:00",
                    "ended_at": "2026-06-29T00:00:00+00:00",
                }
            ]
        )

        self.assertIn("user", rendered)
        self.assertIn("no_tool_calls", rendered)
        self.assertIn("Runtime soft deadline", rendered)

    def test_runtime_timeline_includes_assistant_messages(self) -> None:
        rendered = agent_runtime.render_runtime_timeline_markdown(
            [
                {
                    "role": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": "I am checking the contract before writing files.",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "read_files", "arguments": "{}"},
                            }
                        ],
                    },
                    "usage": {"total_tokens": 321, "cost": 0.001},
                    "duration_ms": 1200,
                    "started_at": "2026-07-01T00:00:00+00:00",
                    "ended_at": "2026-07-01T00:00:01+00:00",
                }
            ]
        )

        self.assertIn("Assistant message:", rendered)
        self.assertIn("I am checking the contract", rendered)
        self.assertIn("tools requested: read_files", rendered)

    def test_runtime_event_log_includes_user_prompts(self) -> None:
        rendered = agent_runtime.render_runtime_events_log(
            [
                {
                    "role": "user",
                    "kind": "no_tool_calls",
                    "message": "Continue by using the available tools.",
                    "started_at": "2026-06-29T00:00:00+00:00",
                    "ended_at": "2026-06-29T00:00:00+00:00",
                }
            ]
        )

        self.assertIn("user kind=no_tool_calls", rendered)
        self.assertIn("Continue by using the available tools", rendered)

    def test_runtime_event_log_includes_no_tool_assistant_message(self) -> None:
        rendered = agent_runtime.render_runtime_events_log(
            [
                {
                    "role": "assistant",
                    "message": {"role": "assistant", "content": "I should write the final report but forgot to call the tool."},
                    "usage": {"total_tokens": 123, "cost": 0.001},
                    "duration_ms": 1500,
                    "started_at": "2026-07-01T00:00:00+00:00",
                    "ended_at": "2026-07-01T00:00:01+00:00",
                }
            ]
        )

        self.assertIn("tool_calls=0", rendered)
        self.assertIn("message=", rendered)
        self.assertIn("forgot to call the tool", rendered)

    def test_runtime_event_log_includes_assistant_message_with_tool_calls(self) -> None:
        rendered = agent_runtime.render_runtime_events_log(
            [
                {
                    "role": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": "I will inspect the workspace before editing.",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "list_files", "arguments": "{}"},
                            }
                        ],
                    },
                    "usage": {"total_tokens": 456, "cost": 0.002},
                    "duration_ms": 500,
                    "started_at": "2026-07-01T00:00:00+00:00",
                    "ended_at": "2026-07-01T00:00:01+00:00",
                }
            ]
        )

        self.assertIn("tool_calls=1", rendered)
        self.assertIn("tools=list_files", rendered)
        self.assertIn("I will inspect the workspace", rendered)

    def test_recovers_text_final_report_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agent_runtime.ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                final_validator=lambda report: None if report.get("status") == "done" else (_ for _ in ()).throw(ValueError("bad status")),
            )

            recovered = agent_runtime.recover_text_final_report(
                runtime,
                'Here is the report: {"status":"done","summary":"ok"}',
            )

            self.assertEqual(recovered, {"status": "done", "summary": "ok"})
            self.assertEqual(runtime.final_report, {"status": "done", "summary": "ok"})

    def test_rejects_invalid_text_final_report_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agent_runtime.ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                final_validator=lambda report: (_ for _ in ()).throw(ValueError("bad report")),
            )

            recovered = agent_runtime.recover_text_final_report(runtime, '{"status":"done"}')

            self.assertIsNone(recovered)
            self.assertIsNone(runtime.final_report)

    def test_extracts_text_declared_tool_actions(self) -> None:
        actions = agent_runtime.extract_text_tool_actions(
            '**assistant Action** ```json {"role":"assistant","content":[{"name":"final_report","arguments":{"status":"done"}}]} ```'
        )

        self.assertEqual(actions, [{"name": "final_report", "arguments": {"status": "done"}}])

    def test_recovers_text_declared_final_report_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agent_runtime.ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )

            recovered = agent_runtime.recover_text_final_report(
                runtime,
                '{"role":"assistant","content":[{"name":"final_report","arguments":{"status":"done"}}]}',
            )

            self.assertEqual(recovered, {"status": "done"})
            self.assertEqual(runtime.final_report, {"status": "done"})

    def test_recovers_markdown_evaluator_final_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agent_runtime.ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                final_validator=lambda report: None if report.get("status") == "done" else (_ for _ in ()).throw(ValueError("bad status")),
            )

            recovered = agent_runtime.recover_text_final_report(
                runtime,
                """**final_report**
Accepted: **false**

**required_changes**
1. Add concrete test names.
2. Remove vague "etc." language.

**review**
The contract is close but still underspecified.
""",
            )

            self.assertEqual(recovered["status"], "done")
            self.assertFalse(recovered["accepted"])
            self.assertEqual(
                recovered["required_changes"],
                ["Add concrete test names.", 'Remove vague "etc." language.'],
            )
            self.assertEqual(recovered["review"], "The contract is close but still underspecified.")

    def test_enabled_tools_rejects_hidden_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agent_runtime.ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                enabled_tools=["final_report"],
            )

            self.assertEqual(
                [tool["function"]["name"] for tool in runtime.tools()],
                ["final_report"],
            )
            result = runtime.run_tool("read_files", {"paths": ["README.md"]})

            self.assertFalse(result["ok"])
            self.assertIn("tool is disabled", result["error"])

    def test_read_file_excerpt_and_read_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
            (root / "b.txt").write_text("alpha\nbeta\n", encoding="utf-8")
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            excerpt = runtime.read_file_excerpt({"path": "a.txt", "start_line": 2, "max_lines": 2})
            many = runtime.read_files({"paths": ["a.txt", "b.txt"], "max_bytes_per_file": 20})

            self.assertEqual(excerpt["content"], "two\nthree")
            self.assertEqual(excerpt["start_line"], 2)
            self.assertEqual(len(many["files"]), 2)

    def test_read_files_reports_per_file_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("one\n", encoding="utf-8")
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

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
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.find_files({"pattern": "src/styles.css"})

            self.assertEqual(result["matches"], ["src/styles.css"])

    def test_search_files_searches_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src/app.js").write_text("class Mirror {}\nconst material = new Mirror();\n", encoding="utf-8")
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

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
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            excerpt = runtime.read_file_excerpt(
                {"path": ".hooky/runs/local/tool-results/test-runs/result.log", "start_line": 2, "max_lines": 1}
            )
            root_listing = runtime.list_files({"path": "."})

            self.assertEqual(excerpt["content"], "line two")
            self.assertNotIn(".hooky", [item["path"] for item in root_listing["entries"]])
            blocked_read = runtime.read_files({"paths": [".hooky/artifacts/state.json"]})
            self.assertEqual(blocked_read["files"][0]["error"], "path not found: .hooky/artifacts/state.json")
            with self.assertRaises(FileNotFoundError):
                runtime.write_files({"files": [{"path": ".hooky/runs/local/tool-results/test-runs/new.log", "content": "nope"}]})

    def test_todo_text_is_used_for_active_log_label(self) -> None:
        self.assertEqual(
            agent_runtime.active_todo_label([{"id": 1, "status": "in_progress", "text": "Analyze existing implementation"}]),
            "Analyze existing implementation",
        )

    def test_malformed_tool_names_are_canonicalized(self) -> None:
        valid = {"read_files", "write_files", "final_report"}

        self.assertEqual(agent_runtime.canonical_tool_name("write_files<|channel|>commentary", valid), "write_files")
        self.assertEqual(agent_runtime.canonical_tool_name("read_files.json", valid), "read_files")
        self.assertEqual(agent_runtime.canonical_tool_name("missing_tool.json", valid), "missing_tool.json")

    def test_runtime_event_line_displays_canonical_tool_name(self) -> None:
        line = agent_runtime.format_runtime_event_line(
            {
                "role": "assistant",
                "ended_at": "2026-06-29T15:08:09+00:00",
                "duration_ms": 73414.35,
                "usage": {"cost": 0.00060479, "total_tokens": 11263},
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "write_spec_contract<|channel|>commentary",
                            }
                        }
                    ]
                },
            }
        )

        self.assertIn("tools=write_spec_contract", line)
        self.assertNotIn("<|channel|>", line)

    def test_runtime_dispatch_accepts_canonicalized_tool_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text("{}\n", encoding="utf-8")
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.run_tool("read_files.json", {"paths": ["package.json"]})

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["files"][0]["content"], "{}\n")

    def test_runtime_can_activate_skill_and_read_resource(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_path = root / ".agents/skills/example/SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text(
                "---\nname: example\ndescription: Example skill.\n---\n\n# Example\n\nUse references only when needed.\n",
                encoding="utf-8",
            )
            (skill_path.parent / "references").mkdir()
            (skill_path.parent / "references/details.md").write_text("Detailed guidance.\n", encoding="utf-8")
            runtime = agent_runtime.ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                skills=agent_skills.discover_skills(root),
            )

            activated = runtime.activate_skill({"name": "example"})
            resource = runtime.read_skill_resource({"name": "example", "path": "references/details.md"})

            self.assertTrue(activated["ok"], activated)
            self.assertIn("Use references only when needed.", activated["body"])
            self.assertEqual(resource["content"], "Detailed guidance.\n")

    def test_runtime_requires_skill_activation_before_resource_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_path = root / ".agents/skills/example/SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text("# Example\n", encoding="utf-8")
            (skill_path.parent / "details.md").write_text("secret\n", encoding="utf-8")
            runtime = agent_runtime.ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                skills=agent_skills.discover_skills(root),
            )

            with self.assertRaisesRegex(ValueError, "activate skill before reading resources"):
                runtime.read_skill_resource({"name": "example", "path": "details.md"})

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

    def test_latest_test_failure_context_writes_bundle_with_error_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            error_context = root / "test-results" / "example-failure" / "error-context.md"
            error_context.parent.mkdir(parents=True)
            error_context.write_text("# Page snapshot\n\nbutton: Save\n", encoding="utf-8")
            runtime = agent_runtime.ToolRuntime(working_folder=root, final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

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
            runtime = agent_runtime.ToolRuntime(working_folder=Path(tmp), final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.latest_test_failure_context({})

            self.assertFalse(result["ok"])
            self.assertIn("no failed run_tests", result["error"])

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
                        "headingInteractiveOverlapCount": 1,
                        "sampleHeadingInteractiveOverlaps": [
                            {"headingText": "Create project", "interactiveText": "Project name", "overlapRatio": 0.35}
                        ],
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

    def test_available_tools_include_latest_test_failure_context(self) -> None:
        self.assertIn("latest_test_failure_context", agent_runtime.available_tool_names())

    def test_available_tools_include_edit_files(self) -> None:
        self.assertIn("edit_files", agent_runtime.available_tool_names())

    def test_available_tools_include_time_extension_request(self) -> None:
        self.assertIn("request_time_extension", agent_runtime.available_tool_names())

    def test_available_tools_include_search_files(self) -> None:
        names = agent_runtime.available_tool_names()

        self.assertIn("search_files", names)

    def test_tool_schema_order_matches_available_tool_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agent_runtime.ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )

            schema_names = [tool["function"]["name"] for tool in runtime.tools()]

            self.assertEqual(schema_names, agent_runtime.available_tool_names())

    def test_compaction_prompt_preserves_required_survival_checklist(self) -> None:
        prompt = agent_runtime.compaction_user_prompt(
            "Previous state.",
            [{"role": "assistant", "content": "I read src/App.jsx and saw a failing test."}],
        )

        self.assertIn("Objective", prompt)
        self.assertIn("Current state", prompt)
        self.assertIn("Decisions and constraints", prompt)
        self.assertIn("Files and artifacts", prompt)
        self.assertIn("Tool results and failures", prompt)
        self.assertIn("Todo state", prompt)
        self.assertIn("Next relevant actions", prompt)
        self.assertIn("Previous state.", prompt)
        self.assertIn("src/App.jsx", prompt)

    def test_evidence_tools_capture_notes_and_command_output_under_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            traces = root / ".hooky/runs/local/attempts/001/traces"
            traces.mkdir(parents=True)
            runtime = agent_runtime.ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                live_log_root=traces,
            )

            note = runtime.append_evidence_note({"title": "Review note", "body": "Checked the contract."})
            command = runtime.append_evidence_command({"title": "Command proof", "command": "printf evidence-ok"})
            report = root / ".hooky/runs/local/attempts/001/evidence.md"

            self.assertTrue(note["ok"])
            self.assertTrue(command["ok"], command)
            self.assertEqual(note["evidence_path"], ".hooky/runs/local/attempts/001/evidence.md")
            self.assertEqual(command["evidence_path"], ".hooky/runs/local/attempts/001/evidence.md")
            self.assertIn("evidence/command-output", command["output_path"])
            self.assertIn("Review note", report.read_text(encoding="utf-8"))
            self.assertIn("Command proof", report.read_text(encoding="utf-8"))
            self.assertIn("evidence-ok", (root / command["output_path"]).read_text(encoding="utf-8"))

    def test_available_tools_include_evidence_capture(self) -> None:
        names = agent_runtime.available_tool_names()
        self.assertIn("append_evidence_note", names)
        self.assertIn("append_evidence_command", names)
        self.assertIn("append_evidence_screenshot", names)

    def test_list_files_hides_custom_read_blocked_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "App.jsx").write_text("export default function App() {}", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "framework.js").write_text("internal", encoding="utf-8")
            runtime = agent_runtime.ToolRuntime(
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
            runtime = agent_runtime.ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                read_blocked_prefixes=[".hooky", "node_modules"],
            )

            result = runtime.run_tool("read_files", {"paths": ["node_modules/framework.js"]})

            self.assertTrue(result["ok"])
            self.assertIn("path not found", result["files"][0]["error"])

    def test_time_extension_is_granted_only_near_deadline_with_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = agent_runtime.ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                max_extension_seconds=120,
                max_extension_requests=1,
            )
            runtime.started_at = time.monotonic() - 26
            runtime.tool_events.append({"name": "write_files", "result": {"ok": True}})
            runtime.tool_events.append({"name": "run_tests", "result": {"ok": False}})

            result = runtime.request_time_extension(
                {
                    "requested_seconds": 90,
                    "reason": "Focused TDD loop is still reducing failures.",
                    "current_status": "One failing Playwright test remains.",
                    "next_step": "Patch the toggle-all handler and rerun that test.",
                }
            )

            self.assertTrue(result["ok"], result)
            self.assertTrue(result["granted"])
            self.assertEqual(result["added_seconds"], 90)
            self.assertEqual(runtime.max_seconds, 120)

    def test_runtime_log_preserves_invocation_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = [
                {
                    "role": "assistant",
                    "message": {"content": "plain text response", "tool_calls": []},
                    "started_at": "2026-07-01T00:00:00+00:00",
                    "ended_at": "2026-07-01T00:00:01+00:00",
                    "duration_ms": 1000,
                }
            ]
            metadata = {
                "schema_version": 1,
                "agent_name": "loop-generator-contract",
                "started_at": "2026-07-01T00:00:00+00:00",
                "written_at": "2026-07-01T00:00:02+00:00",
            }

            agent_runtime.write_runtime_log(root, transcript, [], [], [], metadata=metadata)

            archive = root / "invocations" / "2026-07-01T00-00-00-00-00-loop-generator-contract"
            self.assertTrue((archive / "runtime_transcript.json").exists())
            archived = json.loads((archive / "runtime_transcript.json").read_text(encoding="utf-8"))
            self.assertEqual(archived[0]["message"]["content"], "plain text response")

    def test_time_extension_requires_recent_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agent_runtime.ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                max_extension_seconds=120,
                max_extension_requests=1,
            )
            runtime.started_at = time.monotonic() - 26

            result = runtime.request_time_extension(
                {
                    "requested_seconds": 90,
                    "reason": "Need more time.",
                    "current_status": "Still investigating.",
                    "next_step": "Think more.",
                }
            )

            self.assertFalse(result["ok"])
            self.assertFalse(result["granted"])
            self.assertIn("progress", result["error"])

    def test_passing_tests_near_deadline_grant_report_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agent_runtime.ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                max_post_success_grace_seconds=90,
            )
            runtime.started_at = time.monotonic() - 25

            result = runtime.grant_post_success_grace({"ok": True, "passed": True})

            self.assertIsNotNone(result)
            assert result is not None
            self.assertGreaterEqual(result["added_seconds"], 80)
            self.assertGreater(runtime.max_seconds, 100)

    def test_passing_tests_early_do_not_grant_report_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agent_runtime.ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=300,
                max_post_success_grace_seconds=90,
            )

            result = runtime.grant_post_success_grace({"ok": True, "passed": True})

            self.assertIsNone(result)
            self.assertEqual(runtime.max_seconds, 300)

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

    def test_model_provider_routes_ollama_and_openrouter_credentials(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(agent_runtime.model_provider("ollama/gpt-oss:20b"), "ollama")
            self.assertEqual(agent_runtime.provider_model_name("ollama/gpt-oss:20b"), "gpt-oss:20b")
            self.assertTrue(agent_runtime.model_credentials_available("ollama/gpt-oss:20b"))
            self.assertFalse(agent_runtime.model_credentials_available("openai/gpt-oss-20b"))

    def test_ollama_chat_url_accepts_root_or_v1_base_url(self) -> None:
        self.assertEqual(
            agent_runtime.OllamaChat("http://localhost:11434").chat_completions_url(),
            "http://localhost:11434/v1/chat/completions",
        )
        self.assertEqual(
            agent_runtime.OllamaChat("http://localhost:11434/v1").chat_completions_url(),
            "http://localhost:11434/v1/chat/completions",
        )


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
