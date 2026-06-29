#!/usr/bin/env python3
"""Run Verifier Agent over pass/fail workspaces and climb a model ladder."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import artifact_policy
import agent_runtime
import eval_cases
import eval_runtime
import eval_spec_agent
import spec_agent
import test_agent
import verifier_agent


DEFAULT_CASES_ROOT = Path("tests/fixtures/verifier_agent/cases")
DEFAULT_LADDER = Path(".workflow/model_ladder.json")
DEFAULT_RUN_ROOT = Path(".workflow/eval-runs/verifier-agent")
DEFAULT_CACHE_ROOT = Path(".workflow/eval-cache/verifier-agent")


def main() -> int:
    args = parse_args()
    ladder_config = spec_agent.read_json(args.model_ladder)
    judge_model = args.judge_model or ladder_config["judge"]

    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required for Verifier Agent eval")
    os.environ["OPENROUTER_TIMEOUT_MS"] = str(args.request_timeout_ms)

    cases = eval_cases.load_cases(args.cases_root)
    ladder_report = eval_spec_agent.resolve_model_ladder(
        args,
        ladder_config,
        fixture_for_ladder(cases),
        agent_key="verifier_agent",
    )
    runnable_ladder = [item for item in ladder_report if item["available"] and item["estimated_cost"] is not None]
    if not runnable_ladder:
        raise RuntimeError("No runnable candidate models available from OpenRouter /models")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = args.run_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    cache_key = eval_cache_key(args.cases_root, judge_model)
    cache_root = args.cache_root / cache_key

    eval_spec_agent.print_ladder_summary(runnable_ladder)

    attempts: list[dict[str, Any]] = []
    winner = None
    report_path = run_root / "report.json"
    for model_info in runnable_ladder:
        print(f"\nstarting_model: {eval_runtime.model_display_name(model_info)}", flush=True)
        attempt = run_attempt(
            model_info=model_info,
            cases=cases,
            run_root=run_root,
            cache_root=cache_root,
            no_cache=args.no_cache,
        )
        attempts.append(attempt)
        eval_spec_agent.print_attempt(attempt)
        write_report(report_path, args.cases_root, judge_model, ladder_report, attempts, winner, final=False)
        if attempt["status"] == "pass":
            winner = attempt
            write_report(report_path, args.cases_root, judge_model, ladder_report, attempts, winner, final=True)
            if not args.no_update_selected_model:
                update_selected_model(winner, report_path, judge_model)
            break

    write_report(report_path, args.cases_root, judge_model, ladder_report, attempts, winner, final=True)
    eval_runtime.update_report_index(
        "verifier-agent",
        report_path,
        status=eval_runtime.report_status(winner, final=True),
        winner=winner,
        attempts=attempts,
        total_cost=eval_spec_agent.sum_costs(attempts),
    )
    print(f"\nreport: {report_path}")
    eval_spec_agent.cleanup_runs(args.run_root, args.keep_runs)
    if winner:
        print(f"passed_with: {eval_runtime.model_display_name(winner)}")
        return 0
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases-root", type=Path, default=DEFAULT_CASES_ROOT)
    parser.add_argument("--model-ladder", type=Path, default=DEFAULT_LADDER)
    parser.add_argument("--profile", help="Model ladder profile from .workflow/model_ladder.json")
    parser.add_argument("--models", nargs="+", help="Override verifier model ladder")
    parser.add_argument("--max-models", type=int, help="Override max models to try from the API-priced ladder")
    parser.add_argument("--print-ladder", action="store_true", help="Print API-priced model ladder and exit")
    parser.add_argument("--judge-model", help="Included in cache/report identity for consistency")
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-update-selected-model", action="store_true")
    parser.add_argument("--keep-runs", type=int, default=10)
    parser.add_argument("--request-timeout-ms", type=int, default=120_000)
    return parser.parse_args()


def run_attempt(
    *,
    model_info: dict[str, Any],
    cases: list[dict[str, Any]],
    run_root: Path,
    cache_root: Path,
    no_cache: bool,
) -> dict[str, Any]:
    model = model_info["id"]
    variant_id = model_info.get("variant_id", model)
    cache_path = cache_root / f"{eval_spec_agent.safe_name(variant_id)}.json"
    if not no_cache and cache_path.exists():
        cached = spec_agent.read_json(cache_path)
        cached["cached"] = True
        cached["original_cost"] = cached.get("cost", 0)
        cached["cost"] = 0
        cached["estimated_cost"] = model_info.get("estimated_cost")
        cached.setdefault("tool_use", eval_cases.combined_tool_use(cached.get("cases", [])))
        cached.setdefault("artifact_policy", eval_cases.combined_artifact_policy(cached.get("cases", [])))
        return cached

    previous_model = os.environ.get("OPENROUTER_MODEL")
    previous_reasoning = os.environ.get("OPENROUTER_REASONING")
    previous_context_length = os.environ.get("OPENROUTER_CONTEXT_LENGTH")
    os.environ["OPENROUTER_MODEL"] = model
    eval_spec_agent.apply_reasoning_env(model_info)
    eval_spec_agent.apply_context_length_env(model_info)

    case_results: list[dict[str, Any]] = []
    usages: list[dict[str, Any]] = []
    try:
        for case in cases:
            case_result, usage = run_case(model_info, case, run_root)
            case_results.append(case_result)
            usages.append({"usage": usage})
    except Exception as exc:  # noqa: BLE001
        case_results.append({"name": "generation", "status": "fail", "findings": [f"generation failed: {exc}"]})
    finally:
        eval_spec_agent.restore_model(previous_model)
        eval_spec_agent.restore_reasoning(previous_reasoning)
        eval_spec_agent.restore_context_length(previous_context_length)

    findings = aggregate_findings(case_results)
    status = "pass" if not findings and all(result.get("status") == "pass" for result in case_results) else "fail"
    attempt = {
        "model": model,
        "variant_id": variant_id,
        "reasoning_request": model_info.get("reasoning_request"),
        "context_length": model_info.get("context_length"),
        "tool_use": eval_cases.combined_tool_use(case_results),
        "artifact_policy": eval_cases.combined_artifact_policy(case_results),
        "estimated_cost": model_info.get("estimated_cost"),
        "status": status,
        "artifact_dir": str(run_root / eval_spec_agent.safe_name(variant_id)),
        "cached": False,
        "cost": eval_spec_agent.sum_costs(usages),
        "usage": {"verifier": usages},
        "deterministic": {"status": status, "findings": findings},
        "cases": case_results,
        "judge": None,
    }
    eval_spec_agent.write_attempt_cache(cache_path, attempt)
    return attempt


def run_case(model_info: dict[str, Any], case: dict[str, Any], run_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    variant_id = model_info.get("variant_id", model_info["id"])
    case_name = case["manifest"]["name"]
    attempt_dir = run_root / eval_spec_agent.safe_name(variant_id) / case_name
    shutil.copytree(case["path"], attempt_dir, dirs_exist_ok=True)
    agent_runtime.ensure_git_baseline(attempt_dir)
    before = artifact_policy.snapshot(attempt_dir, exclude_prefixes=verifier_mutable_prefixes())
    with eval_cases.attempt_time_limit("verification", max(1, test_agent.openrouter_timeout_ms() // 1000)):
        report_dir, contract, usage = verifier_agent.generate_verification_artifacts(
            working_folder=attempt_dir,
            generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        )
    deterministic = deterministic_case_eval(attempt_dir, before, contract, case["manifest"])
    return {
        "name": case_name,
        "expected_status": case["manifest"]["expected_status"],
        "actual_status": contract.get("status"),
        "status": deterministic["status"],
        "artifact_dir": str(report_dir),
        "tool_use": eval_spec_agent.tool_use_summary(attempt_dir),
        "artifact_policy": deterministic["artifact_policy"],
        "findings": deterministic["findings"],
        "contract": contract,
    }, usage


def deterministic_case_eval(
    working_folder: Path,
    before: dict[str, str],
    contract: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    findings = []
    expected_status = manifest["expected_status"]
    if contract.get("status") != expected_status:
        findings.append(f"expected verifier status {expected_status}, got {contract.get('status')}")
    if contract.get("safe_to_open_pr") is True and contract.get("status") != "pass":
        findings.append("safe_to_open_pr true on non-passing verification")
    if contract.get("status") == "fail" and not contract.get("required_actions"):
        findings.append("failing verifier report has no required_actions")
    if not contract.get("checks_run"):
        findings.append("checks_run is empty")
    if has_npm_test(working_folder) and not mentions_text(contract.get("checks_run", []), "npm test"):
        findings.append("npm test was not reported in checks_run")
    for topic in manifest.get("expected_topics", []):
        if not mentions_text(contract, topic):
            findings.append(f"missing expected topic: {topic}")
    artifact_report = artifact_policy.protected_changes(
        working_folder,
        before,
        exclude_prefixes=verifier_mutable_prefixes(),
    )
    if artifact_report["protected_changes"]:
        findings.append(f"verifier modified files outside its report area: {artifact_report['protected_changes']}")
    return {"status": "fail" if findings else "pass", "findings": findings, "artifact_policy": artifact_report}


def has_npm_test(working_folder: Path) -> bool:
    package_path = working_folder / "package.json"
    if not package_path.exists():
        return False
    package = spec_agent.read_json(package_path)
    return bool((package.get("scripts") or {}).get("test"))


def verifier_mutable_prefixes() -> list[str]:
    policy = artifact_policy.load_policy(Path(".workflow/agents/verifier/artifact_policy.json"))
    return list(policy["mutable_prefixes"])


def mentions_text(value: Any, needle: str) -> bool:
    return needle.lower() in json.dumps(value, sort_keys=True).lower()


def aggregate_findings(case_results: list[dict[str, Any]]) -> list[str]:
    findings = []
    for result in case_results:
        for finding in result.get("findings", []):
            findings.append(f"{result.get('name')}: {finding}")
    return findings


def fixture_for_ladder(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": "Verifier Agent pass/fail workspace suite",
        "issue": {
            "title": "Verify Builder Agent outputs",
            "body": json.dumps([case["manifest"] for case in cases], sort_keys=True),
        },
    }


def write_report(
    report_path: Path,
    cases_root: Path,
    judge_model: str,
    ladder_report: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    winner: dict[str, Any] | None,
    final: bool,
) -> None:
    eval_runtime.write_report(
        report_path,
        status=eval_runtime.report_status(winner, final),
        fixture=cases_root.as_posix(),
        judge_model=judge_model,
        ladder_report=ladder_report,
        attempts=attempts,
        winner=winner,
        total_cost=eval_spec_agent.sum_costs(attempts),
    )


def eval_cache_key(cases_root: Path, judge_model: str) -> str:
    paths = [
        Path(".workflow/model_ladder.json"),
        Path("scripts/agent_runtime.py"),
        Path("scripts/artifact_policy.py"),
        Path(".workflow/agents/verifier/artifact_policy.json"),
        Path("scripts/eval_cases.py"),
        Path("scripts/spec_agent.py"),
        Path("scripts/verifier_agent.py"),
        Path("scripts/eval_verifier_agent.py"),
    ]
    if cases_root.exists():
        paths.extend(path for path in sorted(cases_root.rglob("*")) if path.is_file())
    paths.extend(sorted(Path(".workflow/agents/verifier/static").glob("*.md")))
    paths.extend(sorted(Path(".workflow/agents/common/static").glob("*.md")))
    return eval_runtime.cache_key(paths, judge_model)


def update_selected_model(winner: dict[str, Any], report_path: Path, judge_model: str) -> None:
    eval_runtime.write_selected_model(verifier_agent.SELECTED_MODEL_PATH, winner, report_path, judge_model)


if __name__ == "__main__":
    raise SystemExit(main())
