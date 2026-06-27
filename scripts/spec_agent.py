#!/usr/bin/env python3
"""Generate Spec Agent artifacts from a GitHub issue event.

This script is intentionally constrained to documentation artifacts. It must not
edit production code, tests, dependency manifests, or runtime configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any


ARTIFACT_ROOT = Path("docs/specs")
SIDECAR_ROOT = Path(".workflow/artifacts/specs")
AGENT_ROOT = Path(".workflow/agents/spec")
STATIC_CONTEXT_ROOT = AGENT_ROOT / "static"
TEMPLATE_ROOT = AGENT_ROOT / "templates"
PROJECT_CONTEXT_FILES = [Path("AGENTS.md")]


def main() -> int:
    args = parse_args()
    event = read_json(Path(args.event))

    if not should_run(event, args.trigger_label):
        write_outputs(args.github_output, {"should_run": "false"})
        return 0

    issue = event["issue"]
    issue_number = int(issue["number"])
    title = issue["title"].strip()
    body = (issue.get("body") or "").strip()
    author = issue.get("user", {}).get("login", "unknown")
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    artifact_dir, _, _ = generate_spec_artifacts(
        issue_number=issue_number,
        title=title,
        body=body,
        author=author,
        generated_at=generated_at,
    )

    branch = f"sdlc/spec-issue-{issue_number}"
    write_outputs(
        args.github_output,
        {
            "should_run": "true",
            "branch": branch,
            "artifact_dir": str(artifact_dir),
            "pr_body": str(artifact_dir / "pull_request_body.md"),
        },
    )
    return 0


def generate_spec_artifacts(
    *,
    issue_number: int,
    title: str,
    body: str,
    author: str,
    generated_at: str,
    artifact_root: Path = ARTIFACT_ROOT,
    sidecar_root: Path = SIDECAR_ROOT,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    dynamic_context = build_dynamic_context(
        issue_number=issue_number,
        title=title,
        body=body,
        author=author,
        generated_at=generated_at,
    )
    contract, usage = generate_contract(
        dynamic_context=dynamic_context,
    )
    validate_contract(contract)

    slug = slugify(title)
    spec_id = f"issue-{issue_number}-{slug}"
    artifact_dir = artifact_root / spec_id
    sidecar_dir = sidecar_root / spec_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    sidecar_dir.mkdir(parents=True, exist_ok=True)

    files = write_artifacts(
        artifact_dir=artifact_dir,
        sidecar_dir=sidecar_dir,
        spec_id=spec_id,
        issue_number=issue_number,
        title=title,
        author=author,
        generated_at=generated_at,
        contract=contract,
        dynamic_context=dynamic_context,
    )

    pr_body = artifact_dir / "pull_request_body.md"
    pr_body.write_text(render_pr_body(issue_number, title, files, contract), encoding="utf-8")
    return artifact_dir, contract, usage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True, help="Path to GitHub event JSON")
    parser.add_argument("--trigger-label", default="sdlc:spec")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def should_run(event: dict[str, Any], trigger_label: str) -> bool:
    if event.get("action") != "labeled":
        return False
    if event.get("label", {}).get("name") != trigger_label:
        return False
    issue = event.get("issue") or {}
    return "pull_request" not in issue


def generate_contract(
    *,
    dynamic_context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required; Spec Agent has no non-AI generation path")
    return generate_contract_with_openrouter(dynamic_context)


def generate_contract_with_openrouter(
    dynamic_context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    from openrouter import OpenRouter

    model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
    agent_context = load_agent_context()
    with OpenRouter(api_key=os.environ["OPENROUTER_API_KEY"]) as client:
        completion = client.chat.send(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": agent_context["system"],
                },
                {
                    "role": "user",
                    "content": spec_prompt(agent_context, dynamic_context),
                },
            ],
        )
    content = completion.choices[0].message.content
    if not content:
        raise RuntimeError("OpenRouter returned an empty response")
    return json.loads(content), response_usage(completion)


def response_usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return usage.model_dump(exclude_none=True)
    if isinstance(usage, dict):
        return {key: value for key, value in usage.items() if value is not None}
    return {
        key: value
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost")
        if (value := getattr(usage, key, None)) is not None
    }


def build_dynamic_context(
    *,
    issue_number: int,
    title: str,
    body: str,
    author: str,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "source": "github_issue",
        "issue": {
            "number": issue_number,
            "title": title,
            "body": body,
            "author": author,
        },
        "generated_at": generated_at,
    }


def load_agent_context() -> dict[str, Any]:
    static_files = {}
    for path in sorted(STATIC_CONTEXT_ROOT.glob("*.md")):
        static_files[path.name] = path.read_text(encoding="utf-8")

    project_files = {}
    for path in PROJECT_CONTEXT_FILES:
        if path.exists():
            project_files[path.as_posix()] = path.read_text(encoding="utf-8")

    return {
        "system": static_files.get("system.md", ""),
        "static_files": static_files,
        "project_files": project_files,
    }


def spec_prompt(agent_context: dict[str, Any], dynamic_context: dict[str, Any]) -> str:
    project_context = format_context_block("Project Context", agent_context["project_files"])
    static_context = format_context_block(
        "Spec Agent Static Context",
        {
            key: value
            for key, value in agent_context["static_files"].items()
            if key != "system.md"
        },
    )
    return f"""{project_context}

{static_context}

Dynamic Context:
```json
{json.dumps(dynamic_context, indent=2, sort_keys=True)}
```

Produce the Spec Agent contract from the dynamic context, constrained by the project and static agent context.
"""


def format_context_block(title: str, files: dict[str, str]) -> str:
    if not files:
        return f"{title}: none"
    parts = [f"{title}:"]
    for name, content in files.items():
        parts.append(f"\n--- {name} ---\n{content.strip()}")
    return "\n".join(parts)


def validate_contract(contract: dict[str, Any]) -> None:
    required = [
        "summary",
        "scope",
        "non_goals",
        "acceptance_criteria",
        "test_plan",
        "risks",
        "cost_plan",
        "requires_human_approval",
    ]
    missing = [field for field in required if field not in contract]
    if missing:
        raise ValueError(f"contract missing required fields: {', '.join(missing)}")
    if contract["requires_human_approval"] is not True:
        raise ValueError("spec contract must require human approval")
    if not contract["acceptance_criteria"]:
        raise ValueError("spec contract must include acceptance criteria")
    if not contract["test_plan"]:
        raise ValueError("spec contract must include a test plan")
    if not isinstance(contract["cost_plan"], dict):
        raise ValueError("cost_plan must be an object")


def write_artifacts(
    *,
    artifact_dir: Path,
    sidecar_dir: Path,
    spec_id: str,
    issue_number: int,
    title: str,
    author: str,
    generated_at: str,
    contract: dict[str, Any],
    dynamic_context: dict[str, Any],
) -> list[Path]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    sidecar_dir.mkdir(parents=True, exist_ok=True)

    context = {
        "title": title,
        "issue_number": str(issue_number),
        "author": author,
        "generated_at": generated_at,
        "summary": contract["summary"],
        "scope": md_list(contract.get("scope", [])),
        "non_goals": md_list(contract.get("non_goals", [])),
        "acceptance_criteria": md_list(contract.get("acceptance_criteria", []), ordered=True),
        "affected_components": md_list(contract.get("affected_components", [])),
        "edge_cases": md_list(contract.get("edge_cases", [])),
        "blocking_questions": md_list(contract.get("blocking_questions", [])),
        "test_plan": md_list(contract.get("test_plan", []), ordered=True),
        "acceptance_criteria_coverage": md_list(
            [f"Criterion {index}: covered by test-plan review before Test Agent approval." for index, _ in enumerate(contract.get("acceptance_criteria", []), 1)]
        ),
        "risks": md_list(contract.get("risks", [])),
        "complexity": contract.get("cost_plan", {}).get("complexity", "unknown"),
        "max_iterations": str(contract.get("cost_plan", {}).get("max_iterations", 3)),
        "model_route": fenced_json(contract.get("cost_plan", {}).get("model_route", {})),
    }

    outputs = {
        "spec.md": render_template("spec.md", context),
        "acceptance_tests.md": render_template("acceptance_tests.md", context),
        "risk_register.md": render_template("risk_register.md", context),
        "cost_plan.md": render_template("cost_plan.md", context),
        "contract.json": json.dumps(contract, indent=2, sort_keys=True) + "\n",
    }
    written: list[Path] = []
    for filename, content in outputs.items():
        path = artifact_dir / filename
        path.write_text(content, encoding="utf-8")
        written.append(path)

    dynamic_context_path = artifact_dir / "dynamic_context.json"
    dynamic_context_path.write_text(json.dumps(dynamic_context, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written.append(dynamic_context_path)

    context_snapshot_path = artifact_dir / "context_snapshot.md"
    context_snapshot_path.write_text(render_context_snapshot(dynamic_context), encoding="utf-8")
    written.append(context_snapshot_path)

    sidecar = {
        "id": spec_id,
        "issue": issue_number,
        "title": title,
        "state": "draft",
        "requires_human_approval": True,
        "created_at": generated_at,
        "artifact_dir": str(artifact_dir),
        "content_hash": hash_files(written),
    }
    sidecar_path = sidecar_dir / "sidecar.json"
    sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written.append(sidecar_path)
    return written


def render_context_snapshot(dynamic_context: dict[str, Any]) -> str:
    agent_context = load_agent_context()
    return (
        "# Spec Agent Context Snapshot\n\n"
        "## Project Context Files\n\n"
        + md_list(agent_context["project_files"].keys())
        + "\n\n## Static Context Files\n\n"
        + md_list(agent_context["static_files"].keys())
        + "\n\n## Dynamic Context\n\n```json\n"
        + json.dumps(dynamic_context, indent=2, sort_keys=True)
        + "\n```\n"
    )


def render_template(name: str, context: dict[str, str]) -> str:
    template = (TEMPLATE_ROOT / name).read_text(encoding="utf-8")
    return Template(template.replace("{{ ", "${").replace(" }}", "}")).safe_substitute(context)


def render_pr_body(issue_number: int, title: str, files: list[Path], contract: dict[str, Any]) -> str:
    file_list = "\n".join(f"- `{path}`" for path in files)
    criteria = md_list(contract.get("acceptance_criteria", []), ordered=True)
    return f"""## Spec Agent Output

Source issue: #{issue_number}
Title: {title}

This PR contains specification artifacts only and requires human approval before the Test Agent flow can run.

## Files

{file_list}

## Acceptance Criteria

{criteria}

## Required Human Review

- Confirm the spec does not invent requirements.
- Answer or resolve blocking questions.
- Approve or amend the acceptance criteria.
- Confirm the test plan is sufficient for the Test Agent.
"""


def md_list(items: list[Any], ordered: bool = False) -> str:
    if not items:
        return "- None."
    lines = []
    for index, item in enumerate(items, 1):
        prefix = f"{index}." if ordered else "-"
        lines.append(f"{prefix} {item}")
    return "\n".join(lines)


def fenced_json(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, sort_keys=True) + "\n```"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:60] or "untitled"


def hash_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def write_outputs(github_output: str | None, outputs: dict[str, str]) -> None:
    if not github_output:
        for key, value in outputs.items():
            print(f"{key}={value}")
        return
    with Path(github_output).open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


if __name__ == "__main__":
    raise SystemExit(main())
