#!/usr/bin/env python3
"""Run the Eval Agent over a verified task workspace."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

import eval_runtime
import spec_agent
import agent_runtime
from agent_runtime import AgentRunError, ToolRuntime, build_runtime_metadata, run_tool_agent, write_runtime_log


REPORT_ROOT = Path(".workflow/artifacts/eval-agent")
AGENT_ROOT = Path(".workflow/agents/eval")
COMMON_STATIC_CONTEXT_ROOT = Path(".workflow/agents/common/static")
STATIC_CONTEXT_ROOT = AGENT_ROOT / "static"
TEMPLATE_ROOT = AGENT_ROOT / "templates"
SELECTED_MODEL_PATH = AGENT_ROOT / "selected_model.json"
PROJECT_CONTEXT_FILES = [
    Path("AGENTS.md"),
    Path("README.md"),
    Path("pyproject.toml"),
    Path("requirements.txt"),
    Path("uv.lock"),
    Path("package.json"),
    Path("package-lock.json"),
    Path("pnpm-lock.yaml"),
    Path("yarn.lock"),
    Path("bun.lockb"),
    Path("Cargo.toml"),
    Path("Cargo.lock"),
    Path("go.mod"),
    Path("go.sum"),
    Path("composer.json"),
    Path("composer.lock"),
    Path("Gemfile"),
    Path("Gemfile.lock"),
    Path("mix.exs"),
    Path("deno.json"),
    Path("vite.config.js"),
    Path("vite.config.mjs"),
    Path("vite.config.ts"),
    Path("playwright.config.js"),
    Path("playwright.config.cjs"),
    Path("playwright.config.mjs"),
]
SPEC_REPORT_ROOTS = [Path("docs/specs"), Path(".workflow/artifacts/specs"), Path(".workflow/artifacts/spec-agent")]
TEST_REPORT_ROOT = Path(".workflow/artifacts/test-agent")
BUILDER_REPORT_ROOT = Path(".workflow/artifacts/builder-agent")
VERIFIER_REPORT_ROOT = Path(".workflow/artifacts/verifier-agent")
FORENSIC_STAGE_ROOTS = {
    "spec": Path(".workflow/artifacts/specs/_runtime"),
    "test": TEST_REPORT_ROOT,
    "builder": BUILDER_REPORT_ROOT,
    "verifier": VERIFIER_REPORT_ROOT,
}


def main() -> int:
    args = parse_args()
    os.environ["OPENROUTER_TIMEOUT_MS"] = str(args.request_timeout_ms)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report_dir, _, _ = generate_eval_artifacts(
        working_folder=args.working_folder,
        generated_at=generated_at,
        report_root=args.report_root,
    )
    print(report_dir)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--working-folder", type=Path, default=Path("."))
    parser.add_argument("--report-root", type=Path, default=REPORT_ROOT)
    parser.add_argument("--request-timeout-ms", type=int, default=120_000)
    return parser.parse_args()


def generate_eval_artifacts(
    *,
    working_folder: Path,
    generated_at: str,
    report_root: Path = REPORT_ROOT,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    working_folder = working_folder.resolve()
    dynamic_context = build_dynamic_context(
        working_folder=working_folder,
        generated_at=generated_at,
        report_root=report_root,
    )
    contract, usage = generate_contract(dynamic_context=dynamic_context, working_folder=working_folder)
    validate_contract_for_context(contract, dynamic_context)
    report_dir = working_folder / report_root
    write_artifacts(report_dir=report_dir, contract=contract, dynamic_context=dynamic_context, generated_at=generated_at)
    return report_dir, contract, usage


def build_dynamic_context(*, working_folder: Path, generated_at: str, report_root: Path) -> dict[str, Any]:
    return {
        "source": "pipeline_workspace",
        "workspace": {
            "working_folder": working_folder.as_posix(),
            "report_root": report_root.as_posix(),
            "spec_report_roots": [root.as_posix() for root in SPEC_REPORT_ROOTS],
            "test_report_root": TEST_REPORT_ROOT.as_posix(),
            "builder_report_root": BUILDER_REPORT_ROOT.as_posix(),
            "verifier_report_root": VERIFIER_REPORT_ROOT.as_posix(),
        },
        "tools": {
            "available": agent_runtime.available_tool_names(),
            "todo_required": True,
        },
        "artifacts": artifact_inventory(working_folder),
        "spec_reports": read_contract_summaries_from_roots([working_folder / root for root in SPEC_REPORT_ROOTS], "contract.json"),
        "test_reports": read_contract_summaries(working_folder / TEST_REPORT_ROOT, "contract.json"),
        "builder_reports": read_contract_summaries(working_folder / BUILDER_REPORT_ROOT, "contract.json"),
        "verifier_reports": read_contract_summaries(working_folder / VERIFIER_REPORT_ROOT, "contract.json"),
        "upstream_evidence": agent_runtime.upstream_evidence_context(working_folder),
        "pipeline_state": read_pipeline_state(working_folder),
        "runtime_metadata": read_runtime_metadata(working_folder),
        "runtime_forensics": read_runtime_forensics(working_folder),
        "deterministic_facts": deterministic_facts(working_folder),
        "generated_at": generated_at,
    }


def generate_contract(*, dynamic_context: dict[str, Any], working_folder: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required; Eval Agent has no non-AI generation path")
    return generate_contract_with_openrouter(dynamic_context=dynamic_context, working_folder=working_folder)


def generate_contract_with_openrouter(
    *,
    dynamic_context: dict[str, Any],
    working_folder: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    model = selected_model()
    agent_context = load_agent_context(working_folder)
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=eval_agent_contract_schema(),
        max_cost_usd=float(os.environ.get("EVAL_AGENT_MAX_COST_USD", "0.25")),
        max_seconds=int(os.environ.get("EVAL_AGENT_MAX_SECONDS", "300")),
        context_window_tokens=agent_context["selected_model"].get("context_length"),
        final_validator=lambda contract: validate_contract_for_context(contract, dynamic_context),
        live_log_root=working_folder / dynamic_context["workspace"]["report_root"],
        live_event_log_paths=[working_folder / ".workflow/runtime_events.log"],
        live_event_prefix="stage=eval ",
        write_enabled=False,
    )
    try:
        result = run_tool_agent(
            model=model,
            system=agent_context["system"],
            user=eval_prompt(agent_context, dynamic_context),
            runtime=runtime,
        )
    except AgentRunError as exc:
        result = exc.result
        write_runtime_log(
            working_folder / dynamic_context["workspace"]["report_root"],
            result.transcript,
            result.tool_events,
            result.compaction_events,
            result.pre_compaction_archives,
            metadata=build_runtime_metadata("eval", model, agent_context["selected_model"], result, status="error", error=str(exc)),
        )
        raise
    write_runtime_log(
        working_folder / dynamic_context["workspace"]["report_root"],
        result.transcript,
        result.tool_events,
        result.compaction_events,
        result.pre_compaction_archives,
        metadata=build_runtime_metadata("eval", model, agent_context["selected_model"], result),
    )
    if result.final_report is None:
        raise RuntimeError("Eval Agent finished without final_report")
    return result.final_report, result.usage


def eval_agent_contract_schema() -> dict[str, Any]:
    score_schema = {"type": "number", "minimum": 0, "maximum": 10}
    return {
        "type": "object",
        "additionalProperties": True,
        "required": [
            "status",
            "scores",
            "findings",
            "root_cause_stage",
            "trajectory_findings",
            "artifact_findings",
            "tooling_findings",
            "cost_findings",
            "human_review_focus",
            "safe_to_merge",
        ],
        "properties": {
            "status": {"type": "string", "enum": ["pass", "fail", "needs_human_review"]},
            "scores": {
                "type": "object",
                "additionalProperties": True,
                "required": [
                    "spec_alignment",
                    "maintainability",
                    "architecture_fit",
                    "risk_awareness",
                    "trajectory_quality",
                    "pr_summary_quality",
                ],
                "properties": {
                    "spec_alignment": score_schema,
                    "maintainability": score_schema,
                    "architecture_fit": score_schema,
                    "risk_awareness": score_schema,
                    "trajectory_quality": score_schema,
                    "pr_summary_quality": score_schema,
                },
            },
            "findings": spec_agent.string_array_schema(),
            "human_review_focus": spec_agent.string_array_schema(),
            "root_cause_stage": {"type": "string"},
            "trajectory_findings": spec_agent.string_array_schema(),
            "artifact_findings": spec_agent.string_array_schema(),
            "tooling_findings": spec_agent.string_array_schema(),
            "cost_findings": spec_agent.string_array_schema(),
            "safe_to_merge": {"type": "boolean"},
        },
    }


def selected_model() -> str:
    env_model = os.environ.get("OPENROUTER_MODEL")
    if env_model:
        return env_model
    if SELECTED_MODEL_PATH.exists():
        data = eval_runtime.read_selected_model(SELECTED_MODEL_PATH)
        model = data.get("model")
        if isinstance(model, str) and model:
            return model
    return "openai/gpt-4.1-mini"


def selected_model_metadata() -> dict[str, Any]:
    env_model = os.environ.get("OPENROUTER_MODEL")
    if env_model:
        return {"model": env_model, "source": "OPENROUTER_MODEL"}
    if SELECTED_MODEL_PATH.exists():
        return eval_runtime.read_selected_model(SELECTED_MODEL_PATH)
    return {"model": "openai/gpt-4.1-mini", "source": "fallback"}


def load_agent_context(working_folder: Path) -> dict[str, Any]:
    common_static_files = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(COMMON_STATIC_CONTEXT_ROOT.glob("*.md"))
    }
    static_files = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(STATIC_CONTEXT_ROOT.glob("*.md"))
    }
    return {
        "system": static_files.get("system.md", ""),
        "common_static_files": common_static_files,
        "static_files": static_files,
        "project_file_index": project_file_index(working_folder),
        "selected_model": selected_model_metadata(),
    }


def eval_prompt(agent_context: dict[str, Any], dynamic_context: dict[str, Any]) -> str:
    project_context = json.dumps(agent_context["project_file_index"], indent=2, sort_keys=True)
    common_context = spec_agent.format_context_block("Common Agent Runtime Context", agent_context["common_static_files"])
    static_context = spec_agent.format_context_block(
        "Eval Agent Static Context",
        {
            key: value
            for key, value in agent_context["static_files"].items()
            if key != "system.md"
        },
    )
    return f"""Project Context File Index:
```json
{project_context}
```

{common_context}

{static_context}

Selected Model:
```json
{json.dumps(agent_context["selected_model"], indent=2, sort_keys=True)}
```

Dynamic Context:
```json
{json.dumps(dynamic_context, indent=2, sort_keys=True)}
```

Use the available tools to inspect prior-stage artifacts and relevant implementation files.
Dynamic context contains file paths, status summaries, and counts only. Read files from the workspace when you need detailed evidence.
The deterministic_facts block is system-generated evidence. Do not contradict it. If a stage failed after writing files, report both facts: the stage failed and files exist.
Use todo tools to track the evaluation work.
You must not edit any files.
Evaluate the full pipeline trajectory even when an earlier stage failed or Verifier did not run.
If Verifier status is missing or not pass, return fail and safe_to_merge false.
Perform forensic analysis of the agent pathway: tool sequence, failed calls, skipped stages, handoff quality, runtime/cost behavior, and artifact quality.
Finish only by calling final_report with the Eval Agent contract.
"""


def validate_contract(contract: dict[str, Any]) -> None:
    required = [
        "status",
        "scores",
        "findings",
        "root_cause_stage",
        "trajectory_findings",
        "artifact_findings",
        "tooling_findings",
        "cost_findings",
        "human_review_focus",
        "safe_to_merge",
    ]
    missing = [field for field in required if field not in contract]
    if missing:
        raise ValueError(f"eval contract missing required fields: {', '.join(missing)}")
    if contract["status"] not in {"pass", "fail", "needs_human_review"}:
        raise ValueError("eval status must be pass, fail, or needs_human_review")
    if contract["safe_to_merge"] is True and contract["status"] != "pass":
        raise ValueError("safe_to_merge may be true only when status is pass")
    if contract["status"] == "fail" and not contract.get("findings"):
        raise ValueError("failed eval must include findings")
    if contract["root_cause_stage"] not in {"spec", "test", "builder", "verifier", "eval", "pipeline", "unknown"}:
        raise ValueError("root_cause_stage must identify a pipeline stage or unknown")
    for field in ("trajectory_findings", "artifact_findings", "tooling_findings", "cost_findings", "human_review_focus", "findings"):
        if not isinstance(contract.get(field), list):
            raise ValueError(f"{field} must be a list")
    required_scores = [
        "spec_alignment",
        "maintainability",
        "architecture_fit",
        "risk_awareness",
        "trajectory_quality",
        "pr_summary_quality",
    ]
    scores = contract.get("scores") or {}
    missing_scores = [score for score in required_scores if score not in scores]
    if missing_scores:
        raise ValueError(f"eval scores missing required fields: {', '.join(missing_scores)}")
    for name in required_scores:
        value = float(scores[name])
        if value < 0 or value > 10:
            raise ValueError(f"eval score out of range for {name}: {value}")


def validate_contract_for_context(contract: dict[str, Any], dynamic_context: dict[str, Any]) -> None:
    validate_contract(contract)
    if not verifier_passed(dynamic_context):
        if contract["status"] == "pass":
            raise ValueError("eval cannot pass when verifier status is missing or not pass")
        if contract["safe_to_merge"] is True:
            raise ValueError("eval cannot mark safe_to_merge true when verifier status is missing or not pass")
    validate_fact_consistency(contract, dynamic_context)


def validate_fact_consistency(contract: dict[str, Any], dynamic_context: dict[str, Any]) -> None:
    facts = dynamic_context.get("deterministic_facts")
    if not isinstance(facts, dict):
        return
    text = contract_text(contract)
    builder = facts.get("stages", {}).get("builder", {}) if isinstance(facts.get("stages"), dict) else {}
    if isinstance(builder, dict):
        todo_calls = int(builder.get("todo_calls") or 0)
        if todo_calls > 0 and contains_any(text, ["no evidence of todo usage", "no todo usage", "todo discipline was not observed"]):
            raise ValueError("eval contradicts deterministic facts: builder todo calls exist")
        files_written = int(builder.get("files_written_count") or 0)
        workspace_files = int(builder.get("workspace_file_count") or 0)
        if (files_written > 0 or workspace_files > 0) and contains_any(
            text,
            [
                "no implementation artifacts",
                "no implementation code",
                "failed to generate any implementation code",
                "non-existent build output",
            ],
        ):
            raise ValueError("eval contradicts deterministic facts: builder wrote implementation/workspace files")
        if not stages_overlap(dynamic_context, "test", "builder") and contains_any(text, ["builder started concurrently", "concurrently with test"]):
            raise ValueError("eval contradicts deterministic facts: builder did not overlap test runtime")
    approvals = facts.get("approvals") if isinstance(facts.get("approvals"), dict) else {}
    if approvals and contains_any(text, ["no human sign-off recorded", "no human approval recorded", "without approval recorded"]):
        raise ValueError("eval contradicts deterministic facts: approvals are recorded")


def contract_text(contract: dict[str, Any]) -> str:
    parts: list[str] = []
    for value in contract.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif isinstance(value, dict):
            parts.append(json.dumps(value, sort_keys=True))
    return "\n".join(parts).lower()


def contains_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def stages_overlap(dynamic_context: dict[str, Any], first: str, second: str) -> bool:
    stages = dynamic_context.get("deterministic_facts", {}).get("stages", {})
    if not isinstance(stages, dict):
        return False
    first_stage = stages.get(first)
    second_stage = stages.get(second)
    if not isinstance(first_stage, dict) or not isinstance(second_stage, dict):
        return False
    first_start = first_stage.get("started_at")
    first_end = first_stage.get("ended_at")
    second_start = second_stage.get("started_at")
    second_end = second_stage.get("ended_at")
    if not all(isinstance(value, str) and value for value in (first_start, first_end, second_start, second_end)):
        return False
    return first_start < second_end and second_start < first_end


def verifier_passed(dynamic_context: dict[str, Any]) -> bool:
    verifier_reports = dynamic_context.get("verifier_reports")
    if isinstance(verifier_reports, dict):
        for report in verifier_reports.values():
            if isinstance(report, dict) and report.get("status") == "pass":
                return True
    task_state = dynamic_context.get("pipeline_state", {}).get("task_state", {})
    if isinstance(task_state, dict):
        stage_status = task_state.get("stage_status")
        if isinstance(stage_status, dict):
            verifier = stage_status.get("verifier")
            if isinstance(verifier, dict) and verifier.get("status") == "passed":
                return True
    return False


def write_artifacts(
    *,
    report_dir: Path,
    contract: dict[str, Any],
    dynamic_context: dict[str, Any],
    generated_at: str,
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "contract.json").write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (report_dir / "dynamic_context.json").write_text(json.dumps(dynamic_context, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (report_dir / "context_snapshot.md").write_text(render_context_snapshot(dynamic_context), encoding="utf-8")
    report_context = {
        "status": contract["status"],
        "scores": json.dumps(contract.get("scores", {}), indent=2, sort_keys=True),
        "findings": spec_agent.md_list(contract.get("findings", [])),
        "human_review_focus": spec_agent.md_list(contract.get("human_review_focus", [])),
        "root_cause_stage": str(contract.get("root_cause_stage") or ""),
        "trajectory_findings": spec_agent.md_list(contract.get("trajectory_findings", [])),
        "artifact_findings": spec_agent.md_list(contract.get("artifact_findings", [])),
        "tooling_findings": spec_agent.md_list(contract.get("tooling_findings", [])),
        "cost_findings": spec_agent.md_list(contract.get("cost_findings", [])),
        "safe_to_merge": str(contract.get("safe_to_merge", False)),
        "generated_at": generated_at,
    }
    template = (TEMPLATE_ROOT / "eval_report.md").read_text(encoding="utf-8")
    rendered = Template(template.replace("{{ ", "${").replace(" }}", "}")).safe_substitute(report_context)
    (report_dir / "eval_report.md").write_text(rendered, encoding="utf-8")


def render_context_snapshot(dynamic_context: dict[str, Any]) -> str:
    working_folder = Path(dynamic_context["workspace"]["working_folder"])
    agent_context = load_agent_context(working_folder)
    return (
        "# Eval Agent Context Snapshot\n\n"
        "## Project Context File Index\n\n```json\n"
        + json.dumps(agent_context["project_file_index"], indent=2, sort_keys=True)
        + "\n```\n"
        + "\n\n## Common Static Context Files\n\n"
        + spec_agent.md_list(agent_context["common_static_files"].keys())
        + "\n\n## Static Context Files\n\n"
        + spec_agent.md_list(agent_context["static_files"].keys())
        + "\n\n## Selected Model\n\n```json\n"
        + json.dumps(agent_context["selected_model"], indent=2, sort_keys=True)
        + "\n```\n\n## Dynamic Context\n\n```json\n"
        + json.dumps(dynamic_context, indent=2, sort_keys=True)
        + "\n```\n"
    )


def project_file_index(working_folder: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for relative_path in PROJECT_CONTEXT_FILES:
        path = working_folder / relative_path
        if path.exists() and path.is_file():
            files.append({"path": relative_path.as_posix(), "bytes": path.stat().st_size})
    return files


def read_json_files(root: Path, name: str) -> dict[str, Any]:
    if not root.exists():
        return {}
    results = {}
    for path in sorted(root.rglob(name)):
        results[path.relative_to(root).as_posix()] = spec_agent.read_json(path)
    return results


def read_contract_summaries(root: Path, name: str) -> dict[str, Any]:
    if not root.exists():
        return {}
    summaries: dict[str, Any] = {}
    for path in sorted(root.rglob(name)):
        summaries[path.relative_to(root).as_posix()] = summarize_contract(path)
    return summaries


def read_contract_summaries_from_roots(roots: list[Path], name: str) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for root in roots:
        for path, payload in read_contract_summaries(root, name).items():
            summaries[f"{root.as_posix()}/{path}"] = payload
    return summaries


def summarize_contract(path: Path) -> dict[str, Any]:
    contract = spec_agent.read_json(path)
    summary: dict[str, Any] = {
        "path": path.as_posix(),
        "keys": sorted(str(key) for key in contract.keys()),
    }
    for key in (
        "status",
        "summary",
        "tests_passing",
        "safe_to_open_pr",
        "safe_to_merge",
        "requires_human_approval",
        "requires_verifier",
    ):
        if key in contract:
            summary[key] = summarize_scalar(contract[key])
    for key in (
        "scope",
        "non_goals",
        "acceptance_criteria",
        "test_plan",
        "risks",
        "files_changed",
        "tests_run",
        "failures_remaining",
        "checks_run",
        "scope_violations",
        "security_findings",
        "required_actions",
        "findings",
        "human_review_focus",
    ):
        if isinstance(contract.get(key), list):
            summary[f"{key}_count"] = len(contract[key])
    return summary


def summarize_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return truncate_value(value, 500)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return type(value).__name__


def artifact_inventory(working_folder: Path) -> dict[str, Any]:
    roots = {
        "spec": SPEC_REPORT_ROOTS,
        "test": [TEST_REPORT_ROOT],
        "builder": [BUILDER_REPORT_ROOT],
        "verifier": [VERIFIER_REPORT_ROOT],
    }
    inventory: dict[str, Any] = {}
    for stage, relative_roots in roots.items():
        files: list[dict[str, Any]] = []
        for relative_root in relative_roots:
            root = working_folder / relative_root
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    files.append(
                        {
                            "path": path.relative_to(working_folder).as_posix(),
                            "bytes": path.stat().st_size,
                        }
                    )
        inventory[stage] = {"files": files, "file_count": len(files)}
    return inventory


def read_pipeline_state(working_folder: Path) -> dict[str, Any]:
    state_path = working_folder / ".workflow/state.json"
    if not state_path.exists():
        return {}
    global_state = spec_agent.read_json(state_path)
    current_task = global_state.get("current_task")
    task_state_path = working_folder / ".workflow/tasks" / str(current_task) / "state.json"
    if not current_task or not task_state_path.exists():
        return {"global_state": global_state}
    return {
        "global_state": global_state,
        "task_state": spec_agent.read_json(task_state_path),
    }


def read_runtime_metadata(working_folder: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for stage, root in FORENSIC_STAGE_ROOTS.items():
        path = working_folder / root / "runtime_metadata.json"
        if path.exists():
            metadata[stage] = spec_agent.read_json(path)
    return metadata


def read_runtime_forensics(working_folder: Path) -> dict[str, Any]:
    forensics: dict[str, Any] = {}
    for stage, relative_root in FORENSIC_STAGE_ROOTS.items():
        root = working_folder / relative_root
        if not root.exists():
            continue
        stage_data: dict[str, Any] = {
            "root": relative_root.as_posix(),
            "evidence_files": existing_relative_files(
                working_folder,
                [
                    root / "runtime_events.log",
                    root / "runtime_timeline.md",
                    root / "tool_events.json",
                    root / "tool_calls.md",
                    root / "runtime_metadata.json",
                    root / "transcript.json",
                ],
            ),
            "tool_summary": summarize_tool_events(root / "tool_events.json"),
        }
        metadata_path = root / "runtime_metadata.json"
        if metadata_path.exists():
            stage_data["runtime_metadata"] = spec_agent.read_json(metadata_path)
        forensics[stage] = stage_data
    return forensics


def deterministic_facts(working_folder: Path) -> dict[str, Any]:
    pipeline_state = read_pipeline_state(working_folder)
    runtime_metadata = read_runtime_metadata(working_folder)
    facts: dict[str, Any] = {
        "stage_status": stage_status_facts(pipeline_state),
        "approvals": approval_facts(pipeline_state),
        "verifier_present": (working_folder / VERIFIER_REPORT_ROOT / "contract.json").exists(),
        "stages": {},
        "workspace_files": workspace_file_facts(working_folder),
    }
    for stage, root in FORENSIC_STAGE_ROOTS.items():
        tool_events = read_tool_event_list(working_folder / root / "tool_events.json")
        metadata = runtime_metadata.get(stage, {}) if isinstance(runtime_metadata.get(stage), dict) else {}
        stage_fact = stage_tool_facts(tool_events)
        stage_fact["runtime_status"] = metadata.get("status")
        stage_fact["runtime_error"] = metadata.get("error")
        stage_fact["final_report_present"] = metadata.get("final_report_present")
        stage_fact["model"] = metadata.get("model")
        stage_fact["started_at"] = metadata.get("started_at")
        stage_fact["ended_at"] = metadata.get("ended_at")
        usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
        stage_fact["cost"] = usage.get("cost")
        stage_fact["total_tokens"] = usage.get("total_tokens")
        if stage == "builder":
            stage_fact.update(builder_artifact_facts(working_folder, tool_events))
        facts["stages"][stage] = stage_fact
    return facts


def stage_status_facts(pipeline_state: dict[str, Any]) -> dict[str, Any]:
    task_state = pipeline_state.get("task_state") if isinstance(pipeline_state.get("task_state"), dict) else {}
    statuses = task_state.get("stage_status") if isinstance(task_state.get("stage_status"), dict) else {}
    facts: dict[str, Any] = {}
    for stage, payload in statuses.items():
        if isinstance(payload, dict):
            facts[str(stage)] = {
                key: payload.get(key)
                for key in ("status", "error", "contract", "report_dir", "cost", "safe_to_merge")
                if key in payload
            }
    return facts


def approval_facts(pipeline_state: dict[str, Any]) -> dict[str, Any]:
    task_state = pipeline_state.get("task_state") if isinstance(pipeline_state.get("task_state"), dict) else {}
    approvals = task_state.get("approvals") if isinstance(task_state.get("approvals"), dict) else {}
    facts: dict[str, Any] = {}
    for stage, payload in approvals.items():
        if isinstance(payload, dict):
            facts[str(stage)] = {
                key: payload.get(key)
                for key in ("approved_at", "approved_by", "note")
                if key in payload
            }
    return facts


def stage_tool_facts(tool_events: list[dict[str, Any]]) -> dict[str, Any]:
    summary = summarize_tool_event_list(tool_events)
    return {
        "tool_calls": summary["total"],
        "tool_errors": summary["errors"],
        "tools": summary["by_tool"],
        "todo_calls": int(summary["by_tool"].get("todo_read", 0)) + int(summary["by_tool"].get("todo_write", 0)),
        "write_file_calls": int(summary["by_tool"].get("write_file", 0)),
        "bash_calls": int(summary["by_tool"].get("bash", 0)),
    }


def builder_artifact_facts(working_folder: Path, tool_events: list[dict[str, Any]]) -> dict[str, Any]:
    written_paths = sorted(
        {
            str((event.get("result") or {}).get("path") or (event.get("arguments") or {}).get("path"))
            for event in tool_events
            if event.get("name") == "write_file" and isinstance(event.get("result"), dict) and event["result"].get("ok") is True
        }
        - {""}
    )
    workspace_files = workspace_file_facts(working_folder)
    return {
        "files_written_count": len(written_paths),
        "files_written": written_paths[:80],
        "workspace_file_count": workspace_files["file_count"],
        "workspace_files_sample": workspace_files["files"][:80],
    }


def workspace_file_facts(working_folder: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(working_folder.rglob("*")):
        if not path.is_file() or ignored_workspace_path(path, working_folder):
            continue
        files.append({"path": path.relative_to(working_folder).as_posix(), "bytes": path.stat().st_size})
    return {"file_count": len(files), "files": files[:120], "truncated": len(files) > 120}


def ignored_workspace_path(path: Path, working_folder: Path) -> bool:
    relative = path.relative_to(working_folder)
    ignored_roots = {".workflow", "node_modules", ".git", "dist", "build", "coverage", ".pytest_cache", "__pycache__"}
    if relative.parts and relative.parts[0] in ignored_roots:
        return True
    ignored_names = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "uv.lock", "Cargo.lock", "composer.lock", "Gemfile.lock"}
    return path.name in ignored_names


def existing_relative_files(working_folder: Path, paths: list[Path]) -> list[str]:
    return [path.relative_to(working_folder).as_posix() for path in paths if path.exists()]


def summarize_tool_events(path: Path) -> dict[str, Any]:
    events = read_tool_event_list(path)
    return summarize_tool_event_list(events)


def summarize_tool_event_list(events: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"total": len(events), "by_tool": {}, "errors": 0}
    by_tool: dict[str, int] = {}
    errors = 0
    for event in events:
        name = str(event.get("name") or "unknown")
        by_tool[name] = by_tool.get(name, 0) + 1
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if result.get("ok") is False:
            errors += 1
    summary["by_tool"] = dict(sorted(by_tool.items()))
    summary["errors"] = errors
    return summary


def read_tool_event_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = spec_agent.read_json(path)
    if not isinstance(payload, list):
        return []
    return [event for event in payload if isinstance(event, dict)]


def truncate_value(value: Any, max_chars: int) -> str:
    text = str(value).replace("\n", " ")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


if __name__ == "__main__":
    raise SystemExit(main())
