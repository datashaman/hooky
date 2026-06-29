from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scripts import test_agent


class TestAgentPolicyTests(unittest.TestCase):
    def test_test_agent_contract_does_not_block_toolchain_filenames(self) -> None:
        test_agent.validate_test_artifact_path(
            {"path": "any-test-tool.config", "purpose": "Configure the generated test harness"},
            "test file",
        )
        test_agent.validate_test_artifact_path(
            {"path": "package.json", "purpose": "Record test dependency changes"},
            "test file",
        )

    def test_test_agent_contract_still_blocks_hooky_owned_files(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be written under .workflow"):
            test_agent.validate_test_artifact_path(
                {"path": ".workflow/artifacts/test-agent/report.json", "purpose": "Write Hooky report"},
                "test file",
            )

    def test_masked_reportable_command_is_diagnostic_not_required_final_evidence(self) -> None:
        test_agent.validate_execution_checks_against_tool_events(
            {"test_execution_checks": [], "dependency_changes": []},
            [
                {
                    "name": "bash",
                    "arguments": {"command": "npm test 2>&1 || true"},
                    "result": {"ok": True, "returncode": 0},
                }
            ],
        )

    def test_unmasked_reportable_command_still_requires_execution_check(self) -> None:
        with self.assertRaisesRegex(ValueError, "setup or test command missing"):
            test_agent.validate_execution_checks_against_tool_events(
                {"test_execution_checks": [], "dependency_changes": []},
                [
                    {
                        "name": "bash",
                        "arguments": {"command": "npm test"},
                        "result": {"ok": True, "returncode": 0},
                    }
                ],
            )

    def test_npm_install_does_not_require_dependency_changes(self) -> None:
        test_agent.validate_execution_checks_against_tool_events(
            {
                "test_execution_checks": [
                    {
                        "command": "npm install 2>&1",
                        "status": "passed",
                        "reason": "Installed declared project dependencies.",
                    }
                ],
                "dependency_changes": [],
            },
            [
                {
                    "name": "bash",
                    "arguments": {"command": "npm install 2>&1"},
                    "result": {"ok": True, "returncode": 0},
                }
            ],
        )

    def test_run_tests_event_satisfies_execution_check(self) -> None:
        test_agent.validate_execution_checks_against_tool_events(
            {
                "test_execution_checks": [
                    {
                        "command": "npx playwright test --list",
                        "status": "passed",
                        "reason": "Discovered generated Playwright tests.",
                    }
                ],
                "dependency_changes": [],
            },
            [
                {
                    "name": "run_tests",
                    "arguments": {"command": "npx playwright test --list", "list_only": True},
                    "result": {"ok": True, "returncode": 0, "command": "npx playwright test --list"},
                }
            ],
        )

    def test_pre_builder_playwright_suite_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not run before Builder"):
            test_agent.validate_execution_checks_against_tool_events(
                {
                    "test_execution_checks": [
                        {
                            "command": "npx playwright test",
                            "status": "failed",
                            "reason": "Attempted browser suite before Builder.",
                        }
                    ],
                    "dependency_changes": [],
                },
                [
                    {
                        "name": "run_tests",
                        "arguments": {"command": "npx playwright test"},
                        "result": {"ok": False, "returncode": 1, "command": "npx playwright test"},
                    }
                ],
                {"source": "approved_spec_contract"},
            )

    def test_pre_builder_playwright_list_is_allowed(self) -> None:
        test_agent.validate_execution_checks_against_tool_events(
            {
                "test_execution_checks": [
                    {
                        "command": "npx playwright test --list",
                        "status": "passed",
                        "reason": "Discovered generated browser tests without launching the app.",
                    }
                ],
                "dependency_changes": [],
            },
            [
                {
                    "name": "run_tests",
                    "arguments": {"command": "npx playwright test --list", "list_only": True},
                    "result": {"ok": True, "returncode": 0, "command": "npx playwright test --list"},
                }
            ],
            {"source": "approved_spec_contract"},
        )

    def test_package_manager_defaults_to_npm_without_js_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(test_agent.detect_js_package_manager(Path(tmp))["selected"], "npm")

    def test_package_manager_detects_lockfile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

            self.assertEqual(test_agent.detect_js_package_manager(root), {"selected": "pnpm", "source": "pnpm-lock.yaml"})

    def test_package_manager_validator_blocks_alternative_probes(self) -> None:
        self.assertIsNotNone(test_agent.package_manager_command_violation("pnpm --version 2>/dev/null || echo missing", "npm"))
        self.assertIsNotNone(test_agent.package_manager_command_violation("which pnpm && pnpm --version", "npm"))
        self.assertIsNotNone(test_agent.package_manager_command_violation("command -v yarn", "npm"))
        self.assertIsNone(test_agent.package_manager_command_violation("npm install", "npm"))
        self.assertIsNone(test_agent.package_manager_command_violation("pnpm install", "pnpm"))


if __name__ == "__main__":
    unittest.main()
