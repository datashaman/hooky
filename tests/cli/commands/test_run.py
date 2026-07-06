from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from hooky import cli, roles
from hooky.cli.commands import run as commands_run
from hooky.runtime import AgentRunError, AgentRunResult


class RunCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / ".hooky").mkdir(parents=True)
        os.environ.pop("HOOKY_RUN_KEY", None)
        os.environ.pop("HOOKY_RUN_DIR", None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_enforce_lint_status_gate_downgrades_pass_with_lint_issues(self) -> None:
        report = {"status": "pass", "recommendation": "continue", "lint_status": "issues", "findings": ["looked fine"]}

        gated = commands_run.enforce_lint_status_gate(report)

        self.assertEqual(gated["status"], "fail")
        self.assertEqual(gated["recommendation"], "continue")
        self.assertIn("looked fine", gated["findings"])
        self.assertTrue(any("lint_status=issues" in item for item in gated["findings"]))

    def test_enforce_lint_status_gate_leaves_clean_pass_untouched(self) -> None:
        report = {"status": "pass", "recommendation": "continue", "lint_status": "clean", "findings": []}

        gated = commands_run.enforce_lint_status_gate(report)

        self.assertEqual(gated, report)

    def test_enforce_lint_status_gate_leaves_failing_attempts_untouched(self) -> None:
        report = {"status": "fail", "recommendation": "restart-attempt", "lint_status": "issues", "findings": []}

        gated = commands_run.enforce_lint_status_gate(report)

        self.assertEqual(gated, report)

    def test_model_role_retry_logs_and_retries_agent_run_error(self) -> None:
        calls = 0

        def flaky_call() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AgentRunError(
                    "agent model request exceeded 120s",
                    AgentRunResult(
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
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])

        with mock.patch.dict(os.environ, {"LOOP_MODEL_ROLE_RETRIES": "1"}):
            result = cli.run_model_role_with_retries(self.workspace, "test-role", flaky_call)

        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)
        log = (self.workspace / ".hooky/runs/local/log.md").read_text(encoding="utf-8")
        self.assertIn("retry test-role", log)

    def test_model_role_retry_runs_on_retry_hook_before_retry(self) -> None:
        calls = 0
        retries: list[int] = []

        def flaky_call() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AgentRunError(
                    "agent produced 6 consecutive assistant messages without tool calls",
                    AgentRunResult(
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
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])

        with mock.patch.dict(os.environ, {"LOOP_MODEL_ROLE_RETRIES": "1"}):
            result = cli.run_model_role_with_retries(
                self.workspace,
                "generator-implementation attempt 001",
                flaky_call,
                on_retry=lambda retry, _exc: retries.append(retry),
            )

        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)

    def test_start_records_last_run_path_before_running_loop(self) -> None:
        last_run_path = self.workspace / "last-run-path"

        def stop_after_recording(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("stop")

        with mock.patch.object(commands_run, "run_model_loop_once", side_effect=stop_after_recording):
            result = CliRunner().invoke(
                cli.app,
                ["-C", str(self.workspace), "start", "--last-run-path", str(last_run_path)],
            )

        self.assertNotEqual(result.exit_code, 0)
        last_run = json.loads(last_run_path.read_text(encoding="utf-8"))
        self.assertEqual(last_run["workspace"], self.workspace.resolve().as_posix())
        self.assertEqual(last_run["run_key"], "local")

    def test_start_skill_option_sets_active_skills_for_run(self) -> None:
        seen: dict[str, str | None] = {}
        last_run_path = self.workspace / "last-run-path"

        def stop_after_recording(*_args: object, **_kwargs: object) -> None:
            seen["skills"] = os.environ.get("HOOKY_ACTIVE_SKILLS")
            raise RuntimeError("stop")

        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(commands_run, "run_model_loop_once", side_effect=stop_after_recording),
        ):
            result = CliRunner().invoke(
                cli.app,
                ["-C", str(self.workspace), "start", "--skill", "visual-ui-review", "--last-run-path", str(last_run_path)],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(seen["skills"], "visual-ui-review")
        last_run = json.loads(last_run_path.read_text(encoding="utf-8"))
        self.assertEqual(last_run["workspace"], self.workspace.resolve().as_posix())
        self.assertEqual(last_run["run_key"], "local")

    def test_start_executor_option_is_passed_to_model_loop(self) -> None:
        seen: dict[str, object] = {}
        last_run_path = self.workspace / "last-run-path"

        def stop_after_recording(*_args: object, **kwargs: object) -> None:
            seen.update(kwargs)
            raise RuntimeError("stop")

        with mock.patch.object(commands_run, "run_model_loop_once", side_effect=stop_after_recording):
            result = CliRunner().invoke(
                cli.app,
                ["-C", str(self.workspace), "start", "--executor", "codex", "--last-run-path", str(last_run_path)],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(seen["executor"], "codex")
        self.assertIn("executor: codex", result.output)

    def test_run_executor_option_is_passed_to_model_loop(self) -> None:
        seen: dict[str, object] = {}
        last_run_path = self.workspace / "last-run-path"

        def stop_after_recording(*_args: object, **kwargs: object) -> None:
            seen.update(kwargs)
            raise RuntimeError("stop")

        with mock.patch.object(commands_run, "run_model_loop_once", side_effect=stop_after_recording):
            result = CliRunner().invoke(
                cli.app,
                ["-C", str(self.workspace), "run", "--executor", "claude", "--last-run-path", str(last_run_path)],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(seen["executor"], "claude")

    def test_watch_uses_last_run_workspace(self) -> None:
        last_run_path = self.workspace / "last-run-path"
        CliRunner().invoke(cli.app, ["-C", str(self.workspace), "init", "--last-run-path", str(last_run_path)])
        cli.write_last_run_workspace(last_run_path, self.workspace)

        result = CliRunner().invoke(
            cli.app,
            ["watch", "--last-run-path", str(last_run_path), "--path"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(str(cli.loop_log_path(self.workspace)), result.output)

    def test_status_uses_last_run_workspace_when_current_directory_has_no_loop(self) -> None:
        last_run_path = self.workspace / "last-run-path"
        CliRunner().invoke(cli.app, ["-C", str(self.workspace), "init", "--last-run-path", str(last_run_path)])

        with tempfile.TemporaryDirectory() as other:
            (Path(other) / ".hooky").mkdir(parents=True)
            result = CliRunner().invoke(
                cli.app,
                ["-C", other, "status", "--last-run-path", str(last_run_path)],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"loop: {(self.workspace / '.hooky').resolve()}", result.output)

    def test_loop_run_reads_proposal_file_and_stdin(self) -> None:
        body_file = self.workspace / "proposal.md"
        body_file.write_text("Build a calendar\n\nUsers can add events.\n", encoding="utf-8")

        file_result = CliRunner().invoke(
            cli.app,
            ["-C", str(self.workspace), "run", "--dry-run", "--proposal-file", str(body_file), "--force"],
        )

        self.assertEqual(file_result.exit_code, 0, file_result.output)
        contract = (self.workspace / ".hooky/runs/local/contract.md").read_text(encoding="utf-8")
        self.assertIn("# Build a calendar", contract)
        self.assertIn("Users can add events.", contract)

        stdin_workspace = self.workspace / "stdin-run"
        stdin_workspace.mkdir()
        stdin_result = CliRunner().invoke(
            cli.app,
            ["-C", str(stdin_workspace), "run", "--dry-run"],
            input="Build a todo app\n\nUse official TodoMVC behavior.\n",
        )

        self.assertEqual(stdin_result.exit_code, 0, stdin_result.output)
        stdin_contract = (stdin_workspace / ".hooky/runs/local/contract.md").read_text(encoding="utf-8")
        self.assertIn("# Build a todo app", stdin_contract)
        self.assertIn("Use official TodoMVC behavior.", stdin_contract)

    def test_run_defaults_to_current_directory_workspace(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            previous = Path.cwd()
            os.chdir(workspace)
            try:
                (workspace / "package.json").write_text('{"scripts":{}}\n', encoding="utf-8")

                result = runner.invoke(
                    cli.app,
                    ["run", "--dry-run", "--proposal", "Build this project"],
                )

                self.assertEqual(result.exit_code, 0, result.output)
                self.assertTrue((workspace / ".hooky/runs/local/state.json").exists())
                self.assertTrue((workspace / ".hooky/runs/local/contract.md").exists())
                self.assertTrue((workspace / ".git").exists())
                self.assertFalse((workspace / (".work" + "flow")).exists())
                self.assertIn("# Build this project", (workspace / ".hooky/runs/local/contract.md").read_text(encoding="utf-8"))
            finally:
                os.chdir(previous)

    def test_loop_run_models_executes_three_role_suite(self) -> None:
        runner = CliRunner()

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(working_folder.resolve(), self.workspace.resolve())
            self.assertIn("Build todos", proposal)
            (working_folder / ".hooky/runs/local/contract.md").write_text(
                "# Loop Contract\n\n## Proposal\n\nBuild todos.\n",
                encoding="utf-8",
            )
            return {"status": "done", "contract_path": ".hooky/runs/local/contract.md", "summary": "Proposal ready."}, {"cost": 0.01}

        contract_feedback: list[str] = []

        def fake_contract(
            *,
            working_folder: Path,
            attempt_id: str | None = None,
            review_feedback: str = "",
        ) -> tuple[dict[str, object], dict[str, object]]:
            contract_feedback.append(review_feedback)
            (working_folder / ".hooky/runs/local/contract.md").write_text(
                "# Loop Contract\n\n## Done Criteria\n\n- Add todos\n",
                encoding="utf-8",
            )
            (working_folder / ".hooky/runs/local/feature_list.json").write_text(
                json.dumps({"schema_version": 1, "features": [{"id": "F001", "text": "Add todos", "status": "pending"}]}) + "\n",
                encoding="utf-8",
            )
            return {
                "status": "done",
                "contract_path": ".hooky/runs/local/contract.md",
                "feature_list_path": ".hooky/runs/local/feature_list.json",
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
            mock.patch.object(roles, "generate_planner_artifacts", side_effect=fake_planner),
            mock.patch.object(roles, "generate_generator_contract_artifacts", side_effect=fake_contract),
            mock.patch.object(roles, "generate_evaluator_contract_artifacts", side_effect=fake_contract_review),
            mock.patch.object(roles, "generate_generator_implementation_artifacts", side_effect=fake_implementation),
            mock.patch.object(roles, "generate_evaluator_attempt_artifacts", side_effect=fake_attempt_review),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = runner.invoke(
                cli.app,
                ["-C", str(self.workspace), "run", "--proposal", "Build todos"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("status: passed", result.output)
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "passed")
        self.assertEqual(state["attempts"][0]["status"], "passed")
        self.assertEqual(state["role_usage"]["evaluator_attempt"]["cost"], 0.05)
        self.assertEqual(review_calls, 2)
        self.assertEqual(contract_feedback[0], "")
        self.assertIn("Use .new-todo exactly.", contract_feedback[1])
        log = (self.workspace / ".hooky/runs/local/log.md").read_text(encoding="utf-8")
        self.assertIn("contract rejected round 1", log)
        self.assertIn("contract accepted round 2", log)

    def test_loop_run_allows_five_contract_rounds_by_default(self) -> None:
        runner = CliRunner()
        contract_calls = 0
        review_calls = 0

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".hooky/runs/local/contract.md").write_text(
                "# Loop Contract\n\n## Proposal\n\nBuild something.\n",
                encoding="utf-8",
            )
            return {"status": "done", "contract_path": ".hooky/runs/local/contract.md", "summary": "Proposal ready."}, {"cost": 0.01}

        def fake_contract(
            *,
            working_folder: Path,
            attempt_id: str | None = None,
            review_feedback: str = "",
        ) -> tuple[dict[str, object], dict[str, object]]:
            nonlocal contract_calls
            contract_calls += 1
            (working_folder / ".hooky/runs/local/contract.md").write_text(
                "# Loop Contract\n\n## Done Criteria\n\n- A concrete assertion that is still incomplete.\n",
                encoding="utf-8",
            )
            (working_folder / ".hooky/runs/local/feature_list.json").write_text(
                json.dumps({"schema_version": 1, "features": [{"id": "F001", "text": "Incomplete", "status": "pending"}]}) + "\n",
                encoding="utf-8",
            )
            return {
                "status": "done",
                "contract_path": ".hooky/runs/local/contract.md",
                "feature_list_path": ".hooky/runs/local/feature_list.json",
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
            mock.patch.object(roles, "generate_planner_artifacts", side_effect=fake_planner),
            mock.patch.object(roles, "generate_generator_contract_artifacts", side_effect=fake_contract),
            mock.patch.object(roles, "generate_evaluator_contract_artifacts", side_effect=fake_contract_review),
        ]
        with patches[0], patches[1], patches[2]:
            result = runner.invoke(
                cli.app,
                ["-C", str(self.workspace), "run", "--proposal", "Build something"],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("status: contract-rejected", result.output)
        self.assertEqual(contract_calls, 5)
        self.assertEqual(review_calls, 5)
        log = (self.workspace / ".hooky/runs/local/log.md").read_text(encoding="utf-8")
        self.assertIn("contract rejected round 5", log)

    def test_loop_run_retries_attempts_with_evaluator_feedback(self) -> None:
        runner = CliRunner()
        attempts: list[tuple[str, str]] = []

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".hooky/runs/local/contract.md").write_text(
                "# Loop Contract\n\n## Proposal\n\nBuild todos.\n",
                encoding="utf-8",
            )
            return {"status": "done", "contract_path": ".hooky/runs/local/contract.md", "summary": "Proposal ready."}, {"cost": 0.01}

        def fake_contract(
            *,
            working_folder: Path,
            attempt_id: str | None = None,
            review_feedback: str = "",
        ) -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".hooky/runs/local/contract.md").write_text(
                "# Loop Contract\n\n## Done Criteria\n\n- Add todos\n",
                encoding="utf-8",
            )
            (working_folder / ".hooky/runs/local/feature_list.json").write_text(
                json.dumps({"schema_version": 1, "features": [{"id": "F001", "text": "Add todos", "status": "pending"}]}) + "\n",
                encoding="utf-8",
            )
            return {
                "status": "done",
                "contract_path": ".hooky/runs/local/contract.md",
                "feature_list_path": ".hooky/runs/local/feature_list.json",
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
            mock.patch.object(roles, "generate_planner_artifacts", side_effect=fake_planner),
            mock.patch.object(roles, "generate_generator_contract_artifacts", side_effect=fake_contract),
            mock.patch.object(roles, "generate_evaluator_contract_artifacts", side_effect=fake_contract_review),
            mock.patch.object(roles, "generate_generator_implementation_artifacts", side_effect=fake_implementation),
            mock.patch.object(roles, "generate_evaluator_attempt_artifacts", side_effect=fake_attempt_review),
            mock.patch.dict(os.environ, {"LOOP_ATTEMPT_MAX_ROUNDS": "2"}),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = runner.invoke(
                cli.app,
                ["-C", str(self.workspace), "run", "--proposal", "Build todos"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("attempt: 002", result.output)
        self.assertIn("status: passed", result.output)
        self.assertEqual(attempts[0], ("001", ""))
        self.assertEqual(attempts[1][0], "002")
        self.assertIn("T007 fails", attempts[1][1])
        state = cli.read_loop_state(self.workspace)
        self.assertEqual([attempt["status"] for attempt in state["attempts"]], ["restarted", "passed"])
        log = (self.workspace / ".hooky/runs/local/log.md").read_text(encoding="utf-8")
        self.assertIn("attempt 001 reset", log)

    def test_loop_run_retries_when_evaluator_errors_without_report(self) -> None:
        runner = CliRunner()
        attempts: list[tuple[str, str]] = []

        def fake_planner(*, working_folder: Path, proposal: str, attempt_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".hooky/runs/local/contract.md").write_text("# Loop Contract\n\n## Proposal\n\nBuild todos.\n", encoding="utf-8")
            return {"status": "done", "contract_path": ".hooky/runs/local/contract.md", "summary": "Proposal ready."}, {"cost": 0.01}

        def fake_contract(*, working_folder: Path, attempt_id: str | None = None, review_feedback: str = "") -> tuple[dict[str, object], dict[str, object]]:
            (working_folder / ".hooky/runs/local/contract.md").write_text("# Loop Contract\n\n## Done Criteria\n\n- Add todos\n", encoding="utf-8")
            (working_folder / ".hooky/runs/local/feature_list.json").write_text(json.dumps({"features": [{"id": "F001", "text": "Add todos"}]}) + "\n", encoding="utf-8")
            return {
                "status": "done",
                "contract_path": ".hooky/runs/local/contract.md",
                "feature_list_path": ".hooky/runs/local/feature_list.json",
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
                raise AgentRunError(
                    "agent produced 6 consecutive assistant messages without tool calls",
                    AgentRunResult(
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
            mock.patch.object(roles, "generate_planner_artifacts", side_effect=fake_planner),
            mock.patch.object(roles, "generate_generator_contract_artifacts", side_effect=fake_contract),
            mock.patch.object(roles, "generate_evaluator_contract_artifacts", side_effect=fake_contract_review),
            mock.patch.object(roles, "generate_generator_implementation_artifacts", side_effect=fake_implementation),
            mock.patch.object(roles, "generate_evaluator_attempt_artifacts", side_effect=fake_attempt_review),
            mock.patch.dict(os.environ, {"LOOP_ATTEMPT_MAX_ROUNDS": "2"}),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = runner.invoke(cli.app, ["-C", str(self.workspace), "run", "--proposal", "Build todos"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("attempt: 002", result.output)
        self.assertIn("status: passed", result.output)
        self.assertIn("TodoMVC implementation pending", attempts[1][1])
        report = cli.read_json(self.workspace / ".hooky/runs/local/attempts/001/evaluator_report.json")
        self.assertEqual(report["recommendation"], "restart-attempt")
        self.assertIn("Evaluator did not produce", report["findings"][0])

    def test_loop_run_executes_complete_local_suite(self) -> None:
        result = CliRunner().invoke(
            cli.app,
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
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "passed")
        self.assertIsNone(state["current_attempt"])
        self.assertEqual(state["attempts"][0]["status"], "passed")
        self.assertTrue((self.workspace / ".hooky/runs/local/attempts/001/evaluator_report.json").exists())
        self.assertTrue((self.workspace / ".hooky/runs/local/attempts/001/traces/planner.jsonl").exists())
        self.assertTrue((self.workspace / ".hooky/runs/local/attempts/001/otel/spans.jsonl").exists())
        contract = (self.workspace / ".hooky/runs/local/contract.md").read_text(encoding="utf-8")
        self.assertIn("Build a TodoMVC-style app.", contract)
        self.assertIn("- Persist todos", contract)
        run_dir = self.workspace / ".hooky/runs/local"
        leftover_tmp_files = [entry.name for entry in run_dir.iterdir() if entry.name.endswith(".tmp")]
        self.assertEqual(leftover_tmp_files, [], "atomic writes for contract.md must not leave temp files behind")

    def test_loop_run_can_record_restart_recommendation(self) -> None:
        result = CliRunner().invoke(
            cli.app,
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

        self.assertEqual(result.exit_code, 1, result.output)
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "restart-attempt")
        self.assertEqual(state["attempts"][0]["status"], "restarted")
        self.assertEqual(state["bottleneck"], "generator_trajectory")

    def test_loop_watch_shows_path_and_log_content(self) -> None:
        runner = CliRunner()
        runner.invoke(cli.app, ["-C", str(self.workspace), "init"])
        runner.invoke(
            cli.app,
            ["-C", str(self.workspace), "log", "--op", "note", "--title", "watchable", "--body", "hello"],
        )

        path_result = runner.invoke(cli.app, ["-C", str(self.workspace), "watch", "--path"])
        content_result = runner.invoke(cli.app, ["-C", str(self.workspace), "watch", "--no-follow"])

        self.assertEqual(path_result.exit_code, 0, path_result.output)
        self.assertIn(".hooky/runs/local/log.md", path_result.output)
        self.assertEqual(content_result.exit_code, 0, content_result.output)
        self.assertIn("watchable", content_result.output)
        self.assertIn("hello", content_result.output)

    def test_loop_status_uses_last_run_workspace_when_current_directory_has_no_loop(self) -> None:
        last_run_path = self.workspace / "loop-last-run-path"
        runner = CliRunner()
        run_result = runner.invoke(
            cli.app,
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
                cli.app,
                ["-C", other, "status", "--last-run-path", str(last_run_path)],
            )

        self.assertEqual(status.exit_code, 0, status.output)
        self.assertIn(f"loop: {(self.workspace / '.hooky').resolve()}", status.output)
        self.assertIn("status: passed", status.output)

    def test_light_implement_skips_planner_and_contract_negotiation(self) -> None:
        runner = CliRunner()

        def fake_implementation(*, working_folder: Path, attempt_id: str, evaluator_feedback: str = "") -> tuple[dict[str, object], dict[str, object]]:
            self.assertEqual(attempt_id, "001")
            contract = (working_folder / ".hooky/runs/local/contract.md").read_text(encoding="utf-8")
            self.assertIn("also handle nulls", contract)
            return {
                "status": "done",
                "summary": "Handled nulls.",
                "changed_files": ["src/app.py"],
                "tests_run": ["pytest"],
                "failures": [],
            }, {"cost": 0.04}

        def fake_attempt_review(*, working_folder: Path, attempt_id: str) -> tuple[dict[str, object], dict[str, object]]:
            return {
                "status": "pass",
                "recommendation": "continue",
                "bottleneck": "none_visible_after_trace_review",
                "findings": [],
                "score": 1.0,
            }, {"cost": 0.05}

        with (
            mock.patch.object(roles, "generate_planner_artifacts") as planner_mock,
            mock.patch.object(roles, "generate_generator_contract_artifacts") as contract_mock,
            mock.patch.object(roles, "generate_evaluator_contract_artifacts") as contract_review_mock,
            mock.patch.object(roles, "generate_generator_implementation_artifacts", side_effect=fake_implementation),
            mock.patch.object(roles, "generate_evaluator_attempt_artifacts", side_effect=fake_attempt_review),
        ):
            result = runner.invoke(
                cli.app,
                ["-C", str(self.workspace), "run", "--light", "--proposal", "also handle nulls"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("status: passed", result.output)
        planner_mock.assert_not_called()
        contract_mock.assert_not_called()
        contract_review_mock.assert_not_called()
        state = cli.read_loop_state(self.workspace)
        self.assertTrue(state["contract_accepted"])

    def test_light_implement_restart_contract_recommendation_stops_instead_of_negotiating(self) -> None:
        runner = CliRunner()

        def fake_implementation(*, working_folder: Path, attempt_id: str, evaluator_feedback: str = "") -> tuple[dict[str, object], dict[str, object]]:
            return {"status": "done", "summary": "Tried.", "changed_files": [], "tests_run": [], "failures": []}, {"cost": 0.01}

        def fake_attempt_review(*, working_folder: Path, attempt_id: str) -> tuple[dict[str, object], dict[str, object]]:
            return {
                "status": "fail",
                "recommendation": "restart-contract",
                "bottleneck": "the request is ambiguous",
                "findings": ["Not clear what 'it' refers to."],
                "score": 0.0,
            }, {"cost": 0.01}

        with (
            mock.patch.object(roles, "generate_generator_implementation_artifacts", side_effect=fake_implementation),
            mock.patch.object(roles, "generate_evaluator_attempt_artifacts", side_effect=fake_attempt_review),
        ):
            result = runner.invoke(
                cli.app,
                ["-C", str(self.workspace), "run", "--light", "--proposal", "fix it"],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        state = cli.read_loop_state(self.workspace)
        self.assertEqual(state["status"], "stopped")

    def test_review_command_reports_verdict_and_writes_report(self) -> None:
        runner = CliRunner()

        def fake_reviewer(*, working_folder: Path, proposal: str) -> tuple[dict[str, object], dict[str, object]]:
            self.assertIn("Refactor auth", proposal)
            return {"verdict": "approve", "summary": "Looks fine.", "findings": []}, {"cost": 0.02}

        with mock.patch.object(roles, "generate_reviewer_artifacts", side_effect=fake_reviewer):
            result = runner.invoke(
                cli.app,
                ["-C", str(self.workspace), "review", "--proposal", "Refactor auth"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("verdict: approve", result.output)
        report_path = self.workspace / ".hooky/runs/local/review_report.json"
        self.assertTrue(report_path.exists())
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["verdict"], "approve")

    def test_review_command_exits_nonzero_on_request_changes(self) -> None:
        runner = CliRunner()

        def fake_reviewer(*, working_folder: Path, proposal: str) -> tuple[dict[str, object], dict[str, object]]:
            return {"verdict": "request_changes", "summary": "Missing tests.", "findings": ["No coverage for the new branch."]}, {"cost": 0.02}

        with mock.patch.object(roles, "generate_reviewer_artifacts", side_effect=fake_reviewer):
            result = runner.invoke(
                cli.app,
                ["-C", str(self.workspace), "review", "--proposal", "Refactor auth"],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("verdict: request_changes", result.output)


if __name__ == "__main__":
    unittest.main()
