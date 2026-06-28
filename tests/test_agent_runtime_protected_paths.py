from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

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


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value)


if __name__ == "__main__":
    unittest.main()
