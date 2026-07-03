"""JSON-schema final-report shapes for each loop role."""

from __future__ import annotations

from typing import Any

from hooky.roles.runner import runtime_rel


def planner_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "contract_path", "summary"],
        "properties": {
            "status": {"type": "string", "enum": ["done", "blocked"]},
            "contract_path": {"type": "string", "enum": [runtime_rel("contract.md")]},
            "summary": {"type": "string"},
        },
    }


def generator_contract_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "contract_path", "feature_list_path", "summary"],
        "properties": {
            "status": {"type": "string", "enum": ["done", "blocked"]},
            "contract_path": {"type": "string", "enum": [runtime_rel("contract.md")]},
            "feature_list_path": {"type": "string", "enum": [runtime_rel("feature_list.json")]},
            "summary": {"type": "string"},
        },
    }


def evaluator_contract_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "accepted", "review", "required_changes"],
        "properties": {
            "status": {"type": "string", "enum": ["done", "blocked"]},
            "accepted": {"type": "boolean"},
            "review": {"type": "string"},
            "required_changes": {"type": "array", "items": {"type": "string"}},
        },
    }


def generator_implementation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "summary", "changed_files", "tests_run", "failures"],
        "properties": {
            "status": {"type": "string", "enum": ["done", "blocked"]},
            "summary": {"type": "string"},
            "changed_files": {"type": "array", "items": {"type": "string"}},
            "tests_run": {"type": "array", "items": {"type": "string"}},
            "failures": {"type": "array", "items": {"type": "string"}},
        },
    }


def evaluator_attempt_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "recommendation", "bottleneck", "findings", "score"],
        "properties": {
            "status": {"type": "string", "enum": ["pass", "fail"]},
            "recommendation": {"type": "string", "enum": ["continue", "restart-attempt", "restart-contract", "stop"]},
            "bottleneck": {"type": "string"},
            "findings": {"type": "array", "items": {"type": "string"}},
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "rubric_scores": {
                "type": "object",
                "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "score_explanation": {"type": "string"},
        },
    }
