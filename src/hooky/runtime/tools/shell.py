"""Test/shell/network/todo/final-report tools, mixed into ToolRuntime."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from hooky.runtime.evidence import read_tail, relative_to, visual_snapshot_script, write_tool_result_artifact
from hooky.runtime.models import runtime_path
from hooky.runtime.project_env import (
    append_shell_arg,
    append_test_name_filter,
    default_test_command,
    detect_project_environment,
    latest_error_context_artifacts,
    latest_failed_run_tests_event,
    normalize_subprocess_output,
    parse_test_output,
    render_failure_context_bundle,
)
from hooky.runtime.rendering import utc_timestamp
from hooky.runtime.search import tavily_search
from hooky.runtime.text import single_line


class ShellToolsMixin:
    """Project detection, test running, visual snapshot, network, and final-report handlers."""

    def detect_project_environment(self, _args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, **detect_project_environment(self.working_folder)}

    def run_tests(self, args: dict[str, Any]) -> dict[str, Any]:
        environment = detect_project_environment(self.working_folder)
        command = str(args.get("command") or "").strip()
        if not command:
            command = default_test_command(environment, bool(args.get("list_only")))
        test_file = str(args.get("test_file") or "").strip()
        test_name = str(args.get("test_name") or "").strip()
        list_only = bool(args.get("list_only"))
        if test_file and test_file not in command:
            command = append_shell_arg(command, test_file)
        if test_name:
            command = append_test_name_filter(command, test_name)
        if list_only and " --list" not in command and "playwright test" in command:
            command += " --list"
        if self.bash_command_validator:
            violation = self.bash_command_validator(command)
            if violation:
                return {"ok": False, "error": f"test command blocked by agent policy: {violation}", "command": command}
        timeout_seconds = int(args.get("timeout_seconds") or self.bash_timeout_seconds)
        started = utc_timestamp()
        completed, timed_out, protected_changes = self.run_guarded_shell_command(command, timeout_seconds=timeout_seconds)
        ended = utc_timestamp()
        output = normalize_subprocess_output(completed.stdout) + normalize_subprocess_output(completed.stderr)
        output_path = write_tool_result_artifact(self.working_folder, "test-runs", output)
        result = {
            "ok": completed.returncode == 0 and not timed_out and not protected_changes,
            "command": command,
            "returncode": completed.returncode,
            "timed_out": timed_out,
            "started_at": started,
            "ended_at": ended,
            "output_path": output_path,
            "output_tail": output[-8000:],
            "summary": parse_test_output(output, completed.returncode),
        }
        if protected_changes:
            result["ok"] = False
            result["error"] = "test command modified protected paths; changes were reverted: " + ", ".join(protected_changes[:20])
        return result

    def latest_test_failure_context(self, args: dict[str, Any]) -> dict[str, Any]:
        max_output_bytes = int(args.get("max_output_bytes") or 8000)
        max_artifact_bytes = int(args.get("max_artifact_bytes") or 12000)
        max_artifacts = int(args.get("max_artifacts") or 3)
        event = latest_failed_run_tests_event(self.tool_events)
        if event is None:
            return {"ok": False, "error": "no failed run_tests event is available"}
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        output_path = str(result.get("output_path") or "")
        output = ""
        if output_path:
            try:
                resolved_output = self.resolve_path(output_path)
                self.validate_read_path(resolved_output)
                output = read_tail(resolved_output, max_output_bytes)
            except Exception:
                output = str(result.get("output_tail") or "")[-max_output_bytes:]
        else:
            output = str(result.get("output_tail") or "")[-max_output_bytes:]
        artifacts = latest_error_context_artifacts(self.working_folder, max_artifacts, max_artifact_bytes)
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        failed_tests = summary.get("failed_tests") if isinstance(summary.get("failed_tests"), list) else []
        bundle = render_failure_context_bundle(
            command=str(result.get("command") or ""),
            returncode=result.get("returncode"),
            timed_out=bool(result.get("timed_out")),
            output_path=output_path,
            failed_tests=[str(item) for item in failed_tests],
            output_tail=output,
            artifacts=artifacts,
        )
        context_path = write_tool_result_artifact(self.working_folder, "failure-context", bundle)
        return {
            "ok": True,
            "context_path": context_path,
            "command": result.get("command"),
            "returncode": result.get("returncode"),
            "timed_out": result.get("timed_out"),
            "output_path": output_path,
            "failed_tests": failed_tests[:10],
            "artifacts": [{"path": item["path"], "bytes": item["bytes"], "truncated": item["truncated"]} for item in artifacts],
            "content": bundle[: max_output_bytes + max_artifact_bytes],
            "truncated": len(bundle.encode("utf-8")) > max_output_bytes + max_artifact_bytes,
        }

    def capture_visual_snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        if not url:
            raise ValueError("capture_visual_snapshot requires a URL; start a dev server first when needed")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("capture_visual_snapshot URL must be http or https")
        viewport_width = int(args.get("viewport_width") or 1280)
        viewport_height = int(args.get("viewport_height") or 900)
        wait_selector = str(args.get("wait_selector") or "body")
        full_page = bool(args.get("full_page", True))
        timeout_seconds = int(args.get("timeout_seconds") or 30)
        output_dir = runtime_path(self.working_folder, "tool-results", "visual-snapshots")
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ%f")[:22]
        screenshot_path = output_dir / f"{stamp}.png"
        script_path = output_dir / f"{stamp}.cjs"
        script_path.write_text(visual_snapshot_script(), encoding="utf-8")
        completed = subprocess.run(
            [
                "node",
                script_path.as_posix(),
                url,
                screenshot_path.as_posix(),
                str(viewport_width),
                str(viewport_height),
                wait_selector,
                "1" if full_page else "0",
            ],
            cwd=self.working_folder,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        if completed.returncode != 0:
            return {
                "ok": False,
                "url": url,
                "returncode": completed.returncode,
                "error": single_line(stderr or stdout or "visual snapshot command failed", 1000),
                "screenshot_path": relative_to(screenshot_path, self.working_folder) if screenshot_path.exists() else "",
            }
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return {
                "ok": False,
                "url": url,
                "returncode": completed.returncode,
                "error": f"visual snapshot returned invalid JSON: {exc}",
                "stdout": single_line(stdout, 1000),
                "stderr": single_line(stderr, 1000),
            }
        payload["ok"] = True
        payload["screenshot_path"] = relative_to(screenshot_path, self.working_folder)
        payload["script_path"] = relative_to(script_path, self.working_folder)
        self.queue_image_input(screenshot_path, f"Visual snapshot for {url} at {viewport_width}x{viewport_height}")
        if stderr:
            payload["stderr"] = single_line(stderr, 1000)
        return payload

    def bash(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args["command"])
        violation = self.command_policy_violation(command, label="bash command")
        if violation:
            return {"ok": False, "error": violation}
        completed, timed_out, protected_changes = self.run_guarded_shell_command(command, timeout_seconds=self.bash_timeout_seconds)
        if protected_changes:
            return {
                "ok": False,
                "returncode": completed.returncode,
                "stdout": completed.stdout[-8000:],
                "stderr": completed.stderr[-8000:],
                "error": "bash command modified protected paths; changes were reverted: " + ", ".join(protected_changes[:20]),
            }
        result = {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-8000:],
            "stderr": completed.stderr[-8000:],
        }
        if timed_out:
            result["error"] = f"Command '{command}' timed out after {self.bash_timeout_seconds} seconds; process group was terminated"
        return result

    def fetch_url(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args["url"])
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("fetch_url requires an http or https URL")
        request = urllib.request.Request(url, headers={"User-Agent": "hooky-agent-runtime/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(1_000_001)
                truncated = len(raw) > 1_000_000
                content = raw[:1_000_000].decode("utf-8", errors="replace")
                return {
                    "ok": True,
                    "url": url,
                    "status": response.status,
                    "content_type": response.headers.get("content-type", ""),
                    "content": content,
                    "truncated": truncated,
                }
        except urllib.error.HTTPError as exc:
            return {"ok": False, "url": url, "status": exc.code, "error": exc.reason}
        except urllib.error.URLError as exc:
            return {"ok": False, "url": url, "error": str(exc.reason)}

    def web_search(self, args: dict[str, Any]) -> dict[str, Any]:
        provider = os.environ.get("WEB_SEARCH_PROVIDER", "").strip().lower()
        if not provider:
            return {"ok": False, "error": "WEB_SEARCH_PROVIDER is not configured"}
        if provider != "tavily":
            return {"ok": False, "error": f"unsupported WEB_SEARCH_PROVIDER: {provider}"}
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            return {"ok": False, "provider": "tavily", "error": "TAVILY_API_KEY is not configured"}
        return tavily_search(
            api_key=api_key,
            query=str(args["query"]),
            max_results=int(args.get("max_results") or 5),
            include_domains=list(args.get("include_domains") or []),
            exclude_domains=list(args.get("exclude_domains") or []),
        )

    def todo_read(self, _args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "items": self.todo_items}

    def todo_write(self, args: dict[str, Any]) -> dict[str, Any]:
        self.todo_items = list(args.get("items") or [])
        return {"ok": True, "items": self.todo_items}

    def finish(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.final_validator:
            self.final_validator(args)
        self.final_report = args
        return {"ok": True, "final_report_received": True}
