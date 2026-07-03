from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hooky.runtime import (
    ToolRuntime,
    compaction_user_prompt,
    extract_text_tool_actions,
    recover_text_final_report,
)


class TextTests(unittest.TestCase):
    def test_recovers_text_final_report_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                final_validator=lambda report: None if report.get("status") == "done" else (_ for _ in ()).throw(ValueError("bad status")),
            )

            recovered = recover_text_final_report(
                runtime,
                'Here is the report: {"status":"done","summary":"ok"}',
            )

            self.assertEqual(recovered, {"status": "done", "summary": "ok"})
            self.assertEqual(runtime.final_report, {"status": "done", "summary": "ok"})

    def test_rejects_invalid_text_final_report_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                final_validator=lambda report: (_ for _ in ()).throw(ValueError("bad report")),
            )

            recovered = recover_text_final_report(runtime, '{"status":"done"}')

            self.assertIsNone(recovered)
            self.assertIsNone(runtime.final_report)

    def test_extracts_text_declared_tool_actions(self) -> None:
        actions = extract_text_tool_actions('**assistant Action** ```json {"role":"assistant","content":[{"name":"final_report","arguments":{"status":"done"}}]} ```')

        self.assertEqual(actions, [{"name": "final_report", "arguments": {"status": "done"}}])

    def test_recovers_text_declared_final_report_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )

            recovered = recover_text_final_report(
                runtime,
                '{"role":"assistant","content":[{"name":"final_report","arguments":{"status":"done"}}]}',
            )

            self.assertEqual(recovered, {"status": "done"})
            self.assertEqual(runtime.final_report, {"status": "done"})

    def test_recovers_markdown_evaluator_final_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                final_validator=lambda report: None if report.get("status") == "done" else (_ for _ in ()).throw(ValueError("bad status")),
            )

            recovered = recover_text_final_report(
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

    def test_compaction_prompt_preserves_required_survival_checklist(self) -> None:
        prompt = compaction_user_prompt(
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


if __name__ == "__main__":
    unittest.main()
