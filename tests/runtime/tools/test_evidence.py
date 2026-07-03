from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from hooky.runtime import ToolRuntime
from hooky.shared import agent_skills


class EvidenceToolsTests(unittest.TestCase):
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
            runtime = ToolRuntime(
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
            runtime = ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                skills=agent_skills.discover_skills(root),
            )

            with self.assertRaisesRegex(ValueError, "activate skill before reading resources"):
                runtime.read_skill_resource({"name": "example", "path": "details.md"})

    def test_evidence_tools_capture_notes_and_command_output_under_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            traces = root / ".hooky/runs/local/attempts/001/traces"
            traces.mkdir(parents=True)
            runtime = ToolRuntime(
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

    def test_time_extension_is_granted_only_near_deadline_with_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ToolRuntime(
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

    def test_time_extension_requires_recent_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ToolRuntime(
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
            runtime = ToolRuntime(
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
            runtime = ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=300,
                max_post_success_grace_seconds=90,
            )

            result = runtime.grant_post_success_grace({"ok": True, "passed": True})

            self.assertIsNone(result)
            self.assertEqual(runtime.max_seconds, 300)


if __name__ == "__main__":
    unittest.main()
