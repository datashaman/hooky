#!/usr/bin/env python3
"""Check whether a model can use Hooky's agent tools cleanly."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import agent_runtime
import eval_runtime
import eval_spec_agent
import spec_agent
from agent_runtime import AgentRunError, ToolRuntime, build_runtime_metadata, run_tool_agent, write_runtime_log


DEFAULT_RUN_ROOT = Path(".workflow/eval-runs/tool-conformance")
DEFAULT_CACHE_ROOT = Path(".workflow/eval-cache/tool-conformance")
DEFAULT_MODEL = "openai/gpt-oss-20b"
DEFAULT_REASONING_LEVELS = ["low", "medium", "high"]
REQUIRED_TOOLS = ["todo_read", "todo_write", "list_files", "read_file", "final_report"]


def main() -> int:
    args = parse_args()
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required for tool conformance eval")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = args.run_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    cache_root = args.cache_root / eval_cache_key()
    model_metadata = openrouter_model_metadata(args.model)

    attempts = []
    for reasoning in args.reasoning:
        model_info = model_info_for(args.model, model_metadata, reasoning, args.reasoning_exclude)
        attempt = run_attempt(model_info, run_root, cache_root, args.no_cache)
        attempts.append(attempt)
        print_attempt(attempt)

    report_path = run_root / "report.json"
    winner = next((attempt for attempt in attempts if attempt["status"] == "pass"), None)
    eval_runtime.write_report(
        report_path,
        status="pass" if winner else "fail",
        fixture="tool conformance",
        judge_model="deterministic",
        ladder_report=[model_info_for(args.model, model_metadata, reasoning, args.reasoning_exclude) for reasoning in args.reasoning],
        attempts=attempts,
        winner=winner,
        total_cost=eval_spec_agent.sum_costs(attempts),
    )
    print(f"\nreport: {report_path}")
    if winner:
        print(f"passed_with: {eval_runtime.model_display_name(winner)}")
        return 0
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", nargs="+", default=DEFAULT_REASONING_LEVELS)
    parser.add_argument("--reasoning-exclude", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--no-cache", action="store_true")
    return parser.parse_args()


def openrouter_model_metadata(model_id: str) -> dict[str, Any]:
    for model in eval_spec_agent.fetch_openrouter_models({"supported_parameters": ["tools", "tool_choice"], "sort": "pricing-low-to-high"}):
        if model.get("id") == model_id:
            return model
    for model in eval_spec_agent.fetch_openrouter_models({}):
        if model.get("id") == model_id:
            return model
    raise RuntimeError(f"model not returned by OpenRouter /models: {model_id}")


def model_info_for(model_id: str, metadata: dict[str, Any], reasoning: str, exclude: bool) -> dict[str, Any]:
    reasoning_request = None if reasoning == "none" else {"effort": reasoning, "exclude": exclude}
    return {
        "id": model_id,
        "model": model_id,
        "variant_id": model_id + eval_spec_agent.reasoning_variant_suffix(reasoning_request),
        "reasoning_request": reasoning_request,
        "context_length": metadata.get("context_length"),
        "estimated_cost": None,
    }


def run_attempt(model_info: dict[str, Any], run_root: Path, cache_root: Path, no_cache: bool) -> dict[str, Any]:
    model = model_info["id"]
    variant_id = model_info["variant_id"]
    cache_path = cache_root / f"{eval_spec_agent.safe_name(variant_id)}.json"
    if not no_cache and cache_path.exists():
        cached = spec_agent.read_json(cache_path)
        cached["cached"] = True
        cached["original_cost"] = cached.get("cost", 0)
        cached["cost"] = 0
        return cached

    attempt_dir = run_root / eval_spec_agent.safe_name(variant_id)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "sample.txt").write_text("sample workspace file for tool conformance\n", encoding="utf-8")

    previous_model = os.environ.get("OPENROUTER_MODEL")
    previous_reasoning = os.environ.get("OPENROUTER_REASONING")
    previous_context_length = os.environ.get("OPENROUTER_CONTEXT_LENGTH")
    os.environ["OPENROUTER_MODEL"] = model
    eval_spec_agent.apply_reasoning_env(model_info)
    eval_spec_agent.apply_context_length_env(model_info)
    runtime = ToolRuntime(
        working_folder=attempt_dir,
        final_report_schema=final_report_schema(),
        max_cost_usd=float(os.environ.get("TOOL_CONFORMANCE_MAX_COST_USD", "0.03")),
        max_seconds=int(os.environ.get("TOOL_CONFORMANCE_MAX_SECONDS", "90")),
        context_window_tokens=model_info.get("context_length"),
        final_validator=validate_final_report,
        live_log_root=attempt_dir,
        write_enabled=False,
    )
    try:
        result = run_tool_agent(
            model=model,
            system=system_prompt(),
            user=user_prompt(),
            runtime=runtime,
        )
    except AgentRunError as exc:
        result = exc.result
        metadata = build_runtime_metadata("tool-conformance", model, model_info, result, status="error", error=str(exc))
        write_runtime_log(attempt_dir, result.transcript, result.tool_events, result.compaction_events, result.pre_compaction_archives, metadata=metadata)
        attempt = attempt_payload(model_info, attempt_dir, result, f"generation failed: {exc}")
        eval_spec_agent.write_attempt_cache(cache_path, attempt)
        return attempt
    finally:
        eval_spec_agent.restore_model(previous_model)
        eval_spec_agent.restore_reasoning(previous_reasoning)
        eval_spec_agent.restore_context_length(previous_context_length)

    write_runtime_log(
        attempt_dir,
        result.transcript,
        result.tool_events,
        result.compaction_events,
        result.pre_compaction_archives,
        metadata=build_runtime_metadata("tool-conformance", model, model_info, result),
    )
    findings = conformance_findings(result)
    attempt = attempt_payload(model_info, attempt_dir, result, None if not findings else "; ".join(findings))
    eval_spec_agent.write_attempt_cache(cache_path, attempt)
    return attempt


def final_report_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "required": ["status", "observed_tools", "notes"],
        "properties": {
            "status": {"type": "string", "enum": ["pass", "fail"]},
            "observed_tools": spec_agent.string_array_schema(),
            "notes": spec_agent.string_array_schema(),
        },
    }


def validate_final_report(report: dict[str, Any]) -> None:
    if report.get("status") not in {"pass", "fail"}:
        raise ValueError("status must be pass or fail")
    for field in ("observed_tools", "notes"):
        if not isinstance(report.get(field), list):
            raise ValueError(f"{field} must be a list")


def system_prompt() -> str:
    return (
        "You are testing tool-call conformance. Use the provided tools exactly as named. "
        "Do not invent channels, wrappers, namespaces, or alternate tool names. "
        "For tools with no parameters, pass an empty JSON object."
    )


def user_prompt() -> str:
    return """Complete this exact sequence:
1. Call todo_read with {}.
2. Call todo_write with one active item named "inspect workspace".
3. Call list_files for ".".
4. Call read_file for "sample.txt".
5. Finish with final_report status "pass" and observed_tools listing the tools you used.
"""


def conformance_findings(result: agent_runtime.AgentRunResult) -> list[str]:
    findings: list[str] = []
    tool_names = [str(event.get("name") or "") for event in result.tool_events]
    valid_tools = set(agent_runtime.available_tool_names()) | {"final_report"}
    for name in tool_names:
        if name not in valid_tools:
            findings.append(f"unknown tool name: {name}")
        if "<|" in name or "|>" in name or "channel" in name:
            findings.append(f"harmony token leaked into tool name: {name}")
    for event in result.tool_events:
        result_payload = event.get("result") if isinstance(event.get("result"), dict) else {}
        error = str(result_payload.get("error") or "")
        if result_payload.get("ok") is False and error:
            findings.append(f"tool error for {event.get('name')}: {error}")
        if "invalid JSON tool arguments" in error:
            findings.append(f"invalid JSON arguments for {event.get('name')}")
        if "unknown tool" in error:
            findings.append(f"unknown tool error for {event.get('name')}")
        arguments_text = json.dumps(event.get("arguments", {}), sort_keys=True)
        if "<|" in arguments_text or "|>" in arguments_text:
            findings.append(f"harmony token leaked into arguments for {event.get('name')}")
        if event.get("name") != "final_report" and isinstance(event.get("arguments"), dict) and event["arguments"].get("ok") is True:
            findings.append(f"tool-result-shaped arguments for {event.get('name')}")
    for required in REQUIRED_TOOLS:
        if required not in tool_names:
            findings.append(f"missing required tool call: {required}")
    if result.final_report is None:
        findings.append("missing final_report")
    elif result.final_report.get("status") != "pass":
        findings.append("final_report status is not pass")
    return sorted(set(findings))


def attempt_payload(
    model_info: dict[str, Any],
    attempt_dir: Path,
    result: agent_runtime.AgentRunResult,
    failure_summary: str | None,
) -> dict[str, Any]:
    findings = conformance_findings(result)
    if failure_summary and not findings:
        findings.append(failure_summary)
    status = "pass" if not findings else "fail"
    return {
        "model": model_info["id"],
        "variant_id": model_info["variant_id"],
        "reasoning_request": model_info.get("reasoning_request"),
        "context_length": model_info.get("context_length"),
        "status": status,
        "artifact_dir": str(attempt_dir),
        "cached": False,
        "cost": float(result.usage.get("cost") or 0),
        "usage": {"tool_conformance": result.usage},
        "tool_use": eval_spec_agent.tool_use_summary(attempt_dir),
        "deterministic": {"status": status, "findings": findings},
        "final_report": result.final_report,
    }


def print_attempt(attempt: dict[str, Any]) -> None:
    print(f"\nmodel: {attempt['model']}")
    if attempt.get("variant_id") and attempt["variant_id"] != attempt["model"]:
        print(f"variant: {attempt['variant_id']}")
    if attempt.get("reasoning_request"):
        print(f"reasoning: {json.dumps(attempt['reasoning_request'], sort_keys=True)}")
    print(f"status: {attempt['status']}")
    print(f"cached: {attempt.get('cached', False)}")
    if attempt.get("cached"):
        print(f"original_cost: {attempt.get('original_cost', 0)}")
    else:
        print(f"actual_cost: {attempt.get('cost', 0)}")
    print(f"tool_use: {json.dumps(attempt.get('tool_use', {}), sort_keys=True)}")
    findings = attempt.get("deterministic", {}).get("findings", [])
    if findings:
        print("findings:")
        for finding in findings:
            print(f"- {finding}")


def eval_cache_key() -> str:
    return eval_runtime.cache_key(
        [
            Path("scripts/eval_tool_conformance.py"),
            Path("scripts/agent_runtime.py"),
            Path("scripts/spec_agent.py"),
        ],
        "tool-conformance-v1",
    )


if __name__ == "__main__":
    raise SystemExit(main())
