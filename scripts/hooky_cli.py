#!/usr/bin/env python3
"""Typer CLI for running Hooky task pipelines."""

from __future__ import annotations

import json
import os
import shlex
import socket
import shutil
import subprocess
import sys
import time
import difflib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import typer


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import builder_agent
import agent_runtime
import eval_agent
import generate_eval_report
import spec_agent
import test_agent
import verifier_agent


app = typer.Typer(help="Run the Hooky agentic SDLC pipeline.", no_args_is_help=True)
task_app = typer.Typer(help="Create, inspect, and switch tasks.", no_args_is_help=True)
run_app = typer.Typer(help="Run pipeline stages for the current task.", no_args_is_help=True)
approve_app = typer.Typer(help="Record human approval gates.", no_args_is_help=True)
app.add_typer(task_app, name="task")
app.add_typer(run_app, name="run")
app.add_typer(approve_app, name="approve")

PIPELINE_STAGES = ["spec", "test", "builder", "verifier", "eval"]
REMEDIABLE_STAGES = {"spec", "test", "builder", "verifier", "eval"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@app.callback()
def main(
    ctx: typer.Context,
    workspace: Annotated[Path, typer.Option("--workspace", "-C", help="Workspace directory. Defaults to current directory.")] = Path("."),
) -> None:
    ctx.obj = {"workspace": workspace.resolve()}


def workspace_from_ctx(ctx: typer.Context) -> Path:
    return ctx.obj["workspace"]


@contextmanager
def in_workspace(workspace: Path):
    previous = Path.cwd()
    os.chdir(workspace)
    try:
        yield
    finally:
        os.chdir(previous)


def repo_file(path: str) -> Path:
    return REPO_ROOT / path


def workflow_dir(workspace: Path) -> Path:
    return workspace / ".workflow"


def tasks_dir(workspace: Path) -> Path:
    return workflow_dir(workspace) / "tasks"


def global_state_path(workspace: Path) -> Path:
    return workflow_dir(workspace) / "state.json"


def task_dir(workspace: Path, task_id: str) -> Path:
    return tasks_dir(workspace) / task_id


def task_state_path(workspace: Path, task_id: str) -> Path:
    return task_dir(workspace, task_id) / "state.json"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_global_state(workspace: Path) -> dict[str, Any]:
    path = global_state_path(workspace)
    return read_json(path) if path.exists() else {}


def write_global_state(workspace: Path, state: dict[str, Any]) -> None:
    write_json(global_state_path(workspace), state)


def set_current_task(workspace: Path, task_id: str) -> None:
    state = read_global_state(workspace)
    state["current_task"] = task_id
    state["updated_at"] = utc_now()
    write_global_state(workspace, state)


def resolve_current_task(workspace: Path) -> str:
    current = read_global_state(workspace).get("current_task")
    if current:
        if not task_state_path(workspace, str(current)).exists():
            raise typer.BadParameter(f"current task does not exist: {current}")
        return str(current)
    task_ids = sorted(path.name for path in tasks_dir(workspace).glob("*") if (path / "state.json").exists()) if tasks_dir(workspace).exists() else []
    if len(task_ids) == 1:
        set_current_task(workspace, task_ids[0])
        return task_ids[0]
    if not task_ids:
        raise typer.BadParameter("no task exists. Run `hooky task create --title ... --body-file ...` first.")
    raise typer.BadParameter("multiple tasks exist. Run `hooky task switch <task-id>` or pass `--task`.")


def load_task_state(workspace: Path, task_id: str | None = None) -> dict[str, Any]:
    resolved = task_id or resolve_current_task(workspace)
    return read_json(task_state_path(workspace, resolved))


def save_task_state(workspace: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_json(task_state_path(workspace, state["task_id"]), state)


def set_stage_status(workspace: Path, state: dict[str, Any], stage: str, status: str, **extra: Any) -> None:
    if status == "running":
        extra = {
            "owner_pid": os.getpid(),
            "owner_host": socket.gethostname(),
            "started_at": utc_now(),
            "trace": stage_runtime_events_path(workspace, stage, state),
            **extra,
        }
    state.setdefault("stage_status", {})[stage] = {
        "status": status,
        "updated_at": utc_now(),
        **extra,
    }
    save_task_state(workspace, state)
    event_fields = dict(extra)
    if status == "running":
        event_fields.setdefault("pid", event_fields.get("owner_pid"))
    append_pipeline_event(
        workspace,
        "stage",
        task=state.get("task_id"),
        stage=stage,
        status=status,
        **event_fields,
    )


def set_pipeline_status(workspace: Path, state: dict[str, Any], status: str, **extra: Any) -> None:
    if status == "running":
        extra = {
            "owner_pid": os.getpid(),
            "owner_host": socket.gethostname(),
            "started_at": utc_now(),
            "trace": pipeline_log_path(workspace).relative_to(workspace).as_posix(),
            **extra,
        }
    state["pipeline_status"] = {
        "status": status,
        "updated_at": utc_now(),
        **extra,
    }
    save_task_state(workspace, state)
    event_fields = dict(extra)
    if status == "running":
        event_fields.setdefault("pid", event_fields.get("owner_pid"))
    append_pipeline_event(
        workspace,
        "pipeline",
        task=state.get("task_id"),
        status=status,
        **event_fields,
    )


def pipeline_log_path(workspace: Path) -> Path:
    return workflow_dir(workspace) / "runtime_events.log"


def remediation_dir(workspace: Path, state: dict[str, Any]) -> Path:
    return workflow_dir(workspace) / "artifacts" / "remediation" / str(state["task_id"])


def current_remediation_path(workspace: Path) -> Path:
    return workflow_dir(workspace) / "artifacts" / "remediation" / "current.json"


def append_pipeline_event(workspace: Path, event: str, **fields: Any) -> None:
    path = pipeline_log_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [utc_now(), event]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={quote_log_value(value)}")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(" ".join(parts) + "\n")


def create_remediation_plan(
    workspace: Path,
    state: dict[str, Any],
    eval_contract: dict[str, Any],
    eval_contract_path: Path,
    *,
    root_cause_stage: str | None = None,
    source: str = "eval",
) -> Path | None:
    root_cause_stage = str(root_cause_stage or eval_contract.get("root_cause_stage") or "unknown")
    if root_cause_stage not in REMEDIABLE_STAGES or root_cause_stage == "eval":
        return None
    generated_at = utc_now()
    rerun_command = f"uv run hooky -C {workspace.as_posix()} run remediation --auto-approve"
    payload = {
        "schema_version": 1,
        "task_id": state.get("task_id"),
        "generated_at": generated_at,
        "root_cause_stage": root_cause_stage,
        "resume_from_stage": root_cause_stage,
        "source": source,
        "eval_contract": display_workspace_path(eval_contract_path, workspace),
        "status": eval_contract.get("status"),
        "safe_to_merge": eval_contract.get("safe_to_merge"),
        "findings": eval_contract.get("findings", []),
        "trajectory_findings": eval_contract.get("trajectory_findings", []),
        "artifact_findings": eval_contract.get("artifact_findings", []),
        "tooling_findings": eval_contract.get("tooling_findings", []),
        "human_review_focus": eval_contract.get("human_review_focus", []),
        "rerun_command": rerun_command,
    }
    plan_dir = remediation_dir(workspace, state)
    plan_path = plan_dir / f"{generated_at.replace(':', '').replace('+', 'Z')}-{root_cause_stage}.json"
    write_json(plan_path, payload)
    write_json(current_remediation_path(workspace), payload)
    state.setdefault("artifacts", {})["remediation"] = {
        "current": current_remediation_path(workspace).relative_to(workspace).as_posix(),
        "plan": plan_path.relative_to(workspace).as_posix(),
        "root_cause_stage": root_cause_stage,
        "rerun_command": rerun_command,
    }
    save_task_state(workspace, state)
    append_pipeline_event(
        workspace,
        "remediation",
        task=state.get("task_id"),
        status="created",
        root_cause_stage=root_cause_stage,
        source=source,
        plan=plan_path.relative_to(workspace).as_posix(),
    )
    return plan_path


def maybe_create_remediation_plan(workspace: Path, state: dict[str, Any], eval_contract: dict[str, Any], eval_contract_path: Path) -> Path | None:
    if eval_contract.get("status") == "pass" and eval_contract.get("safe_to_merge") is True:
        return None
    return create_remediation_plan(workspace, state, eval_contract, eval_contract_path)


def maybe_create_test_remediation_from_builder_failure(workspace: Path, state: dict[str, Any], builder_contract: dict[str, Any], builder_contract_path: Path) -> Path | None:
    findings = builder_contract.get("test_contract_findings")
    if builder_contract.get("tests_passing") is not False or not isinstance(findings, list) or not findings:
        return None
    payload = {
        "status": "fail",
        "safe_to_merge": False,
        "root_cause_stage": "test",
        "findings": [
            "Builder determined the approved test contract is invalid or unimplementable without editing tests.",
            *[str(item) for item in findings],
        ],
        "trajectory_findings": [
            "A downstream stage identified a prior-stage contract defect; remediation must rerun the owning stage.",
        ],
        "artifact_findings": [str(item) for item in builder_contract.get("failures_remaining", [])],
        "tooling_findings": [],
        "human_review_focus": [
            "Regenerate or repair the approved Test Agent artifacts using the Builder findings.",
            "Re-approve tests before rerunning Builder.",
        ],
    }
    return create_remediation_plan(
        workspace,
        state,
        payload,
        builder_contract_path,
        root_cause_stage="test",
        source="builder-test-contract-finding",
    )


def recover_artifacts_for_resume(workspace: Path, state: dict[str, Any], start_stage: str, *, auto_approve: bool) -> None:
    artifacts = state.setdefault("artifacts", {})
    stage_status = state.setdefault("stage_status", {})
    task_id = str(state["task_id"])
    prior_stages = PIPELINE_STAGES[: PIPELINE_STAGES.index(start_stage)]

    if "spec" in prior_stages and "spec" not in artifacts:
        spec_contract = workspace / "docs/specs" / task_id / "contract.json"
        if spec_contract.exists():
            artifacts["spec"] = {
                "artifact_dir": spec_contract.parent.relative_to(workspace).as_posix(),
                "contract": spec_contract.relative_to(workspace).as_posix(),
            }
    if "spec" in prior_stages and "spec" in artifacts:
        stage_status["spec"] = {
            "status": "passed",
            "updated_at": utc_now(),
            "contract": artifacts["spec"].get("contract"),
        }

    if "test" in prior_stages and "test" not in artifacts:
        test_contract = workspace / ".workflow/artifacts/test-agent" / task_id / "contract.json"
        if test_contract.exists():
            test_contract_payload = read_json(test_contract)
            artifacts["test"] = {
                "report_dir": test_contract.parent.relative_to(workspace).as_posix(),
                "contract": test_contract.relative_to(workspace).as_posix(),
                "test_files": [item["path"] for item in test_contract_payload.get("test_files", [])],
                "fixtures": [item["path"] for item in test_contract_payload.get("fixtures", [])],
            }
    if "test" in prior_stages and "test" in artifacts:
        stage_status["test"] = {
            "status": "passed",
            "updated_at": utc_now(),
            "contract": artifacts["test"].get("contract"),
            "report_dir": artifacts["test"].get("report_dir"),
        }

    if "builder" in prior_stages and "builder" not in artifacts:
        builder_contract = workspace / ".workflow/artifacts/builder-agent/contract.json"
        if builder_contract.exists():
            proposal_dir = change_proposal_dir(workspace, task_id)
            artifacts["builder"] = {
                "report_dir": builder_contract.parent.relative_to(workspace).as_posix(),
                "contract": builder_contract.relative_to(workspace).as_posix(),
                "proposal_dir": proposal_dir.relative_to(workspace).as_posix(),
                "proposal": (proposal_dir / "proposal.json").relative_to(workspace).as_posix(),
                "patch": (proposal_dir / "patch.diff").relative_to(workspace).as_posix(),
                "summary": (proposal_dir / "summary.md").relative_to(workspace).as_posix(),
            }
    if "builder" in prior_stages and "builder" in artifacts:
        stage_status["builder"] = {
            "status": "passed",
            "updated_at": utc_now(),
            "contract": artifacts["builder"].get("contract"),
            "report_dir": artifacts["builder"].get("report_dir"),
            "proposal": artifacts["builder"].get("proposal"),
        }

    if auto_approve:
        approvals = state.setdefault("approvals", {})
        if "spec" in prior_stages and "spec" in artifacts and "spec" not in approvals:
            approvals["spec"] = {"approved_at": utc_now(), "source": "remediation-auto-approve"}
        if "test" in prior_stages and "test" in artifacts and "test" not in approvals:
            approvals["test"] = {"approved_at": utc_now(), "source": "remediation-auto-approve"}

    save_task_state(workspace, state)


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stage_max_seconds(stage: str) -> int:
    defaults = {
        "spec": 180,
        "test": 600,
        "builder": 420,
        "verifier": 300,
        "eval": 300,
    }
    env_names = {
        "spec": "SPEC_AGENT_MAX_SECONDS",
        "test": "TEST_AGENT_MAX_SECONDS",
        "builder": "BUILDER_AGENT_MAX_SECONDS",
        "verifier": "VERIFIER_AGENT_MAX_SECONDS",
        "eval": "EVAL_AGENT_MAX_SECONDS",
    }
    env_name = env_names.get(stage)
    if env_name and os.environ.get(env_name):
        return int(os.environ[env_name])
    return defaults.get(stage, 300)


def running_stale_seconds(stage: str) -> int:
    override = os.environ.get("HOOKY_RUNNING_STALE_SECONDS")
    if override:
        return int(override)
    return stage_max_seconds(stage) + 60


def live_artifact_updated_at(workspace: Path, stage: str, state: dict[str, Any]) -> float | None:
    log_dir = runtime_log_dir(workspace, stage, state)
    candidates: list[float] = []
    events_path = log_dir / "runtime_events.log"
    metadata_path = log_dir / "runtime_metadata.json"
    if events_path.exists():
        timestamp = last_event_timestamp(events_path)
        candidates.append(timestamp if timestamp is not None else events_path.stat().st_mtime)
    if metadata_path.exists():
        timestamp = metadata_timestamp(metadata_path)
        candidates.append(timestamp if timestamp is not None else metadata_path.stat().st_mtime)
    return max(candidates) if candidates else None


def parse_timestamp(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def last_event_timestamp(path: Path) -> float | None:
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        return parse_timestamp(line.split(maxsplit=1)[0])
    return None


def metadata_timestamp(path: Path) -> float | None:
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    for key in ("written_at", "ended_at", "started_at"):
        value = payload.get(key)
        if isinstance(value, str):
            timestamp = parse_timestamp(value)
            if timestamp is not None:
                return timestamp
    return None


def refresh_interrupted_stages(workspace: Path, state: dict[str, Any]) -> bool:
    changed = False
    current_host = socket.gethostname()
    pipeline_payload = state.get("pipeline_status")
    if isinstance(pipeline_payload, dict) and pipeline_payload.get("status") == "running":
        owner_pid = pipeline_payload.get("owner_pid")
        owner_host = pipeline_payload.get("owner_host")
        if isinstance(owner_pid, int) and owner_host == current_host and not process_exists(owner_pid):
            pipeline_payload["status"] = "interrupted"
            pipeline_payload["updated_at"] = utc_now()
            pipeline_payload["error"] = f"pipeline owner process {owner_pid} is no longer running"
            append_pipeline_event(
                workspace,
                "pipeline",
                task=state.get("task_id"),
                status="interrupted",
                error=pipeline_payload["error"],
                trace=pipeline_payload.get("trace"),
                pid=owner_pid,
            )
            changed = True
    for stage, payload in list(state.get("stage_status", {}).items()):
        if not isinstance(payload, dict) or payload.get("status") != "running":
            continue
        owner_pid = payload.get("owner_pid")
        owner_host = payload.get("owner_host")
        if isinstance(owner_pid, int):
            if owner_host != current_host or process_exists(owner_pid):
                continue
            reason = f"run owner process {owner_pid} is no longer running"
        else:
            updated_at = live_artifact_updated_at(workspace, stage, state)
            if updated_at is None:
                continue
            idle_seconds = int(time.time() - updated_at)
            stale_seconds = running_stale_seconds(stage)
            if idle_seconds < stale_seconds:
                continue
            reason = f"run has no owner process and no live output for {idle_seconds}s (stale threshold {stale_seconds}s)"
        payload["status"] = "interrupted"
        payload["updated_at"] = utc_now()
        payload["error"] = reason
        append_pipeline_event(
            workspace,
            "stage",
            task=state.get("task_id"),
            stage=stage,
            status="interrupted",
            error=payload["error"],
            trace=payload.get("trace"),
            pid=owner_pid,
        )
        changed = True
    if changed:
        save_task_state(workspace, state)
    return changed


def pipeline_complete(state: dict[str, Any]) -> bool:
    statuses = state.get("stage_status") if isinstance(state.get("stage_status"), dict) else {}
    return all(isinstance(statuses.get(stage), dict) and statuses[stage].get("status") == "passed" for stage in PIPELINE_STAGES)


def quote_log_value(value: Any) -> str:
    text = str(value)
    if not text:
        return '""'
    if any(char.isspace() for char in text) or '"' in text:
        return '"' + " ".join(text.split()).replace('"', '\\"')[:500] + '"'
    return text


def ensure_initialized(workspace: Path) -> None:
    if not (workflow_dir(workspace) / "agents").exists():
        raise typer.BadParameter("workspace is not initialized. Run `hooky init` first.")


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


def make_task_id(title: str, issue_number: int) -> str:
    return f"issue-{issue_number}-{spec_agent.slugify(title)}"


def resolve_workspace_path(workspace: Path, path: Path) -> Path:
    if path.is_absolute() or path.exists():
        return path
    workspace_path = workspace / path
    return workspace_path if workspace_path.exists() else path


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


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


def git_optional(workspace: Path, args: list[str]) -> str | None:
    result = git_command(workspace, args, check=False)
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def git_status_entries(workspace: Path) -> list[dict[str, str]]:
    result = git_command(
        workspace,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".", ":!.workflow"],
        check=False,
    )
    if result.returncode != 0:
        return []
    records = [record for record in result.stdout.split("\0") if record]
    entries: list[dict[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        status = record[:2]
        path = record[3:]
        if status[:1] in {"R", "C"} and index + 1 < len(records):
            index += 1
            path = records[index]
        entries.append({"status": status.strip() or status, "path": path})
        index += 1
    return entries


def git_patch_for_workspace(workspace: Path, entries: list[dict[str, str]]) -> str:
    parts: list[str] = []
    tracked = git_command(workspace, ["diff", "--binary", "--", ".", ":!.workflow"], check=False)
    if tracked.stdout:
        parts.append(tracked.stdout.rstrip() + "\n")
    for entry in entries:
        if entry["status"] != "??":
            continue
        path = workspace / entry["path"]
        if not path.is_file():
            continue
        diff = git_command(workspace, ["diff", "--no-index", "--binary", "--", "/dev/null", entry["path"]], check=False)
        output = diff.stdout or diff.stderr
        if output:
            parts.append(output.rstrip() + "\n")
    return "\n".join(parts)


def snapshot_project_files(workspace: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    skipped_dirs = {
        ".git",
        ".workflow",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "vendor",
    }
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(workspace)
        except ValueError:
            continue
        if any(part in skipped_dirs for part in relative.parts):
            continue
        snapshot[relative.as_posix()] = path.read_bytes()
    return snapshot


def snapshot_delta_entries(before: dict[str, bytes], after: dict[str, bytes]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            entries.append({"status": "A", "path": path})
        elif path not in after:
            entries.append({"status": "D", "path": path})
        elif before[path] != after[path]:
            entries.append({"status": "M", "path": path})
    return entries


def patch_from_snapshots(before: dict[str, bytes], after: dict[str, bytes], entries: list[dict[str, str]]) -> str:
    patches: list[str] = []
    for entry in entries:
        path = entry["path"]
        before_bytes = before.get(path)
        after_bytes = after.get(path)
        before_text = decode_patch_text(before_bytes)
        after_text = decode_patch_text(after_bytes)
        if before_text is None or after_text is None:
            patches.append(binary_patch_notice(path, entry["status"]))
            continue
        fromfile = "/dev/null" if before_bytes is None else f"a/{path}"
        tofile = "/dev/null" if after_bytes is None else f"b/{path}"
        diff = difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
        body = "".join(diff)
        if body:
            patches.append(f"diff --git a/{path} b/{path}\n{body.rstrip()}\n")
    return "\n".join(patches)


def decode_patch_text(content: bytes | None) -> str | None:
    if content is None:
        return ""
    if b"\0" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def binary_patch_notice(path: str, status: str) -> str:
    return f"diff --git a/{path} b/{path}\nBinary file changed ({status})\n"


def change_proposal_dir(workspace: Path, task_id: str) -> Path:
    return workflow_dir(workspace) / "artifacts/change-proposals" / task_id


def create_change_proposal(
    *,
    workspace: Path,
    state: dict[str, Any],
    builder_contract_path: Path,
    generated_at: str,
    before_snapshot: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    task_id = state["task_id"]
    proposal_dir = change_proposal_dir(workspace, task_id)
    proposal_dir.mkdir(parents=True, exist_ok=True)
    builder_contract = read_json(builder_contract_path)
    git_available = is_git_worktree(workspace)
    git_entries = git_status_entries(workspace) if git_available else []
    if before_snapshot is not None:
        after_snapshot = snapshot_project_files(workspace)
        entries = snapshot_delta_entries(before_snapshot, after_snapshot)
        patch = patch_from_snapshots(before_snapshot, after_snapshot, entries)
    else:
        entries = git_entries
        patch = git_patch_for_workspace(workspace, entries) if git_available else ""
    branch_suggestion = f"sdlc/{task_id}"
    origin = git_optional(workspace, ["remote", "get-url", "origin"]) if git_available else None
    current_branch = git_optional(workspace, ["branch", "--show-current"]) if git_available else None
    changed_files = [entry["path"] for entry in entries]
    proposal = {
        "schema_version": 1,
        "type": "local_git_change_proposal",
        "task_id": task_id,
        "title": state.get("title"),
        "created_at": generated_at,
        "git": {
            "available": git_available,
            "current_branch": current_branch,
            "proposal_branch": branch_suggestion,
            "origin": origin,
            "hosted_pr_url": None,
            "hosted_pr_available": bool(origin),
        },
        "builder": {
            "contract": display_workspace_path(builder_contract_path, workspace),
            "summary": builder_contract.get("summary"),
            "tests_run": builder_contract.get("tests_run", []),
            "tests_passing": builder_contract.get("tests_passing"),
            "failures_remaining": builder_contract.get("failures_remaining", []),
        },
        "changed_files": changed_files,
        "status_entries": entries,
        "git_status_entries": git_entries,
        "artifacts": {
            "summary": "summary.md",
            "patch": "patch.diff",
            "proposal": "proposal.json",
        },
    }
    (proposal_dir / "patch.diff").write_text(patch, encoding="utf-8")
    (proposal_dir / "summary.md").write_text(render_change_proposal_summary(proposal), encoding="utf-8")
    write_json(proposal_dir / "proposal.json", proposal)
    return proposal


def display_workspace_path(path: Path, workspace: Path) -> str:
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return path.as_posix()


def render_change_proposal_summary(proposal: dict[str, Any]) -> str:
    builder = proposal.get("builder", {})
    git = proposal.get("git", {})
    lines = [
        f"# {proposal.get('title') or proposal.get('task_id')}",
        "",
        "## Summary",
        "",
        str(builder.get("summary") or "No builder summary provided."),
        "",
        "## Git Proposal",
        "",
        f"- Type: {proposal.get('type')}",
        f"- Current branch: {git.get('current_branch') or 'unknown'}",
        f"- Suggested branch: {git.get('proposal_branch')}",
        f"- Remote: {git.get('origin') or 'none'}",
        f"- Hosted PR URL: {git.get('hosted_pr_url') or 'not created'}",
        "",
        "## Changed Files",
        "",
    ]
    changed_files = proposal.get("changed_files") or []
    lines.extend(f"- {path}" for path in changed_files)
    if not changed_files:
        lines.append("- None detected")
    lines.extend(
        [
            "",
            "## Tests Run",
            "",
        ]
    )
    tests_run = builder.get("tests_run") or []
    lines.extend(f"- {test}" for test in tests_run)
    if not tests_run:
        lines.append("- None reported")
    failures = builder.get("failures_remaining") or []
    lines.extend(["", "## Remaining Failures", ""])
    lines.extend(f"- {failure}" for failure in failures)
    if not failures:
        lines.append("- None reported")
    return "\n".join(lines) + "\n"


@app.command()
def init(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option(help="Overwrite existing Hooky runtime files.")] = False,
    git: Annotated[bool, typer.Option("--git/--no-git", help="Initialize a local git repository when the workspace is not already a worktree.")] = True,
) -> None:
    """Initialize a workspace with Hooky runtime context."""
    workspace = workspace_from_ctx(ctx)
    workspace.mkdir(parents=True, exist_ok=True)
    if git and not is_git_worktree(workspace):
        git_command(workspace, ["init"], check=True)
    copy_missing(repo_file(".workflow/agents"), workspace / ".workflow/agents", force=force)
    copy_missing(repo_file("AGENTS.md"), workspace / "AGENTS.md", force=force)
    (workspace / ".workflow/tasks").mkdir(parents=True, exist_ok=True)
    if git:
        agent_runtime.ensure_git_baseline(workspace)
    typer.echo(f"initialized: {workspace}")


@task_app.command("create")
def task_create(
    ctx: typer.Context,
    title: Annotated[str, typer.Option(help="Issue/task title.")],
    body_file: Annotated[Path | None, typer.Option(help="Path to a Markdown issue body.")] = None,
    body: Annotated[str | None, typer.Option(help="Issue body text.")] = None,
    issue_number: Annotated[int, typer.Option(help="Issue number for artifact naming.")] = 1001,
) -> None:
    """Create a task and make it current."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    if body_file and body:
        raise typer.BadParameter("use either --body-file or --body, not both")
    if body_file:
        resolved_body_file = resolve_workspace_path(workspace, body_file)
        if is_relative_to(resolved_body_file, workspace):
            raise typer.BadParameter("task --body-file must be outside the workspace; use --body or an external temp file so issue input is not visible to later agents")
        body_text = resolved_body_file.read_text(encoding="utf-8")
    else:
        body_text = body or ""
    if not body_text.strip():
        raise typer.BadParameter("task body is required via --body-file or --body")
    task_id = make_task_id(title, issue_number)
    state = {
        "schema_version": 1,
        "task_id": task_id,
        "title": title,
        "issue_number": issue_number,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "approvals": {},
        "artifacts": {},
        "issue": {"number": issue_number, "title": title, "body": body_text, "user": {"login": "local"}},
    }
    write_json(task_dir(workspace, task_id) / "issue.json", {"label": {"name": "sdlc:spec"}, "issue": state["issue"]})
    save_task_state(workspace, state)
    set_current_task(workspace, task_id)
    typer.echo(f"created current task: {task_id}")
    typer.echo("next: hooky run spec")


@task_app.command("list")
def task_list(ctx: typer.Context) -> None:
    """List known tasks."""
    workspace = workspace_from_ctx(ctx)
    current = read_global_state(workspace).get("current_task")
    for path in sorted(tasks_dir(workspace).glob("*")) if tasks_dir(workspace).exists() else []:
        if (path / "state.json").exists():
            typer.echo(f"{'*' if path.name == current else ' '} {path.name}")


@task_app.command("current")
def task_current(ctx: typer.Context) -> None:
    """Print the current task."""
    typer.echo(resolve_current_task(workspace_from_ctx(ctx)))


@task_app.command("switch")
def task_switch(ctx: typer.Context, task_id: Annotated[str, typer.Argument(help="Task id to make current.")]) -> None:
    """Switch the current task."""
    workspace = workspace_from_ctx(ctx)
    if not task_state_path(workspace, task_id).exists():
        raise typer.BadParameter(f"task does not exist: {task_id}")
    set_current_task(workspace, task_id)
    typer.echo(f"current task: {task_id}")


@run_app.command("spec")
def run_spec(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Run the Spec Agent."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    issue = state["issue"]
    set_stage_status(workspace, state, "spec", "running")
    try:
        with in_workspace(workspace):
            artifact_dir, _contract, usage = spec_agent.generate_spec_artifacts(
                issue_number=int(issue["number"]),
                title=issue["title"],
                body=issue["body"],
                author=issue.get("user", {}).get("login", "local"),
                generated_at=utc_now(),
                artifact_root=Path("docs/specs"),
                sidecar_root=Path(".workflow/artifacts/specs"),
                working_folder=Path("."),
            )
    except Exception as exc:
        set_stage_status(workspace, state, "spec", "failed", error=str(exc))
        raise
    state["artifacts"]["spec"] = {"artifact_dir": artifact_dir.as_posix(), "contract": (artifact_dir / "contract.json").as_posix(), "usage": usage}
    set_stage_status(workspace, state, "spec", "passed", contract=(artifact_dir / "contract.json").as_posix(), cost=usage.get("cost"))
    save_task_state(workspace, state)
    typer.echo(f"spec artifacts: {artifact_dir}")
    typer.echo("next: hooky approve spec")


@approve_app.command("spec")
def approve_spec(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Approve the current Spec Agent output."""
    approve_stage(ctx, "spec", "next: hooky run test", task)


@approve_app.command("test")
def approve_test(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Approve the current Test Agent output."""
    approve_stage(ctx, "test", "next: hooky run builder", task)


def approve_stage(ctx: typer.Context, stage: str, next_message: str, task: str | None) -> None:
    workspace = workspace_from_ctx(ctx)
    state = load_task_state(workspace, task)
    if stage not in state.get("artifacts", {}):
        raise typer.BadParameter(f"cannot approve {stage}: stage has not run")
    state.setdefault("approvals", {})[stage] = {"approved_at": utc_now()}
    save_task_state(workspace, state)
    append_pipeline_event(workspace, "approval", task=state.get("task_id"), stage=stage, status="approved")
    typer.echo(f"approved: {stage}")
    typer.echo(next_message)


@run_app.command("test")
def run_test(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Run the Test Agent."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    require_approval(state, "spec")
    spec_contract = Path(state["artifacts"]["spec"]["contract"])
    set_stage_status(workspace, state, "test", "running")
    try:
        with in_workspace(workspace):
            approved_spec = spec_agent.read_json(spec_contract)
            report_dir, contract, usage = test_agent.generate_test_artifacts(
                approved_spec=approved_spec,
                spec_source=spec_contract.as_posix(),
                generated_at=utc_now(),
                working_folder=Path("."),
                project_root=Path("."),
                report_root=Path(".workflow/artifacts/test-agent"),
            )
    except Exception as exc:
        set_stage_status(workspace, state, "test", "failed", error=str(exc))
        raise
    state["artifacts"]["test"] = {
        "report_dir": report_dir.as_posix(),
        "contract": (report_dir / "contract.json").as_posix(),
        "test_files": [item["path"] for item in contract.get("test_files", [])],
        "fixtures": [item["path"] for item in contract.get("fixtures", [])],
        "usage": usage,
    }
    set_stage_status(workspace, state, "test", "passed", contract=(report_dir / "contract.json").as_posix(), report_dir=report_dir.as_posix(), cost=usage.get("cost"))
    save_task_state(workspace, state)
    typer.echo(f"test report: {report_dir}")
    typer.echo("next: hooky approve test")


@run_app.command("builder")
def run_builder(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Run the Builder Agent."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    require_approval(state, "test")
    set_stage_status(workspace, state, "builder", "running")
    before_builder = snapshot_project_files(workspace)
    try:
        with in_workspace(workspace):
            generated_at = utc_now()
            report_dir, contract, usage = builder_agent.generate_build_artifacts(working_folder=Path("."), generated_at=generated_at)
    except Exception as exc:
        set_stage_status(workspace, state, "builder", "failed", error=str(exc))
        raise
    builder_contract_path = report_dir / "contract.json"
    proposal = create_change_proposal(
        workspace=workspace,
        state=state,
        builder_contract_path=builder_contract_path,
        generated_at=generated_at,
        before_snapshot=before_builder,
    )
    proposal_dir = change_proposal_dir(workspace, state["task_id"])
    state["artifacts"]["builder"] = {
        "report_dir": report_dir.as_posix(),
        "contract": builder_contract_path.as_posix(),
        "proposal_dir": display_workspace_path(proposal_dir, workspace),
        "proposal": display_workspace_path(proposal_dir / "proposal.json", workspace),
        "patch": display_workspace_path(proposal_dir / "patch.diff", workspace),
        "summary": display_workspace_path(proposal_dir / "summary.md", workspace),
        "hosted_pr_url": proposal["git"].get("hosted_pr_url"),
        "usage": usage,
    }
    builder_passed = contract.get("tests_passing") is True
    builder_status = "passed" if builder_passed else "failed"
    failure_summary = "; ".join(str(item) for item in contract.get("failures_remaining", [])[:3])
    set_stage_status(
        workspace,
        state,
        "builder",
        builder_status,
        contract=builder_contract_path.as_posix(),
        proposal=display_workspace_path(proposal_dir / "proposal.json", workspace),
        report_dir=report_dir.as_posix(),
        cost=usage.get("cost"),
        error=None if builder_passed else failure_summary or "Builder reported tests_passing=false",
    )
    if not builder_passed:
        maybe_create_test_remediation_from_builder_failure(workspace, state, contract, builder_contract_path)
    save_task_state(workspace, state)
    typer.echo(f"builder proposal: {proposal_dir}")
    typer.echo(f"builder report: {report_dir}")
    if not builder_passed:
        raise RuntimeError(f"builder reported tests_passing=false: {failure_summary or 'see builder report'}")
    typer.echo("next: hooky run verifier")


@run_app.command("verifier")
def run_verifier(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Run the Verifier Agent."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    require_artifact(state, "builder")
    set_stage_status(workspace, state, "verifier", "running")
    try:
        with in_workspace(workspace):
            report_dir, _contract, usage = verifier_agent.generate_verification_artifacts(working_folder=Path("."), generated_at=utc_now())
    except Exception as exc:
        set_stage_status(workspace, state, "verifier", "failed", error=str(exc))
        raise
    state["artifacts"]["verifier"] = {"report_dir": report_dir.as_posix(), "contract": (report_dir / "contract.json").as_posix(), "usage": usage}
    set_stage_status(workspace, state, "verifier", "passed", contract=(report_dir / "contract.json").as_posix(), report_dir=report_dir.as_posix(), cost=usage.get("cost"))
    save_task_state(workspace, state)
    typer.echo(f"verifier report: {report_dir}")
    typer.echo("next: hooky run eval")


@run_app.command("eval")
def run_eval(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Run the Eval Agent."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    set_stage_status(workspace, state, "eval", "running")
    try:
        with in_workspace(workspace):
            report_dir, contract, usage = eval_agent.generate_eval_artifacts(working_folder=Path("."), generated_at=utc_now())
    except Exception as exc:
        set_stage_status(workspace, state, "eval", "failed", error=str(exc))
        raise
    eval_contract_path = report_dir / "contract.json"
    state["artifacts"]["eval"] = {"report_dir": report_dir.as_posix(), "contract": eval_contract_path.as_posix(), "usage": usage}
    eval_status = str(contract.get("status") or "fail")
    stage_status = "passed" if eval_status == "pass" else eval_status
    set_stage_status(
        workspace,
        state,
        "eval",
        stage_status,
        contract=eval_contract_path.as_posix(),
        report_dir=report_dir.as_posix(),
        cost=usage.get("cost"),
        safe_to_merge=contract.get("safe_to_merge"),
    )
    remediation_plan = maybe_create_remediation_plan(workspace, state, contract, eval_contract_path)
    save_task_state(workspace, state)
    typer.echo(f"eval report: {report_dir}")
    if remediation_plan is not None:
        typer.echo(f"remediation plan: {remediation_plan.relative_to(workspace).as_posix()}")
        typer.echo(f"next: uv run hooky -C {workspace.as_posix()} run remediation --auto-approve")
    else:
        typer.echo("next: hooky report")


def run_stage_sequence(ctx: typer.Context, *, start_stage: str, auto_approve: bool, task: str | None) -> list[str]:
    failures: list[str] = []
    stages = PIPELINE_STAGES[PIPELINE_STAGES.index(start_stage) :]

    if "spec" in stages:
        try:
            run_spec(ctx, task=task)
            if auto_approve:
                approve_stage(ctx, "spec", "running test...", task)
        except Exception as exc:
            failures.append(f"spec failed: {exc}")

    if not failures and "test" in stages:
        try:
            run_test(ctx, task=task)
            if auto_approve:
                approve_stage(ctx, "test", "running builder...", task)
        except Exception as exc:
            failures.append(f"test failed: {exc}")

    if not failures and "builder" in stages:
        try:
            run_builder(ctx, task=task)
        except Exception as exc:
            failures.append(f"builder failed: {exc}")

    if not failures and "verifier" in stages:
        try:
            run_verifier(ctx, task=task)
        except Exception as exc:
            failures.append(f"verifier failed: {exc}")

    if "eval" in stages:
        try:
            run_eval(ctx, task=task)
        except Exception as exc:
            failures.append(f"eval failed: {exc}")

    return failures


@run_app.command("remediation")
def run_remediation(
    ctx: typer.Context,
    auto_approve: Annotated[bool, typer.Option(help="Automatically approve remediated spec/test gates.")] = False,
    task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None,
) -> None:
    """Resume the pipeline from the latest eval root-cause stage."""
    if not auto_approve:
        raise typer.BadParameter("run remediation requires --auto-approve. Use individual `hooky run ...` commands for human-gated flow.")
    workspace = workspace_from_ctx(ctx)
    state = load_task_state(workspace, task)
    plan_path = current_remediation_path(workspace)
    if not plan_path.exists():
        raise typer.BadParameter("no remediation plan exists. Run eval first.")
    plan = read_json(plan_path)
    start_stage = str(plan.get("resume_from_stage") or plan.get("root_cause_stage") or "")
    if start_stage not in PIPELINE_STAGES:
        raise typer.BadParameter(f"remediation plan has invalid resume stage: {start_stage or 'missing'}")
    recover_artifacts_for_resume(workspace, state, start_stage, auto_approve=auto_approve)
    state = load_task_state(workspace, task)
    set_pipeline_status(workspace, state, "running", remediation_plan=plan_path.relative_to(workspace).as_posix(), resume_from=start_stage)
    failures = run_stage_sequence(ctx, start_stage=start_stage, auto_approve=auto_approve, task=task)
    if failures:
        message = "; ".join(failures)
        state = load_task_state(workspace, task)
        set_pipeline_status(workspace, state, "failed", error=message)
        raise RuntimeError(message)
    state = load_task_state(workspace, task)
    if not pipeline_complete(state):
        missing = [
            stage
            for stage in PIPELINE_STAGES
            if state.get("stage_status", {}).get(stage, {}).get("status") != "passed"
        ]
        message = "pipeline cannot pass before stages pass: " + ", ".join(missing)
        set_pipeline_status(workspace, state, "failed", error=message)
        raise RuntimeError(message)
    set_pipeline_status(workspace, state, "passed")


@run_app.command("pipeline")
def run_pipeline(
    ctx: typer.Context,
    auto_approve: Annotated[bool, typer.Option(help="Automatically approve spec and test gates.")] = False,
    max_remediations: Annotated[int, typer.Option(help="Maximum automatic remediation attempts after Eval creates a remediation plan.")] = 2,
    task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None,
) -> None:
    """Run all stages for the current task."""
    if not auto_approve:
        raise typer.BadParameter("run pipeline requires --auto-approve. Use individual `hooky run ...` commands for human-gated flow.")
    workspace = workspace_from_ctx(ctx)
    state = load_task_state(workspace, task)
    set_pipeline_status(workspace, state, "running", max_remediations=max_remediations)
    run_pipeline_with_remediation_loop(ctx, workspace=workspace, task=task, auto_approve=auto_approve, max_remediations=max_remediations)


def run_pipeline_with_remediation_loop(
    ctx: typer.Context,
    *,
    workspace: Path,
    task: str | None,
    auto_approve: bool,
    max_remediations: int,
) -> None:
    attempts = 0
    last_failures: list[str] = []
    failures = run_stage_sequence(ctx, start_stage="spec", auto_approve=auto_approve, task=task)
    last_failures = failures
    while True:
        state = load_task_state(workspace, task)
        if pipeline_complete(state):
            set_pipeline_status(workspace, state, "passed", remediation_attempts=attempts)
            return

        plan_path = current_remediation_path(workspace)
        if failures and not plan_path.exists():
            message = "; ".join(failures)
            set_pipeline_status(workspace, state, "failed", error=message, remediation_attempts=attempts)
            raise RuntimeError(message)
        if not plan_path.exists():
            fail_incomplete_pipeline(workspace, state, attempts, last_failures)

        if attempts >= max_remediations:
            missing = incomplete_pipeline_stages(state)
            message = (
                f"pipeline remediation limit reached ({max_remediations}) before stages pass"
                + (": " + ", ".join(missing) if missing else "")
            )
            set_pipeline_status(workspace, state, "failed", error=message, remediation_attempts=attempts)
            raise RuntimeError(message)

        plan = read_json(plan_path)
        start_stage = str(plan.get("resume_from_stage") or plan.get("root_cause_stage") or "")
        if start_stage not in PIPELINE_STAGES:
            message = f"remediation plan has invalid resume stage: {start_stage or 'missing'}"
            set_pipeline_status(workspace, state, "failed", error=message, remediation_attempts=attempts)
            raise RuntimeError(message)
        attempts += 1
        set_pipeline_status(
            workspace,
            state,
            "running",
            remediation_attempts=attempts,
            remediation_plan=plan_path.relative_to(workspace).as_posix(),
            resume_from=start_stage,
        )
        result = run_remediation_subprocess(workspace=workspace, task=task, auto_approve=auto_approve)
        state = load_task_state(workspace, task)
        if pipeline_complete(state):
            set_pipeline_status(workspace, state, "passed", remediation_attempts=attempts)
            return
        if result.returncode != 0:
            last_failures = [f"remediation attempt {attempts} failed with exit code {result.returncode}"]
        else:
            last_failures = []


def run_remediation_subprocess(*, workspace: Path, task: str | None, auto_approve: bool) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        (SCRIPT_DIR / "hooky_cli.py").as_posix(),
        "-C",
        workspace.as_posix(),
        "run",
        "remediation",
    ]
    if auto_approve:
        command.append("--auto-approve")
    if task:
        command.extend(["--task", task])
    append_pipeline_event(
        workspace,
        "remediation_process",
        status="started",
        command=" ".join(shlex.quote(part) for part in command),
    )
    result = subprocess.run(command, cwd=REPO_ROOT, text=True)
    append_pipeline_event(
        workspace,
        "remediation_process",
        status="exited",
        returncode=result.returncode,
    )
    return result


def incomplete_pipeline_stages(state: dict[str, Any]) -> list[str]:
    return [
        stage
        for stage in PIPELINE_STAGES
        if state.get("stage_status", {}).get(stage, {}).get("status") != "passed"
    ]


def fail_incomplete_pipeline(workspace: Path, state: dict[str, Any], attempts: int, failures: list[str]) -> None:
    missing = incomplete_pipeline_stages(state)
    message = "pipeline cannot pass before stages pass: " + ", ".join(missing)
    if failures:
        message += "; failures: " + "; ".join(failures)
    set_pipeline_status(workspace, state, "failed", error=message, remediation_attempts=attempts)
    raise RuntimeError(message)


def require_approval(state: dict[str, Any], stage: str) -> None:
    if stage not in state.get("approvals", {}):
        raise typer.BadParameter(f"{stage} is not approved. Run `hooky approve {stage}` first.")


def require_artifact(state: dict[str, Any], stage: str) -> None:
    if stage not in state.get("artifacts", {}):
        raise typer.BadParameter(f"{stage} has not run yet.")


def runtime_log_dir(workspace: Path, stage: str, state: dict[str, Any]) -> Path:
    if stage == "pipeline":
        return workflow_dir(workspace)
    if stage == "spec":
        return workspace / ".workflow/artifacts/specs/_runtime"
    artifact = state.get("artifacts", {}).get(stage, {})
    if artifact.get("report_dir"):
        return workspace / artifact["report_dir"]
    defaults = {
        "test": ".workflow/artifacts/test-agent",
        "builder": ".workflow/artifacts/builder-agent",
        "verifier": ".workflow/artifacts/verifier-agent",
        "eval": ".workflow/artifacts/eval-agent",
    }
    if stage in defaults:
        return workspace / defaults[stage]
    raise typer.BadParameter(f"unknown stage: {stage}")


def stage_runtime_events_path(workspace: Path, stage: str, state: dict[str, Any]) -> str:
    return (runtime_log_dir(workspace, stage, state) / "runtime_events.log").relative_to(workspace).as_posix()


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
def status(ctx: typer.Context, task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None) -> None:
    """Show the current task's pipeline state and artifact locations."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    refresh_interrupted_stages(workspace, state)
    typer.echo(f"task: {state['task_id']}")
    typer.echo(f"title: {state['title']}")
    pipeline_state = state.get("pipeline_status") if isinstance(state.get("pipeline_status"), dict) else {}
    pipeline_status = pipeline_state.get("status", "not-run") if isinstance(pipeline_state, dict) else "not-run"
    if pipeline_status == "not-run" and any(state.get("stage_status", {}).get(stage, {}).get("status") for stage in PIPELINE_STAGES):
        pipeline_status = "partial"
    typer.echo(f"pipeline: {pipeline_status}")
    if isinstance(pipeline_state, dict) and pipeline_state.get("error"):
        typer.echo(f"  error: {pipeline_state['error']}")
    if isinstance(pipeline_state, dict) and pipeline_state.get("trace"):
        typer.echo(f"  trace: {pipeline_state['trace']}")
    if isinstance(pipeline_state, dict) and pipeline_state.get("owner_pid"):
        typer.echo(f"  pid: {pipeline_state['owner_pid']}")
    for stage in PIPELINE_STAGES:
        stage_state = state.get("stage_status", {}).get(stage, {})
        artifact = state.get("artifacts", {}).get(stage, {})
        approval = state.get("approvals", {}).get(stage)
        status_text = stage_state.get("status", "not-run")
        suffix = " approved" if approval else ""
        typer.echo(f"{stage}: {status_text}{suffix}")
        if stage_state.get("error"):
            typer.echo(f"  error: {stage_state['error']}")
        trace_path = stage_state.get("trace")
        if not trace_path and status_text in {"running", "interrupted"}:
            trace_path = stage_runtime_events_path(workspace, stage, state)
        if trace_path:
            typer.echo(f"  trace: {trace_path}")
        if stage_state.get("owner_pid"):
            typer.echo(f"  pid: {stage_state['owner_pid']}")
        for key in ["artifact_dir", "report_dir", "contract"]:
            if artifact.get(key):
                typer.echo(f"  {key}: {artifact[key]}")
        if stage == "builder":
            for key in ["proposal_dir", "proposal", "patch", "summary", "hosted_pr_url"]:
                if artifact.get(key):
                    typer.echo(f"  {key}: {artifact[key]}")
        if stage == "test":
            for path in artifact.get("test_files", []):
                typer.echo(f"  test_file: {path}")
            for path in artifact.get("fixtures", []):
                typer.echo(f"  fixture: {path}")


@app.command()
def trace(
    ctx: typer.Context,
    stage: Annotated[str, typer.Argument(help="Stage to inspect: pipeline, spec, test, builder, verifier, or eval.")],
    task: Annotated[str | None, typer.Option(help="Task id. Defaults to current task.")] = None,
    raw_path: Annotated[bool, typer.Option("--path", help="Only print the tool-call summary file path.")] = False,
    tail_path: Annotated[bool, typer.Option("--tail-path", help="Only print the append-only runtime log path.")] = False,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow the append-only runtime log.")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="Regenerate the summary from tool_events.json.")] = False,
) -> None:
    """Show a readable tool-call timeline for an agent stage."""
    workspace = workspace_from_ctx(ctx)
    ensure_initialized(workspace)
    state = load_task_state(workspace, task)
    log_dir = runtime_log_dir(workspace, stage, state)
    summary_path = log_dir / "runtime_timeline.md"
    tail_file = log_dir / "runtime_events.log"
    if raw_path:
        typer.echo(tail_file if stage == "pipeline" else summary_path)
        return
    if tail_path:
        typer.echo(tail_file)
        return
    if follow:
        follow_runtime_log(workspace, stage, state, tail_file)
        return
    if stage == "pipeline":
        if not tail_file.exists():
            raise typer.BadParameter(f"pipeline event log not found: {tail_file}")
        typer.echo(tail_file.read_text(encoding="utf-8").rstrip())
        return
    metadata_path = log_dir / "runtime_metadata.json"
    if metadata_path.exists():
        metadata = read_json(metadata_path)
        typer.echo(f"stage: {stage}")
        typer.echo(f"status: {metadata.get('status', 'unknown')}")
        typer.echo(f"model: {metadata.get('variant_id') or metadata.get('model') or 'unknown'}")
        if metadata.get("error"):
            typer.echo(f"error: {metadata['error']}")
        usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
        events = metadata.get("events") if isinstance(metadata.get("events"), dict) else {}
        typer.echo(f"cost: {usage.get('cost', 0)}")
        typer.echo(f"tool_calls: {events.get('tool_calls', 0)}")
        typer.echo("")
    if summary_path.exists() and not refresh:
        typer.echo(summary_path.read_text(encoding="utf-8").rstrip())
        return
    transcript_path = log_dir / "runtime_transcript.json"
    if transcript_path.exists():
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        if isinstance(transcript, list):
            rendered = agent_runtime.render_runtime_timeline_markdown(transcript)
            summary_path.write_text(rendered, encoding="utf-8")
            typer.echo(rendered.rstrip())
            return
    events_path = log_dir / "tool_events.json"
    if not events_path.exists():
        raise typer.BadParameter(f"tool-call log not found: {events_path}")
    events = json.loads(events_path.read_text(encoding="utf-8"))
    if not isinstance(events, list):
        raise typer.BadParameter(f"tool-call log is not a list: {events_path}")
    rendered = agent_runtime.render_tool_calls_markdown(events)
    summary_path.write_text(rendered, encoding="utf-8")
    typer.echo(rendered.rstrip())


@app.command()
def report(ctx: typer.Context, output: Annotated[Path | None, typer.Option(help="HTML output path.")] = None) -> None:
    """Generate the HTML eval health report for this workspace."""
    workspace = workspace_from_ctx(ctx)
    output_path = output if output and output.is_absolute() else workspace / (output or Path(".workflow/eval-runs/report.html"))
    with in_workspace(workspace):
        reports = generate_eval_report.discover_reports(Path(".workflow/eval-runs"))
        selected_models = generate_eval_report.load_selected_models()
        latest = generate_eval_report.latest_reports_by_stage(reports)
        warnings = generate_eval_report.health_warnings(latest, selected_models, datetime.now(timezone.utc))
        status = generate_eval_report.overall_status(latest, selected_models)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(generate_eval_report.render_html(reports, latest, selected_models, warnings, status), encoding="utf-8")
    typer.echo(f"report: {output_path}")


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check workspace and environment readiness."""
    workspace = workspace_from_ctx(ctx)
    provider = os.environ.get("WEB_SEARCH_PROVIDER")
    checks = {
        "workspace": workspace.exists(),
        "agents": (workspace / ".workflow/agents").exists(),
        "OPENROUTER_API_KEY": bool(os.environ.get("OPENROUTER_API_KEY")),
        "current_task": bool(read_global_state(workspace).get("current_task")),
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
