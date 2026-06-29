from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import eval_agent  # noqa: E402


class EvalAgentForensicsTests(unittest.TestCase):
    def test_builder_test_failure_facts_extract_failed_test_runs(self) -> None:
        facts = eval_agent.builder_test_failure_facts(
            [
                {
                    "name": "bash",
                    "arguments": {"command": "npx playwright test 2>&1"},
                    "result": {
                        "ok": False,
                        "returncode": 1,
                        "stdout": "1) tests/todo-routing.spec.js:19:3 › TodoMVC route filtering › AC16: Routes are supported",
                    },
                }
            ]
        )

        self.assertEqual(facts["failed_runs"], 1)
        self.assertIn("tests/todo-routing.spec.js", facts["failing_tests"][0])

    def test_eval_cannot_claim_all_acceptance_criteria_when_builder_tests_failed(self) -> None:
        contract = minimal_eval_contract(
            findings=["Implementation covers all 19 acceptance criteria."],
            trajectory_findings=["Builder timed out."],
        )
        dynamic_context = context_with_builder_test_failures()

        with self.assertRaisesRegex(ValueError, "failed test runs"):
            eval_agent.validate_contract_for_context(contract, dynamic_context)

    def test_eval_must_mention_failed_test_evidence_when_builder_tests_failed(self) -> None:
        contract = minimal_eval_contract(
            findings=["Builder timed out after writing implementation files."],
            trajectory_findings=["Builder made several bash calls."],
        )
        dynamic_context = context_with_builder_test_failures()

        with self.assertRaisesRegex(ValueError, "omitted deterministic failed approved-test evidence"):
            eval_agent.validate_contract_for_context(contract, dynamic_context)

    def test_eval_accepts_failed_test_evidence_when_named(self) -> None:
        contract = minimal_eval_contract(
            findings=["Approved Playwright tests failed in tests/todo-routing.spec.js."],
            trajectory_findings=["Builder timed out before final_report."],
        )
        dynamic_context = context_with_builder_test_failures()

        eval_agent.validate_contract_for_context(contract, dynamic_context)

    def test_visual_evidence_images_discovers_verifier_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            screenshot = root / ".workflow/tool-results/visual-snapshots/shot.png"
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(b"png")
            events = root / ".workflow/artifacts/verifier-agent/tool_events.json"
            events.parent.mkdir(parents=True)
            events.write_text(
                json.dumps(
                    [
                        {
                            "name": "capture_visual_snapshot",
                            "result": {
                                "screenshot_path": ".workflow/tool-results/visual-snapshots/shot.png",
                                "url": "http://127.0.0.1:4173",
                                "metrics": {"clippedElementCount": 1},
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )

            evidence = eval_agent.visual_evidence_images(root)

            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0]["path"], ".workflow/tool-results/visual-snapshots/shot.png")
            self.assertEqual(evidence[0]["source"], "verifier_capture_visual_snapshot")

    def test_eval_cannot_pass_browser_ui_without_visual_evidence(self) -> None:
        contract = passing_eval_contract()
        dynamic_context = context_with_passing_verifier_without_visual_evidence()

        with self.assertRaisesRegex(ValueError, "visual image evidence"):
            eval_agent.validate_contract_for_context(contract, dynamic_context)

    def test_eval_accepts_browser_ui_with_visual_evidence(self) -> None:
        contract = passing_eval_contract()
        dynamic_context = context_with_passing_verifier_without_visual_evidence()
        dynamic_context["visual_evidence"] = [{"path": ".workflow/tool-results/visual-snapshots/shot.png"}]

        eval_agent.validate_contract_for_context(contract, dynamic_context)

    def test_eval_cannot_pass_with_low_average_score(self) -> None:
        contract = passing_eval_contract()
        contract["scores"] = {
            "spec_alignment": 8,
            "maintainability": 8,
            "architecture_fit": 8,
            "risk_awareness": 8,
            "trajectory_quality": 7,
            "pr_summary_quality": 7,
        }

        with self.assertRaisesRegex(ValueError, "average score below 8"):
            eval_agent.validate_contract(contract)

    def test_eval_cannot_pass_with_score_below_six(self) -> None:
        contract = passing_eval_contract()
        contract["scores"] = {
            "spec_alignment": 10,
            "maintainability": 10,
            "architecture_fit": 10,
            "risk_awareness": 10,
            "trajectory_quality": 10,
            "pr_summary_quality": 5,
        }

        with self.assertRaisesRegex(ValueError, "score below 6"):
            eval_agent.validate_contract(contract)


def minimal_eval_contract(**overrides: object) -> dict[str, object]:
    contract: dict[str, object] = {
        "status": "fail",
        "scores": {
            "spec_alignment": 5,
            "maintainability": 5,
            "architecture_fit": 5,
            "risk_awareness": 5,
            "trajectory_quality": 1,
            "pr_summary_quality": 1,
        },
        "findings": ["Approved tests failed."],
        "root_cause_stage": "builder",
        "trajectory_findings": ["Builder failed."],
        "artifact_findings": ["Builder wrote implementation files."],
        "tooling_findings": ["Builder did not call final_report."],
        "cost_findings": ["Builder spent cost before timing out."],
        "human_review_focus": ["Inspect builder failures."],
        "safe_to_merge": False,
    }
    contract.update(overrides)
    return contract


def passing_eval_contract() -> dict[str, object]:
    return minimal_eval_contract(
        status="pass",
        scores={
            "spec_alignment": 8,
            "maintainability": 8,
            "architecture_fit": 8,
            "risk_awareness": 8,
            "trajectory_quality": 8,
            "pr_summary_quality": 8,
        },
        findings=["Verifier passed and quality bar is met."],
        trajectory_findings=["Agent pathway was clean."],
        artifact_findings=["Artifacts are coherent."],
        tooling_findings=["Tooling evidence is sufficient."],
        cost_findings=["Cost is within budget."],
        human_review_focus=[],
        safe_to_merge=True,
    )


def context_with_builder_test_failures() -> dict[str, object]:
    return {
        "verifier_reports": {},
        "pipeline_state": {"task_state": {"stage_status": {"verifier": {"status": "not-run"}}}},
        "deterministic_facts": {
            "stages": {
                "builder": {
                    "todo_calls": 1,
                    "files_written_count": 2,
                    "workspace_file_count": 5,
                    "test_failures": {
                        "failed_runs": 1,
                        "failing_tests": ["tests/todo-routing.spec.js:19:3 › TodoMVC route filtering › AC16"],
                    },
                }
            },
            "approvals": {},
        },
    }


def context_with_passing_verifier_without_visual_evidence() -> dict[str, object]:
    return {
        "package_json": {"dependencies": {"react": "^19.0.0"}, "devDependencies": {"@playwright/test": "^1.0.0"}},
        "verifier_reports": {"contract": {"status": "pass"}},
        "pipeline_state": {"task_state": {"stage_status": {"verifier": {"status": "passed"}}}},
        "visual_evidence": [],
        "deterministic_facts": {
            "stages": {
                "builder": {
                    "todo_calls": 1,
                    "files_written_count": 2,
                    "workspace_file_count": 5,
                    "test_failures": {"failed_runs": 0, "failing_tests": []},
                }
            },
            "approvals": {},
        },
    }


if __name__ == "__main__":
    unittest.main()
