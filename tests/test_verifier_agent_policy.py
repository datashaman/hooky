from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verifier_agent  # noqa: E402


class VerifierAgentPolicyTests(unittest.TestCase):
    def test_contract_requires_visual_findings(self) -> None:
        contract = base_contract()
        contract.pop("visual_findings")

        with self.assertRaisesRegex(ValueError, "visual_findings"):
            verifier_agent.validate_contract(contract)

    def test_contract_allows_not_applicable_visual_findings(self) -> None:
        verifier_agent.validate_contract(base_contract())

    def test_browser_ui_requires_successful_visual_snapshot(self) -> None:
        contract = base_contract()
        contract["visual_findings"] = ["layout inspected from screenshot"]

        with self.assertRaisesRegex(ValueError, "successful visual snapshot"):
            verifier_agent.validate_contract_for_context(contract, browser_context(), [])

    def test_browser_ui_accepts_successful_visual_snapshot(self) -> None:
        contract = base_contract()
        contract["visual_findings"] = ["layout inspected from screenshot"]

        verifier_agent.validate_contract_for_context(
            contract,
            browser_context(),
            [
                {
                    "name": "capture_visual_snapshot",
                    "result": {"ok": True, "screenshot_path": ".workflow/tool-results/visual-snapshots/shot.png"},
                }
            ],
        )


def base_contract() -> dict[str, object]:
    return {
        "status": "pass",
        "summary": "Deterministic checks passed.",
        "checks_run": [
            {
                "name": "tests",
                "command": "npm test",
                "status": "pass",
                "evidence": "22 passed",
            }
        ],
        "scope_violations": [],
        "test_integrity_findings": [],
        "acceptance_coverage_findings": [],
        "visual_findings": ["not_applicable: no browser or visual UI surface detected"],
        "security_findings": [],
        "required_actions": [],
        "safe_to_open_pr": True,
    }


def browser_context() -> dict[str, object]:
    return {
        "package_json": {
            "dependencies": {"react": "^19.0.0"},
            "devDependencies": {"@playwright/test": "^1.0.0"},
        },
        "approved_test_contracts": [],
    }


if __name__ == "__main__":
    unittest.main()
