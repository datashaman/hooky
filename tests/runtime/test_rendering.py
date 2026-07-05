from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hooky.runtime import (
    ToolRuntime,
    active_todo_label,
    append_live_event,
    assistant_message_text,
    assistant_reasoning_trace,
    canonical_tool_name,
    format_runtime_event_line,
    render_runtime_events_log,
    render_runtime_timeline_markdown,
    write_runtime_log,
)


class RenderingTests(unittest.TestCase):
    def test_runtime_timeline_includes_runtime_notices(self) -> None:
        rendered = render_runtime_timeline_markdown(
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
        rendered = render_runtime_timeline_markdown(
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
        rendered = render_runtime_events_log(
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
        rendered = render_runtime_events_log(
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
        rendered = render_runtime_events_log(
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

    def test_assistant_reasoning_is_attached_and_previewed_but_not_visible_text(self) -> None:
        message = {
            "role": "assistant",
            "content": [
                {"type": "reasoning", "text": "private chain of thought"},
                {"type": "text", "text": "Visible response"},
            ],
            "reasoning": "provider reasoning",
        }
        reasoning = assistant_reasoning_trace(message)
        rendered = render_runtime_events_log(
            [
                {
                    "role": "assistant",
                    "message": message,
                    "reasoning": reasoning,
                    "usage": {"total_tokens": 123, "cost": 0.001},
                    "duration_ms": 1500,
                    "started_at": "2026-07-01T00:00:00+00:00",
                    "ended_at": "2026-07-01T00:00:01+00:00",
                }
            ]
        )
        timeline = render_runtime_timeline_markdown(
            [
                {
                    "role": "assistant",
                    "message": message,
                    "reasoning": reasoning,
                    "usage": {"total_tokens": 123, "cost": 0.001},
                    "duration_ms": 1500,
                    "started_at": "2026-07-01T00:00:00+00:00",
                    "ended_at": "2026-07-01T00:00:01+00:00",
                }
            ]
        )

        self.assertIsNotNone(reasoning)
        self.assertEqual(assistant_message_text(message), "Visible response")
        self.assertIn("reasoning=", rendered)
        self.assertIn("provider reasoning", rendered)
        self.assertIn("private chain of thought", rendered)
        self.assertIn("reasoning: present", timeline)
        self.assertNotIn("private chain of thought", timeline)

    def test_todo_text_is_used_for_active_log_label(self) -> None:
        self.assertEqual(
            active_todo_label([{"id": 1, "status": "in_progress", "text": "Analyze existing implementation"}]),
            "Analyze existing implementation",
        )

    def test_malformed_tool_names_are_canonicalized(self) -> None:
        valid = {"read_files", "write_files", "final_report"}

        self.assertEqual(canonical_tool_name("write_files<|channel|>commentary", valid), "write_files")
        self.assertEqual(canonical_tool_name("read_files.json", valid), "read_files")
        self.assertEqual(canonical_tool_name("missing_tool.json", valid), "missing_tool.json")

    def test_runtime_event_line_displays_canonical_tool_name(self) -> None:
        line = format_runtime_event_line(
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

            write_runtime_log(root, transcript, [], [], [], metadata=metadata)

            archive = root / "invocations" / "2026-07-01T00-00-00-00-00-loop-generator-contract"
            self.assertTrue((archive / "runtime_transcript.json").exists())
            archived = json.loads((archive / "runtime_transcript.json").read_text(encoding="utf-8"))
            self.assertEqual(archived[0]["message"]["content"], "plain text response")

    def test_append_live_event_prefixes_the_live_log_root_file_with_role(self) -> None:
        # live_log_root's runtime_events.log used to hardcode an empty prefix,
        # so watching it live gave no indication of which Hooky role
        # (planner/generator/evaluator) emitted a given line - unlike the
        # separate aggregate log.runtime file, which already carried
        # live_event_prefix. Both should show the same role tag.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                live_log_root=root / "traces",
                live_event_log_paths=[root / "log.runtime"],
                live_event_prefix="role=generator ",
            )

            append_live_event(runtime, "2026-07-01T00:00:00+00:00 run start executor=claude")

            live_log_root_line = (root / "traces" / "runtime_events.log").read_text(encoding="utf-8")
            aggregate_line = (root / "log.runtime").read_text(encoding="utf-8")
            self.assertIn("role=generator run start", live_log_root_line)
            self.assertEqual(live_log_root_line, aggregate_line)


if __name__ == "__main__":
    unittest.main()
