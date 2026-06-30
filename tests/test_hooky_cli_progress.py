from __future__ import annotations

import json
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

    def test_model_role_retry_logs_and_retries_agent_run_error(self) -> None:
        calls = 0

        def flaky_call() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise hooky_cli.agent_runtime.AgentRunError(
                    "agent model request exceeded 120s",
                    hooky_cli.agent_runtime.AgentRunResult(
                        final_report=None,
                        usage={},
                        transcript=[],
                        tool_events=[],
                        compaction_events=[],
                        pre_compaction_archives=[],
                        started_at="2026-06-30T00:00:00+00:00",
                        ended_at="2026-06-30T00:00:01+00:00",
                    ),
                )
            return "ok"

        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])

        with mock.patch.dict(os.environ, {"LOOP_MODEL_ROLE_RETRIES": "1"}):
            result = hooky_cli.run_model_role_with_retries(self.workspace, "test-role", flaky_call)

        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)
        log = (self.workspace / ".workflow/loop/log.md").read_text(encoding="utf-8")
        self.assertIn("retry test-role", log)

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

    def test_loop_init_creates_durable_state_files(self) -> None:
        result = CliRunner().invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "init", "--title", "Build a todo app"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((self.workspace / ".workflow/loop/feature_list.json").exists())
        self.assertTrue((self.workspace / ".workflow/loop/progress.md").exists())
        self.assertTrue((self.workspace / ".workflow/loop/contract.md").exists())
        self.assertTrue((self.workspace / ".workflow/loop/log.md").exists())
        self.assertTrue((self.workspace / ".workflow/loop/proposal.md").exists())
        contract = (self.workspace / ".workflow/loop/contract.md").read_text(encoding="utf-8")
        self.assertIn("Build a todo app", contract)
        proposal = (self.workspace / ".workflow/loop/proposal.md").read_text(encoding="utf-8")
        self.assertIn("Build a todo app", proposal)
        feature_list = hooky_cli.read_json(self.workspace / ".workflow/loop/feature_list.json")
        self.assertEqual(feature_list["features"], [])
        log = (self.workspace / ".workflow/loop/log.md").read_text(encoding="utf-8")
        self.assertRegex(log, r"(?m)^## \d{4}-\d{2}-\d{2}$")
        self.assertRegex(log, r"(?m)^- \d{2}:\d{2}:\d{2}Z init \| loop initialized$")
        self.assertRegex(log, r"(?m)^  - workspace: ")

    def test_loop_init_reads_proposal_from_stdin_and_derives_title(self) -> None:
        result = CliRunner().invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "init"],
            input="Build a todo app\n\nUsers can add and complete todos.\n",
        )

        self.assertEqual(result.exit_code, 0, result.output)
        contract = (self.workspace / ".workflow/loop/contract.md").read_text(encoding="utf-8")
        self.assertIn("# Build a todo app", contract)
        self.assertIn("Users can add and complete todos.", contract)

    def test_loop_init_derives_title_from_proposal_file_when_title_missing(self) -> None:
        body_file = self.workspace / "proposal.md"
        body_file.write_text("Build a calendar\n\nUsers can add events.\n", encoding="utf-8")

        result = CliRunner().invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "init", "--proposal-file", str(body_file)],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        contract = (self.workspace / ".workflow/loop/contract.md").read_text(encoding="utf-8")
        self.assertIn("# Build a calendar", contract)
        self.assertIn("Users can add events.", contract)

    def test_loop_start_attempt_requires_accepted_contract(self) -> None:
        CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])

        result = CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "start-attempt"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("contract is not accepted", result.output)

    def test_loop_contract_negotiation_updates_contract_progress_and_log(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])

        proposal = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "proposal", "--proposal", "Build a browser todo app."],
        )
        contract = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "propose-contract", "--body", "- Add todos\n- Persist todos"],
        )
        review = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "review-contract", "--status", "rejected", "--body", "Missing route criteria."],
        )

        self.assertEqual(proposal.exit_code, 0, proposal.output)
        self.assertEqual(contract.exit_code, 0, contract.output)
        self.assertEqual(review.exit_code, 0, review.output)
        contract = (self.workspace / ".workflow/loop/contract.md").read_text(encoding="utf-8")
        proposal_artifact = (self.workspace / ".workflow/loop/proposal.md").read_text(encoding="utf-8")
        self.assertIn("Build a browser todo app.", contract)
        self.assertIn("Build a browser todo app.", proposal_artifact)
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

    def test_loop_planner_command_runs_role_and_updates_state(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])
        contract_path = self.workspace / ".workflow/loop/contract.md"

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(working_folder.resolve(), self.workspace.resolve())
            self.assertIn("Build a calendar", proposal)
            contract_path.write_text("# Loop Contract\n\n## Proposal\n\nBuild a calendar.\n", encoding="utf-8")
            return {"status": "done", "contract_path": ".workflow/loop/contract.md", "summary": "Proposal written."}, {"cost": 0.01}

        with mock.patch.object(hooky_cli.loop_agent, "generate_planner_artifacts", side_effect=fake_planner):
            result = runner.invoke(
                hooky_cli.app,
                ["-C", str(self.workspace), "loop", "planner", "--proposal", "Build a calendar"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Proposal written.", result.output)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "proposal-written")
        self.assertFalse(state["contract_accepted"])
        self.assertEqual(state["role_usage"]["planner"]["cost"], 0.01)
        self.assertIn("Build a calendar", contract_path.read_text(encoding="utf-8"))

    def test_loop_generator_contract_command_runs_role_and_updates_state(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])
        contract_path = self.workspace / ".workflow/loop/contract.md"
        feature_path = self.workspace / ".workflow/loop/feature_list.json"

        def fake_generator(*, working_folder: Path, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(working_folder.resolve(), self.workspace.resolve())
            contract_path.write_text("# Loop Contract\n\n## Done Criteria\n\n- Add events\n", encoding="utf-8")
            feature_path.write_text(
                json.dumps({"schema_version": 1, "features": [{"id": "F001", "text": "Add events", "status": "pending"}]}) + "\n",
                encoding="utf-8",
            )
            return {
                "status": "done",
                "contract_path": ".workflow/loop/contract.md",
                "feature_list_path": ".workflow/loop/feature_list.json",
                "summary": "Criteria proposed.",
            }, {"cost": 0.02}

        with mock.patch.object(hooky_cli.loop_agent, "generate_generator_contract_artifacts", side_effect=fake_generator):
            result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "generator-contract"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Criteria proposed.", result.output)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "contract-proposed")
        self.assertFalse(state["contract_accepted"])
        self.assertEqual(state["role_usage"]["generator_contract"]["cost"], 0.02)
        features = hooky_cli.read_json(feature_path)
        self.assertEqual(features["features"][0]["id"], "F001")

    def test_loop_evaluator_contract_command_runs_role_and_updates_state(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])

        def fake_evaluator(*, working_folder: Path, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(working_folder.resolve(), self.workspace.resolve())
            self.assertIsNone(attempt_id)
            return {
                "status": "done",
                "accepted": False,
                "review": "Criteria are not testable enough.",
                "required_changes": ["Add persistence criteria."],
            }, {"cost": 0.03}

        with mock.patch.object(hooky_cli.loop_agent, "generate_evaluator_contract_artifacts", side_effect=fake_evaluator):
            result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "evaluator-contract"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("contract_review: rejected", result.output)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "contract-rejected")
        self.assertFalse(state["contract_accepted"])
        self.assertEqual(state["role_usage"]["evaluator_contract"]["cost"], 0.03)
        progress = (self.workspace / ".workflow/loop/progress.md").read_text(encoding="utf-8")
        self.assertIn("Add persistence criteria.", progress)

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

    def test_loop_generator_implement_command_runs_role_for_active_attempt(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "start-attempt"])

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

        with mock.patch.object(hooky_cli.loop_agent, "generate_generator_implementation_artifacts", side_effect=fake_generator):
            result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "generator-implement"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Implemented the first pass.", result.output)
        report = hooky_cli.read_json(self.workspace / ".workflow/loop/attempts/001/generator_report.json")
        self.assertEqual(report["changed_files"], ["src/app.py"])
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["role_usage"]["generator_implementation"]["cost"], 0.04)

    def test_loop_evaluator_attempt_command_applies_recommendation(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "start-attempt"])

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

        with mock.patch.object(hooky_cli.loop_agent, "generate_evaluator_attempt_artifacts", side_effect=fake_evaluator):
            result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "evaluator-attempt"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("recommendation: restart-attempt", result.output)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-attempt")
        self.assertIsNone(state["current_attempt"])
        self.assertEqual(state["bottleneck"], "implementation drift")
        self.assertEqual(state["attempts"][0]["status"], "restarted")
        self.assertEqual(state["role_usage"]["evaluator_attempt"]["cost"], 0.05)

    def test_loop_run_models_executes_three_role_suite(self) -> None:
        runner = CliRunner()

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(working_folder.resolve(), self.workspace.resolve())
            self.assertIn("Build todos", proposal)
            (working_folder / ".workflow/loop/contract.md").write_text(
                "# Loop Contract\n\n## Proposal\n\nBuild todos.\n",
                encoding="utf-8",
            )
            return {"status": "done", "contract_path": ".workflow/loop/contract.md", "summary": "Proposal ready."}, {"cost": 0.01}

        contract_feedback: list[str] = []

        def fake_contract(
            *,
            working_folder: Path,
            attempt_id: str | None = None,
            review_feedback: str = "",
        ) -> tuple[dict[str, object], dict[str, object]]:
            contract_feedback.append(review_feedback)
            (working_folder / ".workflow/loop/contract.md").write_text(
                "# Loop Contract\n\n## Done Criteria\n\n- Add todos\n",
                encoding="utf-8",
            )
            (working_folder / ".workflow/loop/feature_list.json").write_text(
                json.dumps({"schema_version": 1, "features": [{"id": "F001", "text": "Add todos", "status": "pending"}]}) + "\n",
                encoding="utf-8",
            )
            return {
                "status": "done",
                "contract_path": ".workflow/loop/contract.md",
                "feature_list_path": ".workflow/loop/feature_list.json",
                "summary": "Contract ready.",
            }, {"cost": 0.02}

        review_calls = 0

        def fake_contract_review(*, working_folder: Path, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            nonlocal review_calls
            review_calls += 1
            if review_calls == 1:
                return {
                    "status": "done",
                    "accepted": False,
                    "review": "Class names are wrong.",
                    "required_changes": ["Use .new-todo exactly."],
                }, {"cost": 0.03}
            return {"status": "done", "accepted": True, "review": "Good enough.", "required_changes": []}, {"cost": 0.03}

        def fake_implementation(*, working_folder: Path, attempt_id: str, evaluator_feedback: str = "") -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(attempt_id, "001")
            return {
                "status": "done",
                "summary": "Built it.",
                "changed_files": ["src/app.py"],
                "tests_run": ["pytest"],
                "failures": [],
            }, {"cost": 0.04}

        def fake_attempt_review(*, working_folder: Path, attempt_id: str) -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(attempt_id, "001")
            return {
                "status": "pass",
                "recommendation": "continue",
                "bottleneck": "",
                "findings": [],
                "score": 1.0,
            }, {"cost": 0.05}

        patches = [
            mock.patch.object(hooky_cli.loop_agent, "generate_planner_artifacts", side_effect=fake_planner),
            mock.patch.object(hooky_cli.loop_agent, "generate_generator_contract_artifacts", side_effect=fake_contract),
            mock.patch.object(hooky_cli.loop_agent, "generate_evaluator_contract_artifacts", side_effect=fake_contract_review),
            mock.patch.object(hooky_cli.loop_agent, "generate_generator_implementation_artifacts", side_effect=fake_implementation),
            mock.patch.object(hooky_cli.loop_agent, "generate_evaluator_attempt_artifacts", side_effect=fake_attempt_review),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = runner.invoke(
                hooky_cli.app,
                ["-C", str(self.workspace), "loop", "run", "--proposal", "Build todos"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("status: passed", result.output)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "passed")
        self.assertEqual(state["attempts"][0]["status"], "passed")
        self.assertEqual(state["role_usage"]["evaluator_attempt"]["cost"], 0.05)
        self.assertEqual(review_calls, 2)
        self.assertEqual(contract_feedback[0], "")
        self.assertIn("Use .new-todo exactly.", contract_feedback[1])
        log = (self.workspace / ".workflow/loop/log.md").read_text(encoding="utf-8")
        self.assertIn("contract rejected round 1", log)
        self.assertIn("contract accepted round 2", log)

    def test_loop_run_allows_five_contract_rounds_by_default(self) -> None:
        runner = CliRunner()
        contract_calls = 0
        review_calls = 0

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".workflow/loop/contract.md").write_text(
                "# Loop Contract\n\n## Proposal\n\nBuild something.\n",
                encoding="utf-8",
            )
            return {"status": "done", "contract_path": ".workflow/loop/contract.md", "summary": "Proposal ready."}, {"cost": 0.01}

        def fake_contract(
            *,
            working_folder: Path,
            attempt_id: str | None = None,
            review_feedback: str = "",
        ) -> tuple[dict[str, object], dict[str, object]]:
            nonlocal contract_calls
            contract_calls += 1
            (working_folder / ".workflow/loop/contract.md").write_text(
                "# Loop Contract\n\n## Done Criteria\n\n- A concrete assertion that is still incomplete.\n",
                encoding="utf-8",
            )
            (working_folder / ".workflow/loop/feature_list.json").write_text(
                json.dumps({"schema_version": 1, "features": [{"id": "F001", "text": "Incomplete", "status": "pending"}]}) + "\n",
                encoding="utf-8",
            )
            return {
                "status": "done",
                "contract_path": ".workflow/loop/contract.md",
                "feature_list_path": ".workflow/loop/feature_list.json",
                "summary": "Contract proposed.",
            }, {"cost": 0.02}

        def fake_contract_review(*, working_folder: Path, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            nonlocal review_calls
            review_calls += 1
            return {
                "status": "done",
                "accepted": False,
                "review": "Still weak.",
                "required_changes": ["Add missing acceptance criteria."],
            }, {"cost": 0.03}

        patches = [
            mock.patch.object(hooky_cli.loop_agent, "generate_planner_artifacts", side_effect=fake_planner),
            mock.patch.object(hooky_cli.loop_agent, "generate_generator_contract_artifacts", side_effect=fake_contract),
            mock.patch.object(hooky_cli.loop_agent, "generate_evaluator_contract_artifacts", side_effect=fake_contract_review),
        ]
        with patches[0], patches[1], patches[2]:
            result = runner.invoke(
                hooky_cli.app,
                ["-C", str(self.workspace), "loop", "run", "--proposal", "Build something"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("status: contract-rejected", result.output)
        self.assertEqual(contract_calls, 5)
        self.assertEqual(review_calls, 5)
        log = (self.workspace / ".workflow/loop/log.md").read_text(encoding="utf-8")
        self.assertIn("contract rejected round 5", log)

    def test_loop_run_retries_attempts_with_evaluator_feedback(self) -> None:
        runner = CliRunner()
        attempts: list[tuple[str, str]] = []

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".workflow/loop/contract.md").write_text(
                "# Loop Contract\n\n## Proposal\n\nBuild todos.\n",
                encoding="utf-8",
            )
            return {"status": "done", "contract_path": ".workflow/loop/contract.md", "summary": "Proposal ready."}, {"cost": 0.01}

        def fake_contract(
            *,
            working_folder: Path,
            attempt_id: str | None = None,
            review_feedback: str = "",
        ) -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".workflow/loop/contract.md").write_text(
                "# Loop Contract\n\n## Done Criteria\n\n- Add todos\n",
                encoding="utf-8",
            )
            (working_folder / ".workflow/loop/feature_list.json").write_text(
                json.dumps({"schema_version": 1, "features": [{"id": "F001", "text": "Add todos", "status": "pending"}]}) + "\n",
                encoding="utf-8",
            )
            return {
                "status": "done",
                "contract_path": ".workflow/loop/contract.md",
                "feature_list_path": ".workflow/loop/feature_list.json",
                "summary": "Contract ready.",
            }, {"cost": 0.02}

        def fake_contract_review(*, working_folder: Path, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            return {"status": "done", "accepted": True, "review": "Accepted.", "required_changes": []}, {"cost": 0.03}

        def fake_implementation(*, working_folder: Path, attempt_id: str, evaluator_feedback: str = "") -> tuple[dict[str, object], dict[str, object]]:
            attempts.append((attempt_id, evaluator_feedback))
            return {
                "status": "done",
                "summary": f"Built {attempt_id}.",
                "changed_files": ["src/app.py"],
                "tests_run": ["pytest"],
                "failures": [],
            }, {"cost": 0.04}

        def fake_attempt_review(*, working_folder: Path, attempt_id: str) -> tuple[dict[str, object], dict[str, object]]:
            if attempt_id == "001":
                return {
                    "status": "fail",
                    "recommendation": "restart-attempt",
                    "bottleneck": "edit flow is broken",
                    "findings": ["T007 fails"],
                    "score": 0.4,
                }, {"cost": 0.05}
            return {
                "status": "pass",
                "recommendation": "continue",
                "bottleneck": "",
                "findings": [],
                "score": 1.0,
            }, {"cost": 0.05}

        patches = [
            mock.patch.object(hooky_cli.loop_agent, "generate_planner_artifacts", side_effect=fake_planner),
            mock.patch.object(hooky_cli.loop_agent, "generate_generator_contract_artifacts", side_effect=fake_contract),
            mock.patch.object(hooky_cli.loop_agent, "generate_evaluator_contract_artifacts", side_effect=fake_contract_review),
            mock.patch.object(hooky_cli.loop_agent, "generate_generator_implementation_artifacts", side_effect=fake_implementation),
            mock.patch.object(hooky_cli.loop_agent, "generate_evaluator_attempt_artifacts", side_effect=fake_attempt_review),
            mock.patch.dict(os.environ, {"LOOP_ATTEMPT_MAX_ROUNDS": "2"}),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = runner.invoke(
                hooky_cli.app,
                ["-C", str(self.workspace), "loop", "run", "--proposal", "Build todos"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("attempt: 002", result.output)
        self.assertIn("status: passed", result.output)
        self.assertEqual(attempts[0], ("001", ""))
        self.assertEqual(attempts[1][0], "002")
        self.assertIn("T007 fails", attempts[1][1])
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual([attempt["status"] for attempt in state["attempts"]], ["restarted", "passed"])
        log = (self.workspace / ".workflow/loop/log.md").read_text(encoding="utf-8")
        self.assertIn("attempt 001 reset", log)

    def test_loop_run_retries_when_evaluator_errors_without_report(self) -> None:
        runner = CliRunner()
        attempts: list[tuple[str, str]] = []

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".workflow/loop/contract.md").write_text("# Loop Contract\n\n## Proposal\n\nBuild todos.\n", encoding="utf-8")
            return {"status": "done", "contract_path": ".workflow/loop/contract.md", "summary": "Proposal ready."}, {"cost": 0.01}

        def fake_contract(*, working_folder: Path, attempt_id: str | None = None, review_feedback: str = "") -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".workflow/loop/contract.md").write_text("# Loop Contract\n\n## Done Criteria\n\n- Add todos\n", encoding="utf-8")
            (working_folder / ".workflow/loop/feature_list.json").write_text(json.dumps({"features": [{"id": "F001", "text": "Add todos"}]}) + "\n", encoding="utf-8")
            return {
                "status": "done",
                "contract_path": ".workflow/loop/contract.md",
                "feature_list_path": ".workflow/loop/feature_list.json",
                "summary": "Contract ready.",
            }, {"cost": 0.02}

        def fake_contract_review(*, working_folder: Path, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            return {"status": "done", "accepted": True, "review": "Accepted.", "required_changes": []}, {"cost": 0.03}

        def fake_implementation(*, working_folder: Path, attempt_id: str, evaluator_feedback: str = "") -> tuple[dict[str, object], dict[str, object]]:
            attempts.append((attempt_id, evaluator_feedback))
            summary = "TodoMVC implementation pending." if attempt_id == "001" else "Built it."
            return {
                "status": "done",
                "summary": summary,
                "changed_files": ["tests/example.test.js"] if attempt_id == "001" else ["src/App.jsx"],
                "tests_run": ["npm test"],
                "failures": [],
            }, {"cost": 0.04}

        def fake_attempt_review(*, working_folder: Path, attempt_id: str) -> tuple[dict[str, object], dict[str, object]]:
            if attempt_id == "001":
                raise hooky_cli.agent_runtime.AgentRunError(
                    "agent produced 6 consecutive assistant messages without tool calls",
                    hooky_cli.agent_runtime.AgentRunResult(
                        final_report=None,
                        usage={"cost": 0.05},
                        transcript=[],
                        tool_events=[],
                        compaction_events=[],
                        pre_compaction_archives=[],
                        started_at="2026-06-30T00:00:00+00:00",
                        ended_at="2026-06-30T00:00:01+00:00",
                    ),
                )
            return {
                "status": "pass",
                "recommendation": "continue",
                "bottleneck": "",
                "findings": [],
                "score": 1.0,
            }, {"cost": 0.05}

        patches = [
            mock.patch.object(hooky_cli.loop_agent, "generate_planner_artifacts", side_effect=fake_planner),
            mock.patch.object(hooky_cli.loop_agent, "generate_generator_contract_artifacts", side_effect=fake_contract),
            mock.patch.object(hooky_cli.loop_agent, "generate_evaluator_contract_artifacts", side_effect=fake_contract_review),
            mock.patch.object(hooky_cli.loop_agent, "generate_generator_implementation_artifacts", side_effect=fake_implementation),
            mock.patch.object(hooky_cli.loop_agent, "generate_evaluator_attempt_artifacts", side_effect=fake_attempt_review),
            mock.patch.dict(os.environ, {"LOOP_ATTEMPT_MAX_ROUNDS": "2"}),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "run", "--proposal", "Build todos"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("attempt: 002", result.output)
        self.assertIn("status: passed", result.output)
        self.assertIn("TodoMVC implementation pending", attempts[1][1])
        report = hooky_cli.read_json(self.workspace / ".workflow/loop/attempts/001/evaluator_report.json")
        self.assertEqual(report["recommendation"], "restart-attempt")
        self.assertIn("Evaluator did not produce", report["findings"][0])

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

    def test_loop_evaluator_report_records_bottleneck_and_report_artifact(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "start-attempt"])

        result = runner.invoke(
            hooky_cli.app,
            [
                "-C",
                str(self.workspace),
                "loop",
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
        report = hooky_cli.read_json(self.workspace / ".workflow/loop/attempts/001/evaluator_report.json")
        self.assertEqual(report["recommendation"], "restart-attempt")
        self.assertEqual(report["score"], 0.42)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-attempt")
        self.assertIsNone(state["current_attempt"])
        self.assertEqual(state["bottleneck"], "generator_trajectory")
        self.assertEqual(state["attempts"][0]["status"], "restarted")

    def test_loop_evaluator_pass_is_downgraded_for_placeholder_only_tests(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "start-attempt"])
        tests_dir = self.workspace / "tests"
        tests_dir.mkdir()
        (tests_dir / "dummy.test.js").write_text(
            "import { test, expect } from '@playwright/test';\n"
            "test('dummy test', async () => { expect(true).toBe(true); });\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            hooky_cli.app,
            [
                "-C",
                str(self.workspace),
                "loop",
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
        report = hooky_cli.read_json(self.workspace / ".workflow/loop/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["recommendation"], "continue")
        self.assertIn("placeholder tests", " ".join(report["findings"]))
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "attempt-failed")

    def test_loop_evaluator_pass_is_downgraded_for_ui_without_visual_snapshot(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])
        (self.workspace / ".workflow/loop/contract.md").write_text(
            "# Loop Contract\n\n## Done Criteria\n\n- Browser UI layout is visually correct.\n",
            encoding="utf-8",
        )
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "start-attempt"])
        (self.workspace / "package.json").write_text('{"dependencies":{"react":"latest","vite":"latest"}}\n', encoding="utf-8")
        tests_dir = self.workspace / "tests"
        tests_dir.mkdir()
        (tests_dir / "app.spec.js").write_text(
            "import { test, expect } from '@playwright/test';\n"
            "test('renders app title', async ({ page }) => { await page.goto('/'); await expect(page).toHaveTitle(/App/); });\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            hooky_cli.app,
            [
                "-C",
                str(self.workspace),
                "loop",
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
        report = hooky_cli.read_json(self.workspace / ".workflow/loop/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertIn("capture_visual_snapshot", " ".join(report["findings"]))

    def test_loop_evaluator_report_restart_contract_reopens_contract(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "start-attempt"])

        result = runner.invoke(
            hooky_cli.app,
            [
                "-C",
                str(self.workspace),
                "loop",
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
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-contract")
        self.assertFalse(state["contract_accepted"])
        self.assertEqual(state["bottleneck"], "weak_contract")

    def test_loop_trace_and_otel_events_append_supporting_artifacts(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "start-attempt"])

        trace = runner.invoke(
            hooky_cli.app,
            [
                "-C",
                str(self.workspace),
                "loop",
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
            hooky_cli.app,
            [
                "-C",
                str(self.workspace),
                "loop",
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
        trace_line = (self.workspace / ".workflow/loop/attempts/001/traces/generator.jsonl").read_text(encoding="utf-8").strip()
        trace_payload = json.loads(trace_line)
        self.assertEqual(trace_payload["role"], "generator")
        self.assertEqual(trace_payload["kind"], "decision")
        self.assertIn("accepted contract", trace_payload["content"])
        otel_line = (self.workspace / ".workflow/loop/attempts/001/otel/spans.jsonl").read_text(encoding="utf-8").strip()
        otel_payload = json.loads(otel_line)
        self.assertEqual(otel_payload["name"], "attempt_started")
        self.assertEqual(otel_payload["attributes"]["tokens"], 1234)
        self.assertEqual(otel_payload["attributes"]["decision"], "continue")

    def test_loop_run_executes_complete_local_suite(self) -> None:
        result = CliRunner().invoke(
            hooky_cli.app,
            [
                "-C",
                str(self.workspace),
                "loop",
                "run",
                "--dry-run",
                "--title",
                "Todo app",
                "--proposal",
                "Build a TodoMVC-style app.",
                "--criteria",
                "- Add todos\n- Persist todos",
                "--status",
                "pass",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("status: passed", result.output)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "passed")
        self.assertIsNone(state["current_attempt"])
        self.assertEqual(state["attempts"][0]["status"], "passed")
        self.assertTrue((self.workspace / ".workflow/loop/attempts/001/evaluator_report.json").exists())
        self.assertTrue((self.workspace / ".workflow/loop/attempts/001/traces/planner.jsonl").exists())
        self.assertTrue((self.workspace / ".workflow/loop/attempts/001/otel/spans.jsonl").exists())
        contract = (self.workspace / ".workflow/loop/contract.md").read_text(encoding="utf-8")
        self.assertIn("Build a TodoMVC-style app.", contract)
        self.assertIn("- Persist todos", contract)

    def test_loop_run_can_record_restart_recommendation(self) -> None:
        result = CliRunner().invoke(
            hooky_cli.app,
            [
                "-C",
                str(self.workspace),
                "loop",
                "run",
                "--dry-run",
                "--criteria",
                "- Build the thing",
                "--status",
                "fail",
                "--recommendation",
                "restart-attempt",
                "--bottleneck",
                "generator_trajectory",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-attempt")
        self.assertEqual(state["attempts"][0]["status"], "restarted")
        self.assertEqual(state["bottleneck"], "generator_trajectory")

    def test_loop_watch_shows_path_and_log_content(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])
        runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "loop", "log", "--op", "note", "--title", "watchable", "--body", "hello"],
        )

        path_result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "watch", "--path"])
        content_result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "watch", "--no-follow"])

        self.assertEqual(path_result.exit_code, 0, path_result.output)
        self.assertIn(".workflow/loop/log.md", path_result.output)
        self.assertEqual(content_result.exit_code, 0, content_result.output)
        self.assertIn("watchable", content_result.output)
        self.assertIn("hello", content_result.output)

    def test_loop_debug_commands_show_runtime_transcript_and_stalls(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "start-attempt"])
        trace_root = self.workspace / ".workflow/loop/attempts/001/traces"
        hooky_cli.write_json(
            trace_root / "runtime_transcript.json",
            {
                "bad": "shape",
            },
        )
        (trace_root / "runtime_transcript.json").write_text(
            json.dumps(
                [
                    {
                        "role": "user",
                        "kind": "initial",
                        "message": "Implement TodoMVC.",
                        "started_at": "2026-06-30T00:00:00+00:00",
                        "ended_at": "2026-06-30T00:00:00+00:00",
                    },
                    {
                        "role": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": "**final_report** {\"status\":\"done\"}",
                        },
                        "started_at": "2026-06-30T00:00:01+00:00",
                        "ended_at": "2026-06-30T00:00:01+00:00",
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (trace_root / "runtime_events.log").write_text("2026-06-30T00:00:01+00:00 assistant tool_calls=0\n", encoding="utf-8")

        transcript = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "transcript", "--attempt", "001"])
        stall = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "stall", "--attempt", "001"])
        runtime_log = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "loop", "runtime-log", "--attempt", "001"])

        self.assertEqual(transcript.exit_code, 0, transcript.output)
        self.assertIn("Implement TodoMVC.", transcript.output)
        self.assertIn("final_report", transcript.output)
        self.assertEqual(stall.exit_code, 0, stall.output)
        self.assertIn("fake_final_report_text_entries: 1", stall.output)
        self.assertEqual(runtime_log.exit_code, 0, runtime_log.output)
        self.assertIn("tool_calls=0", runtime_log.output)

    def test_loop_status_uses_last_run_workspace_when_current_directory_has_no_loop(self) -> None:
        last_run_path = self.workspace / "loop-last-run-path"
        runner = CliRunner()
        run_result = runner.invoke(
            hooky_cli.app,
            [
                "-C",
                str(self.workspace),
                "loop",
                "run",
                "--dry-run",
                "--title",
                "Remember me",
                "--last-run-path",
                str(last_run_path),
            ],
        )
        self.assertEqual(run_result.exit_code, 0, run_result.output)

        with tempfile.TemporaryDirectory() as other:
            status = runner.invoke(
                hooky_cli.app,
                ["-C", other, "loop", "status", "--last-run-path", str(last_run_path)],
            )

        self.assertEqual(status.exit_code, 0, status.output)
        self.assertIn(f"loop: {(self.workspace / '.workflow/loop').resolve()}", status.output)
        self.assertIn("status: passed", status.output)


if __name__ == "__main__":
    unittest.main()
