"""Auto-split from agent_runtime.py — see docs for module boundaries."""

from __future__ import annotations

import subprocess

from pathlib import Path

from hooky.runtime.evidence import relative_to

DISPOSABLE_RUNTIME_DIR_NAMES = {
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    "__pycache__",
    "coverage",
    "test-results",
}


def ensure_git_baseline(workspace: Path, message: str = "Initial workspace baseline") -> None:
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    git_run(workspace, ["init"], check=True)
    ensure_git_identity(workspace)
    git_run(workspace, ["add", "-A"], check=True)
    if git_has_head(workspace):
        if git_staged_changes(workspace):
            git_run(workspace, ["commit", "-m", message], check=True)
        return
    if git_staged_changes(workspace):
        git_run(workspace, ["commit", "-m", message], check=True)
    else:
        git_run(workspace, ["commit", "--allow-empty", "-m", message], check=True)


def ensure_git_identity(workspace: Path) -> None:
    if git_run(workspace, ["config", "user.email"], check=False).returncode != 0:
        git_run(workspace, ["config", "user.email", "hooky@example.local"], check=True)
    if git_run(workspace, ["config", "user.name"], check=False).returncode != 0:
        git_run(workspace, ["config", "user.name", "Hooky"], check=True)


def git_has_head(workspace: Path) -> bool:
    return git_run(workspace, ["rev-parse", "--verify", "HEAD"], check=False).returncode == 0


def git_staged_changes(workspace: Path) -> bool:
    return git_run(workspace, ["diff", "--cached", "--quiet"], check=False).returncode == 1


def git_run(workspace: Path, args: list[str], check: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def snapshot_protected_paths(root: Path, prefixes: list[str]) -> dict[str, bytes | None]:
    root = root.resolve()
    snapshot: dict[str, bytes | None] = {}
    for prefix in prefixes:
        protected = (root / prefix).resolve()
        if protected.is_file():
            snapshot[relative_to(protected, root)] = protected.read_bytes()
        elif protected.is_dir():
            for path in sorted(protected.rglob("*")):
                if path.is_file():
                    snapshot[relative_to(path, root)] = path.read_bytes()
        else:
            snapshot[str(Path(prefix))] = None
    return snapshot


def protected_path_changes(root: Path, prefixes: list[str], before: dict[str, bytes | None]) -> list[str]:
    if not prefixes:
        return []
    root = root.resolve()
    after = snapshot_protected_paths(root, prefixes)
    changes: list[str] = []
    before_keys = set(before)
    after_keys = set(after)
    for path in sorted(after_keys - before_keys):
        if is_disposable_runtime_output(path):
            continue
        if after[path] is not None:
            changes.append(path)
    for path in sorted(before_keys - after_keys):
        if is_disposable_runtime_output(path):
            continue
        if before[path] is not None:
            changes.append(path)
    for path in sorted(before_keys & after_keys):
        if is_disposable_runtime_output(path):
            continue
        if before[path] != after[path]:
            changes.append(path)
    return changes


def restore_protected_paths(root: Path, before: dict[str, bytes | None], changes: list[str]) -> None:
    root = root.resolve()
    for relative in changes:
        path = (root / relative).resolve()
        if path != root and root not in path.parents:
            continue
        content = before.get(relative)
        if content is None:
            if path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def is_disposable_runtime_output(relative: str) -> bool:
    for part in Path(relative).parts:
        if part in DISPOSABLE_RUNTIME_DIR_NAMES:
            return True
        if part.endswith("-report") or part.endswith("-reports"):
            return True
    return False


def matches_any_prefix(parts: tuple[str, ...], prefixes: list[str]) -> bool:
    for prefix in prefixes:
        prefix_parts = Path(prefix).parts
        if parts[: len(prefix_parts)] == prefix_parts:
            return True
    return False

