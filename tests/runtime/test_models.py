from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from hooky.runtime import (
    ModelMessage,
    OllamaChat,
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


if __name__ == "__main__":
    unittest.main()
