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
import agent_skills
from agent_runtime import AgentRunError, ToolRuntime, build_runtime_metadata, run_tool_agent, write_runtime_log


REPORT_ROOT = Path(".workflow/artifacts/verifier-agent")
AGENT_ROOT = Path(".workflow/agents/verifier")
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
    project_manifests = read_project_context_files(working_folder)
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
        "project_manifests": project_manifests,
        "remediation": agent_runtime.read_remediation_context(working_folder, "verifier"),
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
        final_validator=lambda contract: validate_contract_for_context(contract, dynamic_context, runtime.tool_events),
        live_log_root=working_folder / dynamic_context["workspace"]["report_root"],
        live_event_log_paths=[working_folder / ".workflow/runtime_events.log"],
        live_event_prefix="stage=verifier ",
        write_enabled=False,
    )
    try:
        result = run_tool_agent(
            model=model,
            system=agent_context["system"],
            user=verifier_prompt(agent_context, dynamic_context),
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
            metadata=build_runtime_metadata("verifier", model, agent_context["selected_model"], result, status="error", error=str(exc)),
        )
        raise
    write_runtime_log(
        working_folder / dynamic_context["workspace"]["report_root"],
        result.transcript,
        result.tool_events,
        result.compaction_events,
        result.pre_compaction_archives,
        metadata=build_runtime_metadata("verifier", model, agent_context["selected_model"], result),
    )
    if result.final_report is None:
        raise RuntimeError("Verifier Agent finished without final_report")
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
            "visual_findings",
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
            "visual_findings": spec_agent.string_array_schema(),
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
        "skills": agent_skills.discover_skills(working_folder),
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
    active_skills = ["visual-ui-review"] if browser_ui_project(dynamic_context) else []
    skills_catalog = agent_skills.skill_catalog(agent_context["skills"])
    skills_context = agent_skills.skill_context(agent_context["skills"], active_skills)
    return f"""{project_context}

{common_context}

{static_context}

{skills_catalog}

{skills_context}

Selected Model:
```json
{json.dumps(agent_context["selected_model"], indent=2, sort_keys=True)}
```

Dynamic Context:
```json
{json.dumps(dynamic_context, indent=2, sort_keys=True)}
```

Use the available tools to inspect files and run deterministic checks. Prefer detect_project_environment before choosing commands, run_tests for deterministic checks, capture_visual_snapshot for browser UI visual/layout evidence, git_status/git_diff/git_show for read-only git inspection, and read_file_excerpt/read_many_files over shell snippets.
Use todo tools to track the verification work.
You must not edit code, tests, dependency manifests, config, or prior-stage artifacts.
Run the project-defined test command for the detected toolchain. If lint, static analysis, or security commands are defined, run them too.
Audit Builder-generated tests against the approved spec: they should be meaningful, deterministic, and not weakened to fit the implementation. If legacy Test Agent artifacts are present, compare their contents against files in the working folder as additional constraints.
When the project exposes a browser UI or visual surface, start the project using its existing dev/server command, capture at least one visual snapshot at a representative viewport, and inspect the attached image pixels directly. The capture_visual_snapshot tool returns layout metrics and also attaches the screenshot as a model image input on the next turn; do not judge visual quality from metrics or paths alone. Do not pass a UI project based only on functional tests when the visual snapshot shows obvious layout failures such as clipped primary content, huge unintended whitespace, overlapping controls, horizontal overflow, missing visible controls, console errors, or content that is implausibly off-screen. For non-UI projects, set visual_findings to ["not_applicable: no browser or visual UI surface detected"].
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
        "visual_findings",
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
    visual_findings = contract.get("visual_findings")
    if not isinstance(visual_findings, list):
        raise ValueError("visual_findings must be a list")


def validate_contract_for_context(contract: dict[str, Any], dynamic_context: dict[str, Any], tool_events: list[dict[str, Any]]) -> None:
    validate_contract(contract)
    if browser_ui_project(dynamic_context):
        snapshot_events = successful_visual_snapshot_events(tool_events)
        if not snapshot_events:
            raise ValueError("browser/UI verifier must capture a successful visual snapshot before final_report")
        if not contract.get("visual_findings"):
            raise ValueError("browser/UI verifier must include visual_findings from attached screenshot inspection")
        blocking_visual_findings = visual_snapshot_blocking_findings(snapshot_events)
        if blocking_visual_findings and (contract.get("status") == "pass" or contract.get("safe_to_open_pr") is True):
            raise ValueError("browser/UI verifier cannot pass with blocking visual snapshot findings: " + "; ".join(blocking_visual_findings[:5]))
        if blocking_visual_findings:
            validate_blocking_visual_findings_are_reported(contract, blocking_visual_findings)


def browser_ui_project(dynamic_context: dict[str, Any]) -> bool:
    package_json = dynamic_context.get("package_json") if isinstance(dynamic_context.get("package_json"), dict) else {}
    dependencies = {}
    for key in ("dependencies", "devDependencies"):
        value = package_json.get(key)
        if isinstance(value, dict):
            dependencies.update(value)
    if any(name in dependencies for name in ("react", "vue", "svelte", "@angular/core", "vite", "@vitejs/plugin-react", "@playwright/test")):
        return True
    for contract in dynamic_context.get("approved_test_contracts") or []:
        if not isinstance(contract, dict):
            continue
        text = json.dumps(contract, sort_keys=True).lower()
        if any(marker in text for marker in ("playwright", "browser", "ui", "end-to-end", "e2e")):
            return True
    return False


def successful_visual_snapshot_events(tool_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    successful = []
    for event in tool_events:
        if event.get("name") != "capture_visual_snapshot":
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if result.get("ok") is True and result.get("screenshot_path"):
            successful.append(event)
    return successful


def visual_snapshot_blocking_findings(snapshot_events: list[dict[str, Any]]) -> list[str]:
    findings: list[str] = []
    for event in snapshot_events:
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        bounds = metrics.get("contentBounds") if isinstance(metrics.get("contentBounds"), dict) else {}
        if numeric_less_than(bounds.get("y"), 0):
            findings.append(f"content starts above viewport y={bounds.get('y')}")
        if metrics.get("horizontalOverflow") is True:
            findings.append("document has horizontal overflow")
        overlaps = metrics.get("sampleHeadingInteractiveOverlaps") if isinstance(metrics.get("sampleHeadingInteractiveOverlaps"), list) else []
        for item in overlaps:
            if not isinstance(item, dict):
                continue
            heading = str(item.get("headingText") or item.get("headingTag") or "heading").strip()
            control = str(item.get("interactiveText") or item.get("interactiveRole") or item.get("interactiveClassName") or item.get("interactiveTag") or "control").strip()
            ratio = item.get("overlapRatio")
            findings.append(f"heading overlaps interactive control: {heading[:60]} over {control[:60]} ratio={ratio}")
        clipped = metrics.get("sampleClippedElements") if isinstance(metrics.get("sampleClippedElements"), list) else []
        for item in clipped:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag") or "")
            role = str(item.get("role") or "")
            text = str(item.get("text") or "").strip()
            class_name = str(item.get("className") or "")
            if tag in {"h1", "h2", "h3", "input", "button", "textarea", "select", "a"} or role in {"heading", "button", "link"} or text:
                label = text or role or class_name or tag
                findings.append(f"visible {tag or role} is clipped/off-screen: {label[:80]}")
    return list(dict.fromkeys(findings))


def validate_blocking_visual_findings_are_reported(contract: dict[str, Any], findings: list[str]) -> None:
    visual_text = " ".join(str(item) for item in contract.get("visual_findings") or []).lower()
    required_text = " ".join(str(item) for item in contract.get("required_actions") or []).lower()
    downgrade_terms = ("minor", "non-critical", "non critical", "acceptable", "visually correct", "does not interfere")
    if any(term in visual_text for term in downgrade_terms):
        raise ValueError("blocking visual snapshot findings must not be downgraded as minor/non-critical")
    if not any(term in visual_text for term in ("clipped", "off-screen", "offscreen", "overlap", "overflow", "above viewport")):
        raise ValueError("blocking visual snapshot findings must be included in visual_findings: " + "; ".join(findings[:3]))
    if not any(term in required_text for term in ("visual", "layout", "clipped", "off-screen", "offscreen", "overlap", "overflow", "viewport")):
        raise ValueError("blocking visual snapshot findings must have visual/layout required_actions: " + "; ".join(findings[:3]))


def numeric_less_than(value: Any, threshold: float) -> bool:
    try:
        return float(value) < threshold
    except (TypeError, ValueError):
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
        "visual_findings": spec_agent.md_list(contract.get("visual_findings", [])),
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


def read_project_context_files(working_folder: Path) -> dict[str, str]:
    return {
        path.as_posix(): (working_folder / path).read_text(encoding="utf-8")
        for path in PROJECT_CONTEXT_FILES
        if (working_folder / path).exists()
    }


if __name__ == "__main__":
    raise SystemExit(main())
