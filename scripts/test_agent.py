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

import agent_runtime
import eval_runtime
import spec_agent
from agent_runtime import AgentRunError, ToolRuntime, build_runtime_metadata, run_tool_agent, write_runtime_log


REPORT_ROOT = Path(".workflow/artifacts/test-agent")
AGENT_ROOT = Path(".workflow/agents/test")
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
TOOLCHAIN_FILE_NAMES = {
    "bun.lockb",
    "Cargo.lock",
    "Cargo.toml",
    "composer.json",
    "composer.lock",
    "deno.json",
    "Gemfile",
    "Gemfile.lock",
    "go.mod",
    "go.sum",
    "mix.exs",
    "package-lock.json",
    "package.json",
    "playwright.config.cjs",
    "playwright.config.js",
    "playwright.config.mjs",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
    "vite.config.js",
    "vite.config.mjs",
    "vite.config.ts",
    "yarn.lock",
}
INSTALL_COMMAND_BLOCKLIST = [
    "bundle install",
    "cargo install",
    "composer install",
    "composer require",
    "go get ",
    "mix deps.get",
    "npm add",
    "npm i ",
    "npm install",
    "pip install",
    "playwright install",
    "pnpm add",
    "pnpm install",
    "poetry add",
    "poetry install",
    "uv add",
    "uv pip install",
    "yarn add",
    "yarn install",
]


def main() -> int:
    args = parse_args()
    os.environ["OPENROUTER_TIMEOUT_MS"] = str(args.request_timeout_ms)
    approved_spec = spec_agent.read_json(args.spec_contract)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report_dir, _, _ = generate_test_artifacts(
        approved_spec=approved_spec,
        spec_source=args.spec_contract.as_posix(),
        generated_at=generated_at,
        working_folder=Path("."),
        artifact_root=args.artifact_root,
        report_root=args.report_root,
    )
    print(report_dir)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec-contract", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, default=None, help=argparse.SUPPRESS)
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
    artifact_root: Path | None = None,
    report_root: Path = REPORT_ROOT,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    dynamic_context = build_dynamic_context(
        approved_spec=approved_spec,
        spec_source=spec_source,
        generated_at=generated_at,
        working_folder=working_folder,
        project_root=project_root,
        report_root=report_root,
    )
    contract, usage = generate_contract(dynamic_context=dynamic_context, project_root=project_root)
    validate_contract(contract, approved_spec)

    slug = report_slug(spec_source, approved_spec)
    report_dir = report_root / slug
    absolute_report_dir = report_dir if report_dir.is_absolute() else working_folder / report_dir
    absolute_report_dir.mkdir(parents=True, exist_ok=True)

    write_artifacts(
        working_folder=working_folder,
        report_dir=absolute_report_dir,
        contract=contract,
        dynamic_context=dynamic_context,
        spec_source=spec_source,
        generated_at=generated_at,
        title=approved_spec.get("summary", "Approved Spec"),
    )
    return report_dir, contract, usage


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
    report_root = resolved_report_root(dynamic_context)
    runtime = ToolRuntime(
        working_folder=dynamic_context["workspace"]["working_folder"],
        final_report_schema=test_agent_contract_schema(),
        max_cost_usd=float(os.environ.get("TEST_AGENT_MAX_COST_USD", "0.25")),
        max_seconds=int(os.environ.get("TEST_AGENT_MAX_SECONDS", "300")),
        context_window_tokens=agent_context["selected_model"].get("context_length"),
        final_validator=lambda contract: validate_contract(contract, dynamic_context["approved_spec"]),
        live_log_root=report_root,
        live_event_log_paths=[Path(dynamic_context["workspace"]["working_folder"]) / ".workflow/runtime_events.log"],
        live_event_prefix="stage=test ",
        write_blocked_names=sorted(TOOLCHAIN_FILE_NAMES),
        bash_blocked_substrings=INSTALL_COMMAND_BLOCKLIST,
    )
    try:
        result = run_tool_agent(
            model=model,
            system=agent_context["system"],
            user=test_prompt(agent_context, dynamic_context),
            runtime=runtime,
        )
    except AgentRunError as exc:
        result = exc.result
        write_runtime_log(
            report_root,
            result.transcript,
            result.tool_events,
            result.compaction_events,
            result.pre_compaction_archives,
            metadata=build_runtime_metadata("test", model, agent_context["selected_model"], result, status="error", error=str(exc)),
        )
        raise
    write_runtime_log(
        report_root,
        result.transcript,
        result.tool_events,
        result.compaction_events,
        result.pre_compaction_archives,
        metadata=build_runtime_metadata("test", model, agent_context["selected_model"], result),
    )
    if result.final_report is None:
        raise RuntimeError("Test Agent finished without final_report")
    return result.final_report, result.usage


def resolved_report_root(dynamic_context: dict[str, Any]) -> Path:
    report_root = Path(dynamic_context["workspace"]["report_root"])
    if report_root.is_absolute():
        return report_root
    return Path(dynamic_context["workspace"]["working_folder"]) / report_root


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
        data = eval_runtime.read_selected_model(SELECTED_MODEL_PATH)
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
    report_root: Path,
) -> dict[str, Any]:
    return {
        "source": "approved_spec_contract",
        "spec_source": spec_source,
        "approved_spec": approved_spec,
        "workspace": {
            "working_folder": working_folder.as_posix(),
            "project_root": project_root.as_posix(),
            "report_root": report_root.as_posix(),
        },
        "test_artifact_policy": {
            "mode": "project_native_paths",
            "instruction": "Write test files and fixtures at relative paths that match the project's own conventions. Do not put executable tests under .workflow.",
        },
        "tools": {
            "available": agent_runtime.available_tool_names(),
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
        return eval_runtime.read_selected_model(SELECTED_MODEL_PATH)
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
    visible_dynamic_context = json.loads(json.dumps(dynamic_context))
    visible_dynamic_context.get("workspace", {}).pop("report_root", None)
    return f"""{project_context}

{common_context}

{static_context}

Selected Model:
```json
{json.dumps(agent_context["selected_model"], indent=2, sort_keys=True)}
```

Dynamic Context:
```json
{json.dumps(visible_dynamic_context, indent=2, sort_keys=True)}
```

You have filesystem and shell tools scoped to the working folder. Use the todo tools to plan and track work.
Inspect the project context and existing files as needed. Write executable test artifacts at project-native relative paths that match the app's conventions.
Do not write executable tests or fixtures under .workflow; that tree is reserved for Hooky reports and runtime metadata.
You may run commands to check syntax or test discovery when useful. Do not write production implementation.
Do not write package manifests, lockfiles, framework config, workflow reports, contracts, context snapshots, or runtime metadata. Hooky writes system-managed artifacts from final_report.
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
    for item in contract.get("test_files", []):
        validate_test_artifact_path(item, "test file")
    for item in contract.get("fixtures", []):
        validate_test_artifact_path(item, "fixture")


def validate_test_artifact_path(item: dict[str, Any], label: str) -> None:
    for field in ("path", "purpose", "content"):
        if field not in item:
            raise ValueError(f"{label} missing required field: {field}")
    path = Path(str(item["path"]))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} path must be relative and stay inside working folder: {path}")
    if path.parts and path.parts[0] == ".workflow":
        raise ValueError(f"{label} must not be written under .workflow: {path}")
    if path.name in TOOLCHAIN_FILE_NAMES:
        raise ValueError(f"{label} must not modify dependency or config files: {path}")


def write_artifacts(
    *,
    working_folder: Path,
    report_dir: Path,
    contract: dict[str, Any],
    dynamic_context: dict[str, Any],
    spec_source: str,
    generated_at: str,
    title: str,
) -> None:
    working_folder = Path(working_folder)
    report_dir.mkdir(parents=True, exist_ok=True)

    for test_file in contract.get("test_files", []):
        path = working_folder / test_file["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(test_file["content"], encoding="utf-8")

    for fixture in contract.get("fixtures", []):
        path = working_folder / fixture["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
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


def report_slug(spec_source: str, approved_spec: dict[str, Any]) -> str:
    source_path = Path(spec_source)
    if source_path.name == "contract.json" and source_path.parent.name:
        return spec_agent.slugify(source_path.parent.name)
    return spec_agent.slugify(approved_spec.get("summary", "approved-spec"))


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
