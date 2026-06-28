from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
