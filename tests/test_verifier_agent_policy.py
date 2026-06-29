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

    def test_browser_ui_rejects_pass_when_heading_is_clipped(self) -> None:
        contract = base_contract()
        contract["visual_findings"] = ["minor h1 clipping does not affect controls"]

        with self.assertRaisesRegex(ValueError, "clipped/off-screen"):
            verifier_agent.validate_contract_for_context(
                contract,
                browser_context(),
                [
                    {
                        "name": "capture_visual_snapshot",
                        "result": {
                            "ok": True,
                            "screenshot_path": ".workflow/tool-results/visual-snapshots/shot.png",
                            "metrics": {
                                "contentBounds": {"y": -10},
                                "sampleClippedElements": [
                                    {
                                        "tag": "h1",
                                        "role": "",
                                        "text": "Create project",
                                        "rect": {"y": -10, "bottom": 10},
                                    }
                                ],
                            },
                        },
                    }
                ],
            )

    def test_browser_ui_rejects_pass_when_heading_overlaps_input(self) -> None:
        contract = base_contract()
        contract["visual_findings"] = ["title and input are visible"]

        with self.assertRaisesRegex(ValueError, "heading overlaps interactive control"):
            verifier_agent.validate_contract_for_context(
                contract,
                browser_context(),
                [
                    {
                        "name": "capture_visual_snapshot",
                        "result": {
                            "ok": True,
                            "screenshot_path": ".workflow/tool-results/visual-snapshots/shot.png",
                            "metrics": {
                                "contentBounds": {"y": 57},
                                "sampleClippedElements": [],
                                "sampleHeadingInteractiveOverlaps": [
                                    {
                                        "headingText": "Create project",
                                        "headingTag": "h1",
                                        "interactiveText": "Project name",
                                        "interactiveTag": "input",
                                        "overlapRatio": 0.35,
                                    }
                                ],
                            },
                        },
                    }
                ],
            )

    def test_browser_ui_allows_fail_with_blocking_visual_findings(self) -> None:
        contract = base_contract()
        contract["status"] = "fail"
        contract["safe_to_open_pr"] = False
        contract["visual_findings"] = ["h1 is clipped above the viewport"]
        contract["required_actions"] = ["Move the heading fully into the viewport."]

        verifier_agent.validate_contract_for_context(
            contract,
            browser_context(),
            [
                {
                    "name": "capture_visual_snapshot",
                    "result": {
                        "ok": True,
                        "screenshot_path": ".workflow/tool-results/visual-snapshots/shot.png",
                        "metrics": {
                            "contentBounds": {"y": -10},
                            "sampleClippedElements": [{"tag": "h1", "text": "Create project"}],
                        },
                    },
                }
            ],
        )

    def test_browser_ui_rejects_fail_that_downgrades_blocking_visual_findings(self) -> None:
        contract = base_contract()
        contract["status"] = "fail"
        contract["safe_to_open_pr"] = False
        contract["visual_findings"] = ["Non-critical clipped heading element above viewport does not interfere with visible UI"]
        contract["required_actions"] = ["Clean untracked workspace files."]

        with self.assertRaisesRegex(ValueError, "must not be downgraded"):
            verifier_agent.validate_contract_for_context(
                contract,
                browser_context(),
                [
                    {
                        "name": "capture_visual_snapshot",
                        "result": {
                            "ok": True,
                            "screenshot_path": ".workflow/tool-results/visual-snapshots/shot.png",
                            "metrics": {
                                "contentBounds": {"y": -108},
                                "sampleClippedElements": [{"tag": "h1", "text": "Create project"}],
                            },
                        },
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
