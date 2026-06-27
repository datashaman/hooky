#!/usr/bin/env python3
"""Generate Builder Agent implementation files in a populated task workspace."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

import spec_agent
import test_agent


REPORT_ROOT = Path(".workflow/artifacts/builder-agent")
AGENT_ROOT = Path(".workflow/agents/builder")
COMMON_STATIC_CONTEXT_ROOT = Path(".workflow/agents/common/static")
STATIC_CONTEXT_ROOT = AGENT_ROOT / "static"
TEMPLATE_ROOT = AGENT_ROOT / "templates"
SELECTED_MODEL_PATH = AGENT_ROOT / "selected_model.json"
PROJECT_CONTEXT_FILES = [Path("AGENTS.md"), Path("README.md"), Path("package.json"), Path("playwright.config.cjs")]
APPROVED_TEST_CONTRACT = Path(".workflow/artifacts/test-agent/approved-todomvc-implementation-contract/contract.json")
APPROVED_TEST_ROOT = Path("tests/generated/approved-todomvc-implementation-contract")


def main() -> int:
    args = parse_args()
    os.environ["OPENROUTER_TIMEOUT_MS"] = str(args.request_timeout_ms)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report_dir, _, _ = generate_build_artifacts(
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


def generate_build_artifacts(
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
    apply_file_writes(working_folder, contract)
    report_dir = working_folder / report_root
    write_artifacts(report_dir=report_dir, contract=contract, dynamic_context=dynamic_context, generated_at=generated_at)
    return report_dir, contract, usage


def build_dynamic_context(*, working_folder: Path, generated_at: str, report_root: Path) -> dict[str, Any]:
    approved_contract_path = working_folder / APPROVED_TEST_CONTRACT
    approved_contract = spec_agent.read_json(approved_contract_path)
    approved_tests = {
        path.relative_to(working_folder).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((working_folder / APPROVED_TEST_ROOT).glob("*"))
        if path.is_file()
    }
    return {
        "source": "approved_test_agent_workspace",
        "workspace": {
            "working_folder": working_folder.as_posix(),
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
        "approved_test_contract_path": APPROVED_TEST_CONTRACT.as_posix(),
        "approved_test_contract": approved_contract,
        "approved_tests": approved_tests,
        "generated_at": generated_at,
    }


def generate_contract(*, dynamic_context: dict[str, Any], working_folder: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required; Builder Agent has no non-AI generation path")
    return generate_contract_with_openrouter(dynamic_context=dynamic_context, working_folder=working_folder)


def generate_contract_with_openrouter(
    *,
    dynamic_context: dict[str, Any],
    working_folder: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from agent_runtime import ToolRuntime, run_tool_agent, write_runtime_log

    model = selected_model()
    agent_context = load_agent_context(working_folder)
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=builder_agent_contract_schema(),
        max_cost_usd=float(os.environ.get("BUILDER_AGENT_MAX_COST_USD", "0.50")),
        max_seconds=int(os.environ.get("BUILDER_AGENT_MAX_SECONDS", "420")),
        context_window_tokens=agent_context["selected_model"].get("context_length"),
        final_validator=validate_contract,
    )
    result = run_tool_agent(
        model=model,
        system=agent_context["system"],
        user=builder_prompt(agent_context, dynamic_context),
        runtime=runtime,
    )
    write_runtime_log(
        working_folder / dynamic_context["workspace"]["report_root"],
        result.transcript,
        result.tool_events,
        result.compaction_events,
        result.pre_compaction_archives,
    )
    return result.final_report, result.usage


def builder_agent_contract_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "required": [
            "summary",
            "file_writes",
            "commands_to_run",
            "tests_run",
            "tests_passing",
            "failures_remaining",
            "cost_actuals",
            "requires_verifier",
        ],
        "properties": {
            "summary": {"type": "string"},
            "file_writes": {
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
            },
            "commands_to_run": spec_agent.string_array_schema(),
            "tests_run": spec_agent.string_array_schema(),
            "tests_passing": {"type": "boolean"},
            "failures_remaining": spec_agent.string_array_schema(),
            "cost_actuals": {"type": "object", "additionalProperties": True},
            "requires_verifier": {"type": "boolean"},
        },
    }


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


def selected_model_metadata() -> dict[str, Any]:
    env_model = os.environ.get("OPENROUTER_MODEL")
    if env_model:
        return {"model": env_model, "source": "OPENROUTER_MODEL"}
    if SELECTED_MODEL_PATH.exists():
        return spec_agent.read_json(SELECTED_MODEL_PATH)
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


def builder_prompt(agent_context: dict[str, Any], dynamic_context: dict[str, Any]) -> str:
    project_context = spec_agent.format_context_block("Project Context", agent_context["project_files"])
    common_context = spec_agent.format_context_block("Common Agent Runtime Context", agent_context["common_static_files"])
    static_context = spec_agent.format_context_block(
        "Builder Agent Static Context",
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

Use the available tools to inspect project files, approved tests, and runtime behavior as needed.
Use todo tools to track substantive work. You may write production implementation files only.
Do not edit approved tests, prior-stage artifacts, dependency manifests, or runtime configuration.
Finish only by calling final_report with the Builder Agent contract.
"""


def validate_contract(contract: dict[str, Any]) -> None:
    required = [
        "summary",
        "file_writes",
        "commands_to_run",
        "tests_run",
        "tests_passing",
        "failures_remaining",
        "cost_actuals",
        "requires_verifier",
    ]
    missing = [field for field in required if field not in contract]
    if missing:
        raise ValueError(f"builder contract missing required fields: {', '.join(missing)}")
    if contract["requires_verifier"] is not True:
        raise ValueError("builder contract must require verifier")
    if not contract["file_writes"]:
        raise ValueError("builder contract must include production file writes")
    for file_write in contract["file_writes"]:
        validate_file_write(file_write)


def validate_file_write(file_write: dict[str, Any]) -> None:
    for field in ("path", "purpose", "content"):
        if field not in file_write:
            raise ValueError(f"file_write missing required field: {field}")
    path = Path(file_write["path"])
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"file_write path must be relative and stay inside working folder: {path}")
    blocked_roots = {"tests", ".workflow"}
    if path.parts and path.parts[0] in blocked_roots:
        raise ValueError(f"builder must not write approved tests or prior-stage artifacts: {path}")
    if path.name in {"package.json", "playwright.config.cjs"}:
        raise ValueError(f"builder must not change dependency/config files in eval: {path}")


def apply_file_writes(working_folder: Path, contract: dict[str, Any]) -> None:
    for file_write in contract["file_writes"]:
        path = working_folder / file_write["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file_write["content"], encoding="utf-8")


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
        "summary": contract["summary"],
        "files_changed": spec_agent.md_list([item["path"] for item in contract.get("file_writes", [])]),
        "commands_to_run": spec_agent.md_list(contract.get("commands_to_run", [])),
        "tests_run": spec_agent.md_list(contract.get("tests_run", [])),
        "tests_passing": str(contract.get("tests_passing", False)),
        "failures_remaining": spec_agent.md_list(contract.get("failures_remaining", [])),
        "generated_at": generated_at,
    }
    template = (TEMPLATE_ROOT / "build_report.md").read_text(encoding="utf-8")
    rendered = Template(template.replace("{{ ", "${").replace(" }}", "}")).safe_substitute(report_context)
    (report_dir / "build_report.md").write_text(rendered, encoding="utf-8")


def render_context_snapshot(dynamic_context: dict[str, Any]) -> str:
    working_folder = Path(dynamic_context["workspace"]["working_folder"])
    agent_context = load_agent_context(working_folder)
    return (
        "# Builder Agent Context Snapshot\n\n"
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
