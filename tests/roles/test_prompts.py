from __future__ import annotations

import unittest

from hooky import roles


class PromptsTests(unittest.TestCase):
    def test_contract_prompts_expose_original_proposal_and_coverage_rule(self) -> None:
        generator_prompt = roles.generator_contract_user_prompt(
            "contract",
            "- Add todos\n- Complete todos",
            {"model": "test"},
        )
        evaluator_prompt = roles.evaluator_contract_user_prompt(
            "contract",
            '{"features":[]}',
            "- Add todos\n- Complete todos",
            {"model": "test"},
        )

        self.assertIn("Original proposal artifact", generator_prompt)
        self.assertIn("Every proposal checklist item", generator_prompt)
        self.assertIn("proposal_refs", generator_prompt)
        self.assertIn("Original proposal artifact", evaluator_prompt)
        self.assertIn("reject unless every item is covered", evaluator_prompt)

    def test_evaluator_stop_is_reserved_for_automation_blockers(self) -> None:
        system = roles.evaluator_attempt_system_prompt()
        user = roles.evaluator_attempt_user_prompt(
            "contract",
            '{"features":[]}',
            "001",
            {"model": "test"},
        )

        self.assertIn("Do not recommend stop merely because one or more acceptance tests fail", system)
        self.assertIn("stop only when automation is genuinely blocked", system)
        self.assertIn("returned url/ports as authoritative", system)
        self.assertIn("If tests fail", user)
        self.assertIn("continue or restart-attempt", user)
        self.assertIn("pass the returned url directly to capture_visual_snapshot", user)


if __name__ == "__main__":
    unittest.main()
