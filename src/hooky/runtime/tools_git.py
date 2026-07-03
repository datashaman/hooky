"""Git-inspection tools (status/diff/show), mixed into ToolRuntime."""

from __future__ import annotations

import subprocess

from typing import Any

from hooky.runtime.text import sanitize_output_for_read_policy


class GitToolsMixin:
    """Read-only git tool handlers."""

    def git_status(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self.run_git(["status", "--short"])

    def git_diff(self, args: dict[str, Any]) -> dict[str, Any]:
        command = ["diff"]
        if bool(args.get("staged")):
            command.append("--cached")
        path = str(args.get("path") or "").strip()
        if path:
            resolved = self.resolve_path(path)
            self.validate_read_path(resolved)
            command.extend(["--", path])
        return self.run_git(command, max_bytes=int(args.get("max_bytes") or 20000))

    def git_show(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = str(args.get("ref") or "HEAD")
        path = str(args.get("path") or "").strip()
        if path:
            resolved = self.resolve_path(path)
            self.validate_read_path(resolved)
            spec = f"{ref}:{path}"
        else:
            spec = ref
        return self.run_git(["show", "--no-ext-diff", spec], max_bytes=int(args.get("max_bytes") or 20000))

    def run_git(self, command: list[str], max_bytes: int = 20000) -> dict[str, Any]:
        completed = subprocess.run(
            ["git", "--no-pager", *command],
            cwd=self.working_folder,
            text=True,
            capture_output=True,
            timeout=30,
        )
        stdout = sanitize_output_for_read_policy(completed.stdout[-max_bytes:], self.read_blocked_prefixes)
        stderr = sanitize_output_for_read_policy(completed.stderr[-max_bytes:], self.read_blocked_prefixes)
        return {"ok": completed.returncode == 0, "returncode": completed.returncode, "stdout": stdout, "stderr": stderr}

