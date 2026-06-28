#!/usr/bin/env python3
"""Run Builder Agent against approved tests and climb a model ladder until eval passes."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import artifact_policy
import builder_agent
import command_result
import eval_runtime
import eval_spec_agent
import eval_test_agent
import spec_agent
import test_agent


DEFAULT_WORKSPACE_FIXTURE = Path("tests/fixtures/builder_agent/todomvc_approved_tests")
DEFAULT_LADDER = Path(".workflow/model_ladder.json")
DEFAULT_RUN_ROOT = Path(".workflow/eval-runs/builder-agent")
DEFAULT_CACHE_ROOT = Path(".workflow/eval-cache/builder-agent")


class AttemptTimeoutError(TimeoutError):
    pass


def main() -> int:
    args = parse_args()
    ladder_config = spec_agent.read_json(args.model_ladder)
    judge_model = args.judge_model or ladder_config["judge"]

    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required for Builder Agent eval")
    os.environ["OPENROUTER_TIMEOUT_MS"] = str(args.request_timeout_ms)

    fixture_summary = fixture_for_ladder(args.workspace_fixture)
    ladder_report = eval_spec_agent.resolve_model_ladder(args, ladder_config, fixture_summary, agent_key="builder_agent")
    runnable_ladder = [item for item in ladder_report if item["available"] and item["estimated_cost"] is not None]
    if not runnable_ladder:
        raise RuntimeError("No runnable candidate models available from OpenRouter /models")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = args.run_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    cache_key = eval_cache_key(args.workspace_fixture, judge_model)
    cache_root = args.cache_root / cache_key

    eval_spec_agent.print_ladder_summary(runnable_ladder)

    attempts: list[dict[str, Any]] = []
    winner = None
    report_path = run_root / "report.json"
    for model_info in runnable_ladder:
        print(f"\nstarting_model: {eval_runtime.model_display_name(model_info)}", flush=True)
        attempt = run_attempt(
            model_info=model_info,
            judge_model=judge_model,
            run_root=run_root,
            cache_root=cache_root,
            workspace_fixture=args.workspace_fixture,
            no_cache=args.no_cache,
            command_timeout=args.command_timeout,
        )
        attempts.append(attempt)
        eval_spec_agent.print_attempt(attempt)
        write_report(report_path, args.workspace_fixture, judge_model, ladder_report, attempts, winner, final=False)
        if attempt["status"] == "pass":
            winner = attempt
            write_report(report_path, args.workspace_fixture, judge_model, ladder_report, attempts, winner, final=True)
            if not args.no_update_selected_model:
                update_selected_model(winner, report_path, judge_model)
            break

    write_report(report_path, args.workspace_fixture, judge_model, ladder_report, attempts, winner, final=True)
    eval_runtime.update_report_index(
        "builder-agent",
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
    parser.add_argument("--workspace-fixture", type=Path, default=DEFAULT_WORKSPACE_FIXTURE)
    parser.add_argument("--model-ladder", type=Path, default=DEFAULT_LADDER)
    parser.add_argument("--profile", help="Model ladder profile from .workflow/model_ladder.json")
    parser.add_argument("--models", nargs="+", help="Override builder model ladder")
    parser.add_argument("--max-models", type=int, help="Override max models to try from the API-priced ladder")
    parser.add_argument("--print-ladder", action="store_true", help="Print API-priced model ladder and exit")
    parser.add_argument("--judge-model", help="Override judge model")
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-update-selected-model", action="store_true")
    parser.add_argument("--keep-runs", type=int, default=10)
    parser.add_argument("--request-timeout-ms", type=int, default=120_000)
    parser.add_argument("--command-timeout", type=int, default=120)
    return parser.parse_args()


def run_attempt(
    *,
    model_info: dict[str, Any],
    judge_model: str,
    run_root: Path,
    cache_root: Path,
    workspace_fixture: Path,
    no_cache: bool,
    command_timeout: int,
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
        cached.setdefault("tool_use", eval_spec_agent.tool_use_summary(Path(cached["artifact_dir"]) if cached.get("artifact_dir") else None))
        cached.setdefault("artifact_policy", {})
        return cached

    attempt_dir = run_root / eval_spec_agent.safe_name(variant_id)
    prepare_attempt_workspace(workspace_fixture, attempt_dir)
    protected_hashes = protected_file_hashes(attempt_dir)
    previous_model = os.environ.get("OPENROUTER_MODEL")
    previous_reasoning = os.environ.get("OPENROUTER_REASONING")
    previous_context_length = os.environ.get("OPENROUTER_CONTEXT_LENGTH")
    os.environ["OPENROUTER_MODEL"] = model
    eval_spec_agent.apply_reasoning_env(model_info)
    eval_spec_agent.apply_context_length_env(model_info)
    try:
        with attempt_time_limit("generation"):
            report_dir, contract, build_usage = builder_agent.generate_build_artifacts(
                working_folder=attempt_dir,
                generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            )
    except Exception as exc:  # noqa: BLE001
        eval_spec_agent.restore_model(previous_model)
        eval_spec_agent.restore_reasoning(previous_reasoning)
        eval_spec_agent.restore_context_length(previous_context_length)
        attempt = failed_attempt(model, model_info, attempt_dir, f"generation failed: {exc}")
        eval_spec_agent.write_attempt_cache(cache_path, attempt)
        return attempt
    finally:
        eval_spec_agent.restore_model(previous_model)
        eval_spec_agent.restore_reasoning(previous_reasoning)
        eval_spec_agent.restore_context_length(previous_context_length)

    deterministic = deterministic_eval(attempt_dir, contract, protected_hashes, command_timeout)
    if deterministic["status"] == "fail":
        attempt = {
            "model": model,
            "variant_id": variant_id,
            "reasoning_request": model_info.get("reasoning_request"),
            "context_length": model_info.get("context_length"),
            "tool_use": eval_spec_agent.tool_use_summary(attempt_dir),
            "artifact_policy": deterministic.get("artifact_policy", {}),
            "estimated_cost": model_info.get("estimated_cost"),
            "status": "fail",
            "artifact_dir": str(report_dir),
            "cached": False,
            "cost": eval_spec_agent.sum_costs([{"usage": build_usage}]),
            "usage": {"builder": build_usage, "judge": {}},
            "deterministic": deterministic,
            "judge": None,
        }
        eval_spec_agent.write_attempt_cache(cache_path, attempt)
        return attempt

    try:
        with attempt_time_limit("judge"):
            judge, judge_usage = judge_eval(judge_model, attempt_dir, contract, deterministic)
    except Exception as exc:  # noqa: BLE001
        attempt = {
            "model": model,
            "variant_id": variant_id,
            "reasoning_request": model_info.get("reasoning_request"),
            "context_length": model_info.get("context_length"),
            "tool_use": eval_spec_agent.tool_use_summary(attempt_dir),
            "artifact_policy": deterministic.get("artifact_policy", {}),
            "estimated_cost": model_info.get("estimated_cost"),
            "status": "fail",
            "artifact_dir": str(report_dir),
            "cached": False,
            "cost": eval_spec_agent.sum_costs([{"usage": build_usage}]),
            "usage": {"builder": build_usage, "judge": {}},
            "deterministic": deterministic,
            "judge": {"status": "fail", "critical_findings": [f"judge failed: {exc}"]},
        }
        eval_spec_agent.write_attempt_cache(cache_path, attempt)
        return attempt

    status = "pass" if judge_passes(judge) else "fail"
    attempt = {
        "model": model,
        "variant_id": variant_id,
        "reasoning_request": model_info.get("reasoning_request"),
        "context_length": model_info.get("context_length"),
        "tool_use": eval_spec_agent.tool_use_summary(attempt_dir),
        "artifact_policy": deterministic.get("artifact_policy", {}),
        "estimated_cost": model_info.get("estimated_cost"),
        "status": status,
        "artifact_dir": str(report_dir),
        "cached": False,
        "cost": eval_spec_agent.sum_costs([{"usage": build_usage}, {"usage": judge_usage}]),
        "usage": {"builder": build_usage, "judge": judge_usage},
        "deterministic": deterministic,
        "judge": judge,
    }
    eval_spec_agent.write_attempt_cache(cache_path, attempt)
    return attempt


def failed_attempt(model: str, model_info: dict[str, Any], attempt_dir: Path, finding: str) -> dict[str, Any]:
    return {
        "model": model,
        "variant_id": model_info.get("variant_id", model),
        "reasoning_request": model_info.get("reasoning_request"),
        "context_length": model_info.get("context_length"),
        "tool_use": eval_spec_agent.tool_use_summary(attempt_dir),
        "artifact_policy": {},
        "estimated_cost": model_info.get("estimated_cost"),
        "status": "fail",
        "artifact_dir": str(attempt_dir),
        "cached": False,
        "cost": 0,
        "deterministic": {"status": "fail", "findings": [finding]},
        "judge": None,
    }


@contextlib.contextmanager
def attempt_time_limit(label: str):
    seconds = max(1, test_agent.openrouter_timeout_ms() // 1000)
    previous_handler = signal.getsignal(signal.SIGALRM)

    def timeout_handler(_signum, _frame):
        raise AttemptTimeoutError(f"{label} timed out after {seconds}s")

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def prepare_attempt_workspace(workspace_fixture: Path, attempt_dir: Path) -> None:
    if not workspace_fixture.exists():
        raise FileNotFoundError(f"workspace fixture does not exist: {workspace_fixture}")
    shutil.copytree(workspace_fixture, attempt_dir, dirs_exist_ok=True)


def protected_file_hashes(working_folder: Path) -> dict[str, str]:
    return artifact_policy.snapshot(working_folder, include_prefixes=builder_protected_prefixes())


def deterministic_eval(
    working_folder: Path,
    contract: dict[str, Any],
    protected_hashes: dict[str, str],
    command_timeout: int,
) -> dict[str, Any]:
    findings: list[str] = []
    if contract.get("requires_verifier") is not True:
        findings.append("requires_verifier must be true")
    if contract.get("tests_passing") is not True:
        findings.append("builder did not claim tests_passing true")
    artifact_report = artifact_policy.protected_changes(
        working_folder,
        protected_hashes,
        include_prefixes=builder_protected_prefixes(),
    )
    if artifact_report["protected_changes"]:
        findings.append(f"approved tests or prior-stage artifacts changed: {artifact_report['protected_changes']}")
    implementation_files = [item["path"] for item in contract.get("file_writes", [])]
    if not implementation_files:
        findings.append("no production implementation files written")
    install = command_result.run_command(["npm", "install"], working_folder, command_timeout)
    test_result = command_result.run_command(["npm", "test"], working_folder, command_timeout)
    if install["returncode"] != 0:
        findings.append("npm install failed")
    if test_result["returncode"] != 0:
        findings.append("npm test failed")
    return {
        "status": "fail" if findings else "pass",
        "findings": findings,
        "commands": {
            "npm install": command_result.redacted(install),
            "npm test": command_result.redacted(test_result),
        },
        "artifact_policy": artifact_report,
        "implementation_files": implementation_files,
    }


def builder_protected_prefixes() -> list[str]:
    policy = artifact_policy.load_policy(Path(".workflow/agents/builder/artifact_policy.json"))
    return list(policy["protected_prefixes"])


def judge_eval(
    judge_model: str,
    working_folder: Path,
    contract: dict[str, Any],
    deterministic: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    from openrouter import OpenRouter

    with OpenRouter(api_key=os.environ["OPENROUTER_API_KEY"], timeout_ms=test_agent.openrouter_timeout_ms()) as client:
        completion = client.chat.send(
            model=judge_model,
            response_format=spec_agent.structured_response_format(
                "builder_agent_eval",
                spec_agent.agent_eval_schema(
                    [
                        "test_alignment",
                        "scope_discipline",
                        "implementation_quality",
                        "maintainability",
                        "dependency_discipline",
                        "trajectory_quality",
                    ]
                ),
            ),
            **spec_agent.openrouter_request_options(),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are evaluating a Builder Agent output. Return only JSON. "
                        "Penalize test edits, scope drift, hidden dependencies, brittle implementation, "
                        "and overengineering. Do not require elegance beyond the approved tests."
                    ),
                },
                {"role": "user", "content": judge_prompt(working_folder, contract, deterministic)},
            ],
        )
    content = completion.choices[0].message.content
    if not content:
        raise RuntimeError("judge returned empty response")
    return json.loads(content), spec_agent.response_usage(completion)


def judge_prompt(working_folder: Path, contract: dict[str, Any], deterministic: dict[str, Any]) -> str:
    implementation_files = {
        path: (working_folder / path).read_text(encoding="utf-8")
        for path in deterministic.get("implementation_files", [])
        if (working_folder / path).exists()
    }
    test_contract = spec_agent.read_json(builder_agent.approved_test_contract_path(working_folder))
    return f"""Approved Test Agent contract:
{json.dumps(test_contract, indent=2, sort_keys=True)}

Builder Agent contract:
{json.dumps(contract, indent=2, sort_keys=True)}

Deterministic verification:
{json.dumps(deterministic, indent=2, sort_keys=True)}

Implementation files:
{json.dumps(implementation_files, indent=2, sort_keys=True)}

Evaluate against this JSON schema:
{{
  "status": "pass|fail|needs_human_review",
  "scores": {{
    "test_alignment": 0,
    "scope_discipline": 0,
    "implementation_quality": 0,
    "maintainability": 0,
    "dependency_discipline": 0,
    "trajectory_quality": 0
  }},
  "critical_findings": ["string"],
  "findings": ["string"],
  "human_review_focus": ["string"]
}}

Pass criteria: deterministic verification must pass, average score >= 8, no score below 6, and no critical findings.
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


def fixture_for_ladder(workspace_fixture: Path) -> dict[str, Any]:
    contract_path = builder_agent.approved_test_contract_path(workspace_fixture)
    contract = spec_agent.read_json(contract_path)
    return {
        "name": "TodoMVC approved tests to implementation",
        "issue": {
            "title": contract.get("summary", "Approved tests"),
            "body": json.dumps(contract, sort_keys=True),
        },
    }


def write_report(
    report_path: Path,
    workspace_fixture: Path,
    judge_model: str,
    ladder_report: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    winner: dict[str, Any] | None,
    final: bool,
) -> None:
    eval_runtime.write_report(
        report_path,
        status=eval_runtime.report_status(winner, final),
        fixture=workspace_fixture.as_posix(),
        judge_model=judge_model,
        ladder_report=ladder_report,
        attempts=attempts,
        winner=winner,
        total_cost=eval_spec_agent.sum_costs(attempts),
    )


def eval_cache_key(workspace_fixture: Path, judge_model: str) -> str:
    paths = [
        builder_agent.SELECTED_MODEL_PATH,
        Path(".workflow/model_ladder.json"),
        Path("scripts/agent_runtime.py"),
        Path("scripts/artifact_policy.py"),
        Path("scripts/command_result.py"),
        Path(".workflow/agents/builder/artifact_policy.json"),
        Path("scripts/spec_agent.py"),
        Path("scripts/builder_agent.py"),
        Path("scripts/eval_builder_agent.py"),
    ]
    if workspace_fixture.exists():
        paths.extend(path for path in sorted(workspace_fixture.rglob("*")) if path.is_file())
    paths.extend(sorted(Path(".workflow/agents/builder/static").glob("*.md")))
    paths.extend(sorted(Path(".workflow/agents/common/static").glob("*.md")))
    return eval_runtime.cache_key(paths, judge_model)


def update_selected_model(winner: dict[str, Any], report_path: Path, judge_model: str) -> None:
    eval_runtime.write_selected_model(builder_agent.SELECTED_MODEL_PATH, winner, report_path, judge_model)
if __name__ == "__main__":
    raise SystemExit(main())
