#!/usr/bin/env python3
"""Typer CLI for running the Hooky loop pipeline."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, TypeVar

import typer


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import agent_runtime
import agent_skills
import loop_agent


app = typer.Typer(help="Run the Hooky loop pipeline.", no_args_is_help=True)
T = TypeVar("T")
skills_app = typer.Typer(help="Inspect available agent skills.", no_args_is_help=True)
app.add_typer(skills_app, name="skills")
evidence_app = typer.Typer(help="Capture and inspect system-owned evidence reports.", no_args_is_help=True)
app.add_typer(evidence_app, name="evidence")

DEFAULT_LAST_RUN_PATH = Path("/tmp/hooky-last-run-path")
DEFAULT_RUN_KEY = "local"
RUN_KEY_ENV = "HOOKY_RUN_KEY"
RUN_DIR_ENV = "HOOKY_RUN_DIR"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@app.callback()
def main(
    ctx: typer.Context,
    workspace: Annotated[Path, typer.Option("--workspace", "-C", help="Workspace directory. Defaults to current directory.")] = Path("."),
    run_key: Annotated[str | None, typer.Option("--run-key", help="Durable loop run key under .hooky/runs/<key>.")] = None,
) -> None:
    resolved = workspace.resolve()
    if run_key:
        run_key = normalize_run_key(run_key)
        os.environ[RUN_KEY_ENV] = run_key
    ctx.obj = {"workspace": resolved, "run_key": run_key}


def workspace_from_ctx(ctx: typer.Context) -> Path:
    return ctx.obj["workspace"]


def normalize_run_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    if not key:
        raise typer.BadParameter("run key must contain at least one letter or number")
    return key[:120]


def hooky_root(workspace: Path) -> Path:
    return workspace / ".hooky"


def run_dir_rel(run_key: str) -> str:
    return f".hooky/runs/{normalize_run_key(run_key)}"


def current_run_key_path(workspace: Path) -> Path:
    return hooky_root(workspace) / "current.json"


def read_current_run_key(workspace: Path) -> str | None:
    path = current_run_key_path(workspace)
    if not path.exists():
        return None
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    key = payload.get("run_key")
    return normalize_run_key(key) if isinstance(key, str) and key.strip() else None


def selected_run_key(workspace: Path) -> str:
    env_key = os.environ.get(RUN_KEY_ENV)
    if env_key:
        key = normalize_run_key(env_key)
    else:
        key = read_current_run_key(workspace) or DEFAULT_RUN_KEY
    os.environ[RUN_KEY_ENV] = key
    os.environ[RUN_DIR_ENV] = run_dir_rel(key)
    return key


def set_current_run_key(workspace: Path, run_key: str) -> None:
    run_key = normalize_run_key(run_key)
    os.environ[RUN_KEY_ENV] = run_key
    os.environ[RUN_DIR_ENV] = run_dir_rel(run_key)
    write_json(current_run_key_path(workspace), {"schema_version": 1, "run_key": run_key, "updated_at": utc_now()})


@contextmanager
def in_workspace(workspace: Path):
    previous = Path.cwd()
    os.chdir(workspace)
    try:
        yield
    finally:
        os.chdir(previous)


def loop_dir(workspace: Path) -> Path:
    return workspace / run_dir_rel(selected_run_key(workspace))


def loop_attempts_dir(workspace: Path) -> Path:
    return loop_dir(workspace) / "attempts"


def loop_feature_list_path(workspace: Path) -> Path:
    return loop_dir(workspace) / "feature_list.json"


def loop_progress_path(workspace: Path) -> Path:
    return loop_dir(workspace) / "progress.md"


def loop_contract_path(workspace: Path) -> Path:
    return loop_dir(workspace) / "contract.md"


def loop_proposal_path(workspace: Path) -> Path:
    return loop_dir(workspace) / "proposal.md"


def loop_log_path(workspace: Path) -> Path:
    return loop_dir(workspace) / "log.md"


def loop_state_path(workspace: Path) -> Path:
    return loop_dir(workspace) / "state.json"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def default_loop_feature_list() -> dict[str, Any]:
    return {"schema_version": 1, "features": []}


def default_loop_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_key": None,
        "status": "initialized",
        "current_attempt": None,
        "attempts": [],
        "contract_accepted": False,
        "last_action": None,
    }


def read_loop_state(workspace: Path) -> dict[str, Any]:
    path = loop_state_path(workspace)
    if not path.exists():
        raise typer.BadParameter("loop is not initialized. Run `hooky init` first.")
    return read_json(path)


def write_loop_state(workspace: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_json(loop_state_path(workspace), state)


def ensure_loop_initialized(workspace: Path) -> None:
    if not loop_state_path(workspace).exists():
        raise typer.BadParameter("loop is not initialized. Run `hooky init` first.")


def append_loop_log(workspace: Path, op: str, title: str, body: str = "") -> None:
    path = loop_log_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    date = now.date().isoformat()
    time_label = now.time().isoformat().replace("+00:00", "") + "Z"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    day_header = f"## {date}"
    entry = ""
    if day_header not in existing.splitlines():
        entry += ("" if not existing.strip() else "\n") + f"{day_header}\n\n"
    entry += f"- {time_label} {op} | {title}\n"
    if body.strip():
        body_lines = body.strip().splitlines()
        for line in body_lines:
            stripped = line.strip()
            if not stripped:
                entry += "  \n"
            elif stripped.startswith(("- ", "* ")):
                entry += f"  {stripped}\n"
            else:
                entry += f"  - {stripped}\n"
    entry += "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def loop_visible_bottleneck(state: dict[str, Any]) -> str:
    raw = state.get("bottleneck")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    attempts = state.get("attempts") if isinstance(state.get("attempts"), list) else []
    for attempt in reversed(attempts):
        if not isinstance(attempt, dict):
            continue
        bottleneck = attempt.get("bottleneck")
        if isinstance(bottleneck, str) and bottleneck.strip():
            return bottleneck.strip()
    return ""


def write_loop_progress(workspace: Path, state: dict[str, Any], *, note: str | None = None) -> None:
    current_attempt = state.get("current_attempt")
    lines = [
        "# Loop Progress",
        "",
        f"- status: {state.get('status', 'unknown')}",
        f"- current_attempt: {current_attempt if current_attempt is not None else 'none'}",
        f"- contract_accepted: {str(bool(state.get('contract_accepted'))).lower()}",
        f"- last_action: {state.get('last_action') or 'none'}",
    ]
    if note:
        lines.extend(["", "## Note", "", note.strip()])
    attempts = state.get("attempts") if isinstance(state.get("attempts"), list) else []
    if attempts:
        lines.extend(["", "## Attempts", ""])
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            lines.append(f"- {attempt.get('id')}: {attempt.get('status')} ({attempt.get('started_at')})")
    loop_progress_path(workspace).write_text("\n".join(lines) + "\n", encoding="utf-8")


def next_loop_attempt_id(state: dict[str, Any]) -> str:
    attempts = state.get("attempts") if isinstance(state.get("attempts"), list) else []
    numbers: list[int] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        raw = str(attempt.get("id") or "")
        if raw.isdigit():
            numbers.append(int(raw))
    return f"{(max(numbers) if numbers else 0) + 1:03d}"


def loop_attempt_dir(workspace: Path, attempt_id: str) -> Path:
    return loop_attempts_dir(workspace) / attempt_id


def initialize_loop_files(workspace: Path, *, title: str | None, proposal: str = "", force: bool = False) -> Path:
    run_key = selected_run_key(workspace)
    set_current_run_key(workspace, run_key)
    loop_root = loop_dir(workspace)
    if loop_state_path(workspace).exists() and not force:
        raise typer.BadParameter("loop already initialized. Use --force to overwrite.")
    loop_root.mkdir(parents=True, exist_ok=True)
    write_json(loop_feature_list_path(workspace), default_loop_feature_list())
    proposal_artifact = proposal.strip() or (title or "").strip()
    loop_proposal_path(workspace).write_text(
        proposal_artifact + ("\n" if proposal_artifact else ""),
        encoding="utf-8",
    )
    state = default_loop_state()
    state["run_key"] = run_key
    state["created_at"] = utc_now()
    write_loop_state(workspace, state)
    contract_lines = ["# Loop Contract", ""]
    if title:
        contract_lines.extend(["## Problem", "", f"# {title}", ""])
    if proposal:
        contract_lines.extend(["## Proposal", "", proposal.strip(), ""])
    contract_lines.extend(
        [
            "## Done Criteria",
            "",
            "_The generator proposes criteria here; the evaluator accepts or rejects them before implementation._",
            "",
            "## Taste Rubric",
            "",
            "_Optional. Required only when subjective quality matters._",
            "",
        ]
    )
    loop_contract_path(workspace).write_text("\n".join(contract_lines), encoding="utf-8")
    write_loop_progress(workspace, state, note="Loop initialized.")
    loop_log_path(workspace).write_text("", encoding="utf-8")
    append_loop_log(workspace, "init", "loop initialized", f"workspace: {workspace}")
    return loop_root


def active_loop_attempt(state: dict[str, Any]) -> str:
    attempt_id = state.get("current_attempt")
    if not attempt_id:
        raise typer.BadParameter("no active attempt")
    return str(attempt_id)


def update_loop_attempt(state: dict[str, Any], attempt_id: str, **updates: Any) -> None:
    for attempt in state.get("attempts", []):
        if isinstance(attempt, dict) and attempt.get("id") == attempt_id:
            attempt.update(updates)
            return
    raise typer.BadParameter(f"attempt not found: {attempt_id}")


def start_loop_attempt_state(workspace: Path, state: dict[str, Any]) -> tuple[str, Path, dict[str, Any]]:
    if not state.get("contract_accepted"):
        raise typer.BadParameter("contract is not accepted. Run `hooky accept-contract` first.")
    active = state.get("current_attempt")
    if active:
        raise typer.BadParameter(f"attempt already active: {active}")
    attempt_id = next_loop_attempt_id(state)
    attempt_dir = loop_attempt_dir(workspace, attempt_id)
    (attempt_dir / "traces").mkdir(parents=True, exist_ok=True)
    (attempt_dir / "otel").mkdir(parents=True, exist_ok=True)
    (attempt_dir / "traces/planner.jsonl").touch()
    (attempt_dir / "traces/generator.jsonl").touch()
    (attempt_dir / "traces/evaluator.jsonl").touch()
    (attempt_dir / "otel/spans.jsonl").touch()
    attempt = {
        "id": attempt_id,
        "status": "running",
        "started_at": utc_now(),
        "path": attempt_dir.relative_to(workspace).as_posix(),
    }
    state.setdefault("attempts", []).append(attempt)
    state["current_attempt"] = attempt_id
    state["status"] = "attempt-running"
    return attempt_id, attempt_dir, state


def apply_loop_evaluator_report(
    workspace: Path,
    state: dict[str, Any],
    *,
    attempt_id: str,
    report: dict[str, Any],
    report_path: Path,
    action: str,
) -> dict[str, Any]:
    report = enforce_loop_evaluator_evidence(workspace, attempt_id, report)
    write_json(report_path, report)
    status = str(report.get("status"))
    recommendation = str(report.get("recommendation"))
    bottleneck = report.get("bottleneck")
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    attempt_status = "passed" if status == "pass" else "failed"
    if recommendation == "restart-attempt":
        attempt_status = "restarted"
    update_loop_attempt(
        state,
        attempt_id,
        status=attempt_status,
        completed_at=utc_now(),
        evaluator_report=report_path.relative_to(workspace).as_posix(),
        **({"bottleneck": bottleneck} if bottleneck else {}),
    )
    state["current_attempt"] = None
    if recommendation == "restart-attempt":
        state["status"] = "restart-attempt"
    elif recommendation == "restart-contract":
        state["status"] = "restart-contract"
        state["contract_accepted"] = False
    elif status == "pass":
        state["status"] = "passed"
    elif recommendation == "stop":
        state["status"] = "stopped"
    else:
        state["status"] = "attempt-failed"
    state["last_action"] = f"{action}:{recommendation}"
    if bottleneck:
        state["bottleneck"] = bottleneck
    note = f"Evaluator status={status}, recommendation={recommendation}."
    if bottleneck:
        note += f" Bottleneck: {bottleneck}."
    if findings:
        note += " Findings: " + "; ".join(str(item) for item in findings)
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=note)
    append_loop_log(workspace, "evaluator", f"attempt {attempt_id} report", note)
    return state


def enforce_loop_evaluator_evidence(workspace: Path, attempt_id: str, report: dict[str, Any]) -> dict[str, Any]:
    if report.get("status") != "pass":
        return report
    evidence_failures: list[str] = []
    contract_text = loop_contract_path(workspace).read_text(encoding="utf-8", errors="ignore") if loop_contract_path(workspace).exists() else ""
    rubric_restart_required = loop_agent.taste_rubric_required(contract_text) and not loop_agent.taste_rubric_is_substantive(contract_text)
    if rubric_restart_required:
        evidence_failures.append(
            "Evaluator cannot pass this attempt: subjective quality is requested but contract.md does not define a substantive Taste Rubric."
        )
    if has_placeholder_only_tests(workspace):
        evidence_failures.append(
            "Evaluator cannot pass this attempt: the executable test suite appears to contain only placeholder tests."
        )
    test_failures = loop_attempt_test_evidence_failures(workspace, attempt_id)
    evidence_failures.extend(test_failures)
    if loop_attempt_requires_visual_evidence(workspace) and not loop_attempt_captured_visual_snapshot(workspace, attempt_id):
        evidence_failures.append(
            "Evaluator cannot pass this attempt: browser/UI work needs capture_visual_snapshot evidence from the running app."
        )
    reference_visual_failures = loop_attempt_reference_visual_evidence_failures(workspace, attempt_id)
    evidence_failures.extend(reference_visual_failures)
    visual_failures = loop_attempt_blocking_visual_failures(workspace, attempt_id, report)
    evidence_failures.extend(visual_failures)
    if not evidence_failures:
        return report
    amended = dict(report)
    findings = amended.get("findings") if isinstance(amended.get("findings"), list) else []
    amended["findings"] = [*findings, *evidence_failures]
    amended["status"] = "fail"
    amended["recommendation"] = "restart-contract" if rubric_restart_required else "continue"
    current_bottleneck = amended.get("bottleneck")
    if rubric_restart_required:
        amended["bottleneck"] = "missing_taste_rubric"
    elif test_failures:
        amended["bottleneck"] = "verification_tests_failed"
    elif not isinstance(current_bottleneck, str) or not current_bottleneck.strip() or current_bottleneck == "none_visible_after_trace_review":
        amended["bottleneck"] = "verification_evidence"
    current_score = amended.get("score")
    amended["score"] = min(float(current_score), 0.5) if isinstance(current_score, int | float) else 0.5
    return amended


def loop_attempt_test_evidence_failures(workspace: Path, attempt_id: str) -> list[str]:
    events = loop_attempt_tool_events(workspace, attempt_id)
    test_events = [event for event in events if tool_event_is_test_execution(event)]
    if not test_events:
        return []
    latest = test_events[-1]
    result = latest.get("result")
    if not isinstance(result, dict):
        return ["Evaluator cannot pass this attempt: latest executable test evidence is malformed."]
    if tool_result_passed(result):
        return []
    command = str(result.get("command") or "test command")
    reason = str(result.get("error") or result.get("summary") or result.get("output_tail") or "failed")
    return [
        "Evaluator cannot pass this attempt: latest executable test evidence failed "
        f"({command}): {agent_runtime.single_line(reason, 240)}"
    ]


def tool_event_is_test_execution(event: dict[str, Any]) -> bool:
    name = str(event.get("name") or "")
    if name == "run_tests":
        return True
    if name != "bash":
        return False
    result = event.get("result")
    if not isinstance(result, dict):
        return False
    command = str(result.get("command") or "").lower()
    test_terms = (
        "npm test",
        "pnpm test",
        "yarn test",
        "pytest",
        "playwright test",
        "vitest",
        "jest",
    )
    return any(term in command for term in test_terms)


def tool_result_passed(result: dict[str, Any]) -> bool:
    if result.get("ok") is True:
        return True
    if result.get("passed") is True:
        return True
    if result.get("returncode") == 0 and result.get("timed_out") is not True and result.get("failed") is not True:
        return True
    return False


def loop_attempt_blocking_visual_failures(workspace: Path, attempt_id: str, report: dict[str, Any]) -> list[str]:
    if not loop_attempt_requires_visual_evidence(workspace):
        return []
    failures: list[str] = []
    failures.extend(blocking_visual_failures_from_snapshot_events(loop_attempt_tool_events(workspace, attempt_id)))
    findings_text = " ".join(str(item) for item in report.get("findings", []) if isinstance(item, str)).lower()
    bottleneck_text = str(report.get("bottleneck") or "").lower()
    report_text = f"{findings_text} {bottleneck_text}"
    if report_mentions_blocking_visual_defect(report_text):
        failures.append(
            "Evaluator cannot pass this attempt: visual findings report clipped/off-screen/overflowing primary UI content."
        )
    return unique_lines(failures)


def loop_attempt_reference_visual_evidence_failures(workspace: Path, attempt_id: str) -> list[str]:
    if not loop_attempt_requires_reference_visual_evidence(workspace):
        return []
    count = loop_attempt_visual_snapshot_count(workspace, attempt_id)
    if count >= 2:
        return []
    return [
        "Evaluator cannot pass this attempt: reference/canonical UI work needs visual snapshots from multiple states, not only first load."
    ]


def loop_attempt_tool_events(workspace: Path, attempt_id: str) -> list[dict[str, Any]]:
    tool_events = loop_attempt_dir(workspace, attempt_id) / "traces" / "tool_events.json"
    if not tool_events.exists():
        return []
    try:
        events = json.loads(tool_events.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, dict)]


def blocking_visual_failures_from_snapshot_events(events: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for event in events:
        if event.get("name") != "capture_visual_snapshot":
            continue
        result = event.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        metrics = result.get("metrics")
        if not isinstance(metrics, dict):
            continue
        content_bounds = metrics.get("contentBounds")
        if isinstance(content_bounds, dict) and numeric_less_than(content_bounds.get("y"), 0):
            failures.append("Evaluator cannot pass this attempt: visual snapshot primary content starts above the viewport.")
        if metrics.get("horizontalOverflow") is True:
            failures.append("Evaluator cannot pass this attempt: visual snapshot has horizontal overflow.")
        overlaps = metrics.get("sampleHeadingInteractiveOverlaps")
        if isinstance(overlaps, list) and overlaps:
            failures.append("Evaluator cannot pass this attempt: visual snapshot has heading/control overlap.")
        clipped = metrics.get("sampleClippedElements")
        if isinstance(clipped, list):
            for item in clipped:
                if clipped_item_is_blocking(item):
                    failures.append("Evaluator cannot pass this attempt: visual snapshot has clipped visible text or controls.")
                    break
    return unique_lines(failures)


def numeric_less_than(value: object, limit: float) -> bool:
    return isinstance(value, int | float) and float(value) < limit


def clipped_item_is_blocking(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    tag = str(item.get("tag") or "").lower()
    role = str(item.get("role") or "").lower()
    text = str(item.get("text") or item.get("label") or "").strip()
    blocking_tags = {"h1", "h2", "h3", "button", "input", "textarea", "select", "a", "label"}
    blocking_roles = {"button", "link", "textbox", "checkbox", "radio", "combobox", "heading"}
    return bool(text) or tag in blocking_tags or role in blocking_roles


def report_mentions_blocking_visual_defect(text: str) -> bool:
    if not text:
        return False
    visual_terms = (
        "clipped",
        "cut off",
        "cut-off",
        "off-screen",
        "offscreen",
        "above the viewport",
        "outside the viewport",
        "overflow",
        "overlap",
        "hidden primary",
    )
    primary_terms = (
        "heading",
        "title",
        "text",
        "content",
        "control",
        "button",
        "input",
        "link",
        "todos",
        "primary",
        "ui",
        "layout",
    )
    decorative_terms = ("decorative", "background", "ornament", "purely decorative")
    if any(term in text for term in decorative_terms) and not any(term in text for term in primary_terms):
        return False
    return any(term in text for term in visual_terms) and any(term in text for term in primary_terms)


def unique_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        unique.append(line)
    return unique


def has_placeholder_only_tests(workspace: Path) -> bool:
    test_root = workspace / "tests"
    if not test_root.exists():
        return False
    test_files = [
        path
        for path in test_root.rglob("*")
        if path.is_file()
        and path.suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py"}
        and path.name not in {".gitkeep"}
    ]
    if not test_files:
        return False
    return all(is_placeholder_test_file(path) for path in test_files)


def is_placeholder_test_file(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    compact = " ".join(content.lower().split())
    markers = [
        "dummy test",
        "placeholder test",
        "expect(true).tobe(true)",
        "expect(true).toequal(true)",
        "assert true",
        "assert.true",
        "self.asserttrue(true)",
    ]
    return any(marker in compact for marker in markers)


def loop_attempt_requires_visual_evidence(workspace: Path) -> bool:
    contract_text = ""
    for path in [loop_contract_path(workspace), loop_feature_list_path(workspace)]:
        if path.exists():
            contract_text += "\n" + path.read_text(encoding="utf-8", errors="ignore").lower()
    ui_words = ("browser", "ui", "visual", "layout", "css", "html", "react", "vite", "vue", "svelte", "angular")
    if any(word in contract_text for word in ui_words):
        return True
    package_path = workspace / "package.json"
    if not package_path.exists():
        return False
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    dependencies: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        value = package.get(key)
        if isinstance(value, dict):
            dependencies.update(value)
    return any(name in dependencies for name in ("react", "vue", "svelte", "@angular/core", "vite", "@vitejs/plugin-react"))


def loop_attempt_requires_reference_visual_evidence(workspace: Path) -> bool:
    text = ""
    for path in [loop_contract_path(workspace), loop_feature_list_path(workspace), loop_proposal_path(workspace)]:
        if path.exists():
            text += "\n" + path.read_text(encoding="utf-8", errors="ignore")
    return loop_agent.reference_visual_rubric_required(text) or loop_agent.taste_rubric_is_substantive(text) and any(
        term in text.lower() for term in ("reference", "canonical", "template", "official css", "todomvc")
    )


def loop_attempt_captured_visual_snapshot(workspace: Path, attempt_id: str) -> bool:
    return loop_attempt_visual_snapshot_count(workspace, attempt_id) > 0


def loop_attempt_visual_snapshot_count(workspace: Path, attempt_id: str) -> int:
    count = 0
    for event in loop_attempt_tool_events(workspace, attempt_id):
        if event.get("name") != "capture_visual_snapshot":
            continue
        result = event.get("result")
        if isinstance(result, dict) and result.get("ok") is True and result.get("screenshot_path"):
            count += 1
    return count


def format_evaluator_feedback(report: dict[str, Any]) -> str:
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    lines = [
        f"Status: {report.get('status')}",
        f"Recommendation: {report.get('recommendation')}",
    ]
    if report.get("bottleneck"):
        lines.extend(["", f"Bottleneck: {report['bottleneck']}"])
    if findings:
        lines.extend(["", "Findings:"])
        lines.extend(f"- {item}" for item in findings)
    if report.get("score") is not None:
        lines.extend(["", f"Score: {report['score']}"])
    return "\n".join(lines).strip()


def fallback_evaluator_report_from_error(
    *,
    attempt_id: str,
    error: Exception,
    generator_report: dict[str, Any],
) -> dict[str, Any]:
    findings = [
        f"Evaluator did not produce a valid final_report: {error}",
    ]
    summary = str(generator_report.get("summary") or "").strip()
    if summary:
        findings.append(f"Generator summary: {summary}")
    changed_files = generator_report.get("changed_files")
    if isinstance(changed_files, list):
        findings.append("Generator changed files: " + ", ".join(str(item) for item in changed_files))
    failures = generator_report.get("failures")
    if isinstance(failures, list) and failures:
        findings.extend(str(item) for item in failures)
    return {
        "schema_version": 1,
        "attempt": attempt_id,
        "written_at": utc_now(),
        "status": "fail",
        "recommendation": "restart-attempt",
        "bottleneck": "evaluator failed to submit a typed report; retrying with generator evidence",
        "findings": findings,
        "score": 0,
    }


def reset_loop_attempt_workspace(workspace: Path) -> str:
    if not (workspace / ".git").exists():
        return "no git repository; preserving workspace for next attempt"
    commands = [
        ["git", "-C", str(workspace), "reset", "--hard", "HEAD"],
        ["git", "-C", str(workspace), "clean", "-fd", "-e", ".hooky", "-e", "node_modules"],
    ]
    outputs: list[str] = []
    for command in commands:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            raise typer.BadParameter((completed.stderr or completed.stdout or "git cleanup failed").strip())
        output = (completed.stdout or completed.stderr or "").strip()
        if output:
            outputs.append(output)
    return "\n".join(outputs) if outputs else "workspace reset to git baseline"


def replace_markdown_section(text: str, heading: str, body: str) -> str:
    marker = f"## {heading}"
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == marker:
            start = index
            break
    replacement = [marker, "", body.strip(), ""]
    if start is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + "\n".join(replacement) + "\n"
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[:start] + replacement + lines[end:]).rstrip() + "\n"


def read_body_arg_or_file(body: str | None, body_file: Path | None) -> str:
    if body and body_file:
        raise typer.BadParameter("use either --body or --body-file, not both")
    if body_file:
        return body_file.read_text(encoding="utf-8")
    if body:
        return body
    raise typer.BadParameter("body is required via --body or --body-file")


def read_optional_proposal_file_or_stdin(proposal_file: Path | None) -> str:
    if proposal_file:
        return proposal_file.read_text(encoding="utf-8").strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return ""


def title_from_body(body: str) -> str | None:
    if not body:
        return None
    first_line = body.splitlines()[0].strip()
    return first_line or None


def write_last_run_workspace(path: Path, workspace: Path, run_key: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "workspace": workspace.resolve().as_posix(),
        "run_key": normalize_run_key(run_key or selected_run_key(workspace)),
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def last_run_from_path(path: Path) -> tuple[Path, str | None] | None:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return Path(raw).expanduser().resolve(), None
    if not isinstance(payload, dict):
        return None
    workspace = payload.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        return None
    run_key = payload.get("run_key")
    normalized_key = normalize_run_key(run_key) if isinstance(run_key, str) and run_key.strip() else None
    return Path(workspace).expanduser().resolve(), normalized_key


def workspace_for_loop(ctx: typer.Context, last_run_path: Path = DEFAULT_LAST_RUN_PATH) -> Path:
    workspace = workspace_from_ctx(ctx)
    if loop_state_path(workspace).exists():
        return workspace
    last_run = last_run_from_path(last_run_path)
    if last_run:
        last_workspace, last_run_key = last_run
        if last_run_key:
            os.environ[RUN_KEY_ENV] = last_run_key
    if last_run and loop_state_path(last_workspace).exists():
        return last_workspace
    return workspace


def last_nonempty_line(path: Path) -> str | None:
    if not path.exists():
        return None
    last: str | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.rstrip("\n")
            if stripped:
                last = stripped
    return last


def copy_missing(src: Path, dst: Path, force: bool = False) -> None:
    if dst.exists() and not force:
        return
    if dst.exists():
        shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def resolve_workspace_path(workspace: Path, path: Path) -> Path:
    if path.is_absolute() or path.exists():
        return path
    workspace_path = workspace / path
    return workspace_path if workspace_path.exists() else path


def git_command(workspace: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def is_git_worktree(workspace: Path) -> bool:
    result = git_command(workspace, ["rev-parse", "--is-inside-work-tree"], check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def git_has_head(workspace: Path) -> bool:
    return git_command(workspace, ["rev-parse", "--verify", "HEAD"], check=False).returncode == 0


def ensure_workspace_ready(workspace: Path, *, git: bool = True) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    if not git:
        return
    was_worktree = is_git_worktree(workspace)
    if not was_worktree:
        git_command(workspace, ["init"], check=True)
    if not was_worktree or not git_has_head(workspace):
        agent_runtime.ensure_git_baseline(workspace)


@app.command()
def init(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option(help="Overwrite existing Hooky runtime files.")] = False,
    git: Annotated[bool, typer.Option("--git/--no-git", help="Initialize a local git repository when the workspace is not already a worktree.")] = True,
    title: Annotated[str | None, typer.Option(help="Problem title for loop contract.md.")] = None,
    proposal_file: Annotated[Path | None, typer.Option(help="Optional problem proposal Markdown file.")] = None,
    run_key: Annotated[str | None, typer.Option(help="Durable loop run key under .hooky/runs/<key>.")] = None,
    last_run_path: Annotated[Path, typer.Option(help="Path used by status/watch to find the latest loop workspace.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Initialize a workspace with loop runtime context."""
    workspace = workspace_from_ctx(ctx)
    if run_key:
        set_current_run_key(workspace, run_key)
    ensure_workspace_ready(workspace, git=git)
    proposal = read_optional_proposal_file_or_stdin(proposal_file)
    title = title or title_from_body(proposal)
    loop_root = initialize_loop_files(workspace, title=title, proposal=proposal, force=force)
    write_last_run_workspace(last_run_path, workspace)
    typer.echo(f"initialized: {workspace}")
    typer.echo(f"loop: {loop_root}")


@skills_app.command("list")
def skills_list(ctx: typer.Context) -> None:
    """List available agent skills for this workspace."""
    workspace = workspace_from_ctx(ctx)
    skills = agent_skills.discover_skills(workspace)
    if not skills:
        typer.echo("No skills found.")
        return
    for skill in skills:
        suffix = f" - {skill.description}" if skill.description else ""
        typer.echo(f"{skill.name}{suffix}")
        typer.echo(f"  path: {skill.path}")


@skills_app.command("show")
def skills_show(ctx: typer.Context, name: Annotated[str, typer.Argument(help="Skill name to inspect.")]) -> None:
    """Show one skill's instructions and resource index."""
    workspace = workspace_from_ctx(ctx)
    for skill in agent_skills.discover_skills(workspace):
        if skill.name != name:
            continue
        typer.echo(f"name: {skill.name}")
        typer.echo(f"description: {skill.description}")
        typer.echo(f"path: {skill.path}")
        resources = agent_skills.skill_resources(skill)
        if resources:
            typer.echo("resources:")
            for resource in resources:
                typer.echo(f"  {resource['path']} ({resource['bytes']} bytes)")
        typer.echo("")
        typer.echo(skill.body)
        return
    raise typer.BadParameter(f"unknown skill: {name}")


def loop_init(
    ctx: typer.Context,
    title: Annotated[str | None, typer.Option(help="Problem title for contract.md.")] = None,
    proposal_file: Annotated[Path | None, typer.Option(help="Optional problem proposal Markdown file.")] = None,
    force: Annotated[bool, typer.Option(help="Overwrite existing loop files.")] = False,
    last_run_path: Annotated[Path, typer.Option(help="Path used by loop status/watch to find the latest loop workspace.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Initialize the Karpathy-style loop durable state files."""
    workspace = workspace_from_ctx(ctx)
    proposal = read_optional_proposal_file_or_stdin(proposal_file)
    title = title or title_from_body(proposal)
    loop_root = initialize_loop_files(workspace, title=title, proposal=proposal, force=force)
    write_last_run_workspace(last_run_path, workspace)
    typer.echo(f"loop: {loop_root}")
    typer.echo("next: hooky status")


def loop_status(
    ctx: typer.Context,
    last_run_path: Annotated[Path, typer.Option(help="Path written by loop init/run; used when the current directory has no loop.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Show Karpathy-style loop state and durable file locations."""
    workspace = workspace_for_loop(ctx, last_run_path)
    ctx.obj["workspace"] = workspace
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    typer.echo(f"loop: {loop_dir(workspace)}")
    typer.echo(f"run_key: {selected_run_key(workspace)}")
    typer.echo(f"status: {state.get('status')}")
    typer.echo(f"current_attempt: {state.get('current_attempt') or 'none'}")
    typer.echo(f"contract_accepted: {str(bool(state.get('contract_accepted'))).lower()}")
    typer.echo("files:")
    typer.echo(f"  feature_list: {loop_feature_list_path(workspace)}")
    typer.echo(f"  progress: {loop_progress_path(workspace)}")
    typer.echo(f"  contract: {loop_contract_path(workspace)}")
    typer.echo(f"  log: {loop_log_path(workspace)}")
    attempts = state.get("attempts") if isinstance(state.get("attempts"), list) else []
    if attempts:
        typer.echo("attempts:")
        for attempt in attempts:
            if isinstance(attempt, dict):
                typer.echo(f"  {attempt.get('id')}: {attempt.get('status')}")


def loop_watch(
    ctx: typer.Context,
    path_only: Annotated[bool, typer.Option("--path", help="Only print the loop log path.")] = False,
    follow: Annotated[bool, typer.Option("--follow/--no-follow", "-f", help="Follow the loop log.")] = True,
    last_run_path: Annotated[Path, typer.Option(help="Path written by loop init/run; used when the current directory has no loop.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Show or follow the selected run's log.md."""
    workspace = workspace_for_loop(ctx, last_run_path)
    ctx.obj["workspace"] = workspace
    ensure_loop_initialized(workspace)
    path = loop_log_path(workspace)
    if path_only:
        typer.echo(path)
        return
    if follow:
        follow_runtime_log(workspace, "loop", {}, path)
        return
    typer.echo(path.read_text(encoding="utf-8").rstrip())


def latest_loop_attempt_id(state: dict[str, Any]) -> str | None:
    if state.get("current_attempt"):
        return str(state["current_attempt"])
    attempts = state.get("attempts") if isinstance(state.get("attempts"), list) else []
    for attempt in reversed(attempts):
        if isinstance(attempt, dict) and attempt.get("id"):
            return str(attempt["id"])
    return None


def loop_debug_root(workspace: Path, state: dict[str, Any], attempt: str | None) -> Path:
    attempt_id = attempt or latest_loop_attempt_id(state)
    if attempt_id:
        return loop_attempt_dir(workspace, attempt_id) / "traces"
    return loop_dir(workspace)


def evidence_base_dir_for_cli(workspace: Path, state: dict[str, Any], attempt: str | None) -> Path:
    attempt_id = attempt or latest_loop_attempt_id(state)
    if attempt_id:
        path = loop_attempt_dir(workspace, attempt_id)
        if not path.exists():
            raise typer.BadParameter(f"attempt does not exist: {attempt_id}")
        return path
    return loop_dir(workspace)


def evidence_runtime_for_cli(workspace: Path, state: dict[str, Any], attempt: str | None) -> agent_runtime.ToolRuntime:
    base_dir = evidence_base_dir_for_cli(workspace, state, attempt)
    live_root = base_dir / "traces" if base_dir.name != normalize_run_key(selected_run_key(workspace)) else None
    return agent_runtime.ToolRuntime(
        working_folder=workspace,
        final_report_schema={"type": "object", "properties": {}, "additionalProperties": True},
        max_cost_usd=0,
        max_seconds=600,
        live_log_root=live_root,
    )


def evidence_report_path_for_cli(workspace: Path, state: dict[str, Any], attempt: str | None) -> Path:
    return evidence_base_dir_for_cli(workspace, state, attempt) / "evidence.md"


def load_loop_transcript(root: Path) -> list[dict[str, Any]]:
    path = root / "runtime_transcript.json"
    if not path.exists():
        raise typer.BadParameter(f"runtime transcript not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise typer.BadParameter(f"runtime transcript is not a list: {path}")
    return [item for item in payload if isinstance(item, dict)]


def transcript_message(entry: dict[str, Any]) -> dict[str, Any]:
    message = entry.get("message")
    return message if isinstance(message, dict) else entry


def transcript_role(entry: dict[str, Any]) -> str:
    message = transcript_message(entry)
    return str(message.get("role") or entry.get("role") or "event")


def transcript_text(entry: dict[str, Any]) -> str:
    message = transcript_message(entry)
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(entry.get("message"), str):
        return str(entry["message"])
    return ""


def transcript_tool_calls(entry: dict[str, Any]) -> list[dict[str, Any]]:
    message = transcript_message(entry)
    calls = message.get("tool_calls")
    return calls if isinstance(calls, list) else []


@evidence_app.command("path")
def evidence_path(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to latest attempt, or run-level evidence if no attempt exists.")] = None,
) -> None:
    """Print the selected evidence report path."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    typer.echo(evidence_report_path_for_cli(workspace, state, attempt))


@evidence_app.command("init")
def evidence_init(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to latest attempt, or run-level evidence if no attempt exists.")] = None,
) -> None:
    """Create the selected evidence report if it does not exist."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    base_dir = evidence_base_dir_for_cli(workspace, state, attempt)
    path = agent_runtime.ensure_evidence_report(workspace, base_dir)
    typer.echo(f"evidence: {path}")


@evidence_app.command("note")
def evidence_note(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="Evidence section title.")],
    body: Annotated[str, typer.Option(help="Optional Markdown body.")] = "",
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to latest attempt, or run-level evidence if no attempt exists.")] = None,
) -> None:
    """Append a note to the selected evidence report."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    runtime = evidence_runtime_for_cli(workspace, state, attempt)
    result = runtime.append_evidence_note({"title": title, "body": body})
    typer.echo(f"evidence: {workspace / result['evidence_path']}")


@evidence_app.command("exec")
def evidence_exec(
    ctx: typer.Context,
    command: Annotated[str, typer.Argument(help="Shell command to run and capture.")],
    title: Annotated[str, typer.Option(help="Evidence section title.")] = "Command evidence",
    timeout_seconds: Annotated[int, typer.Option(help="Command timeout in seconds.")] = 120,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to latest attempt, or run-level evidence if no attempt exists.")] = None,
) -> None:
    """Run a command, save real output, and append it to the evidence report."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    runtime = evidence_runtime_for_cli(workspace, state, attempt)
    result = runtime.append_evidence_command({"title": title, "command": command, "timeout_seconds": timeout_seconds})
    typer.echo(f"evidence: {workspace / result['evidence_path']}")
    typer.echo(f"output: {workspace / result['output_path']}")
    typer.echo(f"returncode: {result.get('returncode')}")
    if result.get("timed_out"):
        typer.echo("timed_out: true")
    if result.get("error"):
        raise typer.BadParameter(str(result["error"]))
    if result.get("ok") is False:
        raise typer.Exit(1)


@evidence_app.command("screenshot")
def evidence_screenshot(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="HTTP(S) URL to capture.")],
    title: Annotated[str, typer.Option(help="Evidence section title.")] = "Visual evidence",
    wait_selector: Annotated[str, typer.Option(help="Selector to wait for before capture.")] = "body",
    viewport_width: Annotated[int, typer.Option(help="Viewport width.")] = 1280,
    viewport_height: Annotated[int, typer.Option(help="Viewport height.")] = 900,
    full_page: Annotated[bool, typer.Option("--full-page/--viewport-only", help="Capture full page instead of viewport only.")] = True,
    timeout_seconds: Annotated[int, typer.Option(help="Screenshot timeout in seconds.")] = 30,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to latest attempt, or run-level evidence if no attempt exists.")] = None,
) -> None:
    """Capture a browser screenshot and append it to the evidence report."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    runtime = evidence_runtime_for_cli(workspace, state, attempt)
    result = runtime.append_evidence_screenshot(
        {
            "title": title,
            "url": url,
            "wait_selector": wait_selector,
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
            "full_page": full_page,
            "timeout_seconds": timeout_seconds,
        }
    )
    typer.echo(f"evidence: {workspace / result['evidence_path']}")
    if result.get("screenshot_path"):
        typer.echo(f"screenshot: {workspace / result['screenshot_path']}")
    if result.get("error"):
        raise typer.BadParameter(str(result["error"]))


@evidence_app.command("show")
def evidence_show(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to latest attempt, or run-level evidence if no attempt exists.")] = None,
) -> None:
    """Print the selected evidence report."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    path = evidence_report_path_for_cli(workspace, state, attempt)
    if not path.exists():
        raise typer.BadParameter(f"evidence report not found: {path}")
    typer.echo(path.read_text(encoding="utf-8").rstrip())


@app.command("runtime-log")
def loop_runtime_log(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt, or contract-level runtime if none exists.")] = None,
    follow: Annotated[bool, typer.Option("--follow/--no-follow", "-f", help="Follow the runtime event log.")] = False,
    tail_path: Annotated[bool, typer.Option("--tail-path", help="Only print the runtime event log path.")] = False,
    lines: Annotated[int, typer.Option(help="Number of lines to show when not following.")] = 80,
) -> None:
    """Show the live model/tool runtime log for the loop or an attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    path = root / "runtime_events.log"
    if tail_path:
        typer.echo(path)
        return
    if follow:
        follow_runtime_log(workspace, "loop", state, path)
        return
    if not path.exists():
        raise typer.BadParameter(f"runtime log not found: {path}")
    content = path.read_text(encoding="utf-8").splitlines()
    typer.echo("\n".join(content[-lines:]))


@app.command("transcript")
def loop_transcript(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt, or contract-level runtime if none exists.")] = None,
    role: Annotated[str, typer.Option(help="Role filter: all, system, user, assistant, or tool.")] = "all",
    last: Annotated[int, typer.Option(help="Number of matching transcript entries to show.")] = 20,
    full: Annotated[bool, typer.Option("--full", help="Print full message text instead of a preview.")] = False,
) -> None:
    """Show actual persisted model conversation entries for loop debugging."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    entries = load_loop_transcript(root)
    allowed_roles = {"all", "system", "user", "assistant", "tool"}
    if role not in allowed_roles:
        raise typer.BadParameter("role must be one of: all, system, user, assistant, tool")
    selected = [entry for entry in entries if role == "all" or transcript_role(entry) == role]
    for index, entry in list(enumerate(selected, 1))[-last:]:
        entry_role = transcript_role(entry)
        calls = transcript_tool_calls(entry)
        kind = str(entry.get("kind") or "message")
        text = transcript_text(entry).strip()
        typer.echo(f"## {index}. {entry_role} kind={kind} tool_calls={len(calls)}")
        if calls:
            names = [
                str((call.get("function") or {}).get("name") or call.get("name") or "unknown")
                for call in calls
                if isinstance(call, dict)
            ]
            typer.echo("tools: " + ", ".join(names))
        if entry_role == "tool":
            typer.echo("tool: " + str(entry.get("name") or "unknown"))
            result = entry.get("result")
            typer.echo(agent_runtime.single_line(json.dumps(result, sort_keys=True) if isinstance(result, dict) else str(result), 1200))
        elif text:
            typer.echo(text if full else agent_runtime.single_line(text, 1200))
        else:
            typer.echo("[empty]")
        typer.echo("")


@app.command("stall")
def loop_stall(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt.")] = None,
    last: Annotated[int, typer.Option(help="Number of recent assistant entries to inspect.")] = 20,
) -> None:
    """Summarize recent no-tool assistant responses and fake final_report text."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    entries = load_loop_transcript(root)
    assistants = [entry for entry in entries if transcript_role(entry) == "assistant"]
    recent = assistants[-last:]
    no_tool = [entry for entry in recent if not transcript_tool_calls(entry)]
    fake_final = [entry for entry in no_tool if "final_report" in transcript_text(entry)]
    trailing_no_tool = 0
    for entry in reversed(assistants):
        if transcript_tool_calls(entry):
            break
        trailing_no_tool += 1
    typer.echo(f"transcript: {root / 'runtime_transcript.json'}")
    typer.echo(f"assistant_entries_checked: {len(recent)}")
    typer.echo(f"no_tool_assistant_entries: {len(no_tool)}")
    typer.echo(f"fake_final_report_text_entries: {len(fake_final)}")
    typer.echo(f"trailing_no_tool_assistant_entries: {trailing_no_tool}")
    if no_tool:
        typer.echo("")
        typer.echo("recent no-tool assistant messages:")
        for entry in no_tool[-5:]:
            text = transcript_text(entry).strip()
            typer.echo("- " + (agent_runtime.single_line(text, 500) if text else "[empty]"))


@app.command("inspect")
def loop_inspect(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt.")] = None,
    last: Annotated[int, typer.Option(help="Number of recent transcript entries to summarize.")] = 8,
) -> None:
    """Summarize status, runtime log, transcript, tool use, and stall signals."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    attempt_id = attempt or latest_loop_attempt_id(state) or "contract"
    typer.echo(f"loop: {loop_dir(workspace)}")
    typer.echo(f"run_key: {selected_run_key(workspace)}")
    typer.echo(f"status: {state.get('status')}")
    typer.echo(f"attempt: {attempt_id}")
    typer.echo(f"runtime_log: {root / 'runtime_events.log'}")
    typer.echo(f"transcript: {root / 'runtime_transcript.json'}")
    typer.echo(f"tool_events: {root / 'tool_events.json'}")
    report_path = loop_attempt_dir(workspace, attempt_id) / "evaluator_report.json" if attempt_id != "contract" else None
    if report_path and report_path.exists():
        report = read_json(report_path)
        typer.echo(f"evaluator_status: {report.get('status')}")
        typer.echo(f"recommendation: {report.get('recommendation')}")
        typer.echo(f"bottleneck: {report.get('bottleneck') or 'none'}")
        typer.echo(f"score: {report.get('score')}")
    try:
        entries = load_loop_transcript(root)
    except typer.BadParameter:
        entries = []
    if entries:
        assistants = [entry for entry in entries if transcript_role(entry) == "assistant"]
        no_tool = [entry for entry in assistants if not transcript_tool_calls(entry)]
        trailing_no_tool = 0
        for entry in reversed(assistants):
            if transcript_tool_calls(entry):
                break
            trailing_no_tool += 1
        typer.echo(f"transcript_entries: {len(entries)}")
        typer.echo(f"assistant_entries: {len(assistants)}")
        typer.echo(f"no_tool_assistant_entries: {len(no_tool)}")
        typer.echo(f"trailing_no_tool_assistant_entries: {trailing_no_tool}")
        typer.echo("")
        typer.echo("recent:")
        for entry in entries[-last:]:
            role = transcript_role(entry)
            calls = transcript_tool_calls(entry)
            text = transcript_text(entry).strip()
            preview = agent_runtime.single_line(text, 300) if text else "[empty]"
            suffix = f" tools={len(calls)}" if role == "assistant" else ""
            typer.echo(f"- {role}{suffix}: {preview}")
    else:
        typer.echo("transcript_entries: 0")
    tool_events_path = root / "tool_events.json"
    if tool_events_path.exists():
        events = read_json(tool_events_path)
        if isinstance(events, list):
            names: dict[str, int] = {}
            for event in events:
                if not isinstance(event, dict):
                    continue
                name = str(event.get("name") or "unknown")
                names[name] = names.get(name, 0) + 1
            if names:
                typer.echo("")
                typer.echo("tools:")
                for name, count in sorted(names.items()):
                    typer.echo(f"- {name}: {count}")


@app.command("trace-grep")
def loop_trace_grep(
    ctx: typer.Context,
    pattern: Annotated[str, typer.Argument(help="Case-insensitive text to search for in loop transcripts and runtime logs.")],
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt.")] = None,
    lines: Annotated[int, typer.Option(help="Maximum matching lines to print.")] = 20,
) -> None:
    """Search persisted loop debug artifacts without ad hoc shell commands."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    needle = pattern.lower()
    matches = 0
    files = [
        root / "runtime_events.log",
        root / "runtime_transcript.json",
        root / "tool_events.json",
    ]
    files.extend(sorted(root.glob("*.jsonl")))
    for path in files:
        if not path.exists():
            continue
        for index, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if needle not in line.lower():
                continue
            typer.echo(f"{path}:{index}: {agent_runtime.single_line(line, 1000)}")
            matches += 1
            if matches >= lines:
                return
    if matches == 0:
        typer.echo("no matches")


@app.command("harness-review", hidden=True)
def loop_harness_review(ctx: typer.Context, write: Annotated[bool, typer.Option(help="Write .hooky/harness_review.md.")] = True) -> None:
    """Review the loop harness against the Karpathy loop principles."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_dir(workspace)
    files = {
        "feature_list": loop_feature_list_path(workspace).exists(),
        "progress": loop_progress_path(workspace).exists(),
        "contract": loop_contract_path(workspace).exists(),
        "log": loop_log_path(workspace).exists(),
    }
    attempts = state.get("attempts") if isinstance(state.get("attempts"), list) else []
    latest = latest_loop_attempt_id(state)
    trace_root = loop_attempt_dir(workspace, latest) / "traces" if latest else root
    trace_files = [trace_root / "runtime_events.log", trace_root / "runtime_transcript.json", trace_root / "tool_events.json"]
    visible_bottleneck = loop_visible_bottleneck(state)
    contract_text = loop_contract_path(workspace).read_text(encoding="utf-8", errors="ignore") if loop_contract_path(workspace).exists() else ""
    taste_rubric_present = loop_agent.taste_rubric_is_substantive(contract_text)
    reference_visual_required = loop_attempt_requires_reference_visual_evidence(workspace)
    reference_visual_count = loop_attempt_visual_snapshot_count(workspace, latest) if latest else 0
    checks = [
        ("loop_procedure", True, "loop run drives planner, generator, evaluator, and control flow"),
        ("role_separation", True, "planner/generator/evaluator are separate model roles"),
        ("durable_state", all(files.values()), ", ".join(f"{name}={exists}" for name, exists in files.items())),
        ("contract_negotiation", bool(state.get("contract_accepted")) or state.get("status") in {"contract-rejected", "restart-contract"}, f"status={state.get('status')}"),
        ("restart_paths", True, "restart-attempt and restart-contract commands exist; review attempt history for use"),
        ("subjective_scoring", taste_rubric_present, "substantive Taste Rubric present in contract.md" if taste_rubric_present else "missing substantive Taste Rubric in contract.md"),
        (
            "reference_visual_states",
            not reference_visual_required or reference_visual_count >= 2,
            f"snapshots={reference_visual_count}" if reference_visual_required else "not required",
        ),
        ("trace_reading", any(path.exists() for path in trace_files), f"trace_root={trace_root}"),
        ("bottleneck_visible", bool(visible_bottleneck), f"bottleneck={visible_bottleneck or 'none'}"),
        ("harness_restraint", True, "manual review required; delete rules whose failure mode no longer exists"),
    ]
    lines = ["# Loop Harness Review", "", f"- workspace: {workspace}", f"- status: {state.get('status')}", f"- attempts: {len(attempts)}", ""]
    for name, passed, detail in checks:
        mark = "pass" if passed else "gap"
        lines.append(f"- {mark}: {name} - {detail}")
    gaps = [name for name, passed, _detail in checks if not passed]
    lines.extend(["", "## Recommendation", ""])
    if gaps:
        lines.append("Address gaps: " + ", ".join(gaps))
    else:
        lines.append("No structural gaps detected. Read traces before adding more harness.")
    output = "\n".join(lines) + "\n"
    if write:
        path = root / "harness_review.md"
        path.write_text(output, encoding="utf-8")
        append_loop_log(workspace, "harness-review", "reviewed loop harness", f"report: {path.relative_to(workspace).as_posix()}")
        typer.echo(f"report: {path}")
    typer.echo(output.rstrip())


def run_model_role_with_retries(
    workspace: Path,
    label: str,
    call: Callable[[], T],
    *,
    on_retry: Callable[[int, agent_runtime.AgentRunError], None] | None = None,
) -> T:
    max_retries = int(os.environ.get("LOOP_MODEL_ROLE_RETRIES", "1"))
    attempt = 0
    while True:
        try:
            return call()
        except agent_runtime.AgentRunError as exc:
            if attempt >= max_retries:
                raise
            attempt += 1
            append_loop_log(
                workspace,
                "loop-runner",
                f"retry {label}",
                f"Retry {attempt}/{max_retries} after model role error: {exc}",
            )
            if on_retry is not None:
                on_retry(attempt, exc)


def run_model_loop_once(
    workspace: Path,
    *,
    title: str | None,
    proposal: str,
    force: bool,
    last_run_path: Path,
) -> None:
    ensure_workspace_ready(workspace)
    if not loop_state_path(workspace).exists() or force:
        initialize_loop_files(workspace, title=title, proposal=proposal, force=force)
    elif proposal:
        path = loop_contract_path(workspace)
        path.write_text(replace_markdown_section(path.read_text(encoding="utf-8"), "Proposal", proposal), encoding="utf-8")

    state = read_loop_state(workspace)
    planner_report, planner_usage = run_model_role_with_retries(
        workspace,
        "planner",
        lambda: loop_agent.generate_planner_artifacts(
            working_folder=workspace,
            proposal=proposal or loop_contract_path(workspace).read_text(encoding="utf-8"),
            attempt_id=state.get("current_attempt"),
        ),
    )
    state["status"] = "proposal-written"
    state["contract_accepted"] = False
    state["last_action"] = "run:planner"
    state.setdefault("role_usage", {})["planner"] = planner_usage
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=str(planner_report.get("summary") or "Planner wrote contract proposal."))
    append_loop_log(workspace, "planner", "planner wrote contract", str(planner_report.get("summary") or ""))

    max_contract_rounds = int(os.environ.get("LOOP_CONTRACT_MAX_ROUNDS", "5"))
    review_feedback = ""
    accepted = False
    review = ""
    for round_number in range(1, max_contract_rounds + 1):
        generator_contract_report, generator_contract_usage = run_model_role_with_retries(
            workspace,
            f"generator-contract round {round_number}",
            lambda: loop_agent.generate_generator_contract_artifacts(
                working_folder=workspace,
                attempt_id=state.get("current_attempt"),
                review_feedback=review_feedback,
            ),
        )
        state = read_loop_state(workspace)
        state["status"] = "contract-proposed"
        state["contract_accepted"] = False
        state["last_action"] = f"run:generator-contract:{round_number}"
        state.setdefault("role_usage", {})[f"generator_contract_round_{round_number}"] = generator_contract_usage
        write_loop_state(workspace, state)
        write_loop_progress(workspace, state, note=str(generator_contract_report.get("summary") or "Generator proposed contract."))
        append_loop_log(workspace, "generator", f"generator proposed contract round {round_number}", str(generator_contract_report.get("summary") or ""))

        evaluator_contract_report, evaluator_contract_usage = run_model_role_with_retries(
            workspace,
            f"evaluator-contract round {round_number}",
            lambda: loop_agent.generate_evaluator_contract_artifacts(
                working_folder=workspace,
                attempt_id=state.get("current_attempt"),
            ),
        )
        state = read_loop_state(workspace)
        accepted = bool(evaluator_contract_report.get("accepted"))
        state["status"] = "contract-accepted" if accepted else "contract-rejected"
        state["contract_accepted"] = accepted
        state["last_action"] = f"run:evaluator-contract:{round_number}"
        state.setdefault("role_usage", {})[f"evaluator_contract_round_{round_number}"] = evaluator_contract_usage
        write_loop_state(workspace, state)
        review = str(evaluator_contract_report.get("review") or "")
        required_changes = evaluator_contract_report.get("required_changes") if isinstance(evaluator_contract_report.get("required_changes"), list) else []
        if required_changes:
            review_feedback = review + "\n\nRequired changes:\n" + "\n".join(f"- {item}" for item in required_changes)
        else:
            review_feedback = review
        write_loop_progress(workspace, state, note=f"Contract review {'accepted' if accepted else 'rejected'} round {round_number}. {review}")
        append_loop_log(workspace, "evaluator", f"contract {'accepted' if accepted else 'rejected'} round {round_number}", review_feedback)
        if accepted:
            break
    if not accepted:
        write_last_run_workspace(last_run_path, workspace)
        typer.echo("status: contract-rejected")
        typer.echo(f"rounds: {max_contract_rounds}")
        typer.echo(f"review: {review}")
        return

    max_attempt_rounds = int(os.environ.get("LOOP_ATTEMPT_MAX_ROUNDS", "3"))
    evaluator_feedback = ""
    final_attempt_id = ""
    final_report_path: Path | None = None
    for attempt_round in range(1, max_attempt_rounds + 1):
        state = read_loop_state(workspace)
        attempt_id, attempt_dir, state = start_loop_attempt_state(workspace, state)
        final_attempt_id = attempt_id
        state["last_action"] = "run:start-attempt"
        write_loop_state(workspace, state)
        write_loop_progress(workspace, state, note=f"Attempt {attempt_id} started.")
        append_loop_log(workspace, "attempt", f"attempt {attempt_id} started")

        generator_report, generator_usage = run_model_role_with_retries(
            workspace,
            f"generator-implementation attempt {attempt_id}",
            lambda: loop_agent.generate_generator_implementation_artifacts(
                working_folder=workspace,
                attempt_id=attempt_id,
                evaluator_feedback=evaluator_feedback,
            ),
            on_retry=lambda retry, _exc: append_loop_log(
                workspace,
                "loop-runner",
                f"attempt {attempt_id} generator retry reset",
                reset_loop_attempt_workspace(workspace),
            ),
        )
        write_json(attempt_dir / "generator_report.json", generator_report)
        state = read_loop_state(workspace)
        state.setdefault("role_usage", {})["generator_implementation"] = generator_usage
        state.setdefault("role_usage", {})[f"generator_implementation_{attempt_id}"] = generator_usage
        state["last_action"] = "run:generator-implement"
        write_loop_state(workspace, state)
        write_loop_progress(workspace, state, note=str(generator_report.get("summary") or "Generator completed implementation pass."))
        append_loop_log(workspace, "generator", f"attempt {attempt_id} implementation", str(generator_report.get("summary") or ""))

        try:
            evaluator_report, evaluator_usage = run_model_role_with_retries(
                workspace,
                f"evaluator-attempt {attempt_id}",
                lambda: loop_agent.generate_evaluator_attempt_artifacts(
                    working_folder=workspace,
                    attempt_id=attempt_id,
                ),
            )
            evaluator_report = {
                "schema_version": 1,
                "attempt": attempt_id,
                "written_at": utc_now(),
                **evaluator_report,
            }
        except agent_runtime.AgentRunError as exc:
            evaluator_usage = exc.result.usage
            evaluator_report = fallback_evaluator_report_from_error(
                attempt_id=attempt_id,
                error=exc,
                generator_report=generator_report,
            )
        report_path = attempt_dir / "evaluator_report.json"
        final_report_path = report_path
        write_json(report_path, evaluator_report)
        state = read_loop_state(workspace)
        state.setdefault("role_usage", {})["evaluator_attempt"] = evaluator_usage
        state.setdefault("role_usage", {})[f"evaluator_attempt_{attempt_id}"] = evaluator_usage
        state = apply_loop_evaluator_report(
            workspace,
            state,
            attempt_id=attempt_id,
            report=evaluator_report,
            report_path=report_path,
            action="run:evaluator-attempt",
        )
        evaluator_feedback = format_evaluator_feedback(evaluator_report)
        recommendation = str(evaluator_report.get("recommendation"))
        status = str(evaluator_report.get("status"))
        if status == "pass" or recommendation in {"restart-contract", "stop"}:
            break
        if recommendation == "restart-attempt":
            if attempt_round >= max_attempt_rounds:
                break
            reset_note = reset_loop_attempt_workspace(workspace)
            append_loop_log(workspace, "loop-runner", f"attempt {attempt_id} reset", reset_note)
            continue
        if recommendation == "continue" and attempt_round < max_attempt_rounds:
            append_loop_log(workspace, "loop-runner", f"attempt {attempt_id} continuing", "Starting another generator/evaluator attempt with evaluator feedback.")
            continue
        break

    state = read_loop_state(workspace)
    write_last_run_workspace(last_run_path, workspace)
    typer.echo(f"attempt: {final_attempt_id}")
    typer.echo(f"status: {state['status']}")
    if final_report_path is not None:
        typer.echo(f"report: {final_report_path}")


def loop_run(
    ctx: typer.Context,
    title: Annotated[str | None, typer.Option(help="Problem title used when initializing a new loop.")] = None,
    proposal: Annotated[str, typer.Option(help="Planner proposal text for contract.md.")] = "",
    proposal_file: Annotated[Path | None, typer.Option(help="Problem proposal Markdown file.")] = None,
    criteria: Annotated[str, typer.Option(help="Dry-run generator-proposed done criteria.", hidden=True)] = "- Define done criteria explicitly.",
    review: Annotated[str, typer.Option(help="Dry-run evaluator contract review text.", hidden=True)] = "Contract criteria are accepted for this local run.",
    status: Annotated[str, typer.Option(help="Dry-run evaluator status: pass or fail.", hidden=True)] = "pass",
    recommendation: Annotated[str, typer.Option(help="Dry-run evaluator recommendation: continue, restart-attempt, restart-contract, or stop.", hidden=True)] = "continue",
    bottleneck: Annotated[str | None, typer.Option(help="Dry-run evaluator bottleneck.", hidden=True)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Use deterministic local loop plumbing instead of model roles.")] = False,
    force: Annotated[bool, typer.Option(help="Reinitialize the loop before running.")] = False,
    run_key: Annotated[str | None, typer.Option(help="Durable loop run key under .hooky/runs/<key>.")] = None,
    last_run_path: Annotated[Path, typer.Option(help="Path used by loop status/watch to find the latest loop workspace.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Run the Karpathy-style loop suite through one attempt."""
    workspace = workspace_from_ctx(ctx)
    if run_key:
        set_current_run_key(workspace, run_key)
    ensure_workspace_ready(workspace)
    if proposal_file is not None:
        proposal = resolve_workspace_path(workspace, proposal_file).read_text(encoding="utf-8")
    elif not proposal and not sys.stdin.isatty():
        proposal = sys.stdin.read().strip()
    title = title or title_from_body(proposal)
    if not dry_run:
        run_model_loop_once(workspace, title=title, proposal=proposal, force=force, last_run_path=last_run_path)
        return
    if not loop_state_path(workspace).exists() or force:
        initialize_loop_files(workspace, title=title, proposal=proposal, force=force)
    elif proposal:
        path = loop_contract_path(workspace)
        path.write_text(replace_markdown_section(path.read_text(encoding="utf-8"), "Proposal", proposal), encoding="utf-8")
    if status not in {"pass", "fail"}:
        raise typer.BadParameter("status must be pass or fail")
    if recommendation not in {"continue", "restart-attempt", "restart-contract", "stop"}:
        raise typer.BadParameter("recommendation must be continue, restart-attempt, restart-contract, or stop")

    contract_path = loop_contract_path(workspace)
    contract_path.write_text(
        replace_markdown_section(contract_path.read_text(encoding="utf-8"), "Done Criteria", criteria),
        encoding="utf-8",
    )
    append_loop_log(workspace, "generator", "contract proposed", criteria)
    state = read_loop_state(workspace)
    state["status"] = "contract-accepted"
    state["contract_accepted"] = True
    state["last_action"] = "run:contract-accepted"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=f"Contract accepted. {review}")
    append_loop_log(workspace, "evaluator", "contract accepted", review)

    attempt_id = next_loop_attempt_id(state)
    attempt_dir = loop_attempt_dir(workspace, attempt_id)
    (attempt_dir / "traces").mkdir(parents=True, exist_ok=True)
    (attempt_dir / "otel").mkdir(parents=True, exist_ok=True)
    for role in ("planner", "generator", "evaluator"):
        append_jsonl(
            attempt_dir / "traces" / f"{role}.jsonl",
            {
                "timestamp": utc_now(),
                "attempt": attempt_id,
                "role": role,
                "kind": "run",
                "content": f"local loop run {role}",
            },
        )
    append_jsonl(
        attempt_dir / "otel/spans.jsonl",
        {
            "timestamp": utc_now(),
            "name": "attempt_started",
            "attributes": {"attempt": attempt_id, "decision": "continue"},
        },
    )
    state = read_loop_state(workspace)
    state.setdefault("attempts", []).append(
        {
            "id": attempt_id,
            "status": "running",
            "started_at": utc_now(),
            "path": attempt_dir.relative_to(workspace).as_posix(),
        }
    )
    state["current_attempt"] = attempt_id
    state["status"] = "attempt-running"
    state["last_action"] = "run:start-attempt"
    write_loop_state(workspace, state)
    append_loop_log(workspace, "attempt", f"attempt {attempt_id} started")

    report = {
        "schema_version": 1,
        "attempt": attempt_id,
        "written_at": utc_now(),
        "status": status,
        "recommendation": recommendation,
        "bottleneck": bottleneck,
        "findings": [],
        "score": 1.0 if status == "pass" else 0.0,
    }
    report_path = attempt_dir / "evaluator_report.json"
    write_json(report_path, report)
    attempt_status = "passed" if status == "pass" else "failed"
    if recommendation == "restart-attempt":
        attempt_status = "restarted"
    update_loop_attempt(
        state,
        attempt_id,
        status=attempt_status,
        completed_at=utc_now(),
        evaluator_report=report_path.relative_to(workspace).as_posix(),
        **({"bottleneck": bottleneck} if bottleneck else {}),
    )
    if recommendation == "restart-attempt":
        state["status"] = "restart-attempt"
    elif recommendation == "restart-contract":
        state["status"] = "restart-contract"
        state["contract_accepted"] = False
    elif status == "pass":
        state["status"] = "passed"
    elif recommendation == "stop":
        state["status"] = "stopped"
    else:
        state["status"] = "attempt-failed"
    state["current_attempt"] = None
    state["last_action"] = f"run:evaluator-report:{recommendation}"
    if bottleneck:
        state["bottleneck"] = bottleneck
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=f"Run completed with status={status}, recommendation={recommendation}.")
    append_loop_log(workspace, "evaluator", f"attempt {attempt_id} {status}", f"recommendation: {recommendation}")
    write_last_run_workspace(last_run_path, workspace)
    typer.echo(f"attempt: {attempt_id}")
    typer.echo(f"status: {state['status']}")
    typer.echo(f"report: {report_path}")


@app.command("run")
def run_default(
    ctx: typer.Context,
    title: Annotated[str | None, typer.Option(help="Problem title used when initializing a new loop.")] = None,
    proposal: Annotated[str, typer.Option(help="Planner proposal text for contract.md.")] = "",
    proposal_file: Annotated[Path | None, typer.Option(help="Problem proposal Markdown file.")] = None,
    criteria: Annotated[str, typer.Option(help="Dry-run generator-proposed done criteria.", hidden=True)] = "- Define done criteria explicitly.",
    review: Annotated[str, typer.Option(help="Dry-run evaluator contract review text.", hidden=True)] = "Contract criteria are accepted for this local run.",
    status: Annotated[str, typer.Option(help="Dry-run evaluator status: pass or fail.", hidden=True)] = "pass",
    recommendation: Annotated[str, typer.Option(help="Dry-run evaluator recommendation: continue, restart-attempt, restart-contract, or stop.", hidden=True)] = "continue",
    bottleneck: Annotated[str | None, typer.Option(help="Dry-run evaluator bottleneck.", hidden=True)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Use deterministic local loop plumbing instead of model roles.")] = False,
    force: Annotated[bool, typer.Option(help="Reinitialize the loop before running.")] = False,
    run_key: Annotated[str | None, typer.Option(help="Durable loop run key under .hooky/runs/<key>.")] = None,
    last_run_path: Annotated[Path, typer.Option(help="Path used by status/watch to find the latest loop workspace.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Run the loop pipeline."""
    loop_run(
        ctx,
        title=title,
        proposal=proposal,
        proposal_file=proposal_file,
        criteria=criteria,
        review=review,
        status=status,
        recommendation=recommendation,
        bottleneck=bottleneck,
        dry_run=dry_run,
        force=force,
        run_key=run_key,
        last_run_path=last_run_path,
    )


@app.command("log", hidden=True)
def loop_log(
    ctx: typer.Context,
    op: Annotated[str, typer.Option(help="Operation label for the log heading.")] = "note",
    title: Annotated[str, typer.Option(help="Short log title.")] = "manual note",
    body: Annotated[str, typer.Option(help="Optional log body.")] = "",
) -> None:
    """Append an entry to the selected run's log.md."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    append_loop_log(workspace, op, title, body)
    state = read_loop_state(workspace)
    state["last_action"] = op
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=title)
    typer.echo(f"log: {loop_log_path(workspace)}")


@app.command("proposal", hidden=True)
def loop_proposal(
    ctx: typer.Context,
    proposal: Annotated[str | None, typer.Option(help="Problem proposal text.")] = None,
    proposal_file: Annotated[Path | None, typer.Option(help="Problem proposal Markdown file.")] = None,
) -> None:
    """Write the planner problem proposal into contract.md."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    proposal_text = read_body_arg_or_file(proposal, proposal_file)
    loop_proposal_path(workspace).write_text(proposal_text.strip() + ("\n" if proposal_text.strip() else ""), encoding="utf-8")
    path = loop_contract_path(workspace)
    path.write_text(replace_markdown_section(path.read_text(encoding="utf-8"), "Proposal", proposal_text), encoding="utf-8")
    state = read_loop_state(workspace)
    state["status"] = "proposal-written"
    state["contract_accepted"] = False
    state["last_action"] = "proposal"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note="Problem proposal updated.")
    append_loop_log(workspace, "planner", "proposal updated")
    typer.echo(f"contract: {path}")


@app.command("planner", hidden=True)
def loop_planner(
    ctx: typer.Context,
    proposal: Annotated[str, typer.Option(help="Problem proposal text for the planner.")] = "",
) -> None:
    """Run the real planner model role to write contract.md."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    report, usage = loop_agent.generate_planner_artifacts(
        working_folder=workspace,
        proposal=proposal or loop_contract_path(workspace).read_text(encoding="utf-8"),
        attempt_id=state.get("current_attempt"),
    )
    state["status"] = "proposal-written"
    state["contract_accepted"] = False
    state["last_action"] = "planner"
    state.setdefault("role_usage", {})["planner"] = usage
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=str(report.get("summary") or "Planner wrote contract proposal."))
    append_loop_log(workspace, "planner", "planner wrote contract", str(report.get("summary") or ""))
    typer.echo(f"contract: {loop_contract_path(workspace)}")
    typer.echo(f"summary: {report.get('summary')}")


@app.command("propose-contract", hidden=True)
def loop_propose_contract(
    ctx: typer.Context,
    body: Annotated[str | None, typer.Option(help="Done criteria text.")] = None,
    body_file: Annotated[Path | None, typer.Option(help="Done criteria Markdown file.")] = None,
) -> None:
    """Write generator-proposed done criteria into contract.md."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    criteria = read_body_arg_or_file(body, body_file)
    path = loop_contract_path(workspace)
    path.write_text(replace_markdown_section(path.read_text(encoding="utf-8"), "Done Criteria", criteria), encoding="utf-8")
    state = read_loop_state(workspace)
    state["status"] = "contract-proposed"
    state["contract_accepted"] = False
    state["last_action"] = "propose-contract"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note="Done criteria proposed.")
    append_loop_log(workspace, "generator", "contract proposed")
    typer.echo(f"contract: {path}")


@app.command("generator-contract", hidden=True)
def loop_generator_contract(ctx: typer.Context) -> None:
    """Run the real generator model role to propose done criteria."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    report, usage = loop_agent.generate_generator_contract_artifacts(
        working_folder=workspace,
        attempt_id=state.get("current_attempt"),
    )
    state["status"] = "contract-proposed"
    state["contract_accepted"] = False
    state["last_action"] = "generator-contract"
    state.setdefault("role_usage", {})["generator_contract"] = usage
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=str(report.get("summary") or "Generator proposed contract criteria."))
    append_loop_log(workspace, "generator", "generator proposed contract", str(report.get("summary") or ""))
    typer.echo(f"contract: {loop_contract_path(workspace)}")
    typer.echo(f"feature_list: {loop_feature_list_path(workspace)}")
    typer.echo(f"summary: {report.get('summary')}")


@app.command("review-contract", hidden=True)
def loop_review_contract(
    ctx: typer.Context,
    status: Annotated[str, typer.Option(help="Review status: accepted or rejected.")] = "rejected",
    body: Annotated[str | None, typer.Option(help="Evaluator review text.")] = None,
    body_file: Annotated[Path | None, typer.Option(help="Evaluator review Markdown file.")] = None,
) -> None:
    """Record evaluator contract review in log.md and progress.md."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    if status not in {"accepted", "rejected"}:
        raise typer.BadParameter("status must be accepted or rejected")
    review = read_body_arg_or_file(body, body_file)
    state = read_loop_state(workspace)
    state["status"] = "contract-accepted" if status == "accepted" else "contract-rejected"
    state["contract_accepted"] = status == "accepted"
    state["last_action"] = f"review-contract:{status}"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=f"Contract review {status}. {review.strip()}")
    append_loop_log(workspace, "evaluator", f"contract {status}", review)
    typer.echo(f"contract_review: {status}")


@app.command("evaluator-contract", hidden=True)
def loop_evaluator_contract(ctx: typer.Context) -> None:
    """Run the real evaluator model role to accept or reject the proposed contract."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    report, usage = loop_agent.generate_evaluator_contract_artifacts(
        working_folder=workspace,
        attempt_id=state.get("current_attempt"),
    )
    accepted = bool(report.get("accepted"))
    state["status"] = "contract-accepted" if accepted else "contract-rejected"
    state["contract_accepted"] = accepted
    state["last_action"] = "evaluator-contract"
    state.setdefault("role_usage", {})["evaluator_contract"] = usage
    write_loop_state(workspace, state)
    review = str(report.get("review") or "")
    required_changes = report.get("required_changes") if isinstance(report.get("required_changes"), list) else []
    note = f"Contract review {'accepted' if accepted else 'rejected'}. {review}"
    if required_changes:
        note += " Required changes: " + "; ".join(str(item) for item in required_changes)
    write_loop_progress(workspace, state, note=note)
    append_loop_log(workspace, "evaluator", f"contract {'accepted' if accepted else 'rejected'}", note)
    typer.echo(f"contract_review: {'accepted' if accepted else 'rejected'}")
    typer.echo(f"review: {review}")


@app.command("accept-contract", hidden=True)
def loop_accept_contract(ctx: typer.Context) -> None:
    """Mark contract.md as accepted for implementation."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    state["status"] = "contract-accepted"
    state["contract_accepted"] = True
    state["last_action"] = "accept-contract"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note="Contract accepted.")
    append_loop_log(workspace, "contract", "contract accepted")
    typer.echo("contract_accepted: true")


@app.command("start-attempt", hidden=True)
def loop_start_attempt(ctx: typer.Context) -> None:
    """Start a new implementation attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    attempt_id, attempt_dir, state = start_loop_attempt_state(workspace, state)
    state["last_action"] = "start-attempt"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=f"Attempt {attempt_id} started.")
    append_loop_log(workspace, "attempt", f"attempt {attempt_id} started")
    typer.echo(f"attempt: {attempt_id}")
    typer.echo(f"path: {attempt_dir}")


@app.command("generator-implement", hidden=True)
def loop_generator_implement(ctx: typer.Context) -> None:
    """Run the real generator model role for the active implementation attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    report, usage = loop_agent.generate_generator_implementation_artifacts(
        working_folder=workspace,
        attempt_id=attempt_id,
    )
    report_path = loop_attempt_dir(workspace, attempt_id) / "generator_report.json"
    write_json(report_path, report)
    state.setdefault("role_usage", {})["generator_implementation"] = usage
    state["last_action"] = "generator-implement"
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=str(report.get("summary") or "Generator completed implementation pass."))
    append_loop_log(workspace, "generator", f"attempt {attempt_id} implementation", str(report.get("summary") or ""))
    typer.echo(f"report: {report_path}")
    typer.echo(f"summary: {report.get('summary')}")


@app.command("complete-attempt", hidden=True)
def loop_complete_attempt(
    ctx: typer.Context,
    result: Annotated[str, typer.Option(help="Evaluator result: pass or fail.")] = "fail",
    bottleneck: Annotated[str | None, typer.Option(help="Current bottleneck identified by evaluator.")] = None,
) -> None:
    """Complete the active attempt from evaluator evidence."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    if result not in {"pass", "fail"}:
        raise typer.BadParameter("result must be pass or fail")
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    attempt_updates: dict[str, Any] = {
        "status": "passed" if result == "pass" else "failed",
        "completed_at": utc_now(),
    }
    if bottleneck:
        attempt_updates["bottleneck"] = bottleneck
    update_loop_attempt(state, attempt_id, **attempt_updates)
    state["current_attempt"] = None
    state["status"] = "passed" if result == "pass" else "attempt-failed"
    state["last_action"] = f"complete-attempt:{result}"
    if bottleneck:
        state["bottleneck"] = bottleneck
    write_loop_state(workspace, state)
    note = f"Attempt {attempt_id} completed with result={result}."
    if bottleneck:
        note += f" Bottleneck: {bottleneck}."
    write_loop_progress(workspace, state, note=note)
    append_loop_log(workspace, "evaluation", f"attempt {attempt_id} {result}", note)
    typer.echo(f"attempt: {attempt_id}")
    typer.echo(f"result: {result}")


@app.command("evaluator-attempt", hidden=True)
def loop_evaluator_attempt(ctx: typer.Context) -> None:
    """Run the real evaluator model role for the active implementation attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    report, usage = loop_agent.generate_evaluator_attempt_artifacts(
        working_folder=workspace,
        attempt_id=attempt_id,
    )
    report = {
        "schema_version": 1,
        "attempt": attempt_id,
        "written_at": utc_now(),
        **report,
    }
    report_path = loop_attempt_dir(workspace, attempt_id) / "evaluator_report.json"
    write_json(report_path, report)
    state.setdefault("role_usage", {})["evaluator_attempt"] = usage
    state = apply_loop_evaluator_report(
        workspace,
        state,
        attempt_id=attempt_id,
        report=report,
        report_path=report_path,
        action="evaluator-attempt",
    )
    typer.echo(f"report: {report_path}")
    typer.echo(f"recommendation: {report.get('recommendation')}")
    typer.echo(f"status: {state.get('status')}")


@app.command("evaluator-report", hidden=True)
def loop_evaluator_report(
    ctx: typer.Context,
    status: Annotated[str, typer.Option(help="Evaluator status: pass or fail.")] = "fail",
    recommendation: Annotated[str, typer.Option(help="Recommended control action: continue, restart-attempt, restart-contract, or stop.")] = "continue",
    bottleneck: Annotated[str, typer.Option(help="Current bottleneck. Use none_visible_after_trace_review only after trace/artifact review.")] = "none_visible_after_trace_review",
    finding: Annotated[list[str] | None, typer.Option("--finding", help="Evaluator finding. Repeat for multiple findings.")] = None,
    score: Annotated[float | None, typer.Option(help="Optional subjective/objective score from 0.0 to 1.0.")] = None,
) -> None:
    """Write an evaluator report for the active attempt and apply its recommendation."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    if status not in {"pass", "fail"}:
        raise typer.BadParameter("status must be pass or fail")
    if recommendation not in {"continue", "restart-attempt", "restart-contract", "stop"}:
        raise typer.BadParameter("recommendation must be continue, restart-attempt, restart-contract, or stop")
    if not bottleneck.strip():
        raise typer.BadParameter("bottleneck must be non-empty")
    if score is not None and (score < 0 or score > 1):
        raise typer.BadParameter("score must be between 0.0 and 1.0")
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    report = {
        "schema_version": 1,
        "attempt": attempt_id,
        "written_at": utc_now(),
        "status": status,
        "recommendation": recommendation,
        "bottleneck": bottleneck,
        "findings": finding or [],
        "score": score,
    }
    report_path = loop_attempt_dir(workspace, attempt_id) / "evaluator_report.json"
    write_json(report_path, report)
    apply_loop_evaluator_report(
        workspace,
        state,
        attempt_id=attempt_id,
        report=report,
        report_path=report_path,
        action="evaluator-report",
    )
    typer.echo(f"report: {report_path}")
    typer.echo(f"recommendation: {recommendation}")


@app.command("trace-event", hidden=True)
def loop_trace_event(
    ctx: typer.Context,
    role: Annotated[str, typer.Option(help="Model role: planner, generator, or evaluator.")],
    content: Annotated[str, typer.Option(help="Transcript content to append.")],
    kind: Annotated[str, typer.Option(help="Event kind, such as message, tool_call, or decision.")] = "message",
) -> None:
    """Append a grep-friendly JSONL trace event for the active attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    if role not in {"planner", "generator", "evaluator"}:
        raise typer.BadParameter("role must be planner, generator, or evaluator")
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    path = loop_attempt_dir(workspace, attempt_id) / "traces" / f"{role}.jsonl"
    payload = {
        "timestamp": utc_now(),
        "attempt": attempt_id,
        "role": role,
        "kind": kind,
        "content": content,
    }
    append_jsonl(path, payload)
    append_loop_log(workspace, "trace", f"{role} {kind}", f"attempt: {attempt_id}\ntrace: {path.relative_to(workspace).as_posix()}")
    typer.echo(f"trace: {path}")


@app.command("otel-event", hidden=True)
def loop_otel_event(
    ctx: typer.Context,
    name: Annotated[str, typer.Option(help="OpenTelemetry-style event name.")],
    role: Annotated[str | None, typer.Option(help="Optional role associated with the event.")] = None,
    decision: Annotated[str | None, typer.Option(help="Optional loop decision.")] = None,
    cost: Annotated[float | None, typer.Option(help="Optional cost attribute.")] = None,
    tokens: Annotated[int | None, typer.Option(help="Optional token count attribute.")] = None,
) -> None:
    """Append a local OpenTelemetry-style event for the active attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    if role is not None and role not in {"planner", "generator", "evaluator"}:
        raise typer.BadParameter("role must be planner, generator, or evaluator")
    state = read_loop_state(workspace)
    attempt_id = active_loop_attempt(state)
    path = loop_attempt_dir(workspace, attempt_id) / "otel" / "spans.jsonl"
    attributes: dict[str, Any] = {"attempt": attempt_id}
    if role:
        attributes["role"] = role
    if decision:
        attributes["decision"] = decision
    if cost is not None:
        attributes["cost"] = cost
    if tokens is not None:
        attributes["tokens"] = tokens
    payload = {
        "timestamp": utc_now(),
        "name": name,
        "attributes": attributes,
    }
    append_jsonl(path, payload)
    typer.echo(f"otel: {path}")


@app.command("restart-attempt", hidden=True)
def loop_restart_attempt(
    ctx: typer.Context,
    reason: Annotated[str, typer.Option(help="Evaluator evidence for restart.")] = "bad trajectory",
) -> None:
    """Record a restart-attempt decision and clear the active attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    attempt_id = state.get("current_attempt")
    if attempt_id:
        for attempt in state.get("attempts", []):
            if isinstance(attempt, dict) and attempt.get("id") == attempt_id:
                attempt["status"] = "restarted"
                attempt["completed_at"] = utc_now()
                attempt["restart_reason"] = reason
                break
    state["current_attempt"] = None
    state["status"] = "restart-attempt"
    state["last_action"] = "restart-attempt"
    state["bottleneck"] = "bad_attempt"
    write_loop_state(workspace, state)
    note = f"Restart attempt requested. Reason: {reason}"
    write_loop_progress(workspace, state, note=note)
    append_loop_log(workspace, "restart-attempt", "attempt restart", note)
    typer.echo("restart-attempt: recorded")


@app.command("restart-contract", hidden=True)
def loop_restart_contract(
    ctx: typer.Context,
    reason: Annotated[str, typer.Option(help="Reason the contract must be revised.")] = "contract is wrong",
) -> None:
    """Record a restart-contract decision."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    state["current_attempt"] = None
    state["status"] = "restart-contract"
    state["contract_accepted"] = False
    state["last_action"] = "restart-contract"
    state["bottleneck"] = "contract"
    write_loop_state(workspace, state)
    note = f"Restart contract requested. Reason: {reason}"
    write_loop_progress(workspace, state, note=note)
    append_loop_log(workspace, "restart-contract", "contract restart", note)
    typer.echo("restart-contract: recorded")


@app.command()
def start(
    ctx: typer.Context,
    title: Annotated[str | None, typer.Option(help="Problem title used when initializing a new loop.")] = None,
    proposal: Annotated[str, typer.Option(help="Planner proposal text for contract.md.")] = "",
    proposal_file: Annotated[Path | None, typer.Option(help="Problem proposal Markdown file.")] = None,
    force: Annotated[bool, typer.Option(help="Reinitialize the loop before running.")] = False,
    skill: Annotated[list[str] | None, typer.Option("--skill", help="Preselect an agent skill by name for this run. Repeat for multiple skills.")] = None,
    run_key: Annotated[str | None, typer.Option(help="Durable loop run key under .hooky/runs/<key>.")] = None,
    last_run_path: Annotated[Path, typer.Option(help="Path used by `hooky watch` to find the latest workspace.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Start the loop pipeline and register this workspace for `hooky watch`."""
    workspace = workspace_from_ctx(ctx)
    if run_key:
        set_current_run_key(workspace, run_key)
    if skill:
        os.environ["HOOKY_ACTIVE_SKILLS"] = ",".join(skill)
    proposal_text = proposal
    if proposal_file is not None:
        proposal_text = resolve_workspace_path(workspace, proposal_file).read_text(encoding="utf-8")
    write_last_run_workspace(last_run_path, workspace)
    typer.echo(f"workspace: {workspace}")
    if skill:
        typer.echo(f"skills: {', '.join(skill)}")
    typer.echo("watch: hooky watch")
    run_model_loop_once(workspace, title=title, proposal=proposal_text, force=force, last_run_path=last_run_path)


@app.command()
def watch(
    ctx: typer.Context,
    last_run_path: Annotated[Path, typer.Option(help="Path written by `hooky start`.")] = DEFAULT_LAST_RUN_PATH,
    path_only: Annotated[bool, typer.Option("--path", help="Only print the loop log path.")] = False,
    follow: Annotated[bool, typer.Option("--follow/--no-follow", "-f", help="Follow the append-only runtime log.")] = True,
) -> None:
    """Follow the latest started loop log."""
    loop_watch(ctx, path_only=path_only, follow=follow, last_run_path=last_run_path)


def follow_runtime_log(workspace: Path, stage: str, state: dict[str, Any], tail_file: Path) -> None:
    position = 0

    def read_new() -> bool:
        nonlocal position
        with tail_file.open("r", encoding="utf-8") as handle:
            handle.seek(position)
            chunk = handle.read()
            position = handle.tell()
        if chunk:
            typer.echo(chunk.rstrip("\n"))
            return True
        return False

    if not tail_file.exists():
        tail_file.parent.mkdir(parents=True, exist_ok=True)
        tail_file.write_text("", encoding="utf-8")
    read_new()
    try:
        while True:
            time.sleep(1)
            read_new()
    except KeyboardInterrupt:
        return


@app.command()
def status(
    ctx: typer.Context,
    last_run_path: Annotated[Path, typer.Option(help="Path written by `hooky start`; used when the current directory has no loop.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Show the current loop state and durable file locations."""
    loop_status(ctx, last_run_path=last_run_path)


@app.command()
def trace(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt.")] = None,
    raw_path: Annotated[bool, typer.Option("--path", help="Only print the runtime log path.")] = False,
    tail_path: Annotated[bool, typer.Option("--tail-path", help="Only print the append-only runtime log path.")] = False,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow the append-only runtime log.")] = False,
    last_run_path: Annotated[Path, typer.Option(help="Path written by `hooky start`; used when the current directory has no loop.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Show or follow the loop runtime log for the active/latest attempt."""
    workspace = workspace_for_loop(ctx, last_run_path)
    ctx.obj["workspace"] = workspace
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    tail_file = root / "runtime_events.log"
    if raw_path:
        typer.echo(tail_file)
        return
    if tail_path:
        typer.echo(tail_file)
        return
    if follow:
        follow_runtime_log(workspace, "loop", state, tail_file)
        return
    if not tail_file.exists():
        raise typer.BadParameter(f"loop runtime log not found: {tail_file}")
    typer.echo(tail_file.read_text(encoding="utf-8").rstrip())


@app.command()
def report(ctx: typer.Context, output: Annotated[Path | None, typer.Option(help="HTML output path.")] = None) -> None:
    """Write a loop harness review report."""
    if output is not None:
        raise typer.BadParameter("HTML report output is not supported. Use `hooky report`.")
    loop_harness_review(ctx)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check loop workspace and environment readiness."""
    workspace = workspace_from_ctx(ctx)
    provider = os.environ.get("WEB_SEARCH_PROVIDER")
    checks = {
        "workspace": workspace.exists(),
        "loop": loop_dir(workspace).exists(),
        "OPENROUTER_API_KEY": bool(os.environ.get("OPENROUTER_API_KEY")),
    }
    failed = False
    for name, ok in checks.items():
        typer.echo(f"{'ok' if ok else 'missing'}  {name}")
        failed = failed or not ok
    if provider:
        typer.echo(f"ok  WEB_SEARCH_PROVIDER={provider}")
        if provider == "tavily":
            tavily_ok = bool(os.environ.get("TAVILY_API_KEY"))
            typer.echo(f"{'ok' if tavily_ok else 'missing'}  TAVILY_API_KEY")
            failed = failed or not tavily_ok
    else:
        typer.echo("missing  WEB_SEARCH_PROVIDER (web_search tool will return a structured error)")
    if failed:
        raise typer.Exit(1)


def cli() -> None:
    app()


if __name__ == "__main__":
    cli()
