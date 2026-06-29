from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from typer.testing import CliRunner

from scripts import hooky_cli


def eval_role_boundary_findings() -> list[dict[str, str]]:
    return [
        {"role": "spec", "status": "kept", "evidence": "Spec stayed in planning artifacts."},
        {"role": "builder", "status": "kept", "evidence": "Builder changed implementation artifacts only."},
        {"role": "verifier", "status": "kept", "evidence": "Verifier inspected artifacts without edits."},
        {"role": "eval", "status": "kept", "evidence": "Eval reported findings only."},
    ]


class HookyProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / ".workflow/agents").mkdir(parents=True)
        self.state = {
            "schema_version": 1,
            "task_id": "issue-1001-progress",
            "title": "Progress",
            "issue_number": 1001,
            "created_at": hooky_cli.utc_now(),
            "updated_at": hooky_cli.utc_now(),
            "approvals": {},
            "artifacts": {},
            "issue": {"number": 1001, "title": "Progress", "body": "body", "user": {"login": "local"}},
        }
        hooky_cli.save_task_state(self.workspace, self.state)
        hooky_cli.set_current_task(self.workspace, self.state["task_id"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_running_stage_records_owner_and_trace(self) -> None:
        state = hooky_cli.load_task_state(self.workspace)

        hooky_cli.set_stage_status(self.workspace, state, "test", "running")

        saved = hooky_cli.load_task_state(self.workspace)
        stage = saved["stage_status"]["test"]
        self.assertEqual(stage["status"], "running")
        self.assertEqual(stage["owner_pid"], os.getpid())
        self.assertTrue(stage["trace"].endswith(".workflow/artifacts/test-agent/runtime_events.log"))
        self.assertEqual(saved["phase_status"]["act"]["status"], "running")
        self.assertEqual(saved["phase_status"]["act"]["stage"], "test")

        pipeline_log = hooky_cli.pipeline_log_path(self.workspace).read_text(encoding="utf-8")
        self.assertIn("stage=test", pipeline_log)
        self.assertIn("phase=act", pipeline_log)
        self.assertIn(f"owner_pid={os.getpid()}", pipeline_log)
        self.assertIn("trace=.workflow/artifacts/test-agent/runtime_events.log", pipeline_log)

    def test_init_creates_git_baseline_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "package.json").write_text('{"scripts":{}}\n', encoding="utf-8")

            result = CliRunner().invoke(hooky_cli.app, ["-C", str(workspace), "init"])

            self.assertEqual(result.exit_code, 0, result.output)
            head = hooky_cli.git_command(workspace, ["rev-parse", "--verify", "HEAD"], check=False)
            show = hooky_cli.git_command(workspace, ["show", "HEAD:package.json"], check=False)
            status = hooky_cli.git_command(workspace, ["status", "--short"], check=False)
            self.assertEqual(head.returncode, 0)
            self.assertEqual(show.stdout, '{"scripts":{}}\n')
            self.assertEqual(status.stdout, "")

    def test_status_marks_running_stage_interrupted_when_owner_pid_is_gone(self) -> None:
        state = hooky_cli.load_task_state(self.workspace)
        state["stage_status"] = {
            "builder": {
                "status": "running",
                "updated_at": hooky_cli.utc_now(),
                "started_at": hooky_cli.utc_now(),
                "owner_pid": 999_999_999,
                "owner_host": hooky_cli.socket.gethostname(),
                "trace": ".workflow/artifacts/builder-agent/runtime_events.log",
            }
        }
        hooky_cli.save_task_state(self.workspace, state)

        result = CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "status"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("builder: interrupted", result.output)
        self.assertIn("run owner process 999999999 is no longer running", result.output)
        self.assertIn("trace: .workflow/artifacts/builder-agent/runtime_events.log", result.output)

        saved = hooky_cli.load_task_state(self.workspace)
        self.assertEqual(saved["stage_status"]["builder"]["status"], "interrupted")

    def test_status_marks_legacy_running_stage_interrupted_when_live_log_is_stale(self) -> None:
        state = hooky_cli.load_task_state(self.workspace)
        state["stage_status"] = {
            "spec": {
                "status": "running",
                "updated_at": hooky_cli.utc_now(),
            }
        }
        runtime_dir = self.workspace / ".workflow/artifacts/specs/_runtime"
        runtime_dir.mkdir(parents=True)
        log_path = runtime_dir / "runtime_events.log"
        log_path.write_text("2026-06-28T09:50:56+00:00 run start model=test\n", encoding="utf-8")
        old_mtime = 1_000_000
        os.utime(log_path, (old_mtime, old_mtime))
        hooky_cli.save_task_state(self.workspace, state)

        with mock.patch.dict(os.environ, {"HOOKY_RUNNING_STALE_SECONDS": "1"}):
            result = CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "status"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("spec: interrupted", result.output)
        self.assertIn("no owner process and no live output", result.output)
        self.assertIn("trace: .workflow/artifacts/specs/_runtime/runtime_events.log", result.output)

        saved = hooky_cli.load_task_state(self.workspace)
        self.assertEqual(saved["stage_status"]["spec"]["status"], "interrupted")

    def test_status_reports_partial_pipeline_when_only_some_stages_passed(self) -> None:
        state = hooky_cli.load_task_state(self.workspace)
        state["stage_status"] = {
            "spec": {
                "status": "passed",
                "updated_at": hooky_cli.utc_now(),
            }
        }
        hooky_cli.save_task_state(self.workspace, state)

        result = CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "status"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("pipeline: partial", result.output)
        self.assertIn("spec: passed", result.output)
        self.assertNotIn("pipeline: passed", result.output)

    def test_pipeline_complete_requires_all_stages_passed(self) -> None:
        state = hooky_cli.load_task_state(self.workspace)
        state["stage_status"] = {"spec": {"status": "passed"}}

        self.assertFalse(hooky_cli.pipeline_complete(state))

        state["stage_status"] = {
            stage: {"status": "passed"}
            for stage in hooky_cli.PIPELINE_STAGES
        }
        self.assertTrue(hooky_cli.pipeline_complete(state))

    def test_status_shows_workspace_last_event_and_next_action(self) -> None:
        hooky_cli.append_pipeline_event(self.workspace, "pipeline", status="running", task=self.state["task_id"])
        state = hooky_cli.load_task_state(self.workspace)
        state["stage_status"] = {"builder": {"status": "running", "phase": "act"}}
        state["phase_status"] = {"act": {"status": "running", "stage": "builder", "agent": "builder"}}
        hooky_cli.save_task_state(self.workspace, state)

        result = CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "status"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"workspace: {self.workspace.resolve()}", result.output)
        self.assertIn("last_event:", result.output)
        self.assertIn("next:", result.output)
        self.assertIn("phases:", result.output)
        self.assertIn("  act: running stage=builder", result.output)

    def test_start_records_last_run_path_before_running_pipeline(self) -> None:
        last_run_path = self.workspace / "last-run-path"

        def stop_after_recording(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("stop")

        with mock.patch.object(hooky_cli, "run_pipeline", side_effect=stop_after_recording):
            result = CliRunner().invoke(
                hooky_cli.app,
                ["-C", str(self.workspace), "start", "--last-run-path", str(last_run_path)],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(last_run_path.read_text(encoding="utf-8").strip(), self.workspace.resolve().as_posix())

    def test_start_skill_option_sets_active_skills_for_run(self) -> None:
        seen: dict[str, str | None] = {}
        last_run_path = self.workspace / "last-run-path"

        def stop_after_recording(*_args: object, **_kwargs: object) -> None:
            seen["skills"] = os.environ.get("HOOKY_ACTIVE_SKILLS")
            raise RuntimeError("stop")

        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(hooky_cli, "run_pipeline", side_effect=stop_after_recording),
        ):
            result = CliRunner().invoke(
                hooky_cli.app,
                ["-C", str(self.workspace), "start", "--skill", "visual-ui-review", "--last-run-path", str(last_run_path)],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(seen["skills"], "visual-ui-review")
        self.assertEqual(last_run_path.read_text(encoding="utf-8").strip(), self.workspace.resolve().as_posix())

    def test_skills_list_shows_workspace_skill(self) -> None:
        skill_path = self.workspace / ".agents/skills/example/SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text(
            "---\nname: example\ndescription: Workspace skill.\n---\n\n# Example\n",
            encoding="utf-8",
        )

        result = CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "skills", "list"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("example - Workspace skill.", result.output)

    def test_watch_uses_last_run_workspace(self) -> None:
        last_run_path = self.workspace / "last-run-path"
        hooky_cli.write_last_run_workspace(last_run_path, self.workspace)

        result = CliRunner().invoke(
            hooky_cli.app,
            ["watch", "--last-run-path", str(last_run_path), "--tail-path"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(str(hooky_cli.pipeline_log_path(self.workspace)), result.output)

    def test_status_uses_last_run_workspace_when_current_directory_has_no_task(self) -> None:
        last_run_path = self.workspace / "last-run-path"
        hooky_cli.write_last_run_workspace(last_run_path, self.workspace)

        with tempfile.TemporaryDirectory() as other:
            (Path(other) / ".workflow/agents").mkdir(parents=True)
            result = CliRunner().invoke(
                hooky_cli.app,
                ["-C", other, "status", "--last-run-path", str(last_run_path)],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"workspace: {self.workspace.resolve()}", result.output)
        self.assertIn("task: issue-1001-progress", result.output)

    def test_eval_failure_creates_remediation_plan(self) -> None:
        state = hooky_cli.load_task_state(self.workspace)
        eval_contract_path = self.workspace / ".workflow/artifacts/eval-agent/contract.json"
        eval_contract_path.parent.mkdir(parents=True)
        eval_contract_path.write_text("{}", encoding="utf-8")

        plan_path = hooky_cli.create_remediation_plan(
            self.workspace,
            state,
            {
                "status": "fail",
                "root_cause_stage": "test",
                "safe_to_merge": False,
                "findings": ["Generated tests are invalid."],
                "role_boundary_findings": eval_role_boundary_findings(),
                "trajectory_findings": ["Builder spent time diagnosing generated tests."],
                "artifact_findings": [],
                "tooling_findings": [],
                "human_review_focus": ["Review regenerated tests."],
            },
            eval_contract_path,
        )

        self.assertIsNotNone(plan_path)
        current = hooky_cli.read_json(hooky_cli.current_remediation_path(self.workspace))
        self.assertEqual(current["root_cause_stage"], "builder")
        self.assertEqual(current["root_cause_phase"], "act")
        self.assertEqual(current["resume_from_stage"], "builder")
        self.assertEqual(current["resume_from_phase"], "act")
        self.assertIn("run remediation --auto-approve", current["rerun_command"])

        saved = hooky_cli.load_task_state(self.workspace)
        self.assertEqual(saved["artifacts"]["remediation"]["root_cause_stage"], "builder")

    def test_remediation_resume_recovers_prior_artifacts_and_approvals(self) -> None:
        state = hooky_cli.load_task_state(self.workspace)
        task_id = state["task_id"]
        spec_contract = self.workspace / "docs/specs" / task_id / "contract.json"
        spec_contract.parent.mkdir(parents=True)
        spec_contract.write_text("{}", encoding="utf-8")
        state["artifacts"] = {}
        state["approvals"] = {}
        hooky_cli.save_task_state(self.workspace, state)

        hooky_cli.recover_artifacts_for_resume(self.workspace, state, "builder", auto_approve=True)

        saved = hooky_cli.load_task_state(self.workspace)
        self.assertEqual(saved["artifacts"]["spec"]["contract"], f"docs/specs/{task_id}/contract.json")
        self.assertEqual(saved["stage_status"]["spec"]["status"], "passed")
        self.assertIn("spec", saved["approvals"])

    def test_stage_sequence_runs_eval_after_builder_failure(self) -> None:
        calls: list[str] = []

        def fail_builder(ctx: object, task: str | None = None) -> None:
            calls.append("builder")
            raise RuntimeError("server timeout")

        def run_eval(ctx: object, task: str | None = None) -> None:
            calls.append("eval")

        with (
            mock.patch.object(hooky_cli, "run_spec", side_effect=lambda ctx, task=None: calls.append("spec")),
            mock.patch.object(hooky_cli, "approve_stage", side_effect=lambda ctx, stage, message, task=None: calls.append(f"approve-{stage}")),
            mock.patch.object(hooky_cli, "run_builder", side_effect=fail_builder),
            mock.patch.object(hooky_cli, "run_verifier", side_effect=lambda ctx, task=None: calls.append("verifier")),
            mock.patch.object(hooky_cli, "run_eval", side_effect=run_eval),
        ):
            failures = hooky_cli.run_stage_sequence(mock.Mock(), start_stage="spec", auto_approve=True, task=None)

        self.assertEqual(failures, ["builder failed: server timeout"])
        self.assertEqual(calls, ["spec", "approve-spec", "builder", "eval"])

    def test_verifier_failed_contract_marks_stage_failed(self) -> None:
        state = hooky_cli.load_task_state(self.workspace)
        builder_contract = self.workspace / ".workflow/artifacts/builder-agent/contract.json"
        builder_contract.parent.mkdir(parents=True)
        builder_contract.write_text("{}", encoding="utf-8")
        state.setdefault("artifacts", {})["builder"] = {
            "report_dir": ".workflow/artifacts/builder-agent",
            "contract": ".workflow/artifacts/builder-agent/contract.json",
        }
        hooky_cli.save_task_state(self.workspace, state)
        report_dir = Path(".workflow/artifacts/verifier-agent")
        contract = {
            "status": "fail",
            "summary": "Visual check failed.",
            "checks_run": [],
            "scope_violations": [],
            "test_integrity_findings": [],
            "acceptance_coverage_findings": [],
            "visual_findings": ["primary content is clipped"],
            "security_findings": [],
            "required_actions": ["Fix clipped primary content."],
            "safe_to_open_pr": False,
        }

        with mock.patch.object(
            hooky_cli.verifier_agent,
            "generate_verification_artifacts",
            return_value=(report_dir, contract, {"cost": 0.01}),
        ):
            result = CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "run", "verifier"])

        self.assertNotEqual(result.exit_code, 0)
        saved = hooky_cli.load_task_state(self.workspace)
        self.assertEqual(saved["stage_status"]["verifier"]["status"], "failed")
        self.assertIn("Visual check failed.", saved["stage_status"]["verifier"]["error"])
        self.assertEqual(saved["phase_status"]["verify"]["status"], "failed")

    def test_pipeline_failure_preserves_stage_state_written_during_run(self) -> None:
        def fail_after_stage_update(ctx: object, start_stage: str, auto_approve: bool, task: str | None) -> list[str]:
            state = hooky_cli.load_task_state(self.workspace, task)
            state.setdefault("artifacts", {})["spec"] = {
                "artifact_dir": "docs/specs/issue-1001-progress",
                "contract": "docs/specs/issue-1001-progress/contract.json",
            }
            hooky_cli.set_stage_status(
                self.workspace,
                state,
                "spec",
                "passed",
                contract="docs/specs/issue-1001-progress/contract.json",
            )
            return ["builder failed: server timeout"]

        with mock.patch.object(hooky_cli, "run_stage_sequence", side_effect=fail_after_stage_update):
            result = CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "run", "pipeline", "--auto-approve"])

        self.assertNotEqual(result.exit_code, 0)
        saved = hooky_cli.load_task_state(self.workspace)
        self.assertEqual(saved["pipeline_status"]["status"], "failed")
        self.assertEqual(saved["stage_status"]["spec"]["status"], "passed")
        self.assertEqual(saved["artifacts"]["spec"]["contract"], "docs/specs/issue-1001-progress/contract.json")

    def test_pipeline_auto_runs_remediation_plan_until_passed(self) -> None:
        calls: list[str] = []
        remediation_calls: list[tuple[str | None, bool]] = []

        def stage_sequence(ctx: object, start_stage: str, auto_approve: bool, task: str | None) -> list[str]:
            calls.append(start_stage)
            state = hooky_cli.load_task_state(self.workspace, task)
            state["stage_status"] = {stage: {"status": "passed"} for stage in hooky_cli.PIPELINE_STAGES}
            state["stage_status"]["eval"] = {"status": "fail"}
            eval_contract_path = self.workspace / ".workflow/artifacts/eval-agent/contract.json"
            eval_contract_path.parent.mkdir(parents=True)
            eval_contract_path.write_text("{}", encoding="utf-8")
            hooky_cli.create_remediation_plan(
                self.workspace,
                state,
                {
                    "status": "fail",
                    "safe_to_merge": False,
                    "root_cause_stage": "builder",
                    "findings": ["retry builder"],
                    "role_boundary_findings": eval_role_boundary_findings(),
                },
                eval_contract_path,
            )
            return []

        def remediation_subprocess(*, workspace: Path, task: str | None, auto_approve: bool) -> subprocess.CompletedProcess[str]:
            remediation_calls.append((task, auto_approve))
            state = hooky_cli.load_task_state(workspace, task)
            state["stage_status"] = {stage: {"status": "passed"} for stage in hooky_cli.PIPELINE_STAGES}
            hooky_cli.save_task_state(self.workspace, state)
            return subprocess.CompletedProcess(["hooky", "run", "remediation"], 0)

        with (
            mock.patch.object(hooky_cli, "run_stage_sequence", side_effect=stage_sequence),
            mock.patch.object(hooky_cli, "run_remediation_subprocess", side_effect=remediation_subprocess),
        ):
            result = CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "run", "pipeline", "--auto-approve"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(calls, ["spec"])
        self.assertEqual(remediation_calls, [(None, True)])
        saved = hooky_cli.load_task_state(self.workspace)
        self.assertEqual(saved["pipeline_status"]["status"], "passed")
        self.assertEqual(saved["pipeline_status"]["remediation_attempts"], 1)

    def test_pipeline_auto_remediation_stops_at_limit(self) -> None:
        remediation_calls = 0

        def stage_sequence(ctx: object, start_stage: str, auto_approve: bool, task: str | None) -> list[str]:
            state = hooky_cli.load_task_state(self.workspace, task)
            state["stage_status"] = {stage: {"status": "passed"} for stage in hooky_cli.PIPELINE_STAGES}
            state["stage_status"]["eval"] = {"status": "fail"}
            eval_contract_path = self.workspace / ".workflow/artifacts/eval-agent/contract.json"
            eval_contract_path.parent.mkdir(parents=True, exist_ok=True)
            eval_contract_path.write_text("{}", encoding="utf-8")
            hooky_cli.create_remediation_plan(
                self.workspace,
                state,
                {
                    "status": "fail",
                    "safe_to_merge": False,
                    "root_cause_stage": "builder",
                    "findings": ["retry builder"],
                    "role_boundary_findings": eval_role_boundary_findings(),
                },
                eval_contract_path,
            )
            return []

        def remediation_subprocess(*, workspace: Path, task: str | None, auto_approve: bool) -> subprocess.CompletedProcess[str]:
            nonlocal remediation_calls
            remediation_calls += 1
            return subprocess.CompletedProcess(["hooky", "run", "remediation"], 1)

        with (
            mock.patch.object(hooky_cli, "run_stage_sequence", side_effect=stage_sequence),
            mock.patch.object(hooky_cli, "run_remediation_subprocess", side_effect=remediation_subprocess),
        ):
            result = CliRunner().invoke(
                hooky_cli.app,
                ["-C", str(self.workspace), "run", "pipeline", "--auto-approve", "--max-remediations", "1"],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(remediation_calls, 1)
        saved = hooky_cli.load_task_state(self.workspace)
        self.assertEqual(saved["pipeline_status"]["status"], "failed")
        self.assertIn("pipeline remediation limit reached (1)", saved["pipeline_status"]["error"])
        self.assertEqual(saved["pipeline_status"]["remediation_attempts"], 1)

    def test_loop_init_creates_four_durable_state_files(self) -> None:
        result = CliRunner().invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "init", "--title", "Build a todo app"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((self.workspace / ".workflow/loop/feature_list.json").exists())
        self.assertTrue((self.workspace / ".workflow/loop/progress.md").exists())
        self.assertTrue((self.workspace / ".workflow/loop/contract.md").exists())
        self.assertTrue((self.workspace / ".workflow/loop/log.md").exists())
        contract = (self.workspace / ".workflow/loop/contract.md").read_text(encoding="utf-8")
        self.assertIn("Build a todo app", contract)
        feature_list = hooky_cli.read_json(self.workspace / ".workflow/loop/feature_list.json")
        self.assertEqual(feature_list["features"], [])

    def test_loop_start_attempt_requires_accepted_contract(self) -> None:
        CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])

        result = CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "start-attempt"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("contract is not accepted", result.output)

    def test_loop_contract_negotiation_updates_contract_progress_and_log(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])

        boundary = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "boundary", "--body", "Build a browser todo app."],
        )
        proposal = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "propose-contract", "--body", "- Add todos\n- Persist todos"],
        )
        review = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "review-contract", "--status", "rejected", "--body", "Missing route criteria."],
        )

        self.assertEqual(boundary.exit_code, 0, boundary.output)
        self.assertEqual(proposal.exit_code, 0, proposal.output)
        self.assertEqual(review.exit_code, 0, review.output)
        contract = (self.workspace / ".workflow/loop/contract.md").read_text(encoding="utf-8")
        self.assertIn("Build a browser todo app.", contract)
        self.assertIn("- Add todos", contract)
        log = (self.workspace / ".workflow/loop/log.md").read_text(encoding="utf-8")
        self.assertIn("Missing route criteria.", log)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertFalse(state["contract_accepted"])
        self.assertEqual(state["status"], "contract-rejected")

        blocked = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "start-attempt"])
        self.assertNotEqual(blocked.exit_code, 0)

        accepted = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "review-contract", "--status", "accepted", "--body", "Criteria are testable."],
        )

        self.assertEqual(accepted.exit_code, 0, accepted.output)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertTrue(state["contract_accepted"])
        self.assertEqual(state["status"], "contract-accepted")

    def test_loop_attempt_lifecycle_creates_trace_and_otel_artifacts(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "accept-contract"])

        result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "start-attempt"])

        self.assertEqual(result.exit_code, 0, result.output)
        attempt_dir = self.workspace / ".workflow/loop/attempts/001"
        self.assertTrue((attempt_dir / "traces/planner.jsonl").exists())
        self.assertTrue((attempt_dir / "traces/generator.jsonl").exists())
        self.assertTrue((attempt_dir / "traces/evaluator.jsonl").exists())
        self.assertTrue((attempt_dir / "otel/spans.jsonl").exists())
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["current_attempt"], "001")
        self.assertEqual(state["status"], "attempt-running")

        complete = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "complete-attempt", "--result", "fail", "--bottleneck", "generator_trajectory"],
        )

        self.assertEqual(complete.exit_code, 0, complete.output)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertIsNone(state["current_attempt"])
        self.assertEqual(state["status"], "attempt-failed")
        self.assertEqual(state["bottleneck"], "generator_trajectory")
        progress = (self.workspace / ".workflow/loop/progress.md").read_text(encoding="utf-8")
        self.assertIn("generator_trajectory", progress)

    def test_loop_restart_attempt_preserves_durable_files(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "start-attempt"])

        result = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "restart-attempt", "--reason", "patching without convergence"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        for relative in ["feature_list.json", "progress.md", "contract.md", "log.md"]:
            self.assertTrue((self.workspace / ".workflow/loop" / relative).exists())
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-attempt")
        self.assertIsNone(state["current_attempt"])
        self.assertEqual(state["attempts"][0]["status"], "restarted")
        log = (self.workspace / ".workflow/loop/log.md").read_text(encoding="utf-8")
        self.assertIn("patching without convergence", log)


if __name__ == "__main__":
    unittest.main()
