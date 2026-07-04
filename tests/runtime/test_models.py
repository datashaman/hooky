from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from hooky.runtime import (
    ADVISOR_TOOL_TYPE,
    DEFAULT_ADVISOR_INSTRUCTIONS,
    ModelMessage,
    OllamaChat,
    advisor_tool_definition,
    model_credentials_available,
    model_provider,
    provider_model_name,
)


class ModelsTests(unittest.TestCase):
    def test_model_message_preserves_provider_reasoning_fields(self) -> None:
        message = ModelMessage(
            {
                "role": "assistant",
                "content": "visible",
                "thinking": "private thinking",
                "reasoning": "private reasoning",
            }
        )

        payload = message.model_dump()

        self.assertEqual(payload["content"], "visible")
        self.assertEqual(payload["thinking"], "private thinking")
        self.assertEqual(payload["reasoning"], "private reasoning")

    def test_ollama_chat_sends_think_option(self) -> None:
        class FakeResponse:
            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'

        captured: dict[str, object] = {}

        def fake_urlopen(request: object, timeout: int) -> FakeResponse:
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            OllamaChat("http://localhost:11434").send(
                model="ollama/gpt-oss:20b",
                messages=[],
                think="high",
            )

        self.assertEqual(captured["payload"]["think"], "high")

    def test_model_provider_routes_ollama_and_openrouter_credentials(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(model_provider("ollama/gpt-oss:20b"), "ollama")
            self.assertEqual(provider_model_name("ollama/gpt-oss:20b"), "gpt-oss:20b")
            self.assertTrue(model_credentials_available("ollama/gpt-oss:20b"))
            self.assertFalse(model_credentials_available("openai/gpt-oss-20b"))

    def test_ollama_chat_url_accepts_root_or_v1_base_url(self) -> None:
        self.assertEqual(
            OllamaChat("http://localhost:11434").chat_completions_url(),
            "http://localhost:11434/v1/chat/completions",
        )
        self.assertEqual(
            OllamaChat("http://localhost:11434/v1").chat_completions_url(),
            "http://localhost:11434/v1/chat/completions",
        )

    def test_advisor_tool_definition_is_none_when_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(advisor_tool_definition())

    def test_advisor_tool_definition_uses_default_instructions_when_configured(self) -> None:
        with mock.patch.dict(os.environ, {"HOOKY_ADVISOR_MODEL": "anthropic/claude-opus-latest"}, clear=True):
            definition = advisor_tool_definition()

        self.assertEqual(
            definition,
            {
                "type": ADVISOR_TOOL_TYPE,
                "parameters": {
                    "model": "anthropic/claude-opus-latest",
                    "instructions": DEFAULT_ADVISOR_INSTRUCTIONS,
                },
            },
        )

    def test_advisor_tool_definition_respects_instruction_and_token_overrides(self) -> None:
        env = {
            "HOOKY_ADVISOR_MODEL": "openai/gpt-4o-mini",
            "HOOKY_ADVISOR_INSTRUCTIONS": "Be blunt.",
            "HOOKY_ADVISOR_MAX_COMPLETION_TOKENS": "250",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            definition = advisor_tool_definition()

        self.assertEqual(
            definition,
            {
                "type": ADVISOR_TOOL_TYPE,
                "parameters": {
                    "model": "openai/gpt-4o-mini",
                    "instructions": "Be blunt.",
                    "max_completion_tokens": 250,
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
