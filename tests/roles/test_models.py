from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hooky import roles
from hooky.roles import models as roles_models


class ModelsTests(unittest.TestCase):
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
                mock.patch.object(roles_models, "EVALUATOR_SELECTED_MODEL_PATH", selected),
                mock.patch.dict(os.environ, {"OPENROUTER_MODEL": "openai/gpt-oss-20b"}, clear=False),
            ):
                self.assertEqual(roles.selected_evaluator_attempt_model(), "openai/gpt-4.1-mini")
                metadata = roles.selected_evaluator_attempt_model_metadata()

            self.assertEqual(metadata["model"], "openai/gpt-4.1-mini")
            self.assertEqual(metadata["source"], "manual-multimodal-required")

    def test_main_default_model_is_gpt_oss_20b(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(roles_models, "SELECTED_MODEL_PATH", Path("/tmp/does-not-exist-hooky-model.json")),
        ):
            self.assertEqual(roles.selected_model(), "openai/gpt-oss-20b")
            metadata = roles.selected_model_metadata()

        self.assertEqual(metadata["model"], "openai/gpt-oss-20b")
        self.assertEqual(metadata["source"], "fallback")

    def test_loop_evaluator_model_env_overrides_multimodal_default(self) -> None:
        with mock.patch.dict(os.environ, {"LOOP_EVALUATOR_MODEL": "openai/gpt-4.1"}, clear=False):
            self.assertEqual(roles.selected_evaluator_attempt_model(), "openai/gpt-4.1")
            metadata = roles.selected_evaluator_attempt_model_metadata()

        self.assertEqual(metadata["model"], "openai/gpt-4.1")
        self.assertEqual(metadata["source"], "LOOP_EVALUATOR_MODEL")

    def test_ollama_model_env_overrides_main_loop_model(self) -> None:
        with (
            mock.patch.dict(os.environ, {"OLLAMA_MODEL": "gpt-oss:20b"}, clear=True),
            mock.patch.object(roles_models, "SELECTED_MODEL_PATH", Path("/tmp/does-not-exist-hooky-model.json")),
        ):
            self.assertEqual(roles.selected_model(), "ollama/gpt-oss:20b")
            metadata = roles.selected_model_metadata()

        self.assertEqual(metadata["model"], "ollama/gpt-oss:20b")
        self.assertEqual(metadata["source"], "OLLAMA_MODEL")
        self.assertEqual(metadata["base_url"], "http://localhost:11434")

    def test_ollama_think_env_is_recorded_in_selected_model_metadata(self) -> None:
        with (
            mock.patch.dict(os.environ, {"OLLAMA_MODEL": "gpt-oss:20b", "OLLAMA_THINK": "high"}, clear=True),
            mock.patch.object(roles_models, "SELECTED_MODEL_PATH", Path("/tmp/does-not-exist-hooky-model.json")),
        ):
            metadata = roles.selected_model_metadata()

        self.assertEqual(metadata["model"], "ollama/gpt-oss:20b")
        self.assertEqual(metadata["reasoning_request"], {"think": "high"})

    def test_ollama_main_model_does_not_override_multimodal_evaluator_default(self) -> None:
        with (
            mock.patch.dict(os.environ, {"OLLAMA_MODEL": "gpt-oss:20b"}, clear=True),
            mock.patch.object(roles_models, "EVALUATOR_SELECTED_MODEL_PATH", Path("/tmp/does-not-exist-hooky-evaluator-model.json")),
        ):
            self.assertEqual(roles.selected_evaluator_attempt_model(), "openai/gpt-4.1-mini")

    def test_openrouter_reasoning_env_is_recorded_in_selected_model_metadata(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"OPENROUTER_MODEL": "openai/gpt-oss-20b", "OPENROUTER_REASONING": '{"effort":"high"}'},
            clear=False,
        ):
            metadata = roles.selected_model_metadata()

        self.assertEqual(metadata["model"], "openai/gpt-oss-20b")
        self.assertEqual(metadata["reasoning_request"], {"effort": "high"})


if __name__ == "__main__":
    unittest.main()
