from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hooky import roles


class ValidatorsTests(unittest.TestCase):
    def test_generator_contract_write_rejects_truncated_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".hooky/runs/local/contract.md"

            with self.assertRaisesRegex(ValueError, "Done Criteria"):
                roles.validate_generator_contract_write(root, path, "nope")

            roles.validate_generator_contract_write(
                root,
                path,
                "# Loop Contract\n\n## Done Criteria\n\n- A concrete, testable assertion that is long enough to be a real contract.\n",
            )

    def test_generator_contract_write_rejects_invalid_feature_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".hooky/runs/local/feature_list.json"

            with self.assertRaises(ValueError):
                roles.validate_generator_contract_write(root, path, '{"items":[]}')

            roles.validate_generator_contract_write(root, path, '{"features":[]}')

    def test_proposal_checklist_items_extracts_common_markdown_lists(self) -> None:
        proposal = """
# Build something

- [ ] Add items
- [x] Complete items
1. Filter active items
* Persist items
"""

        self.assertEqual(
            roles.proposal_checklist_items(proposal),
            ["Add items", "Complete items", "Filter active items", "Persist items"],
        )

    def test_generator_contract_report_requires_feature_coverage_for_proposal_checklist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loop_dir = root / ".hooky/runs/local"
            loop_dir.mkdir(parents=True)
            (loop_dir / "proposal.md").write_text(
                "- Add todos\n- Complete todos\n- Filter todos\n",
                encoding="utf-8",
            )
            (loop_dir / "contract.md").write_text(
                "# Loop Contract\n\n## Done Criteria\n\n- Add todos\n- Complete todos\n- Filter todos\n",
                encoding="utf-8",
            )
            feature_list = loop_dir / "feature_list.json"
            feature_list.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "features": [
                            {"id": "F001", "text": "Add todos", "status": "pending"},
                            {"id": "F002", "text": "Complete todos", "status": "pending"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "cover every proposal checklist item"):
                roles.validate_generator_contract_report(
                    {
                        "contract_path": ".hooky/runs/local/contract.md",
                        "feature_list_path": ".hooky/runs/local/feature_list.json",
                    },
                    root,
                )

            feature_list.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "features": [
                            {"id": "F001", "text": "Add todos", "status": "pending"},
                            {"id": "F002", "text": "Complete todos", "status": "pending"},
                            {"id": "F003", "text": "Filter todos", "status": "pending"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            roles.validate_generator_contract_report(
                {
                    "contract_path": ".hooky/runs/local/contract.md",
                    "feature_list_path": ".hooky/runs/local/feature_list.json",
                },
                root,
            )

    def test_evaluator_requires_rubric_scores_when_taste_rubric_is_substantive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loop_dir = root / ".hooky/runs/local"
            loop_dir.mkdir(parents=True)
            (loop_dir / "contract.md").write_text(
                "# Loop Contract\n\n"
                "## Done Criteria\n\n- Build a polished dashboard.\n\n"
                "## Taste Rubric\n\n"
                "- design weight 0.35: calm, legible hierarchy\n"
                "- originality weight 0.15: not a generic template\n"
                "- craft weight 0.25: aligned spacing and refined states\n"
                "- functionality weight 0.25: workflows remain clear\n",
                encoding="utf-8",
            )
            base_report = {
                "status": "pass",
                "recommendation": "continue",
                "bottleneck": "taste_calibration",
                "findings": [],
                "score": 0.8,
            }

            with self.assertRaisesRegex(ValueError, "rubric_scores"):
                roles.validate_evaluator_attempt_report(base_report, root)

            roles.validate_evaluator_attempt_report(
                {
                    **base_report,
                    "rubric_scores": {
                        "design": 0.8,
                        "originality": 0.7,
                        "craft": 0.75,
                        "functionality": 0.9,
                    },
                    "score_explanation": "Strong functional fit with adequate polish.",
                },
                root,
            )

    def test_taste_rubric_not_applicable_disclaimer_is_not_substantive_despite_axis_words(self) -> None:
        # "reference" is one of the axis-keyword triggers, but here it appears
        # inside a negation ("no ... reference to grade against") as part of an
        # explicit not-applicable disclaimer for a backend-only task. That must
        # not be misread as a substantive rubric.
        contract = (
            "# Loop Contract\n\n"
            "## Done Criteria\n\n- Add an atomic-write helper.\n\n"
            "## Taste Rubric\n\n"
            "_Not applicable: this proposal is a backend durability/consistency fix "
            "with no visual, UI, or canonical-template reference to grade against._\n"
        )

        self.assertFalse(roles.taste_rubric_is_substantive(contract))

    def test_evaluator_attempt_does_not_require_rubric_scores_for_not_applicable_rubric(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loop_dir = root / ".hooky/runs/local"
            loop_dir.mkdir(parents=True)
            (loop_dir / "contract.md").write_text(
                "# Loop Contract\n\n"
                "## Done Criteria\n\n- Add an atomic-write helper.\n\n"
                "## Taste Rubric\n\n"
                "_Not applicable: this proposal is a backend durability/consistency fix "
                "with no visual, UI, or canonical-template reference to grade against._\n",
                encoding="utf-8",
            )

            roles.validate_evaluator_attempt_report(
                {
                    "status": "pass",
                    "recommendation": "continue",
                    "bottleneck": "none",
                    "findings": [],
                    "score": 0.9,
                },
                root,
            )

    def test_parse_taste_rubric_weights_extracts_axis_weight_pairs(self) -> None:
        contract = (
            "# Loop Contract\n\n"
            "## Done Criteria\n\n- Build a polished dashboard.\n\n"
            "## Taste Rubric\n\n"
            "- design weight 0.35: calm, legible hierarchy\n"
            "- originality weight 0.15: not a generic template\n"
            "- craft weight 0.25: aligned spacing and refined states\n"
            "- functionality weight 0.25: workflows remain clear\n"
        )

        weights = roles.parse_taste_rubric_weights(contract)

        self.assertEqual(weights, {"design": 0.35, "originality": 0.15, "craft": 0.25, "functionality": 0.25})
        self.assertTrue(roles.taste_rubric_weights_are_valid(contract))

    def test_taste_rubric_weights_are_invalid_when_they_do_not_sum_to_one(self) -> None:
        contract = (
            "# Loop Contract\n\n"
            "## Done Criteria\n\n- Build a polished dashboard.\n\n"
            "## Taste Rubric\n\n"
            "- design weight 0.5: calm, legible hierarchy\n"
            "- originality weight 0.1: not a generic template\n"
            "- craft weight 0.1: aligned spacing and refined states\n"
            "- functionality weight 0.1: workflows remain clear\n"
        )

        self.assertFalse(roles.taste_rubric_weights_are_valid(contract))

    def test_taste_rubric_weights_are_invalid_when_an_axis_is_missing(self) -> None:
        contract = (
            "# Loop Contract\n\n"
            "## Done Criteria\n\n- Build a polished dashboard.\n\n"
            "## Taste Rubric\n\n"
            "- design weight 0.5: calm, legible hierarchy\n"
            "- craft weight 0.5: aligned spacing and refined states\n"
        )

        self.assertFalse(roles.taste_rubric_weights_are_valid(contract))

    def test_taste_rubric_weights_are_invalid_when_axis_keywords_present_but_no_weights(self) -> None:
        # taste_rubric_is_substantive would call this rubric substantive (it
        # mentions the axis keywords), but no weights are actually parseable -
        # the stricter scoring gate must not trust it.
        contract = "# Loop Contract\n\n## Done Criteria\n\n- Build a polished dashboard.\n\n## Taste Rubric\n\nGrade design, originality, craft, and functionality holistically.\n"

        self.assertTrue(roles.taste_rubric_is_substantive(contract))
        self.assertFalse(roles.taste_rubric_weights_are_valid(contract))

    def test_evaluator_attempt_requires_non_empty_bottleneck(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hooky/runs/local").mkdir(parents=True)
            (root / ".hooky/runs/local/contract.md").write_text("# Loop Contract\n\n## Done Criteria\n\n- Build it.\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "non-empty bottleneck"):
                roles.validate_evaluator_attempt_report(
                    {
                        "status": "pass",
                        "recommendation": "continue",
                        "bottleneck": "",
                        "findings": [],
                        "score": 1.0,
                    },
                    root,
                )

    def test_taste_rubric_required_detects_subjective_contract_language(self) -> None:
        contract = "# Loop Contract\n\n## Done Criteria\n\n- Build a polished branded interface.\n\n## Taste Rubric\n\n_Optional._\n"

        self.assertTrue(roles.taste_rubric_required(contract))

    def test_reference_visual_rubric_required_detects_todomvc_spec_language(self) -> None:
        proposal = "Build a React/Vite TodoMVC app using todomvc-common and todomvc-app-css. The UI should visually match the canonical TodoMVC template and official CSS."

        self.assertTrue(roles.reference_visual_rubric_required(proposal))

    def test_generator_contract_requires_taste_rubric_for_reference_visual_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loop_dir = root / ".hooky/runs/local"
            loop_dir.mkdir(parents=True)
            (loop_dir / "proposal.md").write_text(
                "Build a TodoMVC app that visually matches the canonical template using todomvc-app-css.\n",
                encoding="utf-8",
            )
            contract = loop_dir / "contract.md"
            contract.write_text(
                "# Loop Contract\n\n## Done Criteria\n\n- UI matches the canonical TodoMVC layout.\n\n## Taste Rubric\n\nOptional when subjective quality matters.\n",
                encoding="utf-8",
            )
            (loop_dir / "feature_list.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "features": [{"id": "F001", "text": "UI matches canonical TodoMVC layout", "status": "pending"}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Taste Rubric"):
                roles.validate_generator_contract_report(
                    {
                        "status": "done",
                        "contract_path": ".hooky/runs/local/contract.md",
                        "feature_list_path": ".hooky/runs/local/feature_list.json",
                        "summary": "done",
                    },
                    root,
                )

            contract.write_text(
                "# Loop Contract\n\n"
                "## Done Criteria\n\n"
                "- UI matches the canonical TodoMVC layout.\n\n"
                "## Taste Rubric\n\n"
                "- design weight 0.35: official TodoMVC CSS layout and spacing match the reference.\n"
                "- originality weight 0.10: restraint and fidelity to the canonical template, not novelty.\n"
                "- craft weight 0.25: canonical DOM/classes allow official CSS to apply across states.\n"
                "- functionality weight 0.30: empty, populated, completed/filter, and editing states remain usable.\n",
                encoding="utf-8",
            )

            roles.validate_generator_contract_report(
                {
                    "status": "done",
                    "contract_path": ".hooky/runs/local/contract.md",
                    "feature_list_path": ".hooky/runs/local/feature_list.json",
                    "summary": "done",
                },
                root,
            )


if __name__ == "__main__":
    unittest.main()
