from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from hooky import cli, roles


class RolesCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / ".hooky").mkdir(parents=True)
        os.environ.pop("HOOKY_RUN_KEY", None)
        os.environ.pop("HOOKY_RUN_DIR", None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_loop_start_attempt_requires_accepted_contract(self) -> None:
        CliRunner().invoke(cli.app, ["-C", str(self.workspace), "init"])

        result = CliRunner().invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("contract is not accepted", result.output)

    def test_loop_contract_negotiation_updates_contract_progress_and_log(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])

        proposal = runner.invoke(
            cli.app,
            ["-C", str(self.workspace), "proposal", "--proposal", "Build a browser todo app."],
        )
        contract = runner.invoke(
            cli.app,
            ["-C", str(self.workspace), "propose-contract", "--body", "- Add todos\n- Persist todos"],
        )
        review = runner.invoke(
            cli.app,
            ["-C", str(self.workspace), "review-contract", "--status", "rejected", "--body", "Missing route criteria."],
        )

        self.assertEqual(proposal.exit_code, 0, proposal.output)
        self.assertEqual(contract.exit_code, 0, contract.output)
        self.assertEqual(review.exit_code, 0, review.output)
        contract = (self.workspace / ".hooky/runs/local/contract.md").read_text(encoding="utf-8")
        proposal_artifact = (self.workspace / ".hooky/runs/local/proposal.md").read_text(encoding="utf-8")
        self.assertIn("Build a browser todo app.", contract)
        self.assertIn("Build a browser todo app.", proposal_artifact)
        self.assertIn("- Add todos", contract)
        run_dir = self.workspace / ".hooky/runs/local"
        leftover_tmp_files = [entry.name for entry in run_dir.iterdir() if entry.name.endswith(".tmp")]
        self.assertEqual(leftover_tmp_files, [], "atomic writes for proposal/contract must not leave temp files behind")
        log = (self.workspace / ".hooky/runs/local/log.md").read_text(encoding="utf-8")
        self.assertIn("Missing route criteria.", log)
        state = cli.read_loop_state(self.workspace)
        self.assertFalse(state["contract_accepted"])
        self.assertEqual(state["status"], "contract-rejected")

        blocked = runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])
        self.assertNotEqual(blocked.exit_code, 0)

        accepted = runner.invoke(
            cli.app,
            ["-C", str(self.workspace), "review-contract", "--status", "accepted", "--body", "Criteria are testable."],
        )

        self.assertEqual(accepted.exit_code, 0, accepted.output)
        state = cli.read_loop_state(self.workspace)
        self.assertTrue(state["contract_accepted"])
        self.assertEqual(state["status"], "contract-accepted")

    def test_loop_planner_command_runs_role_and_updates_state(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        contract_path = self.workspace / ".hooky/runs/local/contract.md"

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(working_folder.resolve(), self.workspace.resolve())
            self.assertIn("Build a calendar", proposal)
            contract_path.write_text("# Loop Contract\n\n## Proposal\n\nBuild a calendar.\n", encoding="utf-8")
            return {"status": "done", "contract_path": ".hooky/runs/local/contract.md", "summary": "Proposal written."}, {"cost": 0.01}

        with mock.patch.object(roles, "generate_planner_artifacts", side_effect=fake_planner):
            result = runner.invoke(
                cli.app,
                ["-C", str(self.workspace), "planner", "--proposal", "Build a calendar"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Proposal written.", result.output)
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "proposal-written")
        self.assertFalse(state["contract_accepted"])
        self.assertEqual(state["role_usage"]["planner"]["cost"], 0.01)
        self.assertIn("Build a calendar", contract_path.read_text(encoding="utf-8"))

    def test_loop_generator_contract_command_runs_role_and_updates_state(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        contract_path = self.workspace / ".hooky/runs/local/contract.md"
        feature_path = self.workspace / ".hooky/runs/local/feature_list.json"

        def fake_generator(*, working_folder: Path, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(working_folder.resolve(), self.workspace.resolve())
            contract_path.write_text("# Loop Contract\n\n## Done Criteria\n\n- Add events\n", encoding="utf-8")
            feature_path.write_text(
                json.dumps({"schema_version": 1, "features": [{"id": "F001", "text": "Add events", "status": "pending"}]}) + "\n",
                encoding="utf-8",
            )
            return {
                "status": "done",
                "contract_path": ".hooky/runs/local/contract.md",
                "feature_list_path": ".hooky/runs/local/feature_list.json",
                "summary": "Criteria proposed.",
            }, {"cost": 0.02}

        with mock.patch.object(roles, "generate_generator_contract_artifacts", side_effect=fake_generator):
            result = runner.invoke(cli.app, ["-C", str(self.workspace), "generator-contract"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Criteria proposed.", result.output)
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "contract-proposed")
        self.assertFalse(state["contract_accepted"])
        self.assertEqual(state["role_usage"]["generator_contract"]["cost"], 0.02)
        features = cli.read_json(feature_path)
        self.assertEqual(features["features"][0]["id"], "F001")

    def test_loop_evaluator_contract_command_runs_role_and_updates_state(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])

        def fake_evaluator(*, working_folder: Path, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(working_folder.resolve(), self.workspace.resolve())
            self.assertIsNone(attempt_id)
            return {
                "status": "done",
                "accepted": False,
                "review": "Criteria are not testable enough.",
                "required_changes": ["Add persistence criteria."],
            }, {"cost": 0.03}

        with mock.patch.object(roles, "generate_evaluator_contract_artifacts", side_effect=fake_evaluator):
            result = runner.invoke(cli.app, ["-C", str(self.workspace), "evaluator-contract"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("contract_review: rejected", result.output)
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "contract-rejected")
        self.assertFalse(state["contract_accepted"])
        self.assertEqual(state["role_usage"]["evaluator_contract"]["cost"], 0.03)
        progress = (self.workspace / ".hooky/runs/local/progress.md").read_text(encoding="utf-8")
        self.assertIn("Add persistence criteria.", progress)

    def test_loop_attempt_lifecycle_creates_trace_and_otel_artifacts(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])

        result = runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])

        self.assertEqual(result.exit_code, 0, result.output)
        attempt_dir = self.workspace / ".hooky/runs/local/attempts/001"
        self.assertTrue((attempt_dir / "traces/planner.jsonl").exists())
        self.assertTrue((attempt_dir / "traces/generator.jsonl").exists())
        self.assertTrue((attempt_dir / "traces/evaluator.jsonl").exists())
        self.assertTrue((attempt_dir / "otel/spans.jsonl").exists())
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["current_attempt"], "001")
        self.assertEqual(state["status"], "attempt-running")

        complete = runner.invoke(
            cli.app,
            ["-C", str(self.workspace), "complete-attempt", "--result", "fail", "--bottleneck", "generator_trajectory"],
        )

        self.assertEqual(complete.exit_code, 0, complete.output)
        state = cli.read_loop_state(self.workspace)
        self.assertIsNone(state["current_attempt"])
        self.assertEqual(state["status"], "attempt-failed")
        self.assertEqual(state["bottleneck"], "generator_trajectory")
        progress = (self.workspace / ".hooky/runs/local/progress.md").read_text(encoding="utf-8")
        self.assertIn("generator_trajectory", progress)

    def test_loop_generator_implement_command_runs_role_for_active_attempt(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])

        def fake_generator(*, working_folder: Path, attempt_id: str) -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(working_folder.resolve(), self.workspace.resolve())
            self.assertEqual(attempt_id, "001")
            return {
                "status": "done",
                "summary": "Implemented the first pass.",
                "changed_files": ["src/app.py"],
                "tests_run": ["pytest"],
                "failures": [],
            }, {"cost": 0.04}

        with mock.patch.object(roles, "generate_generator_implementation_artifacts", side_effect=fake_generator):
            result = runner.invoke(cli.app, ["-C", str(self.workspace), "generator-implement"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Implemented the first pass.", result.output)
        report = cli.read_json(self.workspace / ".hooky/runs/local/attempts/001/generator_report.json")
        self.assertEqual(report["changed_files"], ["src/app.py"])
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["role_usage"]["generator_implementation"]["cost"], 0.04)

    def test_loop_evaluator_attempt_command_applies_recommendation(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])

        def fake_evaluator(*, working_folder: Path, attempt_id: str) -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(working_folder.resolve(), self.workspace.resolve())
            self.assertEqual(attempt_id, "001")
            return {
                "status": "fail",
                "recommendation": "restart-attempt",
                "bottleneck": "implementation drift",
                "findings": ["The UI does not satisfy the contract."],
                "score": 0.25,
            }, {"cost": 0.05}

        with mock.patch.object(roles, "generate_evaluator_attempt_artifacts", side_effect=fake_evaluator):
            result = runner.invoke(cli.app, ["-C", str(self.workspace), "evaluator-attempt"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("recommendation: restart-attempt", result.output)
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-attempt")
        self.assertIsNone(state["current_attempt"])
        self.assertEqual(state["bottleneck"], "implementation drift")
        self.assertEqual(state["attempts"][0]["status"], "restarted")
        self.assertEqual(state["role_usage"]["evaluator_attempt"]["cost"], 0.05)

    def test_loop_restart_attempt_preserves_durable_files(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])

        result = runner.invoke(
            cli.app,
            ["-C", str(self.workspace), "restart-attempt", "--reason", "patching without convergence"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        for relative in ["feature_list.json", "progress.md", "contract.md", "log.md"]:
            self.assertTrue((cli.loop_dir(self.workspace) / relative).exists())
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-attempt")
        self.assertIsNone(state["current_attempt"])
        self.assertEqual(state["attempts"][0]["status"], "restarted")
        log = (self.workspace / ".hooky/runs/local/log.md").read_text(encoding="utf-8")
        self.assertIn("patching without convergence", log)

    def test_loop_evaluator_report_records_bottleneck_and_report_artifact(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])

        result = runner.invoke(
            cli.app,
            [
                "-C",
                str(self.workspace),
                "evaluator-report",
                "--status",
                "fail",
                "--recommendation",
                "restart-attempt",
                "--bottleneck",
                "generator_trajectory",
                "--finding",
                "implementation is patching around the contract",
                "--score",
                "0.42",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        report = cli.read_json(self.workspace / ".hooky/runs/local/attempts/001/evaluator_report.json")
        self.assertEqual(report["recommendation"], "restart-attempt")
        self.assertEqual(report["score"], 0.42)
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-attempt")
        self.assertIsNone(state["current_attempt"])
        self.assertEqual(state["bottleneck"], "generator_trajectory")
        self.assertEqual(state["attempts"][0]["status"], "restarted")

    def test_loop_evaluator_pass_is_downgraded_for_placeholder_only_tests(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])
        tests_dir = self.workspace / "tests"
        tests_dir.mkdir()
        (tests_dir / "dummy.test.js").write_text(
            "import { test, expect } from '@playwright/test';\ntest('dummy test', async () => { expect(true).toBe(true); });\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            cli.app,
            [
                "-C",
                str(self.workspace),
                "evaluator-report",
                "--status",
                "pass",
                "--recommendation",
                "continue",
                "--score",
                "1",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        report = cli.read_json(self.workspace / ".hooky/runs/local/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["recommendation"], "continue")
        self.assertIn("placeholder tests", " ".join(report["findings"]))
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "attempt-failed")

    def test_loop_evaluator_pass_is_downgraded_for_ui_without_visual_snapshot(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        (self.workspace / ".hooky/runs/local/contract.md").write_text(
            "# Loop Contract\n\n## Done Criteria\n\n- Browser UI layout is visually correct.\n",
            encoding="utf-8",
        )
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])
        (self.workspace / "package.json").write_text('{"dependencies":{"react":"latest","vite":"latest"}}\n', encoding="utf-8")
        tests_dir = self.workspace / "tests"
        tests_dir.mkdir()
        (tests_dir / "app.spec.js").write_text(
            "import { test, expect } from '@playwright/test';\ntest('renders app title', async ({ page }) => { await page.goto('/'); await expect(page).toHaveTitle(/App/); });\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            cli.app,
            [
                "-C",
                str(self.workspace),
                "evaluator-report",
                "--status",
                "pass",
                "--recommendation",
                "continue",
                "--score",
                "1",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        report = cli.read_json(self.workspace / ".hooky/runs/local/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertIn("capture_visual_snapshot", " ".join(report["findings"]))

    def test_loop_evaluator_pass_is_downgraded_for_reference_ui_with_only_empty_snapshot(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        (self.workspace / ".hooky/runs/local/proposal.md").write_text(
            "Build a TodoMVC app that visually matches the canonical template using todomvc-app-css.\n",
            encoding="utf-8",
        )
        (self.workspace / ".hooky/runs/local/contract.md").write_text(
            "# Loop Contract\n\n"
            "## Done Criteria\n\n"
            "- UI matches the canonical TodoMVC template using official CSS.\n\n"
            "## Taste Rubric\n\n"
            "- design weight 0.35: official TodoMVC CSS layout and spacing match the reference.\n"
            "- originality weight 0.10: restraint and fidelity to the canonical template, not novelty.\n"
            "- craft weight 0.25: canonical DOM/classes allow official CSS to apply across states.\n"
            "- functionality weight 0.30: empty, populated, completed/filter, and editing states remain usable.\n",
            encoding="utf-8",
        )
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])
        traces = self.workspace / ".hooky/runs/local/attempts/001/traces"
        traces.mkdir(parents=True, exist_ok=True)
        (traces / "tool_events.json").write_text(
            json.dumps(
                [
                    {
                        "name": "capture_visual_snapshot",
                        "result": {
                            "ok": True,
                            "screenshot_path": ".hooky/runs/local/tool-results/visual-snapshots/empty.png",
                            "metrics": {},
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            cli.app,
            [
                "-C",
                str(self.workspace),
                "evaluator-report",
                "--status",
                "pass",
                "--recommendation",
                "continue",
                "--score",
                "1",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        report = cli.read_json(self.workspace / ".hooky/runs/local/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertIn("multiple states", " ".join(report["findings"]))

    def test_loop_evaluator_pass_is_downgraded_for_failed_latest_test_evidence(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])
        traces = self.workspace / ".hooky/runs/local/attempts/001/traces"
        traces.mkdir(parents=True, exist_ok=True)
        (traces / "tool_events.json").write_text(
            json.dumps(
                [
                    {
                        "name": "run_tests",
                        "result": {
                            "ok": False,
                            "command": "npm test",
                            "timed_out": True,
                            "output_tail": "Timed out waiting for web server",
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            cli.app,
            [
                "-C",
                str(self.workspace),
                "evaluator-report",
                "--status",
                "pass",
                "--recommendation",
                "continue",
                "--score",
                "1",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        report = cli.read_json(self.workspace / ".hooky/runs/local/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["bottleneck"], "verification_tests_failed")
        self.assertIn("latest executable test evidence failed", " ".join(report["findings"]))

    def test_loop_evaluator_pass_allows_later_successful_test_evidence(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])
        traces = self.workspace / ".hooky/runs/local/attempts/001/traces"
        traces.mkdir(parents=True, exist_ok=True)
        (traces / "tool_events.json").write_text(
            json.dumps(
                [
                    {"name": "run_tests", "result": {"ok": False, "command": "npm test", "timed_out": True}},
                    {"name": "run_tests", "result": {"ok": True, "command": "npm test", "returncode": 0}},
                    {
                        "name": "capture_visual_snapshot",
                        "result": {
                            "ok": True,
                            "screenshot_path": ".hooky/runs/local/tool-results/visual-snapshots/shot.png",
                            "metrics": {},
                        },
                    },
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            cli.app,
            [
                "-C",
                str(self.workspace),
                "evaluator-report",
                "--status",
                "pass",
                "--recommendation",
                "continue",
                "--bottleneck",
                "none_visible_after_trace_review",
                "--score",
                "1",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        report = cli.read_json(self.workspace / ".hooky/runs/local/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "pass")

    def test_loop_evaluator_pass_restarts_contract_when_subjective_rubric_is_missing(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        (self.workspace / ".hooky/runs/local/contract.md").write_text(
            "# Loop Contract\n\n## Done Criteria\n\n- Build a polished branded dashboard with excellent craft.\n\n## Taste Rubric\n\n_Optional. Required only when subjective quality matters._\n",
            encoding="utf-8",
        )
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])

        result = runner.invoke(
            cli.app,
            [
                "-C",
                str(self.workspace),
                "evaluator-report",
                "--status",
                "pass",
                "--recommendation",
                "continue",
                "--score",
                "0.9",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        report = cli.read_json(self.workspace / ".hooky/runs/local/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["recommendation"], "restart-contract")
        self.assertEqual(report["bottleneck"], "missing_taste_rubric")
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-contract")
        self.assertFalse(state["contract_accepted"])

    def test_loop_evaluator_pass_is_downgraded_for_reported_clipped_primary_ui(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        (self.workspace / ".hooky/runs/local/contract.md").write_text(
            "# Loop Contract\n\n## Done Criteria\n\n- Browser UI layout is visually correct.\n",
            encoding="utf-8",
        )
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])
        traces = self.workspace / ".hooky/runs/local/attempts/001/traces"
        traces.mkdir(parents=True, exist_ok=True)
        (traces / "tool_events.json").write_text(
            json.dumps(
                [
                    {
                        "name": "capture_visual_snapshot",
                        "result": {
                            "ok": True,
                            "screenshot_path": ".hooky/runs/local/tool-results/visual-snapshots/shot.png",
                            "metrics": {},
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            cli.app,
            [
                "-C",
                str(self.workspace),
                "evaluator-report",
                "--finding",
                "Visual snapshot shows the app UI but with clipped 'todos' heading at the top.",
                "--status",
                "pass",
                "--recommendation",
                "continue",
                "--score",
                "0.88",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        report = cli.read_json(self.workspace / ".hooky/runs/local/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertIn("clipped/off-screen/overflowing primary UI", " ".join(report["findings"]))
        self.assertLessEqual(report["score"], 0.5)

    def test_loop_evaluator_pass_is_downgraded_for_blocking_visual_snapshot_metrics(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        (self.workspace / ".hooky/runs/local/contract.md").write_text(
            "# Loop Contract\n\n## Done Criteria\n\n- Browser UI layout is visually correct.\n",
            encoding="utf-8",
        )
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])
        traces = self.workspace / ".hooky/runs/local/attempts/001/traces"
        traces.mkdir(parents=True, exist_ok=True)
        (traces / "tool_events.json").write_text(
            json.dumps(
                [
                    {
                        "name": "capture_visual_snapshot",
                        "result": {
                            "ok": True,
                            "screenshot_path": ".hooky/runs/local/tool-results/visual-snapshots/shot.png",
                            "metrics": {
                                "sampleClippedElements": [{"tag": "h1", "text": "todos"}],
                            },
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            cli.app,
            [
                "-C",
                str(self.workspace),
                "evaluator-report",
                "--status",
                "pass",
                "--recommendation",
                "continue",
                "--score",
                "1",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        report = cli.read_json(self.workspace / ".hooky/runs/local/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertIn("clipped visible text or controls", " ".join(report["findings"]))

    def test_loop_evaluator_report_restart_contract_reopens_contract(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])

        result = runner.invoke(
            cli.app,
            [
                "-C",
                str(self.workspace),
                "evaluator-report",
                "--status",
                "fail",
                "--recommendation",
                "restart-contract",
                "--bottleneck",
                "weak_contract",
                "--finding",
                "done criteria omit persistence",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-contract")
        self.assertFalse(state["contract_accepted"])
        self.assertEqual(state["bottleneck"], "weak_contract")

    def test_loop_trace_and_otel_events_append_supporting_artifacts(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(cli.app, ["-C", str(self.workspace), "start-attempt"])

        trace = runner.invoke(
            cli.app,
            [
                "-C",
                str(self.workspace),
                "trace-event",
                "--role",
                "generator",
                "--kind",
                "decision",
                "--content",
                "will implement from accepted contract",
            ],
        )
        otel = runner.invoke(
            cli.app,
            [
                "-C",
                str(self.workspace),
                "otel-event",
                "--name",
                "attempt_started",
                "--role",
                "generator",
                "--decision",
                "continue",
                "--cost",
                "0.12",
                "--tokens",
                "1234",
            ],
        )

        self.assertEqual(trace.exit_code, 0, trace.output)
        self.assertEqual(otel.exit_code, 0, otel.output)
        trace_line = (self.workspace / ".hooky/runs/local/attempts/001/traces/generator.jsonl").read_text(encoding="utf-8").strip()
        trace_payload = json.loads(trace_line)
        self.assertEqual(trace_payload["role"], "generator")
        self.assertEqual(trace_payload["kind"], "decision")
        self.assertIn("accepted contract", trace_payload["content"])
        otel_line = (self.workspace / ".hooky/runs/local/attempts/001/otel/spans.jsonl").read_text(encoding="utf-8").strip()
        otel_payload = json.loads(otel_line)
        self.assertEqual(otel_payload["name"], "attempt_started")
        self.assertEqual(otel_payload["attributes"]["tokens"], 1234)
        self.assertEqual(otel_payload["attributes"]["decision"], "continue")


if __name__ == "__main__":
    unittest.main()
