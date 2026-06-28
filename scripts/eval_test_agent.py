#!/usr/bin/env python3
"""Run Test Agent against fixtures and climb a model ladder until eval passes."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import eval_spec_agent
import artifact_policy
import eval_runtime
import spec_agent
import test_agent


DEFAULT_FIXTURE = Path("tests/fixtures/test_agent/todomvc_spec_contract.json")
DEFAULT_PROJECT_FIXTURE = Path("tests/fixtures/projects/todomvc")
DEFAULT_LADDER = Path(".workflow/model_ladder.json")
DEFAULT_RUN_ROOT = Path(".workflow/eval-runs/test-agent")
DEFAULT_CACHE_ROOT = Path(".workflow/eval-cache/test-agent")


class AttemptTimeoutError(TimeoutError):
    pass


def main() -> int:
    args = parse_args()
    fixture = spec_agent.read_json(args.fixture)
    ladder_config = spec_agent.read_json(args.model_ladder)
    judge_model = args.judge_model or ladder_config["judge"]

    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required for Test Agent eval")
    os.environ["OPENROUTER_TIMEOUT_MS"] = str(args.request_timeout_ms)

    ladder_report = eval_spec_agent.resolve_model_ladder(
        args,
        ladder_config,
        {"issue": fixture["approved_spec"]},
        agent_key="test_agent",
    )
    runnable_ladder = [item for item in ladder_report if item["available"] and item["estimated_cost"] is not None]
    if not runnable_ladder:
        raise RuntimeError("No runnable candidate models available from OpenRouter /models")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = args.run_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    cache_key = eval_cache_key(args.fixture, args.project_fixture, judge_model)
    cache_root = args.cache_root / cache_key

    eval_spec_agent.print_ladder_summary(runnable_ladder)

    attempts = []
    winner = None
    report_path = run_root / "report.json"
    for model_info in runnable_ladder:
        print(f"\nstarting_model: {eval_runtime.model_display_name(model_info)}", flush=True)
        attempt = run_attempt(fixture, model_info, judge_model, run_root, cache_root, args.project_fixture, args.no_cache)
        attempts.append(attempt)
        eval_spec_agent.print_attempt(attempt)
        write_report(report_path, fixture, judge_model, ladder_report, attempts, winner, final=False)
        if attempt["status"] == "pass":
            winner = attempt
            write_report(report_path, fixture, judge_model, ladder_report, attempts, winner, final=True)
            if not args.no_update_selected_model:
                update_selected_model(winner, report_path, judge_model)
            break

    write_report(report_path, fixture, judge_model, ladder_report, attempts, winner, final=True)
    print(f"\nreport: {report_path}")
    eval_spec_agent.cleanup_runs(args.run_root, args.keep_runs)
    if winner:
        print(f"passed_with: {eval_runtime.model_display_name(winner)}")
        return 0
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--project-fixture", type=Path, default=DEFAULT_PROJECT_FIXTURE)
    parser.add_argument("--model-ladder", type=Path, default=DEFAULT_LADDER)
    parser.add_argument("--profile", help="Model ladder profile from .workflow/model_ladder.json")
    parser.add_argument("--models", nargs="+", help="Override test model ladder")
    parser.add_argument("--max-models", type=int, help="Override max models to try from the API-priced ladder")
    parser.add_argument("--print-ladder", action="store_true", help="Print API-priced model ladder and exit")
    parser.add_argument("--judge-model", help="Override judge model")
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-update-selected-model", action="store_true")
    parser.add_argument("--keep-runs", type=int, default=10)
    parser.add_argument("--request-timeout-ms", type=int, default=120_000)
    return parser.parse_args()


def run_attempt(
    fixture: dict[str, Any],
    model_info: dict[str, Any],
    judge_model: str,
    run_root: Path,
    cache_root: Path,
    project_fixture: Path,
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
        cached.setdefault("tool_use", eval_spec_agent.tool_use_summary(Path(cached["artifact_dir"]) if cached.get("artifact_dir") else None))
        cached.setdefault("artifact_policy", {})
        return cached

    attempt_dir = run_root / eval_spec_agent.safe_name(variant_id)
    prepare_attempt_workspace(project_fixture, attempt_dir)
    previous_model = os.environ.get("OPENROUTER_MODEL")
    previous_reasoning = os.environ.get("OPENROUTER_REASONING")
    previous_context_length = os.environ.get("OPENROUTER_CONTEXT_LENGTH")
    os.environ["OPENROUTER_MODEL"] = model
    eval_spec_agent.apply_reasoning_env(model_info)
    eval_spec_agent.apply_context_length_env(model_info)
    try:
        with attempt_time_limit("generation"):
            output_dir, contract, test_usage = test_agent.generate_test_artifacts(
                approved_spec=fixture["approved_spec"],
                spec_source=args_fixture_source(fixture),
                generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                working_folder=attempt_dir,
                project_root=attempt_dir,
                artifact_root=attempt_dir / "tests/generated",
                report_root=attempt_dir / ".workflow/artifacts/test-agent",
            )
    except Exception as exc:  # noqa: BLE001
        eval_spec_agent.restore_model(previous_model)
        eval_spec_agent.restore_reasoning(previous_reasoning)
        eval_spec_agent.restore_context_length(previous_context_length)
        attempt = {
            "model": model,
            "variant_id": variant_id,
            "reasoning_request": model_info.get("reasoning_request"),
            "context_length": model_info.get("context_length"),
            "tool_use": eval_spec_agent.tool_use_summary(attempt_dir),
            "artifact_policy": {},
            "estimated_cost": model_info.get("estimated_cost"),
            "status": "fail",
            "artifact_dir": str(attempt_dir),
            "cached": False,
            "cost": 0,
            "deterministic": {"status": "fail", "findings": [f"generation failed: {exc}"]},
            "judge": None,
        }
        eval_spec_agent.write_attempt_cache(cache_path, attempt)
        return attempt
    finally:
        eval_spec_agent.restore_model(previous_model)
        eval_spec_agent.restore_reasoning(previous_reasoning)
        eval_spec_agent.restore_context_length(previous_context_length)

    deterministic = deterministic_eval(contract, output_dir, fixture["approved_spec"], fixture["expect"])
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
            "artifact_dir": str(output_dir),
            "cached": False,
            "cost": eval_spec_agent.sum_costs([{"usage": test_usage}]),
            "usage": {"test": test_usage, "judge": {}},
            "deterministic": deterministic,
            "judge": None,
        }
        eval_spec_agent.write_attempt_cache(cache_path, attempt)
        return attempt

    try:
        with attempt_time_limit("judge"):
            judge, judge_usage = judge_eval(judge_model, fixture, contract, project_fixture)
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
            "artifact_dir": str(output_dir),
            "cached": False,
            "cost": eval_spec_agent.sum_costs([{"usage": test_usage}]),
            "usage": {"test": test_usage, "judge": {}},
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
        "artifact_dir": str(output_dir),
        "cached": False,
        "cost": eval_spec_agent.sum_costs([{"usage": test_usage}, {"usage": judge_usage}]),
        "usage": {"test": test_usage, "judge": judge_usage},
        "deterministic": deterministic,
        "judge": judge,
    }
    eval_spec_agent.write_attempt_cache(cache_path, attempt)
    return attempt


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


def prepare_attempt_workspace(project_fixture: Path, attempt_dir: Path) -> None:
    if not project_fixture.exists():
        raise FileNotFoundError(f"project fixture does not exist: {project_fixture}")
    shutil.copytree(project_fixture, attempt_dir, dirs_exist_ok=True)


def args_fixture_source(fixture: dict[str, Any]) -> str:
    return fixture.get("name", "fixture")


def deterministic_eval(
    contract: dict[str, Any],
    output_dir: Path,
    approved_spec: dict[str, Any],
    expect: dict[str, Any],
) -> dict[str, Any]:
    findings: list[str] = []
    if contract.get("requires_human_approval") is not True:
        findings.append("requires_human_approval must be true")
    if len(contract.get("test_files", [])) < expect["min_test_files"]:
        findings.append("too few test files")
    all_content = json.dumps(contract, sort_keys=True).lower()
    test_case_count = count_test_cases(contract)
    if test_case_count < expect["min_test_cases"]:
        findings.append(f"too few test cases: {test_case_count}")
    for topic in expect["required_topics"]:
        if topic.lower() not in all_content:
            findings.append(f"missing required topic: {topic}")
    for topic in expect["forbidden_topics"]:
        if topic.lower() in all_content:
            findings.append(f"contains forbidden topic: {topic}")
    if contract.get("acceptance_criteria_uncovered"):
        findings.append("acceptance criteria uncovered")
    approved = set(approved_spec.get("acceptance_criteria", []))
    covered = set(contract.get("acceptance_criteria_covered", []))
    missing = approved - covered
    if missing:
        findings.append(f"missing covered criteria: {len(missing)}")
    required_artifacts = sorted(
        {Path(item["path"]).name for item in contract.get("test_files", [])}
        | {Path(item["path"]).name for item in contract.get("fixtures", [])}
    )
    artifact_report = artifact_policy.validate_files(output_dir, required_files=required_artifacts)
    if not artifact_report.get("produced_files"):
        findings.append("no generated test files written")
    findings.extend(artifact_report["findings"])
    return {"status": "fail" if findings else "pass", "findings": findings, "artifact_policy": artifact_report}


def count_test_cases(contract: dict[str, Any]) -> int:
    count = 0
    for item in contract.get("test_files", []):
        content = item.get("content", "")
        count += len(re_find_tests(content))
    return count


def re_find_tests(content: str) -> list[str]:
    import re

    return re.findall(r"\b(?:test|it)\s*\(", content)


def judge_eval(
    judge_model: str,
    fixture: dict[str, Any],
    contract: dict[str, Any],
    project_fixture: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from openrouter import OpenRouter

    with OpenRouter(api_key=os.environ["OPENROUTER_API_KEY"], timeout_ms=test_agent.openrouter_timeout_ms()) as client:
        completion = client.chat.send(
            model=judge_model,
            response_format=spec_agent.structured_response_format(
                "test_agent_eval",
                spec_agent.agent_eval_schema(
                    [
                        "spec_coverage",
                        "behavior_focus",
                        "test_quality",
                        "fixture_quality",
                        "scope_discipline",
                        "maintainability",
                    ]
                ),
            ),
            **spec_agent.openrouter_request_options(),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are evaluating a Test Agent output. Return only JSON. "
                        "Penalize production implementation, uncovered acceptance criteria, weak behavioral tests, "
                        "and tests coupled to hidden implementation details. Do not penalize use of project-approved "
                        "public selectors or conventions."
                    ),
                },
                {"role": "user", "content": judge_prompt(fixture, contract, project_fixture)},
            ],
        )
    content = completion.choices[0].message.content
    if not content:
        raise RuntimeError("judge returned empty response")
    return json.loads(content), spec_agent.response_usage(completion)


def judge_prompt(fixture: dict[str, Any], contract: dict[str, Any], project_fixture: Path) -> str:
    return f"""Approved spec:
{json.dumps(fixture["approved_spec"], indent=2, sort_keys=True)}

Project context:
```markdown
{project_context(project_fixture)}
```

Test Agent contract:
{json.dumps(contract, indent=2, sort_keys=True)}

Evaluate against this JSON schema:
{{
  "status": "pass|fail|needs_human_review",
  "scores": {{
    "spec_coverage": 0,
    "behavior_focus": 0,
    "test_quality": 0,
    "fixture_quality": 0,
    "scope_discipline": 0,
    "maintainability": 0
  }},
  "critical_findings": ["string"],
  "findings": ["string"],
  "human_review_focus": ["string"]
}}

Pass criteria: average score >= 8, no score below 6, and no critical findings.
Use the project context when deciding whether a selector, route, storage assertion, or test convention is public contract or hidden implementation detail.
"""


def project_context(project_fixture: Path) -> str:
    context_file = project_fixture / "AGENTS.md"
    if not context_file.exists():
        return ""
    return context_file.read_text(encoding="utf-8")


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
        total_cost=eval_spec_agent.sum_costs(attempts),
    )


def eval_cache_key(fixture_path: Path, project_fixture: Path, judge_model: str) -> str:
    digest = hashlib.sha256()
    paths = [
        fixture_path,
        test_agent.SELECTED_MODEL_PATH,
        Path(".workflow/model_ladder.json"),
        Path("scripts/agent_runtime.py"),
        Path("scripts/artifact_policy.py"),
        Path("scripts/spec_agent.py"),
        Path("scripts/test_agent.py"),
        Path("scripts/eval_test_agent.py"),
    ]
    if project_fixture.exists():
        paths.extend(path for path in sorted(project_fixture.rglob("*")) if path.is_file())
    paths.extend(sorted(Path(".workflow/agents/test/static").glob("*.md")))
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
    eval_runtime.write_selected_model(test_agent.SELECTED_MODEL_PATH, winner, report_path, judge_model)


if __name__ == "__main__":
    raise SystemExit(main())
