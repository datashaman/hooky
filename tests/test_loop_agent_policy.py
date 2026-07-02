from __future__ import annotations

import sys
import tempfile
import unittest
import os
import json
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import loop_agent  # noqa: E402


class LoopAgentPolicyTests(unittest.TestCase):
    def test_generator_contract_write_rejects_truncated_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".hooky/runs/local/contract.md"

            with self.assertRaisesRegex(ValueError, "Done Criteria"):
                loop_agent.validate_generator_contract_write(root, path, "nope")

            loop_agent.validate_generator_contract_write(
                root,
                path,
                "# Loop Contract\n\n## Done Criteria\n\n- A concrete, testable assertion that is long enough to be a real contract.\n",
            )

    def test_generator_contract_write_rejects_invalid_feature_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".hooky/runs/local/feature_list.json"

            with self.assertRaises(ValueError):
                loop_agent.validate_generator_contract_write(root, path, '{"items":[]}')

            loop_agent.validate_generator_contract_write(root, path, '{"features":[]}')

    def test_proposal_checklist_items_extracts_common_markdown_lists(self) -> None:
        proposal = """
# Build something

- [ ] Add items
- [x] Complete items
1. Filter active items
* Persist items
"""

        self.assertEqual(
            loop_agent.proposal_checklist_items(proposal),
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
                loop_agent.validate_generator_contract_report(
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

            loop_agent.validate_generator_contract_report(
                {
                    "contract_path": ".hooky/runs/local/contract.md",
                    "feature_list_path": ".hooky/runs/local/feature_list.json",
                },
                root,
            )

    def test_contract_prompts_expose_original_proposal_and_coverage_rule(self) -> None:
        generator_prompt = loop_agent.generator_contract_user_prompt(
            "contract",
            "- Add todos\n- Complete todos",
            {"model": "test"},
        )
        evaluator_prompt = loop_agent.evaluator_contract_user_prompt(
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
        system = loop_agent.evaluator_attempt_system_prompt()
        user = loop_agent.evaluator_attempt_user_prompt(
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

    def test_evaluator_attempt_uses_multimodal_selected_model_over_global_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            selected = Path(tmp) / "selected_model.json"
            selected.write_text(
                "{"
                '"model":"openai/gpt-4.1-mini",'
                '"variant_id":"openai/gpt-4.1-mini",'
                '"source":"manual-multimodal-required",'
                '"updated_at":"2026-06-30T00:00:00+00:00",'
                '"reasoning_request":null,'
                '"context_length":1047576'
                "}\n",
                encoding="utf-8",
            )

            with (
                mock.patch.object(loop_agent, "EVALUATOR_SELECTED_MODEL_PATH", selected),
                mock.patch.dict(os.environ, {"OPENROUTER_MODEL": "openai/gpt-oss-20b"}, clear=False),
            ):
                self.assertEqual(loop_agent.selected_evaluator_attempt_model(), "openai/gpt-4.1-mini")
                metadata = loop_agent.selected_evaluator_attempt_model_metadata()

            self.assertEqual(metadata["model"], "openai/gpt-4.1-mini")
            self.assertEqual(metadata["source"], "manual-multimodal-required")

    def test_main_default_model_is_gpt_oss_20b(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(loop_agent, "SELECTED_MODEL_PATH", Path("/tmp/does-not-exist-hooky-model.json")),
        ):
            self.assertEqual(loop_agent.selected_model(), "openai/gpt-oss-20b")
            metadata = loop_agent.selected_model_metadata()

        self.assertEqual(metadata["model"], "openai/gpt-oss-20b")
        self.assertEqual(metadata["source"], "fallback")

    def test_loop_evaluator_model_env_overrides_multimodal_default(self) -> None:
        with mock.patch.dict(os.environ, {"LOOP_EVALUATOR_MODEL": "openai/gpt-4.1"}, clear=False):
            self.assertEqual(loop_agent.selected_evaluator_attempt_model(), "openai/gpt-4.1")
            metadata = loop_agent.selected_evaluator_attempt_model_metadata()

        self.assertEqual(metadata["model"], "openai/gpt-4.1")
        self.assertEqual(metadata["source"], "LOOP_EVALUATOR_MODEL")

    def test_ollama_model_env_overrides_main_loop_model(self) -> None:
        with (
            mock.patch.dict(os.environ, {"OLLAMA_MODEL": "gpt-oss:20b"}, clear=True),
            mock.patch.object(loop_agent, "SELECTED_MODEL_PATH", Path("/tmp/does-not-exist-hooky-model.json")),
        ):
            self.assertEqual(loop_agent.selected_model(), "ollama/gpt-oss:20b")
            metadata = loop_agent.selected_model_metadata()

        self.assertEqual(metadata["model"], "ollama/gpt-oss:20b")
        self.assertEqual(metadata["source"], "OLLAMA_MODEL")
        self.assertEqual(metadata["base_url"], "http://localhost:11434")

    def test_ollama_think_env_is_recorded_in_selected_model_metadata(self) -> None:
        with (
            mock.patch.dict(os.environ, {"OLLAMA_MODEL": "gpt-oss:20b", "OLLAMA_THINK": "high"}, clear=True),
            mock.patch.object(loop_agent, "SELECTED_MODEL_PATH", Path("/tmp/does-not-exist-hooky-model.json")),
        ):
            metadata = loop_agent.selected_model_metadata()

        self.assertEqual(metadata["model"], "ollama/gpt-oss:20b")
        self.assertEqual(metadata["reasoning_request"], {"think": "high"})

    def test_ollama_main_model_does_not_override_multimodal_evaluator_default(self) -> None:
        with (
            mock.patch.dict(os.environ, {"OLLAMA_MODEL": "gpt-oss:20b"}, clear=True),
            mock.patch.object(loop_agent, "EVALUATOR_SELECTED_MODEL_PATH", Path("/tmp/does-not-exist-hooky-evaluator-model.json")),
        ):
            self.assertEqual(loop_agent.selected_evaluator_attempt_model(), "openai/gpt-4.1-mini")

    def test_openrouter_reasoning_env_is_recorded_in_selected_model_metadata(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"OPENROUTER_MODEL": "openai/gpt-oss-20b", "OPENROUTER_REASONING": '{"effort":"high"}'},
            clear=False,
        ):
            metadata = loop_agent.selected_model_metadata()

        self.assertEqual(metadata["model"], "openai/gpt-oss-20b")
        self.assertEqual(metadata["reasoning_request"], {"effort": "high"})

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
                loop_agent.validate_evaluator_attempt_report(base_report, root)

            loop_agent.validate_evaluator_attempt_report(
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

    def test_evaluator_attempt_requires_non_empty_bottleneck(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hooky/runs/local").mkdir(parents=True)
            (root / ".hooky/runs/local/contract.md").write_text("# Loop Contract\n\n## Done Criteria\n\n- Build it.\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "non-empty bottleneck"):
                loop_agent.validate_evaluator_attempt_report(
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

        self.assertTrue(loop_agent.taste_rubric_required(contract))

    def test_reference_visual_rubric_required_detects_todomvc_spec_language(self) -> None:
        proposal = (
            "Build a React/Vite TodoMVC app using todomvc-common and todomvc-app-css. "
            "The UI should visually match the canonical TodoMVC template and official CSS."
        )

        self.assertTrue(loop_agent.reference_visual_rubric_required(proposal))

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
                "# Loop Contract\n\n"
                "## Done Criteria\n\n"
                "- UI matches the canonical TodoMVC layout.\n\n"
                "## Taste Rubric\n\n"
                "Optional when subjective quality matters.\n",
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
                loop_agent.validate_generator_contract_report(
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

            loop_agent.validate_generator_contract_report(
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
