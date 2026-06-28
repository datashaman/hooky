#!/usr/bin/env python3
"""Shared helpers for model-ladder eval scripts."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def model_display_name(model_info: dict[str, Any]) -> str:
    return str(model_info.get("variant_id") or model_info.get("id") or model_info["model"])


def write_report(
    report_path: Path,
    *,
    status: str,
    fixture: Any,
    judge_model: str,
    ladder_report: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    winner: dict[str, Any] | None,
    total_cost: float,
) -> None:
    report = {
        "status": status,
        "fixture": fixture,
        "judge_model": judge_model,
        "winner_model": model_display_name(winner) if winner else None,
        "model_ladder": ladder_report,
        "total_cost": total_cost,
        "attempts": attempts,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def report_status(winner: dict[str, Any] | None, final: bool) -> str:
    return "pass" if winner else "fail" if final else "running"


def cache_key(paths: list[Path], *extra_values: str) -> str:
    digest = hashlib.sha256()
    all_paths = [Path(__file__), *paths]
    for path in all_paths:
        if not path.exists():
            continue
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    for value in extra_values:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:24]


def load_cached_attempt(
    cache_path: Path,
    *,
    model_info: dict[str, Any],
    tool_use_factory: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    artifact_policy_factory: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not cache_path.exists():
        return None
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    cached["cached"] = True
    cached["original_cost"] = cached.get("cost", 0)
    cached["cost"] = 0
    cached["estimated_cost"] = model_info.get("estimated_cost")
    if tool_use_factory:
        cached.setdefault("tool_use", tool_use_factory(cached))
    if artifact_policy_factory:
        cached.setdefault("artifact_policy", artifact_policy_factory(cached))
    return cached


def selected_model_payload(winner: dict[str, Any], report_path: Path, judge_model: str) -> dict[str, Any]:
    payload = {
        "model": winner["model"],
        "variant_id": winner.get("variant_id", winner["model"]),
        "reasoning_request": winner.get("reasoning_request"),
        "context_length": winner.get("context_length"),
        "source": "eval",
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "report": report_path.as_posix(),
        "judge_model": judge_model,
        "estimated_cost": winner.get("estimated_cost"),
        "actual_cost": winner.get("original_cost", winner.get("cost")),
        "cached": winner.get("cached", False),
    }
    validate_selected_model(payload)
    return payload


def write_selected_model(path: Path, winner: dict[str, Any], report_path: Path, judge_model: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(selected_model_payload(winner, report_path, judge_model), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_selected_model(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_selected_model(payload)
    return payload


def validate_selected_model(payload: dict[str, Any]) -> None:
    required = ["model", "variant_id", "source", "updated_at"]
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError(f"selected model missing required fields: {', '.join(missing)}")
    string_fields = ["model", "variant_id", "source", "updated_at"]
    for field in string_fields:
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise ValueError(f"selected model field must be a non-empty string: {field}")
    nullable_object_fields = ["reasoning_request"]
    for field in nullable_object_fields:
        if payload.get(field) is not None and not isinstance(payload[field], dict):
            raise ValueError(f"selected model field must be an object or null: {field}")
    if payload.get("context_length") is not None and not isinstance(payload["context_length"], int):
        raise ValueError("selected model context_length must be an integer or null")
    if payload.get("report") is not None and not isinstance(payload["report"], str):
        raise ValueError("selected model report must be a string or null")
    if payload.get("judge_model") is not None and not isinstance(payload["judge_model"], str):
        raise ValueError("selected model judge_model must be a string or null")
    for field in ("estimated_cost", "actual_cost"):
        if payload.get(field) is not None and not isinstance(payload[field], int | float):
            raise ValueError(f"selected model {field} must be numeric or null")
    if payload.get("cached") is not None and not isinstance(payload["cached"], bool):
        raise ValueError("selected model cached must be boolean or null")
