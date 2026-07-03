"""Workspace/run-key path helpers, JSON I/O, and small CLI-arg utilities."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from hooky import runtime
from hooky import loop_executor

DEFAULT_LAST_RUN_PATH = Path("/tmp/hooky-last-run-path")
DEFAULT_RUN_KEY = "local"
RUN_KEY_ENV = "HOOKY_RUN_KEY"
RUN_DIR_ENV = "HOOKY_RUN_DIR"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


@contextmanager
def temporary_executor(executor: str | None):
    if executor is None:
        yield
        return
    selected = loop_executor.selected_executor(executor)
    previous = os.environ.get(loop_executor.EXECUTOR_ENV)
    os.environ[loop_executor.EXECUTOR_ENV] = selected
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(loop_executor.EXECUTOR_ENV, None)
        else:
            os.environ[loop_executor.EXECUTOR_ENV] = previous


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


def loop_attempt_dir(workspace: Path, attempt_id: str) -> Path:
    return loop_attempts_dir(workspace) / attempt_id


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


def is_git_worktree(workspace: Path) -> bool:
    result = runtime.git_run(workspace, ["rev-parse", "--is-inside-work-tree"], check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def ensure_workspace_ready(workspace: Path, *, git: bool = True) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    if not git:
        return
    was_worktree = is_git_worktree(workspace)
    if not was_worktree:
        runtime.git_run(workspace, ["init"], check=True)
    if not was_worktree or not runtime.git_has_head(workspace):
        runtime.ensure_git_baseline(workspace)
