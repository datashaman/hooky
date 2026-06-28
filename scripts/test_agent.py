#!/usr/bin/env python3
"""Generate Test Agent artifacts from an approved Spec Agent contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
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
DEPENDENCY_ADD_COMMAND_BLOCKLIST = [
    "bun add",
    "composer require",
    "go get ",
    "npm add",
    "pnpm add",
    "poetry add",
    "uv add",
    "yarn add",
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
        max_seconds=int(os.environ.get("TEST_AGENT_MAX_SECONDS", "600")),
        bash_timeout_seconds=int(os.environ.get("TEST_AGENT_BASH_TIMEOUT_SECONDS", "120")),
        context_window_tokens=agent_context["selected_model"].get("context_length"),
        live_log_root=report_root,
        live_event_log_paths=[Path(dynamic_context["workspace"]["working_folder"]) / ".workflow/runtime_events.log"],
        live_event_prefix="stage=test ",
        write_blocked_names=sorted(TOOLCHAIN_FILE_NAMES),
        bash_blocked_substrings=DEPENDENCY_ADD_COMMAND_BLOCKLIST,
        bash_command_validator=test_agent_bash_policy_violation,
    )
    runtime.final_validator = lambda contract: validate_contract_with_tool_events(
        contract,
        dynamic_context["approved_spec"],
        runtime.tool_events,
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


def execution_check_array_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": True,
            "required": ["command", "status", "reason"],
            "properties": {
                "command": {"type": "string"},
                "status": {"type": "string", "enum": ["passed", "failed", "skipped"]},
                "reason": {"type": "string"},
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
            "test_execution_checks",
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
            "test_execution_checks": execution_check_array_schema(),
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
You may run commands to set up declared project test dependencies, check syntax, discover tests, and run tests. Do not write production implementation.
If you run any setup, syntax, discovery, or test command, include it in test_execution_checks with its actual status. Test failures are expected before Builder runs; report them as failed, not fixed.
You may install or sync dependencies already declared by project manifests or lockfiles. Do not add new dependencies or edit dependency manifests. Prefer setup commands that avoid creating or changing lockfiles when the package manager supports that.
Do not write package manifests, framework config, workflow reports, contracts, context snapshots, or runtime metadata. Hooky writes system-managed artifacts from final_report.
In acceptance_criteria_covered, acceptance_criteria_uncovered, and untestable_requirements, include only exact full strings copied from approved_spec.acceptance_criteria. Each list item must contain exactly one approved criterion.
Do not claim a criterion is covered unless at least one generated test directly asserts that behavior without contradicting another approved criterion.
Finish only by calling final_report with the Test Agent contract. The contract must list every test file and fixture you created, including each file's content.
"""


def validate_contract(contract: dict[str, Any], approved_spec: dict[str, Any]) -> None:
    required = [
        "summary",
        "test_files",
        "fixtures",
        "coverage_targets",
        "test_execution_checks",
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
    validate_coverage_lists(contract, approved_spec)
    validate_execution_check_shape(contract.get("test_execution_checks"))
    for item in contract.get("test_files", []):
        validate_test_artifact_path(item, "test file")
        validate_test_content(item, approved_spec)
    for item in contract.get("fixtures", []):
        validate_test_artifact_path(item, "fixture")


def validate_contract_with_tool_events(
    contract: dict[str, Any],
    approved_spec: dict[str, Any],
    tool_events: list[dict[str, Any]],
) -> None:
    validate_contract(contract, approved_spec)
    validate_execution_checks_against_tool_events(contract, tool_events)


def validate_execution_check_shape(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("test_execution_checks must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"test_execution_checks[{index}] must be an object")
        for field in ("command", "status", "reason"):
            if field not in item:
                raise ValueError(f"test_execution_checks[{index}] missing required field: {field}")
            if not isinstance(item[field], str) or not item[field].strip():
                raise ValueError(f"test_execution_checks[{index}].{field} must be a non-empty string")
        if item["status"] not in {"passed", "failed", "skipped"}:
            raise ValueError(f"test_execution_checks[{index}].status must be passed, failed, or skipped")


def validate_execution_checks_against_tool_events(contract: dict[str, Any], tool_events: list[dict[str, Any]]) -> None:
    checks = list(contract.get("test_execution_checks") or [])
    bash_events = [event for event in tool_events if event.get("name") == "bash"]
    for event in bash_events:
        command = str(event.get("arguments", {}).get("command") or "")
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if is_blocked_dependency_mutation_event(result):
            raise ValueError(f"blocked dependency mutation command attempted by Test Agent: {command}")
        if is_reportable_test_command(command):
            matching_checks = [check for check in checks if commands_match(str(check.get("command") or ""), command)]
            if not matching_checks:
                raise ValueError(f"setup or test command missing from test_execution_checks: {command}")
            validate_matching_execution_checks(command, result, matching_checks)

    for check in checks:
        command = str(check.get("command") or "")
        if str(check.get("status")) != "passed":
            continue
        matching_events = [
            event
            for event in bash_events
            if commands_match(command, str(event.get("arguments", {}).get("command") or ""))
        ]
        if not matching_events:
            raise ValueError(f"test_execution_checks claims passed without matching bash event: {command}")
        if not any(bash_event_passed(event) for event in matching_events):
            raise ValueError(f"test_execution_checks claims passed but matching bash command did not pass: {command}")


def validate_matching_execution_checks(command: str, result: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    if command_masks_failure(command):
        if command_failed_due_missing_dependency(result):
            if not any(check.get("status") == "skipped" and has_missing_dependency_reason(str(check.get("reason") or "")) for check in checks):
                raise ValueError(f"missing-dependency test execution command must be reported as skipped with reason: {command}")
            return
        if not any(check.get("status") == "failed" for check in checks):
            raise ValueError(f"masked setup or test command must be reported as failed unless a missing dependency makes it skipped: {command}")
        return
    if bash_result_passed(result):
        if not any(check.get("status") == "passed" for check in checks):
            raise ValueError(f"passing test execution command must be reported as passed: {command}")
        return
    if command_failed_due_missing_dependency(result):
        if not any(check.get("status") == "skipped" and has_missing_dependency_reason(str(check.get("reason") or "")) for check in checks):
            raise ValueError(f"missing-dependency test execution command must be reported as skipped with reason: {command}")
        return
    if not any(check.get("status") == "failed" for check in checks):
        raise ValueError(f"failed test execution command must be reported as failed: {command}")


def is_blocked_dependency_mutation_event(result: dict[str, Any]) -> bool:
    error = str(result.get("error") or "")
    return "bash command blocked by agent policy" in error


def is_reportable_test_command(command: str) -> bool:
    lowered = command.lower()
    markers = (
        "bundle install",
        "bun install",
        "cargo fetch",
        "composer install",
        "go mod download",
        "mix deps.get",
        "npm ci",
        "npm install",
        "node --check",
        "npm test",
        "npm run test",
        "npx playwright install",
        "playwright test",
        "playwright install",
        "pnpm install",
        "pnpm test",
        "poetry install",
        "pip install -r",
        "pip install --requirement",
        "uv sync",
        "uv pip install -r",
        "uv pip install --requirement",
        "yarn install",
        "yarn test",
        "bun test",
        "vitest",
        "pytest",
        "cargo test",
        "go test",
        "composer test",
        "phpunit",
        "rspec",
        "mix test",
    )
    return any(marker in lowered for marker in markers)


def command_masks_failure(command: str) -> bool:
    return "|| true" in command.lower()


def test_agent_bash_policy_violation(command: str) -> str | None:
    for segment in split_shell_segments(command):
        tokens = shell_tokens(segment)
        if not tokens:
            continue
        violation = dependency_mutation_violation(tokens)
        if violation:
            return violation
    return None


def split_shell_segments(command: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*(?:&&|\|\||;)\s*", command) if part.strip()]


def shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def dependency_mutation_violation(tokens: list[str]) -> str | None:
    executable = Path(tokens[0]).name
    if executable == "npm" and len(tokens) >= 2:
        subcommand = tokens[1]
        if subcommand == "add":
            return "npm add"
        if subcommand in {"install", "i"} and npm_install_adds_packages(tokens[2:]):
            return "npm install with explicit packages"
    if executable == "pnpm" and len(tokens) >= 2 and tokens[1] == "add":
        return "pnpm add"
    if executable == "yarn" and len(tokens) >= 2 and tokens[1] == "add":
        return "yarn add"
    if executable == "bun" and len(tokens) >= 2 and tokens[1] == "add":
        return "bun add"
    if executable == "composer" and len(tokens) >= 2 and tokens[1] == "require":
        return "composer require"
    if executable == "poetry" and len(tokens) >= 2 and tokens[1] == "add":
        return "poetry add"
    if executable == "uv" and len(tokens) >= 2 and tokens[1] == "add":
        return "uv add"
    if executable == "go" and len(tokens) >= 2 and tokens[1] == "get":
        return "go get"
    if executable == "pip" and len(tokens) >= 2 and tokens[1] == "install" and pip_install_adds_packages(tokens[2:]):
        return "pip install with explicit packages"
    if executable == "uv" and len(tokens) >= 4 and tokens[1:3] == ["pip", "install"] and pip_install_adds_packages(tokens[3:]):
        return "uv pip install with explicit packages"
    return None


def npm_install_adds_packages(args: list[str]) -> bool:
    return any(is_package_argument(arg) for arg in args)


def pip_install_adds_packages(args: list[str]) -> bool:
    if "-r" in args or "--requirement" in args:
        return False
    return any(is_package_argument(arg) for arg in args)


def is_package_argument(arg: str) -> bool:
    if not arg or arg.startswith("-"):
        return False
    return not arg.endswith((".json", ".lock", ".txt", ".in", ".toml"))


def commands_match(reported: str, actual: str) -> bool:
    normalized_reported = normalize_command(reported)
    normalized_actual = normalize_command(actual)
    return (
        normalized_reported == normalized_actual
        or normalized_reported in normalized_actual
        or normalized_actual in normalized_reported
    )


def normalize_command(command: str) -> str:
    return " ".join(command.strip().split())


def bash_event_passed(event: dict[str, Any]) -> bool:
    result = event.get("result") if isinstance(event.get("result"), dict) else {}
    return bash_result_passed(result)


def bash_result_passed(result: dict[str, Any]) -> bool:
    return result.get("ok") is True and int(result.get("returncode") or 0) == 0


def command_failed_due_missing_dependency(result: dict[str, Any]) -> bool:
    text = " ".join(str(result.get(key) or "") for key in ("stdout", "stderr", "error")).lower()
    markers = (
        "cannot find module",
        "module not found",
        "command not found",
        "not recognized as an internal or external command",
        "no such file or directory",
        "could not resolve",
        "missing script",
    )
    return any(marker in text for marker in markers)


def has_missing_dependency_reason(reason: str) -> bool:
    lowered = reason.lower()
    return any(marker in lowered for marker in ("missing", "dependency", "dependencies", "not installed", "not present"))


def validate_coverage_lists(contract: dict[str, Any], approved_spec: dict[str, Any]) -> None:
    approved = list(approved_spec.get("acceptance_criteria", []))
    approved_criteria = set(approved)
    if len(approved_criteria) != len(approved):
        raise ValueError("approved spec has duplicate acceptance criteria")
    covered_list = contract.get("acceptance_criteria_covered", [])
    uncovered_list = contract.get("acceptance_criteria_uncovered", [])
    untestable_list = contract.get("untestable_requirements", [])
    for field, values in (
        ("acceptance_criteria_covered", covered_list),
        ("acceptance_criteria_uncovered", uncovered_list),
        ("untestable_requirements", untestable_list),
    ):
        if not isinstance(values, list):
            raise ValueError(f"{field} must be a list")
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} entries must be non-empty strings")
            if "\n" in value or "\r" in value:
                raise ValueError(f"{field} entries must contain exactly one acceptance criterion, not multiline text")
            if value not in approved_criteria:
                raise ValueError(f"{field} entry does not exactly match an approved acceptance criterion: {value}")
    covered = set(covered_list)
    uncovered = set(uncovered_list)
    untestable = set(untestable_list)
    overlaps = (covered & uncovered) | (covered & untestable) | (uncovered & untestable)
    if overlaps:
        raise ValueError(f"acceptance criteria appear in multiple coverage lists: {sorted(overlaps)}")
    missing_criteria = approved_criteria - covered - uncovered
    missing_criteria -= untestable
    if missing_criteria:
        raise ValueError(f"acceptance criteria missing from coverage lists: {sorted(missing_criteria)}")


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


def validate_test_content(item: dict[str, Any], approved_spec: dict[str, Any]) -> None:
    path = str(item.get("path") or "")
    content = str(item.get("content") or "")
    if not content.strip():
        raise ValueError(f"test file content must not be empty: {path}")
    if path.endswith((".js", ".jsx", ".ts", ".tsx")):
        validate_playwright_content(path, content, approved_spec)


def validate_playwright_content(path: str, content: str, approved_spec: dict[str, Any]) -> None:
    if "@playwright/test" not in content:
        return
    findings = playwright_quality_findings(content, approved_spec)
    if findings:
        raise ValueError(f"invalid Playwright test content in {path}: {'; '.join(findings)}")


def playwright_quality_findings(content: str, approved_spec: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    if re.search(r"locator\(['\"]\\.todo-count['\"]\).*toContainText\(['\"]0 items", content, re.S):
        findings.append("must not expect .todo-count to show 0 items while empty-state footer is hidden")
    if re.search(r"Complete the other todo[\s\S]{0,220}toggleTodo\(page,\s*0\)", content):
        findings.append("mark-all reflection test toggles the first todo while claiming to toggle the other todo")
    if re.search(r"cd\s+/home/user", content):
        findings.append("test content must not assume /home/user")
    if approved_spec_requires_hidden_empty_footer(approved_spec) and ".todo-count" in content:
        empty_counter_test = re.search(
            r"test\([^)]*(?:plural|counter)[\s\S]{0,600}toContainText\(['\"]0 items",
            content,
            re.IGNORECASE,
        )
        if empty_counter_test:
            findings.append("counter pluralization test contradicts hidden footer requirement for zero todos")
    return findings


def approved_spec_requires_hidden_empty_footer(approved_spec: dict[str, Any]) -> bool:
    text = " ".join(str(item).lower() for item in approved_spec.get("acceptance_criteria", []))
    return "footer" in text and "hidden" in text and "no todos" in text


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
        "test_execution_checks": spec_agent.md_list(
            [
                f"{item.get('status')}: {item.get('command')} - {item.get('reason')}"
                for item in contract.get("test_execution_checks", [])
            ]
        ),
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
