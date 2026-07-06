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


class TasteRubricScoringGateTests(unittest.TestCase):
    def test_discards_rubric_scores_when_contract_has_no_taste_rubric(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            loop_contract_path(workspace).parent.mkdir(parents=True, exist_ok=True)
            loop_contract_path(workspace).write_text("# Loop Contract\n\n## Done Criteria\n\n- Add a backend endpoint.\n", encoding="utf-8")
            report = {
                "status": "pass",
                "recommendation": "continue",
                "score": 0.9,
                "findings": [],
                "rubric_scores": {"design": 0.9, "originality": 0.8, "craft": 0.9, "functionality": 0.95},
                "score_explanation": "Looks great.",
            }

            amended = validation.enforce_taste_rubric_scoring_gate(workspace, report)

            self.assertNotIn("rubric_scores", amended)
            self.assertNotIn("score_explanation", amended)
            self.assertTrue(any("Discarded rubric_scores" in finding for finding in amended["findings"]))
            # Everything else about the report is untouched -- this gate only strips scoring data.
            self.assertEqual(amended["status"], "pass")
            self.assertEqual(amended["score"], 0.9)

    def test_discards_rubric_scores_when_rubric_lacks_weights_summing_to_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            loop_contract_path(workspace).parent.mkdir(parents=True, exist_ok=True)
            loop_contract_path(workspace).write_text(
                "# Loop Contract\n\n## Done Criteria\n\n- Build a dashboard.\n\n## Taste Rubric\n\nGrade design, craft, and functionality holistically.\n",
                encoding="utf-8",
            )
            report = {
                "status": "fail",
                "recommendation": "continue",
                "score": 0.4,
                "findings": ["Needs polish."],
                "rubric_scores": {"design": 0.5, "originality": 0.3, "craft": 0.4, "functionality": 0.5},
            }

            amended = validation.enforce_taste_rubric_scoring_gate(workspace, report)

            self.assertNotIn("rubric_scores", amended)

    def test_preserves_rubric_scores_when_contract_defines_a_valid_weighted_rubric(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            loop_contract_path(workspace).parent.mkdir(parents=True, exist_ok=True)
            loop_contract_path(workspace).write_text(
                "# Loop Contract\n\n## Done Criteria\n\n- Build a polished dashboard.\n\n## Taste Rubric\n\n"
                "- design weight 0.35: calm, legible hierarchy\n"
                "- originality weight 0.15: not a generic template\n"
                "- craft weight 0.25: aligned spacing and refined states\n"
                "- functionality weight 0.25: workflows remain clear\n",
                encoding="utf-8",
            )
            rubric_scores = {"design": 0.8, "originality": 0.7, "craft": 0.75, "functionality": 0.9}
            report = {
                "status": "pass",
                "recommendation": "continue",
                "score": 0.8,
                "findings": [],
                "rubric_scores": rubric_scores,
                "score_explanation": "Strong functional fit with adequate polish.",
            }

            amended = validation.enforce_taste_rubric_scoring_gate(workspace, report)

            self.assertEqual(amended["rubric_scores"], rubric_scores)
            self.assertEqual(amended["score_explanation"], "Strong functional fit with adequate polish.")

    def test_is_a_noop_when_evaluator_did_not_submit_rubric_scores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            report = {"status": "pass", "recommendation": "continue", "score": 0.9, "findings": []}

            amended = validation.enforce_taste_rubric_scoring_gate(workspace, report)

            self.assertEqual(amended, report)


if __name__ == "__main__":
    unittest.main()
