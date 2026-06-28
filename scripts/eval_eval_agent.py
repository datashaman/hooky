#!/usr/bin/env python3
"""Run Eval Agent over qualitative quality cases and climb a model ladder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import eval_agent
import artifact_policy
import eval_spec_agent
import spec_agent
import test_agent


DEFAULT_CASES_ROOT = Path("tests/fixtures/eval_agent/cases")
DEFAULT_LADDER = Path(".workflow/model_ladder.json")
DEFAULT_RUN_ROOT = Path(".workflow/eval-runs/eval-agent")
DEFAULT_CACHE_ROOT = Path(".workflow/eval-cache/eval-agent")


class AttemptTimeoutError(TimeoutError):
    pass


def main() -> int:
    args = parse_args()
    ladder_config = spec_agent.read_json(args.model_ladder)
    judge_model = args.judge_model or ladder_config["judge"]

    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required for Eval Agent eval")
    os.environ["OPENROUTER_TIMEOUT_MS"] = str(args.request_timeout_ms)

    cases = load_cases(args.cases_root)
    ladder_report = eval_spec_agent.resolve_model_ladder(
        args,
        ladder_config,
        fixture_for_ladder(cases),
        agent_key="eval_agent",
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
        print(f"\nstarting_model: {eval_spec_agent.model_display_name(model_info)}", flush=True)
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
    print(f"\nreport: {report_path}")
    eval_spec_agent.cleanup_runs(args.run_root, args.keep_runs)
    if winner:
        print(f"passed_with: {eval_spec_agent.model_display_name(winner)}")
        return 0
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases-root", type=Path, default=DEFAULT_CASES_ROOT)
    parser.add_argument("--model-ladder", type=Path, default=DEFAULT_LADDER)
    parser.add_argument("--profile", help="Model ladder profile from .workflow/model_ladder.json")
    parser.add_argument("--models", nargs="+", help="Override eval model ladder")
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


def load_cases(cases_root: Path) -> list[dict[str, Any]]:
    cases = []
    for manifest_path in sorted(cases_root.glob("*/manifest.json")):
        manifest = spec_agent.read_json(manifest_path)
        cases.append({"path": manifest_path.parent, "manifest": manifest})
    if not cases:
        raise RuntimeError(f"No Eval Agent cases found under {cases_root}")
    return cases


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
        cached.setdefault("tool_use", combined_tool_use(cached.get("cases", [])))
        cached.setdefault("artifact_policy", combined_artifact_policy(cached.get("cases", [])))
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
        "tool_use": combined_tool_use(case_results),
        "artifact_policy": combined_artifact_policy(case_results),
        "estimated_cost": model_info.get("estimated_cost"),
        "status": status,
        "artifact_dir": str(run_root / eval_spec_agent.safe_name(variant_id)),
        "cached": False,
        "cost": eval_spec_agent.sum_costs(usages),
        "usage": {"eval": usages},
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
    before = artifact_policy.snapshot(attempt_dir, exclude_prefixes=[".workflow/artifacts/eval-agent"])
    with attempt_time_limit("evaluation"):
        report_dir, contract, usage = eval_agent.generate_eval_artifacts(
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
        findings.append(f"expected eval status {expected_status}, got {contract.get('status')}")
    expected_safe_to_merge = manifest.get("expected_safe_to_merge")
    if expected_safe_to_merge is not None and contract.get("safe_to_merge") is not expected_safe_to_merge:
        findings.append(f"expected safe_to_merge {expected_safe_to_merge}, got {contract.get('safe_to_merge')}")
    if verifier_status(working_folder) != "pass" and contract.get("status") == "pass":
        findings.append("Eval Agent passed a workspace whose Verifier status was not pass")
    if contract.get("safe_to_merge") is True and contract.get("status") != "pass":
        findings.append("safe_to_merge true on non-passing eval")
    score_findings = score_gate_findings(contract)
    findings.extend(score_findings)
    for topic in manifest.get("expected_topics", []):
        if not mentions_text(contract, topic):
            findings.append(f"missing expected topic: {topic}")
    artifact_report = artifact_policy.protected_changes(
        working_folder,
        before,
        exclude_prefixes=[".workflow/artifacts/eval-agent"],
    )
    if artifact_report["protected_changes"]:
        findings.append(f"Eval Agent modified files outside its report area: {artifact_report['protected_changes']}")
    return {"status": "fail" if findings else "pass", "findings": findings, "artifact_policy": artifact_report}


def score_gate_findings(contract: dict[str, Any]) -> list[str]:
    scores = contract.get("scores") or {}
    values = [float(value) for value in scores.values()]
    if not values:
        return ["scores are empty"]
    findings = []
    average = sum(values) / len(values)
    minimum = min(values)
    if contract.get("status") == "pass":
        if average < 8:
            findings.append(f"pass status with average score below 8: {average:.2f}")
        if minimum < 6:
            findings.append(f"pass status with score below 6: {minimum:.2f}")
    low_unexplained = [
        name
        for name, value in scores.items()
        if float(value) < 8 and not low_score_is_explained(name, contract)
    ]
    if low_unexplained:
        findings.append(f"low scores not explained by category: {low_unexplained}")
    return findings


def low_score_is_explained(score_name: str, contract: dict[str, Any]) -> bool:
    evidence = {"findings": contract.get("findings", []), "human_review_focus": contract.get("human_review_focus", [])}
    keywords = {
        "spec_alignment": ["spec", "alignment", "scope"],
        "maintainability": ["maintainability", "maintain", "complexity"],
        "architecture_fit": ["architecture", "architectural", "fit", "overengineering"],
        "risk_awareness": ["risk", "assumption"],
        "trajectory_quality": ["trajectory", "builder", "agent"],
        "pr_summary_quality": ["pr", "summary", "review"],
    }
    return any(mentions_text(evidence, keyword) for keyword in keywords.get(score_name, [score_name]))


def verifier_status(working_folder: Path) -> str | None:
    contracts = list((working_folder / eval_agent.VERIFIER_REPORT_ROOT).rglob("contract.json"))
    if not contracts:
        return None
    return spec_agent.read_json(contracts[0]).get("status")


def mentions_text(value: Any, needle: str) -> bool:
    return needle.lower() in json.dumps(value, sort_keys=True).lower()


def aggregate_findings(case_results: list[dict[str, Any]]) -> list[str]:
    findings = []
    for result in case_results:
        for finding in result.get("findings", []):
            findings.append(f"{result.get('name')}: {finding}")
    return findings


def combined_tool_use(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {"tool_calls": 0, "tools": {}, "compactions": 0, "pre_compaction_archives": 0}
    for result in case_results:
        tool_use = result.get("tool_use") or {}
        summary["tool_calls"] += int(tool_use.get("tool_calls") or 0)
        summary["compactions"] += int(tool_use.get("compactions") or 0)
        summary["pre_compaction_archives"] += int(tool_use.get("pre_compaction_archives") or 0)
        for name, count in (tool_use.get("tools") or {}).items():
            summary["tools"][name] = summary["tools"].get(name, 0) + int(count)
    return summary


def combined_artifact_policy(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    return artifact_policy.merge_reports(*(result.get("artifact_policy") or {} for result in case_results))


def fixture_for_ladder(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": "Eval Agent qualitative quality suite",
        "issue": {
            "title": "Evaluate verified Builder Agent outputs",
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
    report = {
        "status": "pass" if winner else "fail" if final else "running",
        "fixture": cases_root.as_posix(),
        "judge_model": judge_model,
        "winner_model": eval_spec_agent.model_display_name(winner) if winner else None,
        "model_ladder": ladder_report,
        "total_cost": eval_spec_agent.sum_costs(attempts),
        "attempts": attempts,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def eval_cache_key(cases_root: Path, judge_model: str) -> str:
    digest = hashlib.sha256()
    paths = [
        eval_agent.SELECTED_MODEL_PATH,
        Path(".workflow/model_ladder.json"),
        Path("scripts/agent_runtime.py"),
        Path("scripts/artifact_policy.py"),
        Path("scripts/spec_agent.py"),
        Path("scripts/eval_agent.py"),
        Path("scripts/eval_eval_agent.py"),
    ]
    if cases_root.exists():
        paths.extend(path for path in sorted(cases_root.rglob("*")) if path.is_file())
    paths.extend(sorted(Path(".workflow/agents/eval/static").glob("*.md")))
    paths.extend(sorted(Path(".workflow/agents/common/static").glob("*.md")))
    for path in paths:
        if not path.exists():
            continue
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    digest.update(judge_model.encode("utf-8"))
    return digest.hexdigest()[:24]


def update_selected_model(winner: dict[str, Any], report_path: Path, judge_model: str) -> None:
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
    eval_agent.SELECTED_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    eval_agent.SELECTED_MODEL_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
class attempt_time_limit:
    def __init__(self, label: str):
        self.label = label
        self.seconds = max(1, test_agent.openrouter_timeout_ms() // 1000)
        self.previous_handler = None

    def __enter__(self):
        self.previous_handler = signal.getsignal(signal.SIGALRM)

        def timeout_handler(_signum, _frame):
            raise AttemptTimeoutError(f"{self.label} timed out after {self.seconds}s")

        signal.signal(signal.SIGALRM, timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)

    def __exit__(self, _exc_type, _exc, _tb):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
