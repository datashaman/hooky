"""Model client construction, credentials, and request options."""

from __future__ import annotations

import json
import os
import signal
import urllib.error
import urllib.request

from pathlib import Path
from typing import Any

DEFAULT_RUNTIME_DIR = ".hooky/runs/local"
RUNTIME_DIR_ENV = "HOOKY_RUN_DIR"


def runtime_dir() -> str:
    raw = os.environ.get(RUNTIME_DIR_ENV, DEFAULT_RUNTIME_DIR).strip().strip("/")
    return raw or DEFAULT_RUNTIME_DIR


def runtime_path(root: Path, *parts: str) -> Path:
    return root / runtime_dir() / Path(*parts)


def openrouter_request_options() -> dict[str, Any]:
    options: dict[str, Any] = {"provider": {"require_parameters": True}}
    reasoning = os.environ.get("OPENROUTER_REASONING")
    if reasoning:
        options["reasoning"] = json.loads(reasoning)
    return options


def model_provider(model: str) -> str:
    return "ollama" if model.startswith("ollama/") else "openrouter"


def provider_model_name(model: str) -> str:
    return model.removeprefix("ollama/")


def model_request_options(model: str) -> dict[str, Any]:
    if model_provider(model) == "ollama":
        return ollama_request_options()
    return openrouter_request_options()


def ollama_request_options() -> dict[str, Any]:
    options: dict[str, Any] = {}
    reasoning = os.environ.get("OLLAMA_THINK")
    if reasoning:
        options["think"] = reasoning
    return options


class ModelFunctionCall:
    def __init__(self, payload: dict[str, Any]):
        self.name = str(payload.get("name") or "")
        self.arguments = payload.get("arguments") if isinstance(payload.get("arguments"), str) else json.dumps(payload.get("arguments") or {})

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {"name": self.name, "arguments": self.arguments}


class ModelToolCall:
    def __init__(self, payload: dict[str, Any], index: int):
        self.id = str(payload.get("id") or f"tool-{index}")
        self.type = str(payload.get("type") or "function")
        function_payload = payload.get("function") if isinstance(payload.get("function"), dict) else {}
        self.function = ModelFunctionCall(function_payload)

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "function": self.function.model_dump()}


class ModelMessage:
    def __init__(self, payload: dict[str, Any]):
        self.role = str(payload.get("role") or "assistant")
        self.content = payload.get("content")
        self.extra = {
            key: value
            for key, value in payload.items()
            if key not in {"role", "content", "tool_calls"} and value is not None
        }
        tool_calls = payload.get("tool_calls") if isinstance(payload.get("tool_calls"), list) else []
        self.tool_calls = [ModelToolCall(item, index) for index, item in enumerate(tool_calls, 1) if isinstance(item, dict)]

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            payload["content"] = self.content
        if self.tool_calls:
            payload["tool_calls"] = [call.model_dump() for call in self.tool_calls]
        payload.update(self.extra)
        return payload


class ModelChoice:
    def __init__(self, payload: dict[str, Any]):
        message_payload = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        self.message = ModelMessage(message_payload)


class ModelCompletion:
    def __init__(self, payload: dict[str, Any]):
        choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
        self.choices = [ModelChoice(item) for item in choices if isinstance(item, dict)]
        self.usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        if not self.choices:
            self.choices = [ModelChoice({"message": {"role": "assistant", "content": ""}})]


class OllamaChat:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def chat_completions_url(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    def send(self, **kwargs: Any) -> ModelCompletion:
        model = provider_model_name(str(kwargs["model"]))
        payload: dict[str, Any] = {
            "model": model,
            "messages": kwargs.get("messages") or [],
            "stream": False,
        }
        if kwargs.get("tools"):
            payload["tools"] = kwargs["tools"]
        if kwargs.get("tool_choice"):
            payload["tool_choice"] = kwargs["tool_choice"]
        if kwargs.get("response_format"):
            payload["response_format"] = kwargs["response_format"]
        if kwargs.get("think") is not None:
            payload["think"] = kwargs["think"]
        for option in ("max_tokens", "temperature", "top_p", "seed", "stop"):
            if kwargs.get(option) is not None:
                payload[option] = kwargs[option]
        request = urllib.request.Request(
            self.chat_completions_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = max(1, openrouter_timeout_ms() // 1000)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama chat request failed HTTP {exc.code}: {detail[:2000]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama chat request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Ollama chat request timed out after {timeout}s") from exc
        return ModelCompletion(data)


class OllamaClient:
    def __init__(self, base_url: str):
        self.chat = OllamaChat(base_url)

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def model_client(model: str) -> Any:
    if model_provider(model) == "ollama":
        return OllamaClient(os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"))
    from openrouter import OpenRouter

    return OpenRouter(api_key=os.environ["OPENROUTER_API_KEY"], timeout_ms=openrouter_timeout_ms())


def model_credentials_available(model: str) -> bool:
    if model_provider(model) == "ollama":
        return True
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def model_credentials_error(model: str, role_name: str) -> str:
    if model_provider(model) == "ollama":
        return f"Ollama is required for {role_name}; ensure `ollama serve` is running and OLLAMA_MODEL is installed"
    return f"OPENROUTER_API_KEY is required; {role_name} has no non-AI path"


def response_usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return usage.model_dump(exclude_none=True)
    if isinstance(usage, dict):
        return {key: value for key, value in usage.items() if value is not None}
    return {
        key: value
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost")
        if (value := getattr(usage, key, None)) is not None
    }


def openrouter_timeout_ms() -> int:
    return int(os.environ.get("OPENROUTER_TIMEOUT_MS", "120000"))


class LocalDeadline:
    def __init__(self, seconds: int, message: str) -> None:
        self.seconds = seconds
        self.message = message
        self.previous_handler: Any = None
        self.previous_timer: tuple[float, float] = (0.0, 0.0)

    def __enter__(self) -> "LocalDeadline":
        self.previous_handler = signal.getsignal(signal.SIGALRM)
        self.previous_timer = signal.getitimer(signal.ITIMER_REAL)
        signal.signal(signal.SIGALRM, self._raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.previous_handler)
        if self.previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *self.previous_timer)

    def _raise_timeout(self, _signum: int, _frame: Any) -> None:
        raise TimeoutError(self.message)


def relative_or_name(path: Path) -> str:
    return path.as_posix()

