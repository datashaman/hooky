"""Loop state lifecycle: initialize, read/write, transitions, attempt bookkeeping."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from hooky.cli.paths import (
    atomic_write_text,
    loop_attempt_dir,
    loop_contract_path,
    loop_dir,
    loop_feature_list_path,
    loop_log_path,
    loop_progress_path,
    loop_proposal_path,
    loop_state_path,
    read_json,
    selected_run_key,
    set_current_run_key,
    utc_now,
    write_json,
)
from hooky.cli.validation import enforce_loop_evaluator_evidence


class LoopStateConsistencyError(typer.BadParameter):
    """Raised when state.json and progress.md disagree about the current attempt."""


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


def _progress_field(progress_text: str, field: str) -> str | None:
    prefix = f"- {field}: "
    for line in progress_text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def check_loop_state_consistency(workspace: Path, state: dict[str, Any]) -> None:
    """Cross-check ``state.json`` against ``progress.md`` before a resume continues.

    ``progress.md`` is rendered from state on every transition (see
    ``write_loop_progress``), so on a healthy resume the two must agree on the
    current attempt id and status. A killed process mid-write can leave one
    file updated and the other stale/truncated; raise loudly rather than
    silently trusting ``state.json``.
    """
    progress_path = loop_progress_path(workspace)
    if not progress_path.exists():
        return
    progress_text = progress_path.read_text(encoding="utf-8")
    if not progress_text.strip():
        return

    progress_attempt = _progress_field(progress_text, "current_attempt")
    progress_status = _progress_field(progress_text, "status")
    if progress_attempt is None and progress_status is None:
        raise LoopStateConsistencyError(
            f"loop state is inconsistent: {progress_path} has content but no recognizable "
            "'- current_attempt: ...' or '- status: ...' entry could be extracted from it. "
            "This can happen if the process was killed mid-write. Inspect "
            f"{loop_state_path(workspace)} and {progress_path} and repair before resuming."
        )

    state_attempt = state.get("current_attempt")
    state_attempt_str = str(state_attempt) if state_attempt is not None else "none"
    if progress_attempt is not None and progress_attempt != state_attempt_str:
        raise LoopStateConsistencyError(
            "loop state is inconsistent: state.json current_attempt="
            f"{state_attempt_str!r} but progress.md current_attempt={progress_attempt!r}. "
            "This can happen if the process was killed mid-write. Inspect "
            f"{loop_state_path(workspace)} and {progress_path} and repair before resuming."
        )

    state_status = str(state.get("status", "unknown"))
    if progress_status is not None and progress_status != state_status:
        raise LoopStateConsistencyError(
            f"loop state is inconsistent: state.json status={state_status!r} but "
            f"progress.md status={progress_status!r}. This can happen if the process "
            f"was killed mid-write. Inspect {loop_state_path(workspace)} and {progress_path} "
            "and repair before resuming."
        )


def read_loop_state(workspace: Path) -> dict[str, Any]:
    path = loop_state_path(workspace)
    if not path.exists():
        raise typer.BadParameter("loop is not initialized. Run `hooky init` first.")
    try:
        state = read_json(path)
    except json.JSONDecodeError as exc:
        raise LoopStateConsistencyError(
            f"loop state file is corrupted (invalid JSON): {path}. This can happen if the "
            f"process was killed mid-write. Original error: {exc}"
        ) from exc
    check_loop_state_consistency(workspace, state)
    return state


def write_loop_state(workspace: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_json(loop_state_path(workspace), state)


def ensure_loop_initialized(workspace: Path) -> None:
    if not loop_state_path(workspace).exists():
        raise typer.BadParameter("loop is not initialized. Run `hooky init` first.")


def append_loop_log(workspace: Path, op: str, title: str, body: str = "") -> None:
    path = loop_log_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).replace(microsecond=0)
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
    atomic_write_text(loop_progress_path(workspace), "\n".join(lines) + "\n")


def record_loop_transition(workspace: Path, state: dict[str, Any], *, note: str, log_op: str, log_title: str, log_body: str = "") -> None:
    write_loop_state(workspace, state)
    write_loop_progress(workspace, state, note=note)
    append_loop_log(workspace, log_op, log_title, log_body)


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


def initialize_loop_files(workspace: Path, *, title: str | None, proposal: str = "", force: bool = False) -> Path:
    run_key = selected_run_key(workspace)
    set_current_run_key(workspace, run_key)
    loop_root = loop_dir(workspace)
    if loop_state_path(workspace).exists() and not force:
        raise typer.BadParameter("loop already initialized. Use --force to overwrite.")
    loop_root.mkdir(parents=True, exist_ok=True)
    write_json(loop_feature_list_path(workspace), default_loop_feature_list())
    proposal_artifact = proposal.strip() or (title or "").strip()
    atomic_write_text(
        loop_proposal_path(workspace),
        proposal_artifact + ("\n" if proposal_artifact else ""),
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
    atomic_write_text(loop_contract_path(workspace), "\n".join(contract_lines))
    write_loop_progress(workspace, state, note="Loop initialized.")
    atomic_write_text(loop_log_path(workspace), "")
    append_loop_log(workspace, "init", "loop initialized", f"workspace: {workspace}")
    return loop_root


def initialize_light_implementation_files(workspace: Path, *, proposal: str) -> Path:
    """Set up loop files for a light-implement run: no planner, no contract negotiation.

    The proposal is already a scoped, specific ask (a PR comment), so it becomes
    the Done Criteria directly instead of being negotiated into one.
    """
    run_key = selected_run_key(workspace)
    set_current_run_key(workspace, run_key)
    loop_root = loop_dir(workspace)
    loop_root.mkdir(parents=True, exist_ok=True)
    proposal_text = proposal.strip()
    write_json(
        loop_feature_list_path(workspace),
        {
            "schema_version": 1,
            "features": [{"id": "F001", "text": proposal_text, "proposal_refs": [], "status": "pending"}] if proposal_text else [],
        },
    )
    atomic_write_text(loop_proposal_path(workspace), proposal_text + ("\n" if proposal_text else ""))
    state = default_loop_state()
    state["run_key"] = run_key
    state["created_at"] = utc_now()
    state["contract_accepted"] = True
    state["status"] = "contract-accepted"
    write_loop_state(workspace, state)
    contract_lines = ["# Loop Contract", "", "## Done Criteria", "", proposal_text or "_(no proposal text provided)_", ""]
    atomic_write_text(loop_contract_path(workspace), "\n".join(contract_lines))
    write_loop_progress(workspace, state, note="Light implement: contract set directly from the request, no negotiation.")
    atomic_write_text(loop_log_path(workspace), "")
    append_loop_log(workspace, "init", "light-implement initialized", f"workspace: {workspace}")
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
    record_loop_transition(workspace, state, note=note, log_op="evaluator", log_title=f"attempt {attempt_id} report", log_body=note)
    return state


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


def latest_loop_attempt_id(state: dict[str, Any]) -> str | None:
    if state.get("current_attempt"):
        return str(state["current_attempt"])
    attempts = state.get("attempts") if isinstance(state.get("attempts"), list) else []
    for attempt in reversed(attempts):
        if isinstance(attempt, dict) and attempt.get("id"):
            return str(attempt["id"])
    return None
