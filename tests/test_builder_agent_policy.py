from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import builder_agent  # noqa: E402


class BuilderAgentPolicyTests(unittest.TestCase):
    def test_normal_builder_requires_file_writes(self) -> None:
        with self.assertRaisesRegex(ValueError, "must include file writes"):
            builder_agent.validate_contract(base_contract())

    def test_builder_file_write_content_is_optional_because_files_are_on_disk(self) -> None:
        contract = base_contract()
        contract["file_writes"] = [{"path": "src/App.jsx", "purpose": "Implement app"}]

        builder_agent.validate_contract(contract)

    def test_builder_can_write_tests_as_part_of_tdd_loop(self) -> None:
        builder_agent.validate_file_write({"path": "tests/todomvc.spec.js", "purpose": "Acceptance coverage"})

    def test_remediation_builder_can_report_noop_when_tests_passed(self) -> None:
        builder_agent.validate_contract_for_context(
            base_contract(),
            {"remediation": {"root_cause_stage": "builder", "findings": ["prior trajectory issue"]}},
        )

    def test_builder_blocks_dependency_internal_bash_inspection(self) -> None:
        violation = builder_agent.builder_bash_command_violation(
            'grep -rn "async check" node_modules/playwright-core/lib/ | head -20'
        )

        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertIn("dependency or framework internals", violation)

    def test_builder_allows_project_test_result_artifact_inspection(self) -> None:
        self.assertIsNone(
            builder_agent.builder_bash_command_violation(
                "cat test-results/todomvc-failing-case/error-context.md"
            )
        )

    def test_builder_allows_approved_test_execution(self) -> None:
        self.assertIsNone(builder_agent.builder_bash_command_violation("npm test tests/todomvc.spec.cjs:364"))


def base_contract() -> dict[str, object]:
    return {
        "summary": "Existing implementation already passes after remediation review.",
        "file_writes": [],
        "commands_to_run": [],
        "tests_run": ["npm test"],
        "tests_passing": True,
        "failures_remaining": [],
        "cost_actuals": {},
        "requires_verifier": True,
    }


if __name__ == "__main__":
    unittest.main()
