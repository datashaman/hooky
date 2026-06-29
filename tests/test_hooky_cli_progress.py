from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from typer.testing import CliRunner

from scripts import hooky_cli


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

        pipeline_log = hooky_cli.pipeline_log_path(self.workspace).read_text(encoding="utf-8")
        self.assertIn("stage=test", pipeline_log)
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
                "trajectory_findings": ["Builder spent time diagnosing prior-stage output."],
                "artifact_findings": [],
                "tooling_findings": [],
                "human_review_focus": ["Review regenerated tests."],
            },
            eval_contract_path,
        )

        self.assertIsNotNone(plan_path)
        current = hooky_cli.read_json(hooky_cli.current_remediation_path(self.workspace))
        self.assertEqual(current["root_cause_stage"], "test")
        self.assertEqual(current["resume_from_stage"], "test")
        self.assertIn("run remediation --auto-approve", current["rerun_command"])

        saved = hooky_cli.load_task_state(self.workspace)
        self.assertEqual(saved["artifacts"]["remediation"]["root_cause_stage"], "test")

    def test_remediation_resume_recovers_prior_artifacts_and_approvals(self) -> None:
        state = hooky_cli.load_task_state(self.workspace)
        task_id = state["task_id"]
        spec_contract = self.workspace / "docs/specs" / task_id / "contract.json"
        spec_contract.parent.mkdir(parents=True)
        spec_contract.write_text("{}", encoding="utf-8")
        test_contract = self.workspace / ".workflow/artifacts/test-agent" / task_id / "contract.json"
        test_contract.parent.mkdir(parents=True)
        test_contract.write_text('{"test_files": [{"path": "tests/example.spec"}], "fixtures": []}', encoding="utf-8")
        state["artifacts"] = {}
        state["approvals"] = {}
        hooky_cli.save_task_state(self.workspace, state)

        hooky_cli.recover_artifacts_for_resume(self.workspace, state, "builder", auto_approve=True)

        saved = hooky_cli.load_task_state(self.workspace)
        self.assertEqual(saved["artifacts"]["spec"]["contract"], f"docs/specs/{task_id}/contract.json")
        self.assertEqual(saved["artifacts"]["test"]["contract"], f".workflow/artifacts/test-agent/{task_id}/contract.json")
        self.assertEqual(saved["stage_status"]["spec"]["status"], "passed")
        self.assertEqual(saved["stage_status"]["test"]["status"], "passed")
        self.assertIn("spec", saved["approvals"])
        self.assertIn("test", saved["approvals"])

    def test_builder_test_contract_finding_creates_test_remediation(self) -> None:
        state = hooky_cli.load_task_state(self.workspace)
        builder_contract_path = self.workspace / ".workflow/artifacts/builder-agent/contract.json"
        builder_contract_path.parent.mkdir(parents=True)
        builder_contract_path.write_text("{}", encoding="utf-8")

        plan_path = hooky_cli.maybe_create_test_remediation_from_builder_failure(
            self.workspace,
            state,
            {
                "tests_passing": False,
                "failures_remaining": ["approved tests fail before implementation is exercised"],
                "test_contract_findings": ["test helper accesses runtime before setup"],
            },
            builder_contract_path,
        )

        self.assertIsNotNone(plan_path)
        current = hooky_cli.read_json(hooky_cli.current_remediation_path(self.workspace))
        self.assertEqual(current["root_cause_stage"], "test")
        self.assertEqual(current["resume_from_stage"], "test")
        self.assertEqual(current["source"], "builder-test-contract-finding")
        self.assertIn("test helper accesses runtime before setup", current["findings"])

    def test_eval_does_not_overwrite_builder_test_contract_remediation(self) -> None:
        state = hooky_cli.load_task_state(self.workspace)
        builder_contract_path = self.workspace / ".workflow/artifacts/builder-agent/contract.json"
        builder_contract_path.parent.mkdir(parents=True)
        builder_contract_path.write_text("{}", encoding="utf-8")
        hooky_cli.create_remediation_plan(
            self.workspace,
            state,
            {
                "status": "fail",
                "safe_to_merge": False,
                "root_cause_stage": "test",
                "findings": ["Approved test contract is invalid."],
                "trajectory_findings": [],
                "artifact_findings": [],
                "tooling_findings": [],
                "human_review_focus": [],
            },
            builder_contract_path,
            root_cause_stage="test",
            source="builder-test-contract-finding",
        )
        eval_contract_path = self.workspace / ".workflow/artifacts/eval-agent/contract.json"
        eval_contract_path.parent.mkdir(parents=True)
        eval_contract_path.write_text("{}", encoding="utf-8")

        plan_path = hooky_cli.maybe_create_remediation_plan(
            self.workspace,
            state,
            {
                "status": "fail",
                "safe_to_merge": False,
                "root_cause_stage": "builder",
                "findings": ["Builder failed because tests are still failing."],
            },
            eval_contract_path,
        )

        self.assertEqual(plan_path, hooky_cli.current_remediation_path(self.workspace))
        current = hooky_cli.read_json(hooky_cli.current_remediation_path(self.workspace))
        self.assertEqual(current["root_cause_stage"], "test")
        self.assertEqual(current["source"], "builder-test-contract-finding")
        pipeline_log = hooky_cli.pipeline_log_path(self.workspace).read_text(encoding="utf-8")
        self.assertIn("status=preserved", pipeline_log)
        self.assertIn("ignored_eval_root_cause=builder", pipeline_log)

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
            mock.patch.object(hooky_cli, "run_test", side_effect=lambda ctx, task=None: calls.append("test")),
            mock.patch.object(hooky_cli, "run_builder", side_effect=fail_builder),
            mock.patch.object(hooky_cli, "run_verifier", side_effect=lambda ctx, task=None: calls.append("verifier")),
            mock.patch.object(hooky_cli, "run_eval", side_effect=run_eval),
        ):
            failures = hooky_cli.run_stage_sequence(mock.Mock(), start_stage="spec", auto_approve=True, task=None)

        self.assertEqual(failures, ["builder failed: server timeout"])
        self.assertEqual(calls, ["spec", "approve-spec", "test", "approve-test", "builder", "eval"])

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
                {"status": "fail", "safe_to_merge": False, "root_cause_stage": "builder", "findings": ["retry builder"]},
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
                {"status": "fail", "safe_to_merge": False, "root_cause_stage": "builder", "findings": ["retry builder"]},
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


if __name__ == "__main__":
    unittest.main()
