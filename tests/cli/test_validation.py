from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hooky.cli import validation
from hooky.cli.paths import loop_contract_path, loop_feature_list_path


class VisualSnapshotCountTests(unittest.TestCase):
    def test_counts_capture_visual_snapshot_and_interact_and_snapshot_events(self) -> None:
        events = [
            {"name": "capture_visual_snapshot", "result": {"ok": True, "screenshot_path": "a.png"}},
            {"name": "interact_and_snapshot", "result": {"ok": True, "screenshot_path": "b.png"}},
            {"name": "run_tests", "result": {"ok": True}},
        ]

        self.assertEqual(validation.loop_attempt_visual_snapshot_count(events), 2)
        self.assertTrue(validation.loop_attempt_captured_visual_snapshot(events))

    def test_ignores_failed_or_missing_screenshot_events(self) -> None:
        events = [
            {"name": "interact_and_snapshot", "result": {"ok": False, "screenshot_path": "b.png"}},
            {"name": "capture_visual_snapshot", "result": {"ok": True}},
        ]

        self.assertEqual(validation.loop_attempt_visual_snapshot_count(events), 0)
        self.assertFalse(validation.loop_attempt_captured_visual_snapshot(events))

    def test_blocking_visual_failures_are_detected_from_interact_and_snapshot_events(self) -> None:
        events = [
            {
                "name": "interact_and_snapshot",
                "result": {
                    "ok": True,
                    "metrics": {
                        "horizontalOverflow": True,
                    },
                },
            }
        ]

        failures = validation.blocking_visual_failures_from_snapshot_events(events)

        self.assertTrue(any("horizontal overflow" in failure for failure in failures))


class BlockingVisualFailuresFromReportTests(unittest.TestCase):
    def test_does_not_flag_a_negated_visual_term_in_one_finding_combined_with_an_unrelated_finding(self) -> None:
        # Regression: a real evaluator report had one finding saying "...with no clipping or
        # overlap." (a confirmation, containing the word "overlap" but no primary-content word)
        # and an unrelated finding mentioning "link"/"content" for a different reason. Joining
        # all findings into one string before scanning let those combine into a false positive.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            loop_contract_path(workspace).parent.mkdir(parents=True, exist_ok=True)
            loop_contract_path(workspace).write_text("Build a browser UI.", encoding="utf-8")
            loop_feature_list_path(workspace).write_text("{}", encoding="utf-8")
            report = {
                "findings": [
                    "Visual snapshot of empty state shows canonical markup with no clipping or overlap.",
                    "Interaction snapshot confirms the Active filter link updates content correctly.",
                ],
                "bottleneck": "none_visible_after_trace_review",
            }

            failures = validation.loop_attempt_blocking_visual_failures(workspace, [], report)

            self.assertEqual(failures, [])

    def test_still_flags_a_genuine_defect_reported_in_a_single_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            loop_contract_path(workspace).parent.mkdir(parents=True, exist_ok=True)
            loop_contract_path(workspace).write_text("Build a browser UI.", encoding="utf-8")
            loop_feature_list_path(workspace).write_text("{}", encoding="utf-8")
            report = {
                "findings": ["The primary heading overlaps the input control."],
                "bottleneck": "",
            }

            failures = validation.loop_attempt_blocking_visual_failures(workspace, [], report)

            self.assertTrue(any("clipped/off-screen/overflowing" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main()
