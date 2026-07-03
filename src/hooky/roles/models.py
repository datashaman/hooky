"""Model selection for loop roles (generator/planner vs evaluator, env overrides)."""

from __future__ import annotations

import json
import os

from pathlib import Path
from typing import Any

from hooky import loop_executor
from hooky import runtime

SELECTED_MODEL_PATH = Path(".hooky/models/generator.json")
EVALUATOR_SELECTED_MODEL_PATH = Path(".hooky/models/evaluator.json")
DEFAULT_MODEL = "openai/gpt-oss-20b"


def read_selected_model(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError(f"selected model file must contain a non-empty model string: {path}")
    return payload


def selected_model() -> str:
    env_model = os.environ.get("OPENROUTER_MODEL")
    if env_model:
        return env_model
    ollama_model = os.environ.get("OLLAMA_MODEL")
    if ollama_model:
        return "ollama/" + ollama_model.removeprefix("ollama/")
    if SELECTED_MODEL_PATH.exists():
        data = read_selected_model(SELECTED_MODEL_PATH)
        model = data.get("model")
        if isinstance(model, str) and model:
            return model
    return DEFAULT_MODEL


def selected_model_metadata() -> dict[str, Any]:
    env_model = os.environ.get("OPENROUTER_MODEL")
    if env_model:
        return {"model": env_model, "source": "OPENROUTER_MODEL", "reasoning_request": env_reasoning_request()}
    ollama_model = os.environ.get("OLLAMA_MODEL")
    if ollama_model:
        return {
            "model": "ollama/" + ollama_model.removeprefix("ollama/"),
            "source": "OLLAMA_MODEL",
            "base_url": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            "reasoning_request": env_ollama_reasoning_request(),
        }
    if SELECTED_MODEL_PATH.exists():
        return read_selected_model(SELECTED_MODEL_PATH)
    return {"model": DEFAULT_MODEL, "source": "fallback"}


def selected_evaluator_attempt_model() -> str:
    env_model = os.environ.get("LOOP_EVALUATOR_MODEL")
    if env_model:
        return env_model
    if EVALUATOR_SELECTED_MODEL_PATH.exists():
        data = read_selected_model(EVALUATOR_SELECTED_MODEL_PATH)
        model = data.get("model")
        if isinstance(model, str) and model:
            return model
    return "openai/gpt-4.1-mini"


def selected_evaluator_attempt_model_metadata() -> dict[str, Any]:
    env_model = os.environ.get("LOOP_EVALUATOR_MODEL")
    if env_model:
        return {"model": env_model, "variant_id": env_model, "source": "LOOP_EVALUATOR_MODEL", "reasoning_request": env_reasoning_request()}
    if EVALUATOR_SELECTED_MODEL_PATH.exists():
        return read_selected_model(EVALUATOR_SELECTED_MODEL_PATH)
    return {
        "model": "openai/gpt-4.1-mini",
        "variant_id": "openai/gpt-4.1-mini",
        "source": "fallback-multimodal-required",
        "reasoning_request": None,
    }


def env_reasoning_request() -> dict[str, Any] | None:
    raw = os.environ.get("OPENROUTER_REASONING")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"invalid": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def env_ollama_reasoning_request() -> dict[str, Any] | None:
    raw = os.environ.get("OLLAMA_THINK")
    if not raw:
        return None
    return {"think": raw}


def ensure_model_available(model: str, role_name: str) -> None:
    if loop_executor.selected_executor() != "native":
        return
    if not runtime.model_credentials_available(model):
        raise RuntimeError(runtime.model_credentials_error(model, role_name))
