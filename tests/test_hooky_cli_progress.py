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


class HookyProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / ".hooky").mkdir(parents=True)

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
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])

        with mock.patch.dict(os.environ, {"LOOP_MODEL_ROLE_RETRIES": "1"}):
            result = hooky_cli.run_model_role_with_retries(self.workspace, "test-role", flaky_call)

        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)
        log = (self.workspace / ".hooky/log.md").read_text(encoding="utf-8")
        self.assertIn("retry test-role", log)

    def test_model_role_retry_runs_on_retry_hook_before_retry(self) -> None:
        calls = 0
        retries: list[int] = []

        def flaky_call() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise hooky_cli.agent_runtime.AgentRunError(
                    "agent produced 6 consecutive assistant messages without tool calls",
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
            self.assertEqual(retries, [1])
            return "ok"

        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])

        with mock.patch.dict(os.environ, {"LOOP_MODEL_ROLE_RETRIES": "1"}):
            result = hooky_cli.run_model_role_with_retries(
                self.workspace,
                "generator-implementation attempt 001",
                flaky_call,
                on_retry=lambda retry, _exc: retries.append(retry),
            )

        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)

    def test_reset_loop_attempt_workspace_removes_untracked_files_but_preserves_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "src").mkdir()
            (workspace / "src/App.jsx").write_text("before\n", encoding="utf-8")
            (workspace / ".hooky").mkdir(parents=True)
            (workspace / ".hooky/log.md").write_text("log\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(workspace), "add", "src/App.jsx"], check=True)
            subprocess.run(["git", "-C", str(workspace), "commit", "-q", "-m", "baseline"], check=True)

            (workspace / "src/App.jsx").write_text("after\n", encoding="utf-8")
            (workspace / "tests").mkdir()
            (workspace / "tests/stale.spec.js").write_text("stale\n", encoding="utf-8")

            note = hooky_cli.reset_loop_attempt_workspace(workspace)

            self.assertIn("HEAD is now at", note)
            self.assertEqual((workspace / "src/App.jsx").read_text(encoding="utf-8"), "before\n")
            self.assertFalse((workspace / "tests/stale.spec.js").exists())
            self.assertTrue((workspace / ".hooky/log.md").exists())

    def test_init_creates_git_baseline_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "package.json").write_text('{"scripts":{}}\n', encoding="utf-8")

            result = CliRunner().invoke(hooky_cli.app, ["-C", str(workspace), "init"])

            self.assertEqual(result.exit_code, 0, result.output)
            head = hooky_cli.git_command(workspace, ["rev-parse", "--verify", "HEAD"], check=False)
            show = hooky_cli.git_command(workspace, ["show", "HEAD:package.json"], check=False)
            status = hooky_cli.git_command(workspace, ["status", "--short", "--", ".", ":!.hooky"], check=False)
            self.assertEqual(head.returncode, 0)
            self.assertEqual(show.stdout, '{"scripts":{}}\n')
            self.assertEqual(status.stdout, "")

    def test_start_records_last_run_path_before_running_loop(self) -> None:
        last_run_path = self.workspace / "last-run-path"

        def stop_after_recording(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("stop")

        with mock.patch.object(hooky_cli, "run_model_loop_once", side_effect=stop_after_recording):
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
            mock.patch.object(hooky_cli, "run_model_loop_once", side_effect=stop_after_recording),
        ):
            result = CliRunner().invoke(
                hooky_cli.app,
                ["-C", str(self.workspace), "start", "--skill", "visual-ui-review", "--last-run-path", str(last_run_path)],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(seen["skills"], "visual-ui-review")
        self.assertEqual(last_run_path.read_text(encoding="utf-8").strip(), self.workspace.resolve().as_posix())

    def test_removed_pipeline_commands_are_not_registered(self) -> None:
        help_result = CliRunner().invoke(hooky_cli.app, ["--help"])

        self.assertEqual(help_result.exit_code, 0, help_result.output)
        self.assertNotIn(" task ", help_result.output)
        self.assertNotIn(" approve ", help_result.output)

        loop_result = CliRunner().invoke(hooky_cli.app, ["loop"])
        self.assertNotEqual(loop_result.exit_code, 0)
        pipeline_result = CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "run", "pipeline"])
        self.assertNotEqual(pipeline_result.exit_code, 0)

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
        CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "init", "--last-run-path", str(last_run_path)])
        hooky_cli.write_last_run_workspace(last_run_path, self.workspace)

        result = CliRunner().invoke(
            hooky_cli.app,
            ["watch", "--last-run-path", str(last_run_path), "--path"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(str(hooky_cli.loop_log_path(self.workspace)), result.output)

    def test_status_uses_last_run_workspace_when_current_directory_has_no_loop(self) -> None:
        last_run_path = self.workspace / "last-run-path"
        CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "init", "--last-run-path", str(last_run_path)])

        with tempfile.TemporaryDirectory() as other:
            (Path(other) / ".hooky").mkdir(parents=True)
            result = CliRunner().invoke(
                hooky_cli.app,
                ["-C", other, "status", "--last-run-path", str(last_run_path)],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"loop: {(self.workspace / '.hooky').resolve()}", result.output)

    def test_loop_init_creates_durable_state_files(self) -> None:
        result = CliRunner().invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "init", "--title", "Build a todo app"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((self.workspace / ".hooky/feature_list.json").exists())
        self.assertTrue((self.workspace / ".hooky/progress.md").exists())
        self.assertTrue((self.workspace / ".hooky/contract.md").exists())
        self.assertTrue((self.workspace / ".hooky/log.md").exists())
        self.assertTrue((self.workspace / ".hooky/proposal.md").exists())
        contract = (self.workspace / ".hooky/contract.md").read_text(encoding="utf-8")
        self.assertIn("Build a todo app", contract)
        proposal = (self.workspace / ".hooky/proposal.md").read_text(encoding="utf-8")
        self.assertIn("Build a todo app", proposal)
        feature_list = hooky_cli.read_json(self.workspace / ".hooky/feature_list.json")
        self.assertEqual(feature_list["features"], [])
        log = (self.workspace / ".hooky/log.md").read_text(encoding="utf-8")
        self.assertRegex(log, r"(?m)^## \d{4}-\d{2}-\d{2}$")
        self.assertRegex(log, r"(?m)^- \d{2}:\d{2}:\d{2}Z init \| loop initialized$")
        self.assertRegex(log, r"(?m)^  - workspace: ")

    def test_loop_init_reads_proposal_from_stdin_and_derives_title(self) -> None:
        result = CliRunner().invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "init"],
            input="Build a todo app\n\nUsers can add and complete todos.\n",
        )

        self.assertEqual(result.exit_code, 0, result.output)
        contract = (self.workspace / ".hooky/contract.md").read_text(encoding="utf-8")
        self.assertIn("# Build a todo app", contract)
        self.assertIn("Users can add and complete todos.", contract)

    def test_loop_init_derives_title_from_proposal_file_when_title_missing(self) -> None:
        body_file = self.workspace / "proposal.md"
        body_file.write_text("Build a calendar\n\nUsers can add events.\n", encoding="utf-8")

        result = CliRunner().invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "init", "--proposal-file", str(body_file)],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        contract = (self.workspace / ".hooky/contract.md").read_text(encoding="utf-8")
        self.assertIn("# Build a calendar", contract)
        self.assertIn("Users can add events.", contract)

    def test_loop_run_reads_proposal_file_and_stdin(self) -> None:
        body_file = self.workspace / "proposal.md"
        body_file.write_text("Build a calendar\n\nUsers can add events.\n", encoding="utf-8")

        file_result = CliRunner().invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "run", "--dry-run", "--proposal-file", str(body_file), "--force"],
        )

        self.assertEqual(file_result.exit_code, 0, file_result.output)
        contract = (self.workspace / ".hooky/contract.md").read_text(encoding="utf-8")
        self.assertIn("# Build a calendar", contract)
        self.assertIn("Users can add events.", contract)

        stdin_workspace = self.workspace / "stdin-run"
        stdin_workspace.mkdir()
        stdin_result = CliRunner().invoke(
            hooky_cli.app,
            ["-C", str(stdin_workspace), "run", "--dry-run"],
            input="Build a todo app\n\nUse official TodoMVC behavior.\n",
        )

        self.assertEqual(stdin_result.exit_code, 0, stdin_result.output)
        stdin_contract = (stdin_workspace / ".hooky/contract.md").read_text(encoding="utf-8")
        self.assertIn("# Build a todo app", stdin_contract)
        self.assertIn("Use official TodoMVC behavior.", stdin_contract)

    def test_run_defaults_to_current_directory_workspace(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            workspace = Path.cwd()
            (workspace / "package.json").write_text('{"scripts":{}}\n', encoding="utf-8")

            result = runner.invoke(
                hooky_cli.app,
                ["run", "--dry-run", "--proposal", "Build this project"],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue((workspace / ".hooky/state.json").exists())
            self.assertTrue((workspace / ".hooky/contract.md").exists())
            self.assertTrue((workspace / ".git").exists())
            self.assertFalse((workspace / (".work" + "flow")).exists())
            self.assertIn("# Build this project", (workspace / ".hooky/contract.md").read_text(encoding="utf-8"))

    def test_loop_start_attempt_requires_accepted_contract(self) -> None:
        CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])

        result = CliRunner().invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("contract is not accepted", result.output)

    def test_loop_contract_negotiation_updates_contract_progress_and_log(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])

        proposal = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "proposal", "--proposal", "Build a browser todo app."],
        )
        contract = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "propose-contract", "--body", "- Add todos\n- Persist todos"],
        )
        review = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "review-contract", "--status", "rejected", "--body", "Missing route criteria."],
        )

        self.assertEqual(proposal.exit_code, 0, proposal.output)
        self.assertEqual(contract.exit_code, 0, contract.output)
        self.assertEqual(review.exit_code, 0, review.output)
        contract = (self.workspace / ".hooky/contract.md").read_text(encoding="utf-8")
        proposal_artifact = (self.workspace / ".hooky/proposal.md").read_text(encoding="utf-8")
        self.assertIn("Build a browser todo app.", contract)
        self.assertIn("Build a browser todo app.", proposal_artifact)
        self.assertIn("- Add todos", contract)
        log = (self.workspace / ".hooky/log.md").read_text(encoding="utf-8")
        self.assertIn("Missing route criteria.", log)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertFalse(state["contract_accepted"])
        self.assertEqual(state["status"], "contract-rejected")

        blocked = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])
        self.assertNotEqual(blocked.exit_code, 0)

        accepted = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "review-contract", "--status", "accepted", "--body", "Criteria are testable."],
        )

        self.assertEqual(accepted.exit_code, 0, accepted.output)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertTrue(state["contract_accepted"])
        self.assertEqual(state["status"], "contract-accepted")

    def test_loop_planner_command_runs_role_and_updates_state(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        contract_path = self.workspace / ".hooky/contract.md"

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(working_folder.resolve(), self.workspace.resolve())
            self.assertIn("Build a calendar", proposal)
            contract_path.write_text("# Loop Contract\n\n## Proposal\n\nBuild a calendar.\n", encoding="utf-8")
            return {"status": "done", "contract_path": ".hooky/contract.md", "summary": "Proposal written."}, {"cost": 0.01}

        with mock.patch.object(hooky_cli.loop_agent, "generate_planner_artifacts", side_effect=fake_planner):
            result = runner.invoke(
                hooky_cli.app,
                ["-C", str(self.workspace), "planner", "--proposal", "Build a calendar"],
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
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        contract_path = self.workspace / ".hooky/contract.md"
        feature_path = self.workspace / ".hooky/feature_list.json"

        def fake_generator(*, working_folder: Path, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(working_folder.resolve(), self.workspace.resolve())
            contract_path.write_text("# Loop Contract\n\n## Done Criteria\n\n- Add events\n", encoding="utf-8")
            feature_path.write_text(
                json.dumps({"schema_version": 1, "features": [{"id": "F001", "text": "Add events", "status": "pending"}]}) + "\n",
                encoding="utf-8",
            )
            return {
                "status": "done",
                "contract_path": ".hooky/contract.md",
                "feature_list_path": ".hooky/feature_list.json",
                "summary": "Criteria proposed.",
            }, {"cost": 0.02}

        with mock.patch.object(hooky_cli.loop_agent, "generate_generator_contract_artifacts", side_effect=fake_generator):
            result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "generator-contract"])

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
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])

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
            result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "evaluator-contract"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("contract_review: rejected", result.output)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "contract-rejected")
        self.assertFalse(state["contract_accepted"])
        self.assertEqual(state["role_usage"]["evaluator_contract"]["cost"], 0.03)
        progress = (self.workspace / ".hooky/progress.md").read_text(encoding="utf-8")
        self.assertIn("Add persistence criteria.", progress)

    def test_loop_attempt_lifecycle_creates_trace_and_otel_artifacts(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])

        result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])

        self.assertEqual(result.exit_code, 0, result.output)
        attempt_dir = self.workspace / ".hooky/attempts/001"
        self.assertTrue((attempt_dir / "traces/planner.jsonl").exists())
        self.assertTrue((attempt_dir / "traces/generator.jsonl").exists())
        self.assertTrue((attempt_dir / "traces/evaluator.jsonl").exists())
        self.assertTrue((attempt_dir / "otel/spans.jsonl").exists())
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["current_attempt"], "001")
        self.assertEqual(state["status"], "attempt-running")

        complete = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "complete-attempt", "--result", "fail", "--bottleneck", "generator_trajectory"],
        )

        self.assertEqual(complete.exit_code, 0, complete.output)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertIsNone(state["current_attempt"])
        self.assertEqual(state["status"], "attempt-failed")
        self.assertEqual(state["bottleneck"], "generator_trajectory")
        progress = (self.workspace / ".hooky/progress.md").read_text(encoding="utf-8")
        self.assertIn("generator_trajectory", progress)

    def test_loop_generator_implement_command_runs_role_for_active_attempt(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])

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
            result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "generator-implement"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Implemented the first pass.", result.output)
        report = hooky_cli.read_json(self.workspace / ".hooky/attempts/001/generator_report.json")
        self.assertEqual(report["changed_files"], ["src/app.py"])
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["role_usage"]["generator_implementation"]["cost"], 0.04)

    def test_loop_evaluator_attempt_command_applies_recommendation(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])

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
            result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "evaluator-attempt"])

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
            (working_folder / ".hooky/contract.md").write_text(
                "# Loop Contract\n\n## Proposal\n\nBuild todos.\n",
                encoding="utf-8",
            )
            return {"status": "done", "contract_path": ".hooky/contract.md", "summary": "Proposal ready."}, {"cost": 0.01}

        contract_feedback: list[str] = []

        def fake_contract(
            *,
            working_folder: Path,
            attempt_id: str | None = None,
            review_feedback: str = "",
        ) -> tuple[dict[str, object], dict[str, object]]:
            contract_feedback.append(review_feedback)
            (working_folder / ".hooky/contract.md").write_text(
                "# Loop Contract\n\n## Done Criteria\n\n- Add todos\n",
                encoding="utf-8",
            )
            (working_folder / ".hooky/feature_list.json").write_text(
                json.dumps({"schema_version": 1, "features": [{"id": "F001", "text": "Add todos", "status": "pending"}]}) + "\n",
                encoding="utf-8",
            )
            return {
                "status": "done",
                "contract_path": ".hooky/contract.md",
                "feature_list_path": ".hooky/feature_list.json",
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
                "bottleneck": "none_visible_after_trace_review",
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
                ["-C", str(self.workspace), "run", "--proposal", "Build todos"],
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
        log = (self.workspace / ".hooky/log.md").read_text(encoding="utf-8")
        self.assertIn("contract rejected round 1", log)
        self.assertIn("contract accepted round 2", log)

    def test_loop_run_allows_five_contract_rounds_by_default(self) -> None:
        runner = CliRunner()
        contract_calls = 0
        review_calls = 0

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".hooky/contract.md").write_text(
                "# Loop Contract\n\n## Proposal\n\nBuild something.\n",
                encoding="utf-8",
            )
            return {"status": "done", "contract_path": ".hooky/contract.md", "summary": "Proposal ready."}, {"cost": 0.01}

        def fake_contract(
            *,
            working_folder: Path,
            attempt_id: str | None = None,
            review_feedback: str = "",
        ) -> tuple[dict[str, object], dict[str, object]]:
            nonlocal contract_calls
            contract_calls += 1
            (working_folder / ".hooky/contract.md").write_text(
                "# Loop Contract\n\n## Done Criteria\n\n- A concrete assertion that is still incomplete.\n",
                encoding="utf-8",
            )
            (working_folder / ".hooky/feature_list.json").write_text(
                json.dumps({"schema_version": 1, "features": [{"id": "F001", "text": "Incomplete", "status": "pending"}]}) + "\n",
                encoding="utf-8",
            )
            return {
                "status": "done",
                "contract_path": ".hooky/contract.md",
                "feature_list_path": ".hooky/feature_list.json",
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
                ["-C", str(self.workspace), "run", "--proposal", "Build something"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("status: contract-rejected", result.output)
        self.assertEqual(contract_calls, 5)
        self.assertEqual(review_calls, 5)
        log = (self.workspace / ".hooky/log.md").read_text(encoding="utf-8")
        self.assertIn("contract rejected round 5", log)

    def test_loop_run_retries_attempts_with_evaluator_feedback(self) -> None:
        runner = CliRunner()
        attempts: list[tuple[str, str]] = []

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".hooky/contract.md").write_text(
                "# Loop Contract\n\n## Proposal\n\nBuild todos.\n",
                encoding="utf-8",
            )
            return {"status": "done", "contract_path": ".hooky/contract.md", "summary": "Proposal ready."}, {"cost": 0.01}

        def fake_contract(
            *,
            working_folder: Path,
            attempt_id: str | None = None,
            review_feedback: str = "",
        ) -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".hooky/contract.md").write_text(
                "# Loop Contract\n\n## Done Criteria\n\n- Add todos\n",
                encoding="utf-8",
            )
            (working_folder / ".hooky/feature_list.json").write_text(
                json.dumps({"schema_version": 1, "features": [{"id": "F001", "text": "Add todos", "status": "pending"}]}) + "\n",
                encoding="utf-8",
            )
            return {
                "status": "done",
                "contract_path": ".hooky/contract.md",
                "feature_list_path": ".hooky/feature_list.json",
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
                "bottleneck": "none_visible_after_trace_review",
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
                ["-C", str(self.workspace), "run", "--proposal", "Build todos"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("attempt: 002", result.output)
        self.assertIn("status: passed", result.output)
        self.assertEqual(attempts[0], ("001", ""))
        self.assertEqual(attempts[1][0], "002")
        self.assertIn("T007 fails", attempts[1][1])
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual([attempt["status"] for attempt in state["attempts"]], ["restarted", "passed"])
        log = (self.workspace / ".hooky/log.md").read_text(encoding="utf-8")
        self.assertIn("attempt 001 reset", log)

    def test_loop_run_retries_when_evaluator_errors_without_report(self) -> None:
        runner = CliRunner()
        attempts: list[tuple[str, str]] = []

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".hooky/contract.md").write_text("# Loop Contract\n\n## Proposal\n\nBuild todos.\n", encoding="utf-8")
            return {"status": "done", "contract_path": ".hooky/contract.md", "summary": "Proposal ready."}, {"cost": 0.01}

        def fake_contract(*, working_folder: Path, attempt_id: str | None = None, review_feedback: str = "") -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".hooky/contract.md").write_text("# Loop Contract\n\n## Done Criteria\n\n- Add todos\n", encoding="utf-8")
            (working_folder / ".hooky/feature_list.json").write_text(json.dumps({"features": [{"id": "F001", "text": "Add todos"}]}) + "\n", encoding="utf-8")
            return {
                "status": "done",
                "contract_path": ".hooky/contract.md",
                "feature_list_path": ".hooky/feature_list.json",
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
                "bottleneck": "none_visible_after_trace_review",
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
            result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "run", "--proposal", "Build todos"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("attempt: 002", result.output)
        self.assertIn("status: passed", result.output)
        self.assertIn("TodoMVC implementation pending", attempts[1][1])
        report = hooky_cli.read_json(self.workspace / ".hooky/attempts/001/evaluator_report.json")
        self.assertEqual(report["recommendation"], "restart-attempt")
        self.assertIn("Evaluator did not produce", report["findings"][0])

    def test_loop_restart_attempt_preserves_durable_files(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])

        result = runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "restart-attempt", "--reason", "patching without convergence"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        for relative in ["feature_list.json", "progress.md", "contract.md", "log.md"]:
            self.assertTrue((self.workspace / ".hooky" / relative).exists())
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-attempt")
        self.assertIsNone(state["current_attempt"])
        self.assertEqual(state["attempts"][0]["status"], "restarted")
        log = (self.workspace / ".hooky/log.md").read_text(encoding="utf-8")
        self.assertIn("patching without convergence", log)

    def test_loop_evaluator_report_records_bottleneck_and_report_artifact(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])

        result = runner.invoke(
            hooky_cli.app,
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
        report = hooky_cli.read_json(self.workspace / ".hooky/attempts/001/evaluator_report.json")
        self.assertEqual(report["recommendation"], "restart-attempt")
        self.assertEqual(report["score"], 0.42)
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-attempt")
        self.assertIsNone(state["current_attempt"])
        self.assertEqual(state["bottleneck"], "generator_trajectory")
        self.assertEqual(state["attempts"][0]["status"], "restarted")

    def test_loop_evaluator_pass_is_downgraded_for_placeholder_only_tests(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])
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
        report = hooky_cli.read_json(self.workspace / ".hooky/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["recommendation"], "continue")
        self.assertIn("placeholder tests", " ".join(report["findings"]))
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "attempt-failed")

    def test_loop_evaluator_pass_is_downgraded_for_ui_without_visual_snapshot(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        (self.workspace / ".hooky/contract.md").write_text(
            "# Loop Contract\n\n## Done Criteria\n\n- Browser UI layout is visually correct.\n",
            encoding="utf-8",
        )
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])
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
        report = hooky_cli.read_json(self.workspace / ".hooky/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertIn("capture_visual_snapshot", " ".join(report["findings"]))

    def test_loop_evaluator_pass_is_downgraded_for_reference_ui_with_only_empty_snapshot(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        (self.workspace / ".hooky/proposal.md").write_text(
            "Build a TodoMVC app that visually matches the canonical template using todomvc-app-css.\n",
            encoding="utf-8",
        )
        (self.workspace / ".hooky/contract.md").write_text(
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
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])
        traces = self.workspace / ".hooky/attempts/001/traces"
        traces.mkdir(parents=True, exist_ok=True)
        (traces / "tool_events.json").write_text(
            json.dumps(
                [
                    {
                        "name": "capture_visual_snapshot",
                        "result": {
                            "ok": True,
                            "screenshot_path": ".hooky/tool-results/visual-snapshots/empty.png",
                            "metrics": {},
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            hooky_cli.app,
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
        report = hooky_cli.read_json(self.workspace / ".hooky/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertIn("multiple states", " ".join(report["findings"]))

    def test_loop_evaluator_pass_is_downgraded_for_failed_latest_test_evidence(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])
        traces = self.workspace / ".hooky/attempts/001/traces"
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
            hooky_cli.app,
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
        report = hooky_cli.read_json(self.workspace / ".hooky/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["bottleneck"], "verification_tests_failed")
        self.assertIn("latest executable test evidence failed", " ".join(report["findings"]))

    def test_loop_evaluator_pass_allows_later_successful_test_evidence(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])
        traces = self.workspace / ".hooky/attempts/001/traces"
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
                            "screenshot_path": ".hooky/tool-results/visual-snapshots/shot.png",
                            "metrics": {},
                        },
                    },
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            hooky_cli.app,
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
        report = hooky_cli.read_json(self.workspace / ".hooky/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "pass")

    def test_loop_evaluator_pass_restarts_contract_when_subjective_rubric_is_missing(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        (self.workspace / ".hooky/contract.md").write_text(
            "# Loop Contract\n\n"
            "## Done Criteria\n\n- Build a polished branded dashboard with excellent craft.\n\n"
            "## Taste Rubric\n\n_Optional. Required only when subjective quality matters._\n",
            encoding="utf-8",
        )
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])

        result = runner.invoke(
            hooky_cli.app,
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
        report = hooky_cli.read_json(self.workspace / ".hooky/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["recommendation"], "restart-contract")
        self.assertEqual(report["bottleneck"], "missing_taste_rubric")
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-contract")
        self.assertFalse(state["contract_accepted"])

    def test_loop_evaluator_pass_is_downgraded_for_reported_clipped_primary_ui(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        (self.workspace / ".hooky/contract.md").write_text(
            "# Loop Contract\n\n## Done Criteria\n\n- Browser UI layout is visually correct.\n",
            encoding="utf-8",
        )
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])
        traces = self.workspace / ".hooky/attempts/001/traces"
        traces.mkdir(parents=True, exist_ok=True)
        (traces / "tool_events.json").write_text(
            json.dumps(
                [
                    {
                        "name": "capture_visual_snapshot",
                        "result": {
                            "ok": True,
                            "screenshot_path": ".hooky/tool-results/visual-snapshots/shot.png",
                            "metrics": {},
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            hooky_cli.app,
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
        report = hooky_cli.read_json(self.workspace / ".hooky/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertIn("clipped/off-screen/overflowing primary UI", " ".join(report["findings"]))
        self.assertLessEqual(report["score"], 0.5)

    def test_loop_evaluator_pass_is_downgraded_for_blocking_visual_snapshot_metrics(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        (self.workspace / ".hooky/contract.md").write_text(
            "# Loop Contract\n\n## Done Criteria\n\n- Browser UI layout is visually correct.\n",
            encoding="utf-8",
        )
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])
        traces = self.workspace / ".hooky/attempts/001/traces"
        traces.mkdir(parents=True, exist_ok=True)
        (traces / "tool_events.json").write_text(
            json.dumps(
                [
                    {
                        "name": "capture_visual_snapshot",
                        "result": {
                            "ok": True,
                            "screenshot_path": ".hooky/tool-results/visual-snapshots/shot.png",
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
            hooky_cli.app,
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
        report = hooky_cli.read_json(self.workspace / ".hooky/attempts/001/evaluator_report.json")
        self.assertEqual(report["status"], "fail")
        self.assertIn("clipped visible text or controls", " ".join(report["findings"]))

    def test_loop_evaluator_report_restart_contract_reopens_contract(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])

        result = runner.invoke(
            hooky_cli.app,
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
        state = hooky_cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-contract")
        self.assertFalse(state["contract_accepted"])
        self.assertEqual(state["bottleneck"], "weak_contract")

    def test_loop_trace_and_otel_events_append_supporting_artifacts(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])

        trace = runner.invoke(
            hooky_cli.app,
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
            hooky_cli.app,
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
        trace_line = (self.workspace / ".hooky/attempts/001/traces/generator.jsonl").read_text(encoding="utf-8").strip()
        trace_payload = json.loads(trace_line)
        self.assertEqual(trace_payload["role"], "generator")
        self.assertEqual(trace_payload["kind"], "decision")
        self.assertIn("accepted contract", trace_payload["content"])
        otel_line = (self.workspace / ".hooky/attempts/001/otel/spans.jsonl").read_text(encoding="utf-8").strip()
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
        self.assertTrue((self.workspace / ".hooky/attempts/001/evaluator_report.json").exists())
        self.assertTrue((self.workspace / ".hooky/attempts/001/traces/planner.jsonl").exists())
        self.assertTrue((self.workspace / ".hooky/attempts/001/otel/spans.jsonl").exists())
        contract = (self.workspace / ".hooky/contract.md").read_text(encoding="utf-8")
        self.assertIn("Build a TodoMVC-style app.", contract)
        self.assertIn("- Persist todos", contract)

    def test_loop_run_can_record_restart_recommendation(self) -> None:
        result = CliRunner().invoke(
            hooky_cli.app,
            [
                "-C",
                str(self.workspace),
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
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(
            hooky_cli.app,
            ["-C", str(self.workspace), "log", "--op", "note", "--title", "watchable", "--body", "hello"],
        )

        path_result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "watch", "--path"])
        content_result = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "watch", "--no-follow"])

        self.assertEqual(path_result.exit_code, 0, path_result.output)
        self.assertIn(".hooky/log.md", path_result.output)
        self.assertEqual(content_result.exit_code, 0, content_result.output)
        self.assertIn("watchable", content_result.output)
        self.assertIn("hello", content_result.output)

    def test_loop_debug_commands_show_runtime_transcript_and_stalls(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])
        trace_root = self.workspace / ".hooky/attempts/001/traces"
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

        transcript = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "transcript", "--attempt", "001"])
        stall = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "stall", "--attempt", "001"])
        runtime_log = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "runtime-log", "--attempt", "001"])

        self.assertEqual(transcript.exit_code, 0, transcript.output)
        self.assertIn("Implement TodoMVC.", transcript.output)
        self.assertIn("final_report", transcript.output)
        self.assertEqual(stall.exit_code, 0, stall.output)
        self.assertIn("fake_final_report_text_entries: 1", stall.output)
        self.assertEqual(runtime_log.exit_code, 0, runtime_log.output)
        self.assertIn("tool_calls=0", runtime_log.output)

    def test_loop_inspect_trace_grep_and_harness_review_surface_debug_state(self) -> None:
        runner = CliRunner()
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "accept-contract"])
        runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "start-attempt"])
        trace_root = self.workspace / ".hooky/attempts/001/traces"
        (trace_root / "runtime_transcript.json").write_text(
            json.dumps(
                [
                    {"role": "system", "message": "system prompt"},
                    {"role": "user", "message": "Build TodoMVC"},
                    {
                        "role": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": "I will inspect the contract.",
                            "tool_calls": [{"function": {"name": "read_file"}}],
                        },
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (trace_root / "runtime_events.log").write_text("role=evaluator assistant tools=read_file\n", encoding="utf-8")
        (trace_root / "tool_events.json").write_text(json.dumps([{"name": "read_file", "result": {"ok": True}}]), encoding="utf-8")

        inspect = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "inspect", "--attempt", "001"])
        grep = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "trace-grep", "TodoMVC", "--attempt", "001"])
        review = runner.invoke(hooky_cli.app, ["-C", str(self.workspace), "harness-review"])

        self.assertEqual(inspect.exit_code, 0, inspect.output)
        self.assertIn("transcript_entries: 3", inspect.output)
        self.assertIn("read_file: 1", inspect.output)
        self.assertEqual(grep.exit_code, 0, grep.output)
        self.assertIn("TodoMVC", grep.output)
        self.assertEqual(review.exit_code, 0, review.output)
        self.assertIn("Loop Harness Review", review.output)
        self.assertIn("missing substantive Taste Rubric in contract.md", review.output)
        self.assertTrue((self.workspace / ".hooky/harness_review.md").exists())

    def test_loop_status_uses_last_run_workspace_when_current_directory_has_no_loop(self) -> None:
        last_run_path = self.workspace / "loop-last-run-path"
        runner = CliRunner()
        run_result = runner.invoke(
            hooky_cli.app,
            [
                "-C",
                str(self.workspace),
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
                ["-C", other, "status", "--last-run-path", str(last_run_path)],
            )

        self.assertEqual(status.exit_code, 0, status.output)
        self.assertIn(f"loop: {(self.workspace / '.hooky').resolve()}", status.output)
        self.assertIn("status: passed", status.output)


if __name__ == "__main__":
    unittest.main()
