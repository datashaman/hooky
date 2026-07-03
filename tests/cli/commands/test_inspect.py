from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from hooky import cli


class InspectCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / ".hooky").mkdir(parents=True)
        os.environ.pop("HOOKY_RUN_KEY", None)
        os.environ.pop("HOOKY_RUN_DIR", None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_loop_debug_commands_show_runtime_transcript_and_stalls(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])
        trace_root = self.workspace / ".hooky/runs/local/attempts/001/traces"
        cli.write_json(
            trace_root / "runtime_transcript.json",
            {
                "bad": "shape",
            },
        )
        (trace_root / "runtime_transcript.json").write_text(
            json.dumps(
                [
                    {
                        "role": "user",
                        "kind": "initial",
                        "message": "Implement TodoMVC.",
                        "started_at": "2026-06-30T00:00:00+00:00",
                        "ended_at": "2026-06-30T00:00:00+00:00",
                    },
                    {
                        "role": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": '**final_report** {"status":"done"}',
                        },
                        "reasoning": {
                            "entries": [{"source": "message.reasoning", "content": "private diagnostic reasoning"}],
                            "chars": 28,
                            "visible_by_default": False,
                        },
                        "started_at": "2026-06-30T00:00:01+00:00",
                        "ended_at": "2026-06-30T00:00:01+00:00",
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (trace_root / "runtime_events.log").write_text("2026-06-30T00:00:01+00:00 assistant tool_calls=0\n", encoding="utf-8")
        (trace_root / "tool_events.json").write_text("[]\n", encoding="utf-8")
        (trace_root / "compaction_events.json").write_text(json.dumps([{"reason": "context_threshold"}]) + "\n", encoding="utf-8")
        (trace_root / "pre_compaction_archives.json").write_text(
            json.dumps([{"older_messages": [{"role": "assistant", "content": "older"}]}]) + "\n",
            encoding="utf-8",
        )

        transcript = runner.invoke(cli.app, ["-C", str(self.workspace), "transcript", "--attempt", "001"])
        transcript_reasoning = runner.invoke(cli.app, ["-C", str(self.workspace), "transcript", "--attempt", "001", "--reasoning"])
        stall = runner.invoke(cli.app, ["-C", str(self.workspace), "stall", "--attempt", "001"])
        context = runner.invoke(cli.app, ["-C", str(self.workspace), "context", "--attempt", "001"])
        runtime_log = runner.invoke(cli.app, ["-C", str(self.workspace), "runtime-log", "--attempt", "001"])

        self.assertEqual(transcript.exit_code, 0, transcript.output)
        self.assertIn("Implement TodoMVC.", transcript.output)
        self.assertIn("final_report", transcript.output)
        self.assertIn("reasoning_chars=28", transcript.output)
        self.assertNotIn("private diagnostic reasoning", transcript.output)
        self.assertEqual(transcript_reasoning.exit_code, 0, transcript_reasoning.output)
        self.assertIn("private diagnostic reasoning", transcript_reasoning.output)
        self.assertEqual(stall.exit_code, 0, stall.output)
        self.assertIn("fake_final_report_text_entries: 1", stall.output)
        self.assertEqual(context.exit_code, 0, context.output)
        self.assertIn("compactions: 1", context.output)
        self.assertIn("archived_older_messages: 1", context.output)
        self.assertIn("tool_schema_order: read_files, read_file_excerpt, write_files, edit_files", context.output)
        self.assertEqual(runtime_log.exit_code, 0, runtime_log.output)
        self.assertIn("tool_calls=0", runtime_log.output)

    def test_loop_inspect_trace_grep_and_harness_review_surface_debug_state(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])
        trace_root = self.workspace / ".hooky/runs/local/attempts/001/traces"
        (trace_root / "runtime_transcript.json").write_text(
            json.dumps(
                [
                    {"role": "system", "message": "system prompt"},
                    {"role": "user", "message": "Build TodoMVC"},
                    {
                        "role": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": "I will inspect the contract.",
                            "tool_calls": [{"function": {"name": "read_files"}}],
                        },
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (trace_root / "runtime_events.log").write_text("role=evaluator assistant tools=read_files\n", encoding="utf-8")
        (trace_root / "tool_events.json").write_text(json.dumps([{"name": "read_files", "result": {"ok": True}}]), encoding="utf-8")
        (trace_root / "compaction_events.json").write_text("[]\n", encoding="utf-8")
        (trace_root / "pre_compaction_archives.json").write_text("[]\n", encoding="utf-8")

        inspect = runner.invoke(cli.app, ["-C", str(self.workspace), "inspect", "--attempt", "001"])
        grep = runner.invoke(cli.app, ["-C", str(self.workspace), "trace-grep", "TodoMVC", "--attempt", "001"])
        review = runner.invoke(cli.app, ["-C", str(self.workspace), "harness-review"])

        self.assertEqual(inspect.exit_code, 0, inspect.output)
        self.assertIn("transcript_entries: 3", inspect.output)
        self.assertIn("read_files: 1", inspect.output)
        self.assertEqual(grep.exit_code, 0, grep.output)
        self.assertIn("TodoMVC", grep.output)
        self.assertEqual(review.exit_code, 0, review.output)
        self.assertIn("Loop Harness Review", review.output)
        self.assertIn("missing substantive Taste Rubric in contract.md", review.output)
        self.assertIn("context_transcript", review.output)
        self.assertIn("tool_prefix_stability", review.output)
        self.assertTrue((cli.loop_dir(self.workspace) / "harness_review.md").exists())


if __name__ == "__main__":
    unittest.main()
