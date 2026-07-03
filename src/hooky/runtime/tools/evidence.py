"""Skill activation, time-budget, and evidence-capture tools, mixed into ToolRuntime."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from hooky.runtime.evidence import (
    append_evidence_command_file,
    append_evidence_note_file,
    append_evidence_screenshot_file,
    read_text_prefix,
    relative_to,
    write_evidence_command_output,
)
from hooky.runtime.git import protected_path_changes, restore_protected_paths, snapshot_protected_paths
from hooky.runtime.models import runtime_path
from hooky.runtime.process import long_running_bash_violation, run_shell_command
from hooky.runtime.project_env import normalize_subprocess_output
from hooky.runtime.rendering import utc_timestamp
from hooky.shared import agent_skills


class EvidenceToolsMixin:
    """Skill activation, time-extension, and evidence-note/command/screenshot tool handlers."""

    def skill_by_name(self, name: str) -> agent_skills.AgentSkill:
        for skill in self.skills:
            if skill.name == name:
                return skill
        raise ValueError(f"unknown skill: {name}")

    def activate_skill(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        if not name:
            raise ValueError("skill name is required")
        skill = self.skill_by_name(name)
        already_active = skill.name in self.activated_skill_names
        self.activated_skill_names.add(skill.name)
        resources = agent_skills.skill_resources(skill)
        return {
            "ok": True,
            "name": skill.name,
            "description": skill.description,
            "skill_path": skill.path.as_posix(),
            "body": skill.body,
            "resources": resources,
            "already_active": already_active,
        }

    def read_skill_resource(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        resource_path = str(args.get("path") or "").strip()
        if not name or not resource_path:
            raise ValueError("name and path are required")
        if name not in self.activated_skill_names:
            raise ValueError(f"activate skill before reading resources: {name}")
        skill = self.skill_by_name(name)
        path = agent_skills.resolve_skill_resource(skill, resource_path)
        max_bytes = int(args.get("max_bytes") or 20000)
        content = read_text_prefix(path, max_bytes)
        return {
            "ok": True,
            "name": skill.name,
            "path": resource_path,
            "bytes": path.stat().st_size,
            "content": content,
            "truncated": path.stat().st_size > len(content.encode("utf-8")),
        }

    def request_time_extension(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.max_extension_seconds <= 0 or self.max_extension_requests <= 0:
            return {"ok": False, "granted": False, "error": "time extensions are disabled for this agent"}
        if self.extension_requests_used >= self.max_extension_requests:
            return {"ok": False, "granted": False, "error": "time extension request limit reached"}
        if self.extension_seconds_used >= self.max_extension_seconds:
            return {"ok": False, "granted": False, "error": "time extension budget exhausted"}
        reason = str(args.get("reason") or "").strip()
        current_status = str(args.get("current_status") or "").strip()
        next_step = str(args.get("next_step") or "").strip()
        if not reason or not current_status or not next_step:
            return {"ok": False, "granted": False, "error": "reason, current_status, and next_step are required"}
        elapsed = time.monotonic() - self.started_at
        if elapsed < self.max_seconds * 0.75:
            return {"ok": False, "granted": False, "error": "extension requests are only available near the runtime deadline"}
        if not self.recent_progress_evidence():
            return {"ok": False, "granted": False, "error": "no recent progress evidence from tool results"}
        requested = int(args.get("requested_seconds") or 180)
        remaining_budget = self.max_extension_seconds - self.extension_seconds_used
        granted = max(0, min(requested, remaining_budget))
        if granted <= 0:
            return {"ok": False, "granted": False, "error": "time extension budget exhausted"}
        self.max_seconds += granted
        self.extension_seconds_used += granted
        self.extension_requests_used += 1
        return {
            "ok": True,
            "granted": True,
            "added_seconds": granted,
            "max_seconds": self.max_seconds,
            "extension_requests_used": self.extension_requests_used,
            "extension_seconds_used": self.extension_seconds_used,
        }

    def recent_progress_evidence(self) -> bool:
        recent_events = self.tool_events[-12:]
        saw_write = False
        saw_test = False
        saw_successful_tool = False
        for event in recent_events:
            result = event.get("result") or {}
            if result.get("ok"):
                saw_successful_tool = True
            if event.get("name") in {"write_files", "edit_files"} and result.get("ok"):
                saw_write = True
            if event.get("name") == "run_tests":
                saw_test = True
        return saw_successful_tool and (saw_write or saw_test)

    def grant_post_success_grace(self, result: dict[str, Any]) -> dict[str, Any] | None:
        if self.max_post_success_grace_seconds <= 0:
            return None
        if not result.get("ok") or result.get("passed") is not True:
            return None
        remaining_budget = self.max_post_success_grace_seconds - self.post_success_grace_seconds_used
        if remaining_budget <= 0:
            return None
        elapsed = time.monotonic() - self.started_at
        target_deadline = elapsed + remaining_budget
        if target_deadline <= self.max_seconds:
            return None
        added_seconds = int(target_deadline - self.max_seconds)
        if added_seconds <= 0:
            return None
        self.max_seconds += added_seconds
        self.post_success_grace_seconds_used += added_seconds
        return {
            "added_seconds": added_seconds,
            "max_seconds": self.max_seconds,
            "post_success_grace_seconds_used": self.post_success_grace_seconds_used,
        }

    def evidence_base_dir(self) -> Path:
        if self.live_log_root and self.live_log_root.name == "traces":
            return self.live_log_root.resolve().parent
        return runtime_path(self.working_folder)

    def evidence_report_path(self) -> Path:
        return self.evidence_base_dir() / "evidence.md"

    def append_evidence_note(self, args: dict[str, Any]) -> dict[str, Any]:
        title = str(args.get("title") or "Evidence note").strip() or "Evidence note"
        body = str(args.get("body") or "").strip()
        path = append_evidence_note_file(
            self.working_folder,
            self.evidence_base_dir(),
            title=title,
            body=body,
        )
        return {"ok": True, "evidence_path": relative_to(path, self.working_folder)}

    def command_policy_violation(self, command: str, *, label: str = "command") -> str | None:
        long_running_violation = long_running_bash_violation(command)
        if long_running_violation:
            return long_running_violation
        lowered = command.lower()
        if ".hooky" in lowered and self.read_blocked_prefixes:
            return f"{label} references a path that is not available to this agent"
        for blocked in self.bash_blocked_substrings:
            if blocked.lower() in lowered:
                return f"{label} blocked by agent policy: {blocked}"
        if self.bash_command_validator:
            violation = self.bash_command_validator(command)
            if violation:
                return f"{label} blocked by agent policy: {violation}"
        return None

    def run_guarded_shell_command(self, command: str, *, timeout_seconds: int) -> tuple[subprocess.CompletedProcess[str], bool, list[str]]:
        before = snapshot_protected_paths(self.working_folder, self.bash_protected_prefixes)
        completed, timed_out = run_shell_command(command, cwd=self.working_folder, timeout_seconds=timeout_seconds)
        protected_changes = protected_path_changes(self.working_folder, self.bash_protected_prefixes, before)
        if protected_changes:
            restore_protected_paths(self.working_folder, before, protected_changes)
        return completed, timed_out, protected_changes

    def append_evidence_command(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ValueError("command is required")
        violation = self.command_policy_violation(command)
        if violation:
            return {"ok": False, "captured": False, "error": violation, "command": command}
        timeout_seconds = int(args.get("timeout_seconds") or self.bash_timeout_seconds)
        title = str(args.get("title") or "Command evidence").strip() or "Command evidence"
        started = utc_timestamp()
        completed, timed_out, protected_changes = self.run_guarded_shell_command(command, timeout_seconds=timeout_seconds)
        ended = utc_timestamp()
        output = normalize_subprocess_output(completed.stdout) + normalize_subprocess_output(completed.stderr)
        command_output_path = write_evidence_command_output(
            self.working_folder,
            self.evidence_base_dir(),
            command=command,
            output=output,
        )
        evidence_path = append_evidence_command_file(
            self.working_folder,
            self.evidence_base_dir(),
            title=title,
            command=command,
            returncode=completed.returncode,
            timed_out=timed_out,
            started_at=started,
            ended_at=ended,
            output_path=command_output_path,
            output_tail=output[-4000:],
            protected_changes=protected_changes,
        )
        return {
            "ok": completed.returncode == 0 and not timed_out and not protected_changes,
            "captured": True,
            "command": command,
            "returncode": completed.returncode,
            "timed_out": timed_out,
            "started_at": started,
            "ended_at": ended,
            "evidence_path": relative_to(evidence_path, self.working_folder),
            "output_path": relative_to(command_output_path, self.working_folder),
            "output_tail": output[-4000:],
            **({"error": "command modified protected paths; changes were reverted: " + ", ".join(protected_changes[:20])} if protected_changes else {}),
        }

    def append_evidence_screenshot(self, args: dict[str, Any]) -> dict[str, Any]:
        title = str(args.get("title") or "Visual evidence").strip() or "Visual evidence"
        snapshot = self.capture_visual_snapshot(args)
        evidence_path = append_evidence_screenshot_file(
            self.working_folder,
            self.evidence_base_dir(),
            title=title,
            url=str(args.get("url") or ""),
            snapshot=snapshot,
        )
        return {
            **snapshot,
            "evidence_path": relative_to(evidence_path, self.working_folder),
        }

    def queue_image_input(self, path: Path, label: str) -> None:
        resolved = path if path.is_absolute() else self.working_folder / path
        if not resolved.exists() or not resolved.is_file():
            return
        self.pending_image_inputs.append(
            {
                "path": relative_to(resolved, self.working_folder),
                "label": label,
            }
        )
