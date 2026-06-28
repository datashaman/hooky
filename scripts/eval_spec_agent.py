#!/usr/bin/env python3
"""Run Spec Agent against fixtures and climb a model ladder until eval passes."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import artifact_policy
import eval_runtime
import spec_agent


DEFAULT_FIXTURE = Path("tests/fixtures/spec_agent/moderately_complex.json")
DEFAULT_LADDER = Path(".workflow/model_ladder.json")
DEFAULT_RUN_ROOT = Path(".workflow/eval-runs")
DEFAULT_CACHE_ROOT = Path(".workflow/eval-cache/spec-agent")
REASONING_EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def main() -> int:
    args = parse_args()
    fixture = read_json(args.fixture)
    ladder_config = read_json(args.model_ladder)
    judge_model = args.judge_model or ladder_config["judge"]

    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required for Spec Agent eval")

    ladder_report = resolve_model_ladder(args, ladder_config, fixture, agent_key="spec_agent")
    runnable_ladder = [item for item in ladder_report if item["available"] and item["estimated_cost"] is not None]
    if not runnable_ladder:
        raise RuntimeError("No runnable candidate models available from OpenRouter /models")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = args.run_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    cache_key = eval_cache_key(args.fixture, judge_model)
    cache_root = args.cache_root / cache_key

    print_ladder_summary(runnable_ladder)

    attempts = []
    winner = None
    report_path = run_root / "report.json"
    for model_info in runnable_ladder:
        attempt = run_attempt(fixture, model_info, judge_model, run_root, cache_root, args.no_cache)
        attempts.append(attempt)
        print_attempt(attempt)
        write_report(report_path, fixture, judge_model, ladder_report, attempts, winner, final=False)
        if attempt["status"] == "pass":
            winner = attempt
            write_report(report_path, fixture, judge_model, ladder_report, attempts, winner, final=True)
            if not args.no_update_selected_model:
                update_selected_model(winner, report_path, judge_model)
            break

    write_report(report_path, fixture, judge_model, ladder_report, attempts, winner, final=True)
    eval_runtime.update_report_index(
        "spec-agent",
        report_path,
        status=eval_runtime.report_status(winner, final=True),
        winner=winner,
        attempts=attempts,
        total_cost=sum_costs(attempts),
    )
    print(f"\nreport: {report_path}")
    cleanup_runs(args.run_root, args.keep_runs)
    if winner:
        print(f"passed_with: {model_display_name(winner)}")
        return 0
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--model-ladder", type=Path, default=DEFAULT_LADDER)
    parser.add_argument("--profile", help="Model ladder profile from .workflow/model_ladder.json")
    parser.add_argument("--models", nargs="+", help="Override spec model ladder")
    parser.add_argument("--max-models", type=int, help="Override max models to try from the API-priced ladder")
    parser.add_argument("--print-ladder", action="store_true", help="Print API-priced model ladder and exit")
    parser.add_argument("--judge-model", help="Override judge model")
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--no-cache", action="store_true", help="Ignore cached model attempts and call OpenRouter again")
    parser.add_argument("--no-update-selected-model", action="store_true", help="Do not persist the passing model as the selected Spec Agent model")
    parser.add_argument("--keep-runs", type=int, default=10)
    return parser.parse_args()


def print_ladder_summary(ladder: list[dict[str, Any]]) -> None:
    print("planned_ladder:")
    for index, item in enumerate(ladder, 1):
        print(f"{index}. estimated={item['estimated_cost']} model={model_display_name(item)}")


def model_display_name(model_info: dict[str, Any]) -> str:
    return eval_runtime.model_display_name(model_info)


def write_report(
    report_path: Path,
    fixture: dict[str, Any],
    judge_model: str,
    ladder_report: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    winner: dict[str, Any] | None,
    final: bool,
) -> None:
    eval_runtime.write_report(
        report_path,
        status=eval_runtime.report_status(winner, final),
        fixture=fixture["name"],
        judge_model=judge_model,
        ladder_report=ladder_report,
        attempts=attempts,
        winner=winner,
        total_cost=sum_costs(attempts),
    )


def resolve_model_ladder(
    args: argparse.Namespace,
    ladder_config: dict[str, Any],
    fixture: dict[str, Any],
    agent_key: str = "spec_agent",
) -> list[dict[str, Any]]:
    selector = resolve_agent_selector(ladder_config, agent_key, getattr(args, "profile", None))
    if args.models:
        candidate_ids = set(args.models)
        max_models = len(candidate_ids)
    elif isinstance(selector, list):
        candidate_ids = set(selector)
        max_models = len(candidate_ids)
    else:
        candidate_ids = set(selector.get("candidate_model_ids", []))
        max_models = args.max_models or int(selector.get("max_models", 10))
    include_model_ids = set(selector.get("include_model_ids", [])) if isinstance(selector, dict) and not candidate_ids else set()
    if max_models <= 0:
        raise RuntimeError("max_models must be greater than zero")

    prompt_tokens = estimate_tokens(fixture)
    if isinstance(selector, dict):
        prompt_tokens = int(selector.get("estimated_prompt_tokens", prompt_tokens))
        completion_tokens = int(selector.get("estimated_completion_tokens", 1500))
        required_parameters = set(selector.get("require_parameters", []))
        exclude_free_models = bool(selector.get("exclude_free_models", True))
        min_context_length = int(selector.get("min_context_length", 0))
        exclude_id_patterns = [pattern.lower() for pattern in selector.get("exclude_id_patterns", [])]
        expand_reasoning = bool(selector.get("expand_reasoning_efforts", True))
        reasoning_exclude = bool(selector.get("reasoning_exclude", True))
    else:
        completion_tokens = 1500
        required_parameters = set()
        exclude_free_models = True
        min_context_length = 0
        exclude_id_patterns = []
        expand_reasoning = True
        reasoning_exclude = True

    ladder = []
    unavailable = []
    returned_ids = set()
    api_filters = {}
    if isinstance(selector, dict) and not candidate_ids:
        api_filters = dict(selector.get("api_filters") or {})
        if required_parameters and "supported_parameters" not in api_filters:
            api_filters["supported_parameters"] = sorted(required_parameters)
        if min_context_length and "context" not in api_filters:
            api_filters["context"] = min_context_length
        api_filters.setdefault("sort", "pricing-low-to-high")
        if "category" in api_filters:
            # OpenRouter rejects category combined with supported_parameters; keep
            # the category server-side and enforce capabilities locally below.
            api_filters.pop("supported_parameters", None)

    for model in fetch_openrouter_models(api_filters):
        model_id = model.get("id")
        if not model_id:
            continue
        returned_ids.add(model_id)
        if candidate_ids and model_id not in candidate_ids:
            continue
        context_length = int(model.get("context_length") or 0)
        if context_length < min_context_length:
            if candidate_ids:
                unavailable.append(model_summary(model, None, prompt_tokens, completion_tokens, f"context_length below {min_context_length}"))
            continue
        excluded_pattern = next((pattern for pattern in exclude_id_patterns if pattern in model_id.lower()), None)
        if excluded_pattern:
            if candidate_ids:
                unavailable.append(model_summary(model, None, prompt_tokens, completion_tokens, f"excluded by id pattern: {excluded_pattern}"))
            continue
        supported_parameters = set(model.get("supported_parameters") or [])
        missing_parameters = sorted(required_parameters - supported_parameters)
        if missing_parameters:
            if candidate_ids:
                unavailable.append(model_summary(model, None, prompt_tokens, completion_tokens, f"missing supported_parameters: {missing_parameters}"))
            continue
        pricing = model.get("pricing") or {}
        estimated_cost = estimate_cost(pricing, prompt_tokens, completion_tokens)
        if estimated_cost is None:
            if candidate_ids:
                unavailable.append(model_summary(model, None, prompt_tokens, completion_tokens, "missing or invalid pricing"))
            continue
        if exclude_free_models and estimated_cost <= 0:
            if candidate_ids:
                unavailable.append(model_summary(model, estimated_cost, prompt_tokens, completion_tokens, "excluded free model"))
            continue
        ladder.append(model_summary(model, estimated_cost, prompt_tokens, completion_tokens))

    sorted_ladder = sorted(ladder, key=lambda item: (item["estimated_cost"], item["id"]))
    ordered = sorted_ladder if candidate_ids else sorted_ladder[:max_models]
    ordered_variant_ids = {item.get("variant_id", item["id"]) for item in ordered}
    for item in sorted_ladder:
        variant_id = item.get("variant_id", item["id"])
        if item["id"] in include_model_ids and variant_id not in ordered_variant_ids:
            anchor = dict(item)
            anchor["included_by_profile"] = True
            ordered.append(anchor)
            ordered_variant_ids.add(variant_id)
    ordered = expand_reasoning_variants(ordered, expand_reasoning, reasoning_exclude)
    if candidate_ids:
        for missing_id in sorted(candidate_ids - returned_ids):
            unavailable.append(
                {
                    "id": missing_id,
                    "name": missing_id,
                    "estimated_cost": None,
                    "estimated_prompt_tokens": prompt_tokens,
                    "estimated_completion_tokens": completion_tokens,
                    "pricing": {},
                    "available": False,
                    "reason": "not returned by OpenRouter /models",
                }
            )
    ordered.extend(unavailable)

    if args.print_ladder:
        print(json.dumps(ordered, indent=2, sort_keys=True))
        raise SystemExit(0)
    return ordered


def resolve_agent_selector(ladder_config: dict[str, Any], agent_key: str, profile: str | None) -> Any:
    if "model_selector" in ladder_config:
        base_selector = ladder_config["model_selector"]
        agent_selector = ladder_config.get(agent_key, {})
        if not isinstance(base_selector, dict) or not isinstance(agent_selector, dict):
            raise RuntimeError("model_selector and agent selector entries must be objects")
        return resolve_selector(deep_merge(base_selector, agent_selector), profile)
    return resolve_selector(ladder_config[agent_key], profile)


def resolve_selector(selector: Any, profile: str | None) -> Any:
    if not isinstance(selector, dict) or not selector.get("profiles"):
        if profile:
            raise RuntimeError("model ladder does not define profiles")
        return selector

    profile_name = profile or selector.get("default_profile")
    if not profile_name:
        return {key: value for key, value in selector.items() if key not in {"profiles", "default_profile"}}
    profiles = selector.get("profiles") or {}
    if profile_name not in profiles:
        available = ", ".join(sorted(profiles))
        raise RuntimeError(f"unknown model ladder profile: {profile_name}. Available profiles: {available}")

    base = {key: value for key, value in selector.items() if key not in {"profiles", "default_profile"}}
    return deep_merge(base, profiles[profile_name])


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def model_summary(
    model: dict[str, Any],
    estimated_cost: float | None,
    prompt_tokens: int,
    completion_tokens: int,
    reason: str | None = None,
    reasoning_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = {
        "id": model["id"],
        "name": model.get("name", model["id"]),
        "variant_id": model["id"] + reasoning_variant_suffix(reasoning_request),
        "estimated_cost": estimated_cost,
        "estimated_prompt_tokens": prompt_tokens,
        "estimated_completion_tokens": completion_tokens,
        "pricing": model.get("pricing") or {},
        "context_length": model.get("context_length"),
        "supported_parameters": model.get("supported_parameters") or [],
        "reasoning": model.get("reasoning") or {},
        "available": reason is None,
    }
    if reasoning_request:
        summary["reasoning_request"] = reasoning_request
    if reason:
        summary["reason"] = reason
    return summary


def expand_reasoning_variants(
    summaries: list[dict[str, Any]],
    expand_reasoning: bool,
    reasoning_exclude: bool,
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for summary in summaries:
        efforts = reasoning_efforts(summary)
        if not expand_reasoning or not efforts:
            expanded.append(summary)
            continue
        for effort in efforts:
            variant = dict(summary)
            variant["variant_id"] = summary["id"] + reasoning_variant_suffix({"effort": effort})
            variant["reasoning_request"] = {"effort": effort, "exclude": reasoning_exclude}
            expanded.append(variant)
    return expanded


def reasoning_efforts(model: dict[str, Any]) -> list[str]:
    reasoning = model.get("reasoning") or {}
    supported = reasoning.get("supported_efforts") or []
    if not supported:
        return []
    order = {name: index for index, name in enumerate(REASONING_EFFORT_ORDER)}
    return sorted([str(value) for value in supported], key=lambda value: (order.get(value, 999), value))


def reasoning_variant_suffix(reasoning_request: dict[str, Any] | None) -> str:
    if not reasoning_request:
        return ""
    effort = reasoning_request.get("effort")
    return f"#reasoning={effort}" if effort else "#reasoning"


def fetch_openrouter_models(api_filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    url = "https://openrouter.ai/api/v1/models"
    query = encode_openrouter_model_filters(api_filters or {})
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter /models returned {exc.code}: {detail}") from exc
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("OpenRouter /models response did not include a data array")
    return data


def encode_openrouter_model_filters(api_filters: dict[str, Any]) -> str:
    query: dict[str, str] = {}
    for key, value in api_filters.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple, set)):
            query[key] = ",".join(str(item) for item in value)
        else:
            query[key] = str(value)
    return urllib.parse.urlencode(query)


def estimate_tokens(fixture: dict[str, Any]) -> int:
    text = json.dumps(fixture["issue"], sort_keys=True)
    return max(1000, len(text) // 4 + 1200)


def estimate_cost(pricing: dict[str, Any], prompt_tokens: int, completion_tokens: int) -> float | None:
    try:
        prompt_price = float(pricing.get("prompt") or 0)
        completion_price = float(pricing.get("completion") or 0)
    except (TypeError, ValueError):
        return None
    return round(prompt_price * prompt_tokens + completion_price * completion_tokens, 10)


def run_attempt(
    fixture: dict[str, Any],
    model_info: dict[str, Any],
    judge_model: str,
    run_root: Path,
    cache_root: Path,
    no_cache: bool,
) -> dict[str, Any]:
    model = model_info["id"]
    variant_id = model_info.get("variant_id", model)
    cache_path = cache_root / f"{safe_name(variant_id)}.json"
    if not no_cache and cache_path.exists():
        cached = read_json(cache_path)
        cached["cached"] = True
        cached["original_cost"] = cached.get("cost", 0)
        cached["cost"] = 0
        cached["estimated_cost"] = model_info.get("estimated_cost")
        cached.setdefault("tool_use", tool_use_summary(Path(cached["artifact_dir"]) if cached.get("artifact_dir") else None))
        cached.setdefault("artifact_policy", {})
        return cached

    issue = fixture["issue"]
    attempt_dir = run_root / safe_name(variant_id)
    artifact_root = attempt_dir / "docs/specs"
    sidecar_root = attempt_dir / ".workflow/artifacts/specs"
    attempt_dir.mkdir(parents=True, exist_ok=True)

    previous_model = os.environ.get("OPENROUTER_MODEL")
    previous_reasoning = os.environ.get("OPENROUTER_REASONING")
    previous_context_length = os.environ.get("OPENROUTER_CONTEXT_LENGTH")
    os.environ["OPENROUTER_MODEL"] = model
    apply_reasoning_env(model_info)
    apply_context_length_env(model_info)
    try:
        artifact_dir, contract, spec_usage = spec_agent.generate_spec_artifacts(
            issue_number=int(issue["number"]),
            title=issue["title"],
            body=issue.get("body") or "",
            author=issue.get("author", "fixture"),
            generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            artifact_root=artifact_root,
            sidecar_root=sidecar_root,
            working_folder=attempt_dir,
        )
    except Exception as exc:  # noqa: BLE001 - eval report should capture failed attempts.
        restore_model(previous_model)
        restore_reasoning(previous_reasoning)
        restore_context_length(previous_context_length)
        runtime_root = sidecar_root / "_runtime"
        attempt = {
            "model": model,
            "variant_id": variant_id,
            "reasoning_request": model_info.get("reasoning_request"),
            "context_length": model_info.get("context_length"),
            "tool_use": tool_use_summary(runtime_root),
            "artifact_policy": {},
            "estimated_cost": model_info.get("estimated_cost"),
            "status": "fail",
            "artifact_dir": str(attempt_dir),
            "cached": False,
            "cost": 0,
            "deterministic": {"status": "fail", "findings": [f"generation failed: {exc}"]},
            "judge": None,
        }
        return attempt
    finally:
        restore_model(previous_model)
        restore_reasoning(previous_reasoning)
        restore_context_length(previous_context_length)

    deterministic = deterministic_eval(contract, artifact_dir, fixture["expect"])
    if deterministic["status"] == "fail":
        runtime_root = sidecar_root / "_runtime"
        attempt = {
            "model": model,
            "variant_id": variant_id,
            "reasoning_request": model_info.get("reasoning_request"),
            "context_length": model_info.get("context_length"),
            "tool_use": tool_use_summary(runtime_root),
            "artifact_policy": deterministic.get("artifact_policy", {}),
            "estimated_cost": model_info.get("estimated_cost"),
            "status": "fail",
            "artifact_dir": str(artifact_dir),
            "cached": False,
            "cost": sum_costs([{"usage": spec_usage}]),
            "usage": {
                "spec": spec_usage,
                "judge": {},
            },
            "deterministic": deterministic,
            "judge": None,
        }
        write_attempt_cache(cache_path, attempt)
        return attempt

    judge, judge_usage = judge_eval(judge_model, fixture, contract)
    status = "pass" if judge_passes(judge) else "fail"
    runtime_root = sidecar_root / "_runtime"
    attempt = {
        "model": model,
        "variant_id": variant_id,
        "reasoning_request": model_info.get("reasoning_request"),
        "context_length": model_info.get("context_length"),
        "tool_use": tool_use_summary(runtime_root),
        "artifact_policy": deterministic.get("artifact_policy", {}),
        "estimated_cost": model_info.get("estimated_cost"),
        "status": status,
        "artifact_dir": str(artifact_dir),
        "cached": False,
        "cost": sum_costs([{"usage": spec_usage}, {"usage": judge_usage}]),
        "usage": {
            "spec": spec_usage,
            "judge": judge_usage,
        },
        "deterministic": deterministic,
        "judge": judge,
    }
    write_attempt_cache(cache_path, attempt)
    return attempt


def deterministic_eval(contract: dict[str, Any], artifact_dir: Path, expect: dict[str, Any]) -> dict[str, Any]:
    findings: list[str] = []
    required_fields = [
        "summary",
        "scope",
        "non_goals",
        "acceptance_criteria",
        "test_plan",
        "risks",
        "cost_plan",
        "requires_human_approval",
    ]
    for field in required_fields:
        if field not in contract:
            findings.append(f"missing required field: {field}")
    if contract.get("requires_human_approval") is not True:
        findings.append("requires_human_approval must be true")
    if len(contract.get("acceptance_criteria", [])) < expect["min_acceptance_criteria"]:
        findings.append("too few acceptance criteria")
    if len(contract.get("test_plan", [])) < expect["min_test_plan_items"]:
        findings.append("too few test plan items")
    if len(contract.get("risks", [])) < expect["min_risks"]:
        findings.append("too few risks")

    text = json.dumps(contract, sort_keys=True).lower()
    aliases = expect.get("topic_aliases", {})
    for topic in expect["required_topics"]:
        topic_terms = aliases.get(topic, [topic])
        if not any(term.lower() in text for term in topic_terms):
            findings.append(f"missing required topic: {topic}")
    forbidden_text = json.dumps(
        {
            key: value
            for key, value in contract.items()
            if key not in {"non_goals", "summary"}
        },
        sort_keys=True,
    ).lower()
    for topic in expect["forbidden_topics"]:
        if topic.lower() in forbidden_text:
            findings.append(f"contains forbidden topic outside non-goals: {topic}")
    non_goals = " ".join(contract.get("non_goals", [])).lower()
    for topic in expect["non_goals"]:
        topic_terms = aliases.get(topic, [topic])
        if not any(term.lower() in non_goals for term in topic_terms):
            findings.append(f"non-goals missing topic: {topic}")

    policy = artifact_policy.load_policy(Path(".workflow/agents/spec/artifact_policy.json"))
    artifact_report = artifact_policy.validate_files(
        artifact_dir,
        required_files=policy["required_files"],
        allowed_files=policy["allowed_files"],
    )
    findings.extend(artifact_report["findings"])

    return {"status": "fail" if findings else "pass", "findings": findings, "artifact_policy": artifact_report}


def judge_eval(judge_model: str, fixture: dict[str, Any], contract: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    from openrouter import OpenRouter

    with OpenRouter(api_key=os.environ["OPENROUTER_API_KEY"]) as client:
        completion = client.chat.send(
            model=judge_model,
            response_format=spec_agent.structured_response_format(
                "spec_agent_eval",
                spec_agent.agent_eval_schema(
                    [
                        "spec_alignment",
                        "acceptance_criteria_quality",
                        "test_plan_quality",
                        "risk_awareness",
                        "scope_discipline",
                        "blocking_question_quality",
                    ]
                ),
            ),
            **spec_agent.openrouter_request_options(),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are evaluating a Spec Agent output. Return only JSON. "
                        "Do not reward implementation detail. Penalize invented requirements, vague acceptance criteria, "
                        "missing risk analysis, and weak test planning."
                    ),
                },
                {"role": "user", "content": judge_prompt(fixture, contract)},
            ],
        )
    content = completion.choices[0].message.content
    if not content:
        raise RuntimeError("judge returned empty response")
    return json.loads(content), spec_agent.response_usage(completion)


def judge_prompt(fixture: dict[str, Any], contract: dict[str, Any]) -> str:
    return f"""Original issue:
{json.dumps(fixture["issue"], indent=2, sort_keys=True)}

Spec Agent contract:
{json.dumps(contract, indent=2, sort_keys=True)}

Evaluate against this JSON schema:
{{
  "status": "pass|fail|needs_human_review",
  "scores": {{
    "spec_alignment": 0,
    "acceptance_criteria_quality": 0,
    "test_plan_quality": 0,
    "risk_awareness": 0,
    "scope_discipline": 0,
    "blocking_question_quality": 0
  }},
  "critical_findings": ["string"],
  "findings": ["string"],
  "human_review_focus": ["string"]
}}

Pass criteria: average score >= 8, no score below 6, and no critical findings.
"""


def judge_passes(judge: dict[str, Any]) -> bool:
    if judge.get("status") != "pass":
        return False
    if judge.get("critical_findings"):
        return False
    scores = judge.get("scores", {})
    if not scores:
        return False
    values = [float(value) for value in scores.values()]
    return min(values) >= 6 and sum(values) / len(values) >= 8


def print_attempt(attempt: dict[str, Any]) -> None:
    print(f"\nmodel: {attempt['model']}")
    if attempt.get("variant_id") and attempt.get("variant_id") != attempt["model"]:
        print(f"variant: {attempt['variant_id']}")
    if attempt.get("reasoning_request"):
        print(f"reasoning: {json.dumps(attempt['reasoning_request'], sort_keys=True)}")
    print(f"estimated_cost: {attempt.get('estimated_cost')}")
    print(f"status: {attempt['status']}")
    print(f"cached: {attempt.get('cached', False)}")
    if "tool_use" in attempt:
        print(f"tool_use: {json.dumps(attempt['tool_use'], sort_keys=True)}")
    if attempt.get("cached"):
        print(f"current_run_cost: {attempt.get('cost', 0)}")
        print(f"original_cost: {attempt.get('original_cost', 0)}")
    elif "cost" in attempt:
        print(f"actual_cost: {attempt['cost']}")
    deterministic = attempt["deterministic"]
    if deterministic["findings"]:
        print("deterministic_findings:")
        for finding in deterministic["findings"]:
            print(f"- {finding}")
    artifact_report = attempt.get("artifact_policy") or {}
    if artifact_report.get("findings"):
        print("artifact_policy_findings:")
        for finding in artifact_report["findings"]:
            print(f"- {finding}")
    judge = attempt.get("judge")
    if judge:
        print(f"judge_status: {judge.get('status')}")
        print(f"judge_scores: {json.dumps(judge.get('scores', {}), sort_keys=True)}")
        for finding in judge.get("critical_findings", []):
            print(f"- critical: {finding}")


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_attempt_cache(path: Path, attempt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cache_attempt = dict(attempt)
    cache_attempt["cached"] = False
    path.write_text(json.dumps(cache_attempt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def tool_use_summary(report_root: Path | None) -> dict[str, Any]:
    summary = {
        "tool_calls": 0,
        "tools": {},
        "compactions": 0,
        "pre_compaction_archives": 0,
    }
    if not report_root:
        return summary
    tool_events_path = first_matching_file(report_root, "tool_events.json")
    compaction_events_path = first_matching_file(report_root, "compaction_events.json")
    pre_compaction_path = first_matching_file(report_root, "pre_compaction_archives.json")
    if tool_events_path.exists():
        tool_events = read_json_list(tool_events_path)
        summary["tool_calls"] = len(tool_events)
        tools: dict[str, int] = {}
        for event in tool_events:
            name = str(event.get("name", "unknown"))
            tools[name] = tools.get(name, 0) + 1
        summary["tools"] = tools
    if compaction_events_path.exists():
        summary["compactions"] = len(read_json_list(compaction_events_path))
    if pre_compaction_path.exists():
        summary["pre_compaction_archives"] = len(read_json_list(pre_compaction_path))
    return summary


def first_matching_file(root: Path, name: str) -> Path:
    direct = root / name
    if direct.exists():
        return direct
    matches = sorted(root.rglob(name)) if root.exists() else []
    return matches[0] if matches else direct


def read_json_list(path: Path) -> list[dict[str, Any]]:
    data = read_json(path)
    return data if isinstance(data, list) else []


def eval_cache_key(fixture_path: Path, judge_model: str) -> str:
    paths = [
        fixture_path,
        Path("AGENTS.md"),
        spec_agent.SELECTED_MODEL_PATH,
        Path(".workflow/model_ladder.json"),
        Path("scripts/agent_runtime.py"),
        Path("scripts/artifact_policy.py"),
        Path(".workflow/agents/spec/artifact_policy.json"),
        Path("scripts/spec_agent.py"),
        Path("scripts/eval_spec_agent.py"),
    ]
    paths.extend(sorted(Path(".workflow/agents/spec/static").glob("*.md")))
    return eval_runtime.cache_key(paths, judge_model)


def update_selected_model(winner: dict[str, Any], report_path: Path, judge_model: str) -> None:
    eval_runtime.write_selected_model(spec_agent.SELECTED_MODEL_PATH, winner, report_path, judge_model)


def restore_model(previous_model: str | None) -> None:
    if previous_model is None:
        os.environ.pop("OPENROUTER_MODEL", None)
    else:
        os.environ["OPENROUTER_MODEL"] = previous_model


def apply_reasoning_env(model_info: dict[str, Any]) -> None:
    reasoning_request = model_info.get("reasoning_request")
    if reasoning_request:
        os.environ["OPENROUTER_REASONING"] = json.dumps(reasoning_request, sort_keys=True)
    else:
        os.environ.pop("OPENROUTER_REASONING", None)


def restore_reasoning(previous_reasoning: str | None) -> None:
    if previous_reasoning is None:
        os.environ.pop("OPENROUTER_REASONING", None)
    else:
        os.environ["OPENROUTER_REASONING"] = previous_reasoning


def apply_context_length_env(model_info: dict[str, Any]) -> None:
    context_length = model_info.get("context_length")
    if context_length:
        os.environ["OPENROUTER_CONTEXT_LENGTH"] = str(context_length)
    else:
        os.environ.pop("OPENROUTER_CONTEXT_LENGTH", None)


def restore_context_length(previous_context_length: str | None) -> None:
    if previous_context_length is None:
        os.environ.pop("OPENROUTER_CONTEXT_LENGTH", None)
    else:
        os.environ["OPENROUTER_CONTEXT_LENGTH"] = previous_context_length


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value).strip("-")


def sum_costs(items: list[dict[str, Any]]) -> float:
    total = 0.0
    for item in items:
        if "cost" in item and isinstance(item["cost"], (int, float)):
            total += float(item["cost"])
            continue
        usage = item.get("usage") or {}
        if isinstance(usage, dict):
            total += float(usage.get("cost") or 0)
            continue
        nested_usage = item.get("usage", {})
        if isinstance(nested_usage, dict):
            for value in nested_usage.values():
                if isinstance(value, dict):
                    total += float(value.get("cost") or 0)
    return round(total, 8)


def cleanup_runs(run_root: Path, keep_runs: int) -> None:
    if not run_root.exists():
        return
    runs = sorted([path for path in run_root.iterdir() if path.is_dir()])
    for old_run in runs[:-keep_runs]:
        shutil.rmtree(old_run, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
