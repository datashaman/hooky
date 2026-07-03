from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hooky import loop_executor
from hooky.runtime import ToolRuntime


class LoopExecutorTests(unittest.TestCase):
    def test_selected_executor_prefers_explicit_then_env_then_native(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(loop_executor.selected_executor(), "native")
            os.environ["HOOKY_EXECUTOR"] = "codex"
            self.assertEqual(loop_executor.selected_executor(), "codex")
            self.assertEqual(loop_executor.selected_executor("claude"), "claude")

    def test_selected_executor_rejects_unknown_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown Hooky executor"):
            loop_executor.selected_executor("wat")

    def test_codex_command_uses_user_config_by_default_and_accepts_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            command = loop_executor.codex_command(Path(tmp), Path(tmp) / "exec")
            self.assertIn("codex", command)
            self.assertIn("exec", command)
            self.assertNotIn("--model", command)
            self.assertNotIn("--config", command)
            self.assertIn("--output-last-message", command)
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
            self.assertEqual(command[-1], "-")

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(
                os.environ,
                {"HOOKY_CODEX_MODEL": "gpt-5.5", "HOOKY_CODEX_REASONING_EFFORT": "high"},
                clear=True,
            ),
        ):
            command = loop_executor.codex_command(Path(tmp), Path(tmp) / "exec")
            self.assertIn("--model", command)
            self.assertIn("gpt-5.5", command)
            self.assertIn("--config", command)
            self.assertIn('model_reasoning_effort="high"', command)

    def test_claude_command_uses_user_config_by_default_and_accepts_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            command = loop_executor.claude_command(Path(tmp) / "exec")
            self.assertEqual(command[0], "claude")
            self.assertIn("--print", command)
            self.assertIn("--verbose", command)
            self.assertIn("--output-format", command)
            self.assertIn("stream-json", command)
            self.assertNotIn("--model", command)
            self.assertNotIn("--effort", command)
            self.assertIn("--setting-sources", command)

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(
                os.environ,
                {"HOOKY_CLAUDE_MODEL": "opusplan", "HOOKY_CLAUDE_EFFORT": "medium"},
                clear=True,
            ),
        ):
            command = loop_executor.claude_command(Path(tmp) / "exec")
            self.assertIn("--model", command)
            self.assertIn("opusplan", command)
            self.assertIn("--effort", command)
            self.assertIn("medium", command)

    def test_shell_executor_reads_input_and_writes_validated_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            workspace = Path(tmp)
            script = workspace / "fake_executor.py"
            script.write_text(
                """
from __future__ import annotations
import json
import sys
from pathlib import Path

input_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
assert "Hooky role invocation" in input_path.read_text(encoding="utf-8")
output_path.write_text(json.dumps({"ok": True, "summary": "done"}) + "\\n", encoding="utf-8")
print("wrote", output_path)
""".lstrip(),
                encoding="utf-8",
            )
            runtime = ToolRuntime(
                working_folder=workspace,
                final_report_schema={"type": "object"},
                max_cost_usd=0.01,
                max_seconds=10,
                final_validator=lambda report: None if report.get("ok") is True else (_ for _ in ()).throw(ValueError("not ok")),
                live_log_root=workspace / ".hooky/runs/local",
            )
            invocation = loop_executor.RoleInvocation(
                role="planner",
                agent_name="loop-planner",
                model="test-model",
                model_metadata={"model": "test-model"},
                system="system",
                user="user",
                runtime=runtime,
            )
            os.environ["HOOKY_EXECUTOR_COMMAND"] = f"{shlex.quote(sys.executable)} {shlex.quote(script.as_posix())} $input $output"

            result = loop_executor.run_role(invocation, executor="shell")

            self.assertEqual(result.final_report, {"ok": True, "summary": "done"})
            self.assertEqual(result.usage["executor"], "shell")
            self.assertTrue((workspace / ".hooky/runs/local/executor/planner/input.md").exists())
            metadata = json.loads((workspace / ".hooky/runs/local/executor/planner/metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["returncode"], 0)

    def test_extract_executor_error_reads_claude_stream_json_result(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}),
            ]
        )

        self.assertEqual(loop_executor.extract_executor_error(stdout, ""), "Not logged in · Please run /login")

    def test_external_usage_reads_claude_result_tokens_and_cost(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 1}}}),
                json.dumps(
                    {
                        "type": "result",
                        "is_error": False,
                        "total_cost_usd": 0.3042908,
                        "usage": {
                            "input_tokens": 13716,
                            "cache_creation_input_tokens": 24355,
                            "cache_read_input_tokens": 254096,
                            "output_tokens": 2635,
                        },
                        "modelUsage": {
                            "claude-haiku-4-5-20251001": {
                                "inputTokens": 1159,
                                "outputTokens": 20,
                                "cacheReadInputTokens": 0,
                                "cacheCreationInputTokens": 0,
                                "costUSD": 0.001259,
                            },
                            "claude-sonnet-5": {
                                "inputTokens": 13716,
                                "outputTokens": 2635,
                                "cacheReadInputTokens": 254096,
                                "cacheCreationInputTokens": 24355,
                                "costUSD": 0.3030318,
                            },
                        },
                    }
                ),
            ]
        )

        usage = loop_executor.external_usage(stdout, executor="claude")

        self.assertEqual(usage["executor"], "claude")
        self.assertAlmostEqual(usage["cost"], 0.3042908)
        self.assertEqual(usage["prompt_tokens"], 292167)
        self.assertEqual(usage["completion_tokens"], 2635)
        self.assertEqual(usage["total_tokens"], 294802)
        self.assertIn("model_usage", usage)


if __name__ == "__main__":
    unittest.main()
