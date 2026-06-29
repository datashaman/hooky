#!/usr/bin/env python3
"""Generate Builder Agent implementation files in a populated task workspace."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

import eval_runtime
import spec_agent
import agent_runtime
import agent_skills
from agent_runtime import build_runtime_metadata


REPORT_ROOT = Path(".workflow/artifacts/builder-agent")
AGENT_ROOT = Path(".workflow/agents/builder")
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
APPROVED_TEST_CONTRACT = Path(".workflow/artifacts/test-agent/approved-todomvc-implementation-contract/contract.json")


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
    validate_contract_for_context(contract, dynamic_context)
    apply_file_writes(working_folder, contract)
    report_dir = working_folder / report_root
    write_artifacts(report_dir=report_dir, contract=contract, dynamic_context=dynamic_context, generated_at=generated_at)
    return report_dir, contract, usage


def build_dynamic_context(*, working_folder: Path, generated_at: str, report_root: Path) -> dict[str, Any]:
    spec_contract_path = approved_spec_contract_path(working_folder)
    approved_spec_contract = spec_agent.read_json(spec_contract_path)
    approved_contract_path = optional_approved_test_contract_path(working_folder)
    approved_contract = spec_agent.read_json(approved_contract_path) if approved_contract_path else None
    approved_tests = read_approved_test_artifacts(working_folder, approved_contract) if approved_contract else {}
    return {
        "source": "approved_spec_tdd_workspace",
        "workspace": {
            "working_folder": working_folder.as_posix(),
            "report_root": report_root.as_posix(),
        },
        "tools": {
            "available": agent_runtime.available_tool_names(),
            "todo_required": True,
        },
        "approved_spec_contract_path": display_path(spec_contract_path, working_folder),
        "approved_spec_contract": approved_spec_contract,
        "approved_test_contract_path": display_path(approved_contract_path, working_folder) if approved_contract_path else "",
        "approved_test_contract": approved_contract or {},
        "approved_tests": approved_tests,
        "remediation": agent_runtime.read_remediation_context(working_folder, "builder"),
        "generated_at": generated_at,
    }


def approved_spec_contract_path(working_folder: Path) -> Path:
    state_path = working_folder / ".workflow/state.json"
    if state_path.exists():
        state = spec_agent.read_json(state_path)
        current_task = state.get("current_task")
        task_state_path = working_folder / ".workflow/tasks" / str(current_task) / "state.json"
        if current_task and task_state_path.exists():
            task_state = spec_agent.read_json(task_state_path)
            contract = task_state.get("artifacts", {}).get("spec", {}).get("contract")
            if contract:
                return working_folder / contract
    contracts = sorted((working_folder / "docs/specs").glob("*/contract.json"))
    if len(contracts) == 1:
        return contracts[0]
    if not contracts:
        raise FileNotFoundError("approved Spec Agent contract not found")
    raise RuntimeError("multiple Spec Agent contracts found")


def optional_approved_test_contract_path(working_folder: Path) -> Path | None:
    try:
        return approved_test_contract_path(working_folder)
    except FileNotFoundError:
        return None


def approved_test_contract_path(working_folder: Path) -> Path:
    env_path = os.environ.get("HOOKY_APPROVED_TEST_CONTRACT")
    if env_path:
        path = Path(env_path)
        return path if path.is_absolute() else working_folder / path

    state_path = working_folder / ".workflow/state.json"
    if state_path.exists():
        state = spec_agent.read_json(state_path)
        current_task = state.get("current_task")
        task_state_path = working_folder / ".workflow/tasks" / str(current_task) / "state.json"
        if current_task and task_state_path.exists():
            task_state = spec_agent.read_json(task_state_path)
            contract = task_state.get("artifacts", {}).get("test", {}).get("contract")
            if contract:
                return working_folder / contract

    contracts = sorted((working_folder / ".workflow/artifacts/test-agent").glob("*/contract.json"))
    if len(contracts) == 1:
        return contracts[0]
    if (working_folder / APPROVED_TEST_CONTRACT).exists():
        return working_folder / APPROVED_TEST_CONTRACT
    if not contracts:
        raise FileNotFoundError("approved Test Agent contract not found")
    raise RuntimeError("multiple Test Agent contracts found; set HOOKY_APPROVED_TEST_CONTRACT")


def read_approved_test_artifacts(working_folder: Path, contract: dict[str, Any]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for item in list(contract.get("test_files", [])) + list(contract.get("fixtures", [])):
        path = Path(str(item.get("path", "")))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"approved test artifact path is unsafe: {path}")
        full_path = working_folder / path
        if not full_path.exists():
            raise FileNotFoundError(f"approved test artifact is missing: {path}")
        artifacts[path.as_posix()] = full_path.read_text(encoding="utf-8")
    return artifacts


def display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def generate_contract(*, dynamic_context: dict[str, Any], working_folder: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required; Builder Agent has no non-AI generation path")
    return generate_contract_with_openrouter(dynamic_context=dynamic_context, working_folder=working_folder)


def generate_contract_with_openrouter(
    *,
    dynamic_context: dict[str, Any],
    working_folder: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from agent_runtime import AgentRunError, ToolRuntime, run_tool_agent, write_runtime_log

    model = selected_model()
    agent_context = load_agent_context(working_folder)
    runtime = ToolRuntime(
        working_folder=working_folder,
        final_report_schema=builder_agent_contract_schema(),
        max_cost_usd=float(os.environ.get("BUILDER_AGENT_MAX_COST_USD", "0.50")),
        max_seconds=int(os.environ.get("BUILDER_AGENT_MAX_SECONDS", "420")),
        max_extension_seconds=int(os.environ.get("BUILDER_AGENT_MAX_EXTENSION_SECONDS", "180")),
        max_extension_requests=int(os.environ.get("BUILDER_AGENT_MAX_EXTENSION_REQUESTS", "1")),
        max_post_success_grace_seconds=int(os.environ.get("BUILDER_AGENT_POST_SUCCESS_GRACE_SECONDS", "180")),
        context_window_tokens=agent_context["selected_model"].get("context_length"),
        final_validator=lambda contract: validate_contract_for_context(contract, dynamic_context),
        skills=agent_context["skills"],
        live_log_root=working_folder / dynamic_context["workspace"]["report_root"],
        live_event_log_paths=[working_folder / ".workflow/runtime_events.log"],
        live_event_prefix="stage=builder ",
        read_blocked_prefixes=[".workflow", "node_modules"],
        read_allowed_prefixes=[".workflow/tool-results"],
        write_blocked_prefixes=[".workflow"],
        bash_command_validator=builder_bash_command_violation,
        bash_protected_prefixes=["docs/specs", ".workflow/artifacts/test-agent", ".workflow/artifacts/specs"],
    )
    try:
        result = run_tool_agent(
            model=model,
            system=agent_context["system"],
            user=builder_prompt(agent_context, dynamic_context),
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
            metadata=build_runtime_metadata("builder", model, agent_context["selected_model"], result, status="error", error=str(exc)),
        )
        raise
    write_runtime_log(
        working_folder / dynamic_context["workspace"]["report_root"],
        result.transcript,
        result.tool_events,
        result.compaction_events,
        result.pre_compaction_archives,
        metadata=build_runtime_metadata("builder", model, agent_context["selected_model"], result),
    )
    if result.final_report is None:
        raise RuntimeError("Builder Agent finished without final_report")
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
                    "required": ["path", "purpose"],
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
            "test_contract_findings": spec_agent.string_array_schema(),
            "cost_actuals": {"type": "object", "additionalProperties": True},
            "requires_verifier": {"type": "boolean"},
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
        "skills": agent_skills.discover_skills(working_folder),
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
    skills_catalog = agent_skills.skill_catalog(agent_context["skills"])
    return f"""{project_context}

{common_context}

{static_context}

{skills_catalog}

Load skill details only when needed by calling activate_skill with the skill name. If an activated skill lists resources, read only the specific relevant resource files with read_skill_resource.

Selected Model:
```json
{json.dumps(agent_context["selected_model"], indent=2, sort_keys=True)}
```

Dynamic Context:
```json
{json.dumps(dynamic_context, indent=2, sort_keys=True)}
```

Use the available tools to inspect project files, the approved spec, optional legacy approved tests, and runtime behavior as needed. Prefer read_file_excerpt/read_many_files over shell snippets, detect_project_environment over package-manager probes, and run_tests over bash for test execution.
Use todo tools to track substantive work. You own the TDD loop for this task: create or update executable tests from the approved spec, then implement production code until those tests pass.
You may create or update dependency manifests, lockfiles, build config, and toolchain config only when required by the approved spec or project context.
Do not edit prior-stage artifacts or runtime configuration. If legacy approved tests are present in dynamic context, treat them as constraints; otherwise generate tests directly from the approved spec.
Use managed process tools for long-running local servers: start_process, read_process, stop_process, and list_processes. Do not background servers through bash with `&`, shell job control, or manual port cleanup unless you are only diagnosing an already-orphaned external process.
Run the TDD suite deliberately:
- Write tests before or alongside implementation, and include every test file you create or update in file_writes.
- Do not weaken tests just to match a broken implementation. If a test is invalid, fix the test and explain why in test_contract_findings.
- Run a full approved test command at most twice after implementation changes unless the previous full run passed.
- After a failed run_tests call, use latest_test_failure_context before reading raw logs or shelling into test artifacts. It writes a concise system-owned diagnostic bundle with the failing command, parsed failures, output tail, and related Playwright error-context files.
- Rerun only the specific failing test file or focused test while debugging.
- If tests still fail after three implementation attempts, call final_report with tests_passing false and exact failures_remaining instead of continuing to churn.
Keep failure diagnosis inside the product workspace:
- Do inspect generated tests, optional legacy approved tests, project source/config, run_tests output artifacts, Playwright error-context files under test-results, browser-visible DOM state, and screenshots.
- Do not inspect dependency or framework internals such as node_modules, Playwright source, test-runner source, package manager cache directories, or bundled framework code when fixing application behavior.
- If a focused failing test appears to implicate the test runner itself, report that as a test_contract_finding with evidence instead of spelunking dependency internals.
If you receive a runtime soft-deadline notice, do not start another long command. Call final_report immediately with the current implementation state, tests_run, tests_passing, and failures_remaining.
Verifier will audit test integrity and acceptance coverage independently, so make the tests readable, deterministic, and grounded in the approved spec.
Finish only by calling final_report with the Builder Agent contract.
"""


def builder_bash_command_violation(command: str) -> str | None:
    lowered = command.lower()
    unmanaged_server_patterns = [
        r"\b(npm|pnpm|yarn|bun)\s+(run\s+)?(dev|start|preview)\b",
        r"\b(npx\s+)?vite\b",
        r"\bnext\s+dev\b",
        r"\bpython3?\s+-m\s+http\.server\b",
        r"\b(http-server|serve)\b",
    ]
    uses_backgrounding = any(fragment in lowered for fragment in [" &", "& ", "nohup ", "setsid ", "disown", "jobs", "fg %", "bg %"])
    if uses_backgrounding and any(re.search(pattern, lowered) for pattern in unmanaged_server_patterns):
        return (
            "Builder must use managed process tools for long-running local servers; "
            "call start_process/list_processes/read_process/stop_process instead of starting servers through bash"
        )
    blocked_fragments = [
        "node_modules",
        "playwright-core/lib",
        "playwright/lib",
        ".pnpm/",
        ".yarn/cache",
        "npm cache",
    ]
    for fragment in blocked_fragments:
        if fragment in lowered:
            return (
                "Builder must not inspect dependency or framework internals while fixing application behavior; "
                "use project source, approved tests, run_tests output, and test-results artifacts instead"
            )
    return None


def validate_contract(contract: dict[str, Any]) -> None:
    validate_contract_for_context(contract, {})


def validate_contract_for_context(contract: dict[str, Any], dynamic_context: dict[str, Any]) -> None:
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
    if contract["tests_passing"] is False and not contract["failures_remaining"]:
        raise ValueError("builder contract with tests_passing false must list failures_remaining")
    if not contract["file_writes"] and not contract.get("test_contract_findings") and not remediation_noop_allowed(contract, dynamic_context):
        raise ValueError("builder contract must include file writes unless reporting invalid tests")
    for file_write in contract["file_writes"]:
        validate_file_write(file_write)


def remediation_noop_allowed(contract: dict[str, Any], dynamic_context: dict[str, Any]) -> bool:
    remediation = dynamic_context.get("remediation") if isinstance(dynamic_context.get("remediation"), dict) else {}
    return bool(remediation) and contract.get("tests_passing") is True and bool(contract.get("tests_run"))


def validate_file_write(file_write: dict[str, Any]) -> None:
    for field in ("path", "purpose"):
        if field not in file_write:
            raise ValueError(f"file_write missing required field: {field}")
    path = Path(file_write["path"])
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"file_write path must be relative and stay inside working folder: {path}")
    blocked_roots = {".workflow"}
    if path.parts and path.parts[0] in blocked_roots:
        raise ValueError(f"builder must not write approved tests or prior-stage artifacts: {path}")


def apply_file_writes(working_folder: Path, contract: dict[str, Any]) -> None:
    for file_write in contract["file_writes"]:
        if "content" not in file_write:
            continue
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
        "test_contract_findings": spec_agent.md_list(contract.get("test_contract_findings", [])),
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
