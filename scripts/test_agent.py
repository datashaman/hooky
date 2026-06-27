#!/usr/bin/env python3
"""Generate Test Agent artifacts from an approved Spec Agent contract."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

import spec_agent
from agent_runtime import ToolRuntime, run_tool_agent


ARTIFACT_ROOT = Path("tests/generated")
REPORT_ROOT = Path(".workflow/artifacts/test-agent")
AGENT_ROOT = Path(".workflow/agents/test")
COMMON_STATIC_CONTEXT_ROOT = Path(".workflow/agents/common/static")
STATIC_CONTEXT_ROOT = AGENT_ROOT / "static"
TEMPLATE_ROOT = AGENT_ROOT / "templates"
SELECTED_MODEL_PATH = AGENT_ROOT / "selected_model.json"
PROJECT_CONTEXT_FILES = [Path("AGENTS.md")]


def main() -> int:
    args = parse_args()
    os.environ["OPENROUTER_TIMEOUT_MS"] = str(args.request_timeout_ms)
    approved_spec = spec_agent.read_json(args.spec_contract)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    output_dir, _, _ = generate_test_artifacts(
        approved_spec=approved_spec,
        spec_source=args.spec_contract.as_posix(),
        generated_at=generated_at,
        working_folder=Path("."),
        artifact_root=args.artifact_root,
        report_root=args.report_root,
    )
    print(output_dir)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec-contract", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, default=ARTIFACT_ROOT)
    parser.add_argument("--report-root", type=Path, default=REPORT_ROOT)
    parser.add_argument("--request-timeout-ms", type=int, default=120_000)
    return parser.parse_args()


def generate_test_artifacts(
    *,
    approved_spec: dict[str, Any],
    spec_source: str,
    generated_at: str,
    working_folder: Path = Path("."),
    project_root: Path = Path("."),
    artifact_root: Path = ARTIFACT_ROOT,
    report_root: Path = REPORT_ROOT,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    dynamic_context = build_dynamic_context(
        approved_spec=approved_spec,
        spec_source=spec_source,
        generated_at=generated_at,
        working_folder=working_folder,
        project_root=project_root,
        artifact_root=artifact_root,
        report_root=report_root,
    )
    contract, usage = generate_contract(dynamic_context=dynamic_context, project_root=project_root)
    validate_contract(contract, approved_spec)

    slug = spec_agent.slugify(approved_spec.get("summary", "approved-spec"))
    output_dir = artifact_root / slug
    report_dir = report_root / slug
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    write_artifacts(
        output_dir=output_dir,
        report_dir=report_dir,
        contract=contract,
        dynamic_context=dynamic_context,
        spec_source=spec_source,
        generated_at=generated_at,
        title=approved_spec.get("summary", "Approved Spec"),
    )
    return output_dir, contract, usage


def generate_contract(*, dynamic_context: dict[str, Any], project_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required; Test Agent has no non-AI generation path")
    return generate_contract_with_openrouter(dynamic_context, project_root)


def generate_contract_with_openrouter(
    dynamic_context: dict[str, Any],
    project_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    model = selected_model()
    agent_context = load_agent_context(project_root)
    runtime = ToolRuntime(
        working_folder=dynamic_context["workspace"]["working_folder"],
        final_report_schema=test_agent_contract_schema(),
        max_cost_usd=float(os.environ.get("TEST_AGENT_MAX_COST_USD", "0.25")),
        max_seconds=int(os.environ.get("TEST_AGENT_MAX_SECONDS", "300")),
        final_validator=lambda contract: validate_contract(contract, dynamic_context["approved_spec"]),
    )
    result = run_tool_agent(
        model=model,
        system=agent_context["system"],
        user=test_prompt(agent_context, dynamic_context),
        runtime=runtime,
    )
    write_runtime_log(Path(dynamic_context["workspace"]["report_root"]), result.transcript, result.tool_events)
    return result.final_report, result.usage


def artifact_array_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": True,
            "required": ["path", "purpose", "content"],
            "properties": {
                "path": {"type": "string"},
                "purpose": {"type": "string"},
                "content": {"type": "string"},
            },
        },
    }


def test_agent_contract_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "required": [
            "summary",
            "test_files",
            "fixtures",
            "coverage_targets",
            "acceptance_criteria_covered",
            "acceptance_criteria_uncovered",
            "untestable_requirements",
            "requires_human_approval",
        ],
        "properties": {
            "summary": {"type": "string"},
            "test_files": artifact_array_schema(),
            "fixtures": artifact_array_schema(),
            "coverage_targets": spec_agent.string_array_schema(),
            "acceptance_criteria_covered": spec_agent.string_array_schema(),
            "acceptance_criteria_uncovered": spec_agent.string_array_schema(),
            "untestable_requirements": spec_agent.string_array_schema(),
            "requires_human_approval": {"type": "boolean"},
        },
    }


def openrouter_timeout_ms() -> int:
    return int(os.environ.get("OPENROUTER_TIMEOUT_MS", "120000"))


def selected_model() -> str:
    env_model = os.environ.get("OPENROUTER_MODEL")
    if env_model:
        return env_model
    if SELECTED_MODEL_PATH.exists():
        data = spec_agent.read_json(SELECTED_MODEL_PATH)
        model = data.get("model")
        if isinstance(model, str) and model:
            return model
    return "openai/gpt-4.1-mini"


def build_dynamic_context(
    *,
    approved_spec: dict[str, Any],
    spec_source: str,
    generated_at: str,
    working_folder: Path,
    project_root: Path,
    artifact_root: Path,
    report_root: Path,
) -> dict[str, Any]:
    return {
        "source": "approved_spec_contract",
        "spec_source": spec_source,
        "approved_spec": approved_spec,
        "workspace": {
            "working_folder": working_folder.as_posix(),
            "project_root": project_root.as_posix(),
            "artifact_root": artifact_root.as_posix(),
            "report_root": report_root.as_posix(),
        },
        "tools": {
            "available": [
                "read_file",
                "write_file",
                "list_files",
                "grep_files",
                "find_files",
                "bash",
                "todo_read",
                "todo_write",
            ],
            "todo_required": True,
        },
        "generated_at": generated_at,
    }


def load_agent_context(project_root: Path = Path(".")) -> dict[str, Any]:
    common_static_files = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(COMMON_STATIC_CONTEXT_ROOT.glob("*.md"))
    }
    static_files = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(STATIC_CONTEXT_ROOT.glob("*.md"))
    }
    project_files = {
        path.as_posix(): (project_root / path).read_text(encoding="utf-8")
        for path in PROJECT_CONTEXT_FILES
        if (project_root / path).exists()
    }
    return {
        "system": static_files.get("system.md", ""),
        "common_static_files": common_static_files,
        "static_files": static_files,
        "project_files": project_files,
        "selected_model": selected_model_metadata(),
    }


def selected_model_metadata() -> dict[str, Any]:
    env_model = os.environ.get("OPENROUTER_MODEL")
    if env_model:
        return {"model": env_model, "source": "OPENROUTER_MODEL"}
    if SELECTED_MODEL_PATH.exists():
        return spec_agent.read_json(SELECTED_MODEL_PATH)
    return {"model": "openai/gpt-4.1-mini", "source": "fallback"}


def test_prompt(agent_context: dict[str, Any], dynamic_context: dict[str, Any]) -> str:
    project_context = spec_agent.format_context_block("Project Context", agent_context["project_files"])
    common_context = spec_agent.format_context_block("Common Agent Runtime Context", agent_context["common_static_files"])
    static_context = spec_agent.format_context_block(
        "Test Agent Static Context",
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

You have filesystem and shell tools scoped to the working folder. Use the todo tools to plan and track work.
Inspect the project context and existing files as needed. Write executable test artifacts under the configured artifact_root.
You may run commands to check syntax or test discovery when useful. Do not write production implementation.
Finish only by calling final_report with the Test Agent contract. The contract must list every test file and fixture you created, including each file's content.
"""


def validate_contract(contract: dict[str, Any], approved_spec: dict[str, Any]) -> None:
    required = [
        "summary",
        "test_files",
        "fixtures",
        "coverage_targets",
        "acceptance_criteria_covered",
        "acceptance_criteria_uncovered",
        "untestable_requirements",
        "requires_human_approval",
    ]
    missing = [field for field in required if field not in contract]
    if missing:
        raise ValueError(f"test contract missing required fields: {', '.join(missing)}")
    if contract["requires_human_approval"] is not True:
        raise ValueError("test contract must require human approval")
    if not contract["test_files"]:
        raise ValueError("test contract must include test files")
    approved_criteria = set(approved_spec.get("acceptance_criteria", []))
    covered = set(contract.get("acceptance_criteria_covered", []))
    uncovered = set(contract.get("acceptance_criteria_uncovered", []))
    missing_criteria = approved_criteria - covered - uncovered
    if missing_criteria:
        raise ValueError(f"acceptance criteria missing from coverage lists: {sorted(missing_criteria)}")


def write_runtime_log(report_root: Path, transcript: list[dict[str, Any]], tool_events: list[dict[str, Any]]) -> None:
    report_root.mkdir(parents=True, exist_ok=True)
    (report_root / "runtime_transcript.json").write_text(json.dumps(transcript, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (report_root / "tool_events.json").write_text(json.dumps(tool_events, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_artifacts(
    *,
    output_dir: Path,
    report_dir: Path,
    contract: dict[str, Any],
    dynamic_context: dict[str, Any],
    spec_source: str,
    generated_at: str,
    title: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    for test_file in contract.get("test_files", []):
        path = output_dir / Path(test_file["path"]).name
        path.write_text(test_file["content"], encoding="utf-8")

    for fixture in contract.get("fixtures", []):
        path = output_dir / Path(fixture["path"]).name
        path.write_text(fixture["content"], encoding="utf-8")

    (report_dir / "contract.json").write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (report_dir / "dynamic_context.json").write_text(json.dumps(dynamic_context, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (report_dir / "context_snapshot.md").write_text(render_context_snapshot(dynamic_context), encoding="utf-8")

    report_context = {
        "title": title,
        "spec_source": spec_source,
        "generated_at": generated_at,
        "summary": contract["summary"],
        "test_files": spec_agent.md_list([f"{item['path']}: {item['purpose']}" for item in contract.get("test_files", [])]),
        "fixtures": spec_agent.md_list([f"{item['path']}: {item['purpose']}" for item in contract.get("fixtures", [])]),
        "coverage_targets": spec_agent.md_list(contract.get("coverage_targets", [])),
        "acceptance_criteria_covered": spec_agent.md_list(contract.get("acceptance_criteria_covered", [])),
        "acceptance_criteria_uncovered": spec_agent.md_list(contract.get("acceptance_criteria_uncovered", [])),
        "untestable_requirements": spec_agent.md_list(contract.get("untestable_requirements", [])),
    }
    template = (TEMPLATE_ROOT / "test_report.md").read_text(encoding="utf-8")
    rendered = Template(template.replace("{{ ", "${").replace(" }}", "}")).safe_substitute(report_context)
    (report_dir / "test_report.md").write_text(rendered, encoding="utf-8")


def render_context_snapshot(dynamic_context: dict[str, Any]) -> str:
    project_root = Path(dynamic_context["workspace"].get("project_root", "."))
    agent_context = load_agent_context(project_root)
    return (
        "# Test Agent Context Snapshot\n\n"
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


if __name__ == "__main__":
    raise SystemExit(main())
