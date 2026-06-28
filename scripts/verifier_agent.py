#!/usr/bin/env python3
"""Run the Verifier Agent over a populated Builder Agent workspace."""

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
from agent_runtime import ToolRuntime, build_runtime_metadata, run_tool_agent, write_runtime_log


REPORT_ROOT = Path(".workflow/artifacts/verifier-agent")
AGENT_ROOT = Path(".workflow/agents/verifier")
COMMON_STATIC_CONTEXT_ROOT = Path(".workflow/agents/common/static")
STATIC_CONTEXT_ROOT = AGENT_ROOT / "static"
TEMPLATE_ROOT = AGENT_ROOT / "templates"
SELECTED_MODEL_PATH = AGENT_ROOT / "selected_model.json"
PROJECT_CONTEXT_FILES = [Path("AGENTS.md"), Path("README.md"), Path("package.json")]
TEST_AGENT_REPORT_ROOT = Path(".workflow/artifacts/test-agent")
BUILDER_REPORT_ROOT = Path(".workflow/artifacts/builder-agent")


def main() -> int:
    args = parse_args()
    os.environ["OPENROUTER_TIMEOUT_MS"] = str(args.request_timeout_ms)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report_dir, _, _ = generate_verification_artifacts(
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


def generate_verification_artifacts(
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
    approved_test_contracts = read_json_files(working_folder / TEST_AGENT_REPORT_ROOT, "contract.json")
    builder_reports = read_json_files(working_folder / BUILDER_REPORT_ROOT, "contract.json")
    package_json = read_optional_json(working_folder / "package.json")
    return {
        "source": "builder_agent_workspace",
        "workspace": {
            "working_folder": working_folder.as_posix(),
            "report_root": report_root.as_posix(),
            "test_agent_report_root": TEST_AGENT_REPORT_ROOT.as_posix(),
            "builder_report_root": BUILDER_REPORT_ROOT.as_posix(),
        },
        "tools": {
            "available": agent_runtime.available_tool_names(),
            "todo_required": True,
        },
        "approved_test_contracts": approved_test_contracts,
        "builder_reports": builder_reports,
        "package_json": package_json,
        "generated_at": generated_at,
    }


def generate_contract(*, dynamic_context: dict[str, Any], working_folder: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required; Verifier Agent has no non-AI generation path")
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
        final_report_schema=verifier_agent_contract_schema(),
        max_cost_usd=float(os.environ.get("VERIFIER_AGENT_MAX_COST_USD", "0.25")),
        max_seconds=int(os.environ.get("VERIFIER_AGENT_MAX_SECONDS", "300")),
        context_window_tokens=agent_context["selected_model"].get("context_length"),
        final_validator=validate_contract,
    )
    result = run_tool_agent(
        model=model,
        system=agent_context["system"],
        user=verifier_prompt(agent_context, dynamic_context),
        runtime=runtime,
    )
    write_runtime_log(
        working_folder / dynamic_context["workspace"]["report_root"],
        result.transcript,
        result.tool_events,
        result.compaction_events,
        result.pre_compaction_archives,
        metadata=build_runtime_metadata("verifier", model, agent_context["selected_model"], result),
    )
    return result.final_report, result.usage


def verifier_agent_contract_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "required": [
            "status",
            "summary",
            "checks_run",
            "scope_violations",
            "test_integrity_findings",
            "acceptance_coverage_findings",
            "security_findings",
            "required_actions",
            "safe_to_open_pr",
        ],
        "properties": {
            "status": {"type": "string", "enum": ["pass", "fail"]},
            "summary": {"type": "string"},
            "checks_run": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "required": ["name", "command", "status", "evidence"],
                    "properties": {
                        "name": {"type": "string"},
                        "command": {"type": "string"},
                        "status": {"type": "string", "enum": ["pass", "fail", "not_applicable"]},
                        "evidence": {"type": "string"},
                    },
                },
            },
            "scope_violations": spec_agent.string_array_schema(),
            "test_integrity_findings": spec_agent.string_array_schema(),
            "acceptance_coverage_findings": spec_agent.string_array_schema(),
            "security_findings": spec_agent.string_array_schema(),
            "required_actions": spec_agent.string_array_schema(),
            "safe_to_open_pr": {"type": "boolean"},
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


def verifier_prompt(agent_context: dict[str, Any], dynamic_context: dict[str, Any]) -> str:
    project_context = spec_agent.format_context_block("Project Context", agent_context["project_files"])
    common_context = spec_agent.format_context_block("Common Agent Runtime Context", agent_context["common_static_files"])
    static_context = spec_agent.format_context_block(
        "Verifier Agent Static Context",
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

Use the available tools to inspect files and run deterministic checks.
Use todo tools to track the verification work.
You must not edit code, tests, dependency manifests, config, or prior-stage artifacts.
If the project defines `npm test`, run it. If lint, static analysis, or security commands are defined, run them too.
Compare approved test artifact contents against files in the working folder when content is available in prior-stage contracts.
Finish only by calling final_report with the Verifier Agent contract.
"""


def validate_contract(contract: dict[str, Any]) -> None:
    required = [
        "status",
        "summary",
        "checks_run",
        "scope_violations",
        "test_integrity_findings",
        "acceptance_coverage_findings",
        "security_findings",
        "required_actions",
        "safe_to_open_pr",
    ]
    missing = [field for field in required if field not in contract]
    if missing:
        raise ValueError(f"verifier contract missing required fields: {', '.join(missing)}")
    if contract["status"] not in {"pass", "fail"}:
        raise ValueError("verifier status must be pass or fail")
    if contract["safe_to_open_pr"] is True and contract["status"] != "pass":
        raise ValueError("safe_to_open_pr may be true only when status is pass")
    if contract["status"] == "fail" and not contract["required_actions"]:
        raise ValueError("failed verifier output must include required_actions")


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
        "status": contract["status"],
        "checks_run": spec_agent.md_list(
            [
                f"{item.get('name')}: {item.get('status')} ({item.get('command')}) - {item.get('evidence')}"
                for item in contract.get("checks_run", [])
            ]
        ),
        "scope_violations": spec_agent.md_list(contract.get("scope_violations", [])),
        "test_integrity_findings": spec_agent.md_list(contract.get("test_integrity_findings", [])),
        "acceptance_coverage_findings": spec_agent.md_list(contract.get("acceptance_coverage_findings", [])),
        "security_findings": spec_agent.md_list(contract.get("security_findings", [])),
        "required_actions": spec_agent.md_list(contract.get("required_actions", [])),
        "safe_to_open_pr": str(contract.get("safe_to_open_pr", False)),
        "generated_at": generated_at,
    }
    template = (TEMPLATE_ROOT / "verification_report.md").read_text(encoding="utf-8")
    rendered = Template(template.replace("{{ ", "${").replace(" }}", "}")).safe_substitute(report_context)
    (report_dir / "verification_report.md").write_text(rendered, encoding="utf-8")


def render_context_snapshot(dynamic_context: dict[str, Any]) -> str:
    working_folder = Path(dynamic_context["workspace"]["working_folder"])
    agent_context = load_agent_context(working_folder)
    return (
        "# Verifier Agent Context Snapshot\n\n"
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


def read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return spec_agent.read_json(path)


if __name__ == "__main__":
    raise SystemExit(main())
