from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
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


if __name__ == "__main__":
    unittest.main()
