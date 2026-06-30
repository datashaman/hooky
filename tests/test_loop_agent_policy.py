from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import loop_agent  # noqa: E402


class LoopAgentPolicyTests(unittest.TestCase):
    def test_evaluator_stop_is_reserved_for_automation_blockers(self) -> None:
        system = loop_agent.evaluator_attempt_system_prompt()
        user = loop_agent.evaluator_attempt_user_prompt(
            "contract",
            '{"features":[]}',
            "001",
            {"model": "test"},
        )

        self.assertIn("Do not recommend stop merely because one or more acceptance tests fail", system)
        self.assertIn("stop only when automation is genuinely blocked", system)
        self.assertIn("If tests fail", user)
        self.assertIn("continue or restart-attempt", user)


if __name__ == "__main__":
    unittest.main()
