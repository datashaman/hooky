from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_runtime  # noqa: E402
import spec_agent  # noqa: E402


class SpecAgentPolicyTests(unittest.TestCase):
    def test_final_report_rejects_missing_contract_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "contract_path does not exist yet"):
                spec_agent.validate_spec_finish_report({"contract_path": "docs/specs/issue-1/contract.json"}, root)

    def test_spec_contract_write_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "docs/specs/issue-1/contract.json"

            with self.assertRaisesRegex(ValueError, "spec contract JSON is invalid"):
                spec_agent.validate_spec_write(path, '{"acceptance_criteria": ["bad "quote""]}', root)

    def test_spec_contract_write_allows_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_agent.validate_spec_write(root / "docs/specs/issue-1/spec.md", "not json", root)

    def test_write_spec_contract_writes_valid_contract_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = agent_runtime.ToolRuntime(
                working_folder=root,
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                spec_contract_write_enabled=True,
                write_allowed_prefixes=["docs/specs"],
                write_blocked_prefixes=[".workflow"],
            )

            result = runtime.write_spec_contract(
                {
                    "path": "docs/specs/issue-1/contract.json",
                    "contract": valid_spec_contract(),
                }
            )

            self.assertTrue(result["ok"], result)
            written = spec_agent.read_json(root / "docs/specs/issue-1/contract.json")
            self.assertEqual(written["summary"], "Implement a small feature.")

    def test_write_spec_contract_rejects_non_contract_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agent_runtime.ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
                spec_contract_write_enabled=True,
            )

            with self.assertRaisesRegex(ValueError, "docs/specs/<task-id>/contract.json"):
                runtime.write_spec_contract({"path": "contract.json", "contract": valid_spec_contract()})


def valid_spec_contract() -> dict[str, object]:
    return {
        "summary": "Implement a small feature.",
        "scope": ["Create the requested behavior."],
        "non_goals": ["Do not change unrelated behavior."],
        "acceptance_criteria": ["The behavior works."],
        "test_plan": ["Add an acceptance test."],
        "risks": ["Ambiguous edge cases."],
        "cost_plan": {
            "complexity": "small",
            "max_iterations": 1,
            "model_route": {"agent": "builder"},
        },
        "requires_human_approval": True,
    }


if __name__ == "__main__":
    unittest.main()
