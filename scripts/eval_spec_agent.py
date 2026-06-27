#!/usr/bin/env python3
"""Run Spec Agent against fixtures and climb a model ladder until eval passes."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import spec_agent


DEFAULT_FIXTURE = Path("tests/fixtures/spec_agent/moderately_complex.json")
DEFAULT_LADDER = Path(".workflow/model_ladder.json")
DEFAULT_RUN_ROOT = Path(".workflow/eval-runs")


def main() -> int:
    args = parse_args()
    fixture = read_json(args.fixture)
    ladder_config = read_json(args.model_ladder)
    judge_model = args.judge_model or ladder_config["judge"]

    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required for Spec Agent eval")

    ladder_report = resolve_model_ladder(args, ladder_config, fixture)
    runnable_ladder = [item for item in ladder_report if item["available"] and item["estimated_cost"] is not None]
    models = [item["id"] for item in runnable_ladder]
    if not models:
        raise RuntimeError("No runnable candidate models available from OpenRouter /models")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = args.run_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)

    attempts = []
    winner = None
    for model in models:
        attempt = run_attempt(fixture, model, judge_model, run_root)
        attempts.append(attempt)
        print_attempt(attempt)
        if attempt["status"] == "pass":
            winner = attempt
            break

    report = {
        "status": "pass" if winner else "fail",
        "fixture": fixture["name"],
        "judge_model": judge_model,
        "winner_model": winner["model"] if winner else None,
        "model_ladder": ladder_report,
        "total_cost": sum_costs(attempts),
        "attempts": attempts,
    }
    report_path = run_root / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nreport: {report_path}")
    cleanup_runs(args.run_root, args.keep_runs)
    if winner:
        print(f"passed_with: {winner['model']}")
        return 0
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--model-ladder", type=Path, default=DEFAULT_LADDER)
    parser.add_argument("--models", nargs="+", help="Override spec model ladder")
    parser.add_argument("--print-ladder", action="store_true", help="Print API-priced model ladder and exit")
    parser.add_argument("--judge-model", help="Override judge model")
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--keep-runs", type=int, default=10)
    return parser.parse_args()


def resolve_model_ladder(args: argparse.Namespace, ladder_config: dict[str, Any], fixture: dict[str, Any]) -> list[dict[str, Any]]:
    selector = ladder_config["spec_agent"]
    if args.models:
        candidate_ids = set(args.models)
        max_models = len(candidate_ids)
    elif isinstance(selector, list):
        candidate_ids = set(selector)
        max_models = len(candidate_ids)
    else:
        candidate_ids = set(selector.get("candidate_model_ids", []))
        max_models = int(selector.get("max_models", 10))
    if max_models <= 0:
        raise RuntimeError("max_models must be greater than zero")

    prompt_tokens = estimate_tokens(fixture)
    if isinstance(selector, dict):
        prompt_tokens = int(selector.get("estimated_prompt_tokens", prompt_tokens))
        completion_tokens = int(selector.get("estimated_completion_tokens", 1500))
        required_parameters = set(selector.get("require_parameters", []))
        exclude_free_models = bool(selector.get("exclude_free_models", True))
    else:
        completion_tokens = 1500
        required_parameters = set()
        exclude_free_models = True

    ladder = []
    unavailable = []
    returned_ids = set()
    for model in fetch_openrouter_models():
        model_id = model.get("id")
        if not model_id:
            continue
        returned_ids.add(model_id)
        if candidate_ids and model_id not in candidate_ids:
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

    ordered = sorted(ladder, key=lambda item: (item["estimated_cost"], item["id"]))[:max_models]
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


def model_summary(
    model: dict[str, Any],
    estimated_cost: float | None,
    prompt_tokens: int,
    completion_tokens: int,
    reason: str | None = None,
) -> dict[str, Any]:
    summary = {
        "id": model["id"],
        "name": model.get("name", model["id"]),
        "estimated_cost": estimated_cost,
        "estimated_prompt_tokens": prompt_tokens,
        "estimated_completion_tokens": completion_tokens,
        "pricing": model.get("pricing") or {},
        "context_length": model.get("context_length"),
        "supported_parameters": model.get("supported_parameters") or [],
        "available": reason is None,
    }
    if reason:
        summary["reason"] = reason
    return summary


def fetch_openrouter_models() -> list[dict[str, Any]]:
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
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


def run_attempt(fixture: dict[str, Any], model: str, judge_model: str, run_root: Path) -> dict[str, Any]:
    issue = fixture["issue"]
    attempt_dir = run_root / safe_name(model)
    artifact_root = attempt_dir / "docs/specs"
    sidecar_root = attempt_dir / ".workflow/artifacts/specs"
    attempt_dir.mkdir(parents=True, exist_ok=True)

    previous_model = os.environ.get("OPENROUTER_MODEL")
    os.environ["OPENROUTER_MODEL"] = model
    try:
        artifact_dir, contract, spec_usage = spec_agent.generate_spec_artifacts(
            issue_number=int(issue["number"]),
            title=issue["title"],
            body=issue.get("body") or "",
            author=issue.get("author", "fixture"),
            generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            artifact_root=artifact_root,
            sidecar_root=sidecar_root,
        )
    except Exception as exc:  # noqa: BLE001 - eval report should capture failed attempts.
        restore_model(previous_model)
        return {
            "model": model,
            "status": "fail",
            "artifact_dir": str(attempt_dir),
            "deterministic": {"status": "fail", "findings": [f"generation failed: {exc}"]},
            "judge": None,
        }
    finally:
        restore_model(previous_model)

    deterministic = deterministic_eval(contract, artifact_dir, fixture["expect"])
    if deterministic["status"] == "fail":
        return {
            "model": model,
            "status": "fail",
            "artifact_dir": str(artifact_dir),
            "cost": sum_costs([{"usage": spec_usage}]),
            "usage": {
                "spec": spec_usage,
                "judge": {},
            },
            "deterministic": deterministic,
            "judge": None,
        }

    judge, judge_usage = judge_eval(judge_model, fixture, contract)
    status = "pass" if judge_passes(judge) else "fail"
    return {
        "model": model,
        "status": status,
        "artifact_dir": str(artifact_dir),
        "cost": sum_costs([{"usage": spec_usage}, {"usage": judge_usage}]),
        "usage": {
            "spec": spec_usage,
            "judge": judge_usage,
        },
        "deterministic": deterministic,
        "judge": judge,
    }


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
    for topic in expect["required_topics"]:
        if topic.lower() not in text:
            findings.append(f"missing required topic: {topic}")
    for topic in expect["forbidden_topics"]:
        if topic.lower() in text:
            findings.append(f"contains forbidden topic: {topic}")
    non_goals = " ".join(contract.get("non_goals", [])).lower()
    for topic in expect["non_goals"]:
        if topic.lower() not in non_goals:
            findings.append(f"non-goals missing topic: {topic}")

    allowed = {"spec.md", "acceptance_tests.md", "risk_register.md", "cost_plan.md", "contract.json", "pull_request_body.md"}
    produced = {path.name for path in artifact_dir.glob("*") if path.is_file()}
    unexpected = produced - allowed
    if unexpected:
        findings.append(f"unexpected artifact files: {sorted(unexpected)}")

    return {"status": "fail" if findings else "pass", "findings": findings}


def judge_eval(judge_model: str, fixture: dict[str, Any], contract: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    from openrouter import OpenRouter

    with OpenRouter(api_key=os.environ["OPENROUTER_API_KEY"]) as client:
        completion = client.chat.send(
            model=judge_model,
            response_format={"type": "json_object"},
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
    print(f"status: {attempt['status']}")
    if "cost" in attempt:
        print(f"cost: {attempt['cost']}")
    deterministic = attempt["deterministic"]
    if deterministic["findings"]:
        print("deterministic_findings:")
        for finding in deterministic["findings"]:
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


def restore_model(previous_model: str | None) -> None:
    if previous_model is None:
        os.environ.pop("OPENROUTER_MODEL", None)
    else:
        os.environ["OPENROUTER_MODEL"] = previous_model


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
