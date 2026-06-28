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
PROJECT_CONTEXT_FILES = [Path("AGENTS.md"), Path("README.md"), Path("package.json")]
SPEC_REPORT_ROOTS = [Path("docs/specs"), Path(".workflow/artifacts/specs"), Path(".workflow/artifacts/spec-agent")]
TEST_REPORT_ROOT = Path(".workflow/artifacts/test-agent")
BUILDER_REPORT_ROOT = Path(".workflow/artifacts/builder-agent")
VERIFIER_REPORT_ROOT = Path(".workflow/artifacts/verifier-agent")


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
    validate_contract(contract)
    report_dir = working_folder / report_root
    write_artifacts(report_dir=report_dir, contract=contract, dynamic_context=dynamic_context, generated_at=generated_at)
    return report_dir, contract, usage


def build_dynamic_context(*, working_folder: Path, generated_at: str, report_root: Path) -> dict[str, Any]:
    return {
        "source": "verified_builder_workspace",
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
        "spec_reports": read_json_files_from_roots([working_folder / root for root in SPEC_REPORT_ROOTS], "contract.json"),
        "test_reports": read_json_files(working_folder / TEST_REPORT_ROOT, "contract.json"),
        "builder_reports": read_json_files(working_folder / BUILDER_REPORT_ROOT, "contract.json"),
        "verifier_reports": read_json_files(working_folder / VERIFIER_REPORT_ROOT, "contract.json"),
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
        final_validator=validate_contract,
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
    project_files = {
        path.as_posix(): (working_folder / path).read_text(encoding="utf-8")
        for path in PROJECT_CONTEXT_FILES
        if (working_folder / path).exists()
    }
    return {
        "system": static_files.get("system.md", ""),
        "common_static_files": common_static_files,
        "static_files": static_files,
        "project_files": project_files,
        "selected_model": selected_model_metadata(),
    }


def eval_prompt(agent_context: dict[str, Any], dynamic_context: dict[str, Any]) -> str:
    project_context = spec_agent.format_context_block("Project Context", agent_context["project_files"])
    common_context = spec_agent.format_context_block("Common Agent Runtime Context", agent_context["common_static_files"])
    static_context = spec_agent.format_context_block(
        "Eval Agent Static Context",
        {
            key: value
            for key, value in agent_context["static_files"].items()
            if key != "system.md"
        },
    )
    return f"""{project_context}

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
Use todo tools to track the evaluation work.
You must not edit any files.
If Verifier status is not pass, return fail and safe_to_merge false.
Finish only by calling final_report with the Eval Agent contract.
"""


def validate_contract(contract: dict[str, Any]) -> None:
    required = ["status", "scores", "findings", "human_review_focus", "safe_to_merge"]
    missing = [field for field in required if field not in contract]
    if missing:
        raise ValueError(f"eval contract missing required fields: {', '.join(missing)}")
    if contract["status"] not in {"pass", "fail", "needs_human_review"}:
        raise ValueError("eval status must be pass, fail, or needs_human_review")
    if contract["safe_to_merge"] is True and contract["status"] != "pass":
        raise ValueError("safe_to_merge may be true only when status is pass")
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
        "## Project Context Files\n\n"
        + spec_agent.md_list(agent_context["project_files"].keys())
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


def read_json_files(root: Path, name: str) -> dict[str, Any]:
    if not root.exists():
        return {}
    results = {}
    for path in sorted(root.rglob(name)):
        results[path.relative_to(root).as_posix()] = spec_agent.read_json(path)
    return results


def read_json_files_from_roots(roots: list[Path], name: str) -> dict[str, Any]:
    results = {}
    for root in roots:
        for path, payload in read_json_files(root, name).items():
            results[f"{root.as_posix()}/{path}"] = payload
    return results


if __name__ == "__main__":
    raise SystemExit(main())
