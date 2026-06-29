from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import builder_agent  # noqa: E402


class BuilderAgentPolicyTests(unittest.TestCase):
    def test_normal_builder_requires_file_writes(self) -> None:
        with self.assertRaisesRegex(ValueError, "must include production file writes"):
            builder_agent.validate_contract(base_contract())

    def test_remediation_builder_can_report_noop_when_tests_passed(self) -> None:
        builder_agent.validate_contract_for_context(
            base_contract(),
            {"remediation": {"root_cause_stage": "builder", "findings": ["prior trajectory issue"]}},
        )


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
