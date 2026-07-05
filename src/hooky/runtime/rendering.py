"""Rendering, formatting, and runtime-log helpers for tool output."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hooky.runtime.text import assistant_message_text, compact_json, quote_value, reasoning_trace_text, single_line

if TYPE_CHECKING:
    from hooky.runtime.agent_loop import AgentRunResult
    from hooky.runtime.tool_runtime import ToolRuntime


def write_runtime_artifact_files(
    root: Path,
    transcript: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    compaction_events: list[dict[str, Any]],
    pre_compaction_archives: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "runtime_transcript.json").write_text(json.dumps(transcript, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "tool_events.json").write_text(json.dumps(tool_events, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "tool_calls.md").write_text(render_tool_calls_markdown(tool_events), encoding="utf-8")
    (root / "runtime_timeline.md").write_text(render_runtime_timeline_markdown(transcript), encoding="utf-8")
    (root / "runtime_events.snapshot.log").write_text(render_runtime_events_log(transcript), encoding="utf-8")
    (root / "compaction_events.json").write_text(json.dumps(compaction_events, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "pre_compaction_archives.json").write_text(json.dumps(pre_compaction_archives, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "runtime_metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_runtime_log(
    report_root: Path,
    transcript: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    compaction_events: list[dict[str, Any]],
    pre_compaction_archives: list[dict[str, Any]],
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    resolved_metadata = metadata or {"schema_version": 1, "written_at": utc_timestamp()}
    write_runtime_artifact_files(report_root, transcript, tool_events, compaction_events, pre_compaction_archives, resolved_metadata)
    write_runtime_invocation_archive(
        report_root,
        transcript,
        tool_events,
        compaction_events,
        pre_compaction_archives,
        resolved_metadata,
    )


def write_runtime_invocation_archive(
    report_root: Path,
    transcript: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    compaction_events: list[dict[str, Any]],
    pre_compaction_archives: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> None:
    agent_name = safe_path_segment(str(metadata.get("agent_name") or "agent"))
    timestamp = safe_path_segment(str(metadata.get("started_at") or metadata.get("written_at") or utc_timestamp()))
    archive_root = report_root / "invocations" / f"{timestamp}-{agent_name}"
    write_runtime_artifact_files(archive_root, transcript, tool_events, compaction_events, pre_compaction_archives, metadata)


def safe_path_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-") or "unknown"


def append_live_event(runtime: ToolRuntime, line: str) -> None:
    targets: list[tuple[Path, str]] = []
    if runtime.live_log_root is not None:
        targets.append((runtime.live_log_root / "runtime_events.log", runtime.live_event_prefix))
    for path in runtime.live_event_log_paths:
        targets.append((path, runtime.live_event_prefix))
    for path, prefix in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(prefix_event_line(line.rstrip(), prefix) + "\n")


def prefix_event_line(line: str, prefix: str) -> str:
    if not prefix:
        return line
    timestamp, sep, rest = line.partition(" ")
    if not sep:
        return prefix + line
    return f"{timestamp} {prefix}{rest}"


def render_runtime_events_log(transcript: list[dict[str, Any]]) -> str:
    lines = [format_runtime_event_line(item) for item in transcript if item.get("role") in {"system", "user", "assistant", "tool", "runtime_notice", "compaction", "pre_compaction"}]
    return "\n".join(line for line in lines if line) + ("\n" if lines else "")


def format_runtime_event_line(item: dict[str, Any]) -> str:
    role = str(item.get("role") or "event")
    timestamp = str(item.get("ended_at") or item.get("started_at") or utc_timestamp())
    duration = format_duration(item.get("duration_ms"))
    if role == "assistant":
        message = item.get("message") if isinstance(item.get("message"), dict) else {}
        tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        names = [
            display_tool_call_name(str(call.get("function", {}).get("name")))
            for call in tool_calls
            if isinstance(call, dict) and isinstance(call.get("function"), dict) and call.get("function", {}).get("name")
        ]
        assistant_text = assistant_message_text(message)
        reasoning_text = reasoning_trace_text(item.get("reasoning") if isinstance(item.get("reasoning"), dict) else None)
        return (
            f"{timestamp} assistant duration={duration} cost=${float(usage.get('cost') or 0):.8f} "
            f"tokens={int(usage.get('total_tokens') or 0)} tool_calls={len(tool_calls)}"
            + (f" tools={','.join(names)}" if names else "")
            + (f" message={quote_value(single_line(assistant_text, 320), 320)}" if assistant_text else "")
            + (f" reasoning={quote_value(single_line(reasoning_text, 500), 500)}" if reasoning_text else "")
        )
    if role == "tool":
        name = str(item.get("name") or "unknown")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        status = "ok" if result.get("ok") is True else "error" if result.get("ok") is False else "unknown"
        return f"{timestamp} tool name={name} status={status} duration={duration} {tail_detail(name, arguments, result)}".rstrip()
    if role in {"system", "user", "runtime_notice"}:
        kind = str(item.get("kind") or "message")
        message = single_line(str(item.get("message") or ""), 180)
        return f"{timestamp} {role} kind={kind} message={quote_value(message, 180)}"
    return f"{timestamp} {role}"


def tail_detail(name: str, arguments: dict[str, Any], result: dict[str, Any]) -> str:
    if result.get("error"):
        return "error=" + quote_value(str(result["error"]), 220)
    if name in {"read_files", "write_files", "edit_files"}:
        files = result.get("files") if isinstance(result.get("files"), list) else []
        total_bytes = sum(int(item.get("bytes") or len(str(item.get("content") or "").encode("utf-8"))) for item in files if isinstance(item, dict))
        paths = ",".join(str(item.get("path") or "") for item in files[:5] if isinstance(item, dict))
        size_key = "bytes_read" if name == "read_files" else "bytes_written"
        edits = sum(int(item.get("edits") or 0) for item in files if isinstance(item, dict))
        edit_detail = f" edits={edits}" if name == "edit_files" else ""
        return f"files={len(files)} {size_key}={total_bytes}{edit_detail}" + (f" paths={quote_value(paths, 180)}" if paths else "")
    if name == "bash":
        stdout = single_line(str(result.get("stdout") or ""), 180)
        stderr = single_line(str(result.get("stderr") or ""), 180)
        detail = f"command={quote_value(str(arguments.get('command') or ''), 180)} returncode={result.get('returncode')}"
        if stdout:
            detail += f" stdout={quote_value(stdout, 180)}"
        if stderr:
            detail += f" stderr={quote_value(stderr, 180)}"
        return detail
    if name == "run_tests":
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        detail = (
            f"command={quote_value(str(result.get('command') or ''), 180)} "
            f"returncode={result.get('returncode')} passed={summary.get('passed')} "
            f"failed={summary.get('failed')} output_path={quote_value(str(result.get('output_path') or ''), 180)}"
        )
        failed_tests = summary.get("failed_tests") if isinstance(summary.get("failed_tests"), list) else []
        if failed_tests:
            detail += f" failing={quote_value(single_line('; '.join(str(item) for item in failed_tests[:3]), 180), 180)}"
        return detail
    if name == "latest_test_failure_context":
        failed_tests = result.get("failed_tests") if isinstance(result.get("failed_tests"), list) else []
        detail = f"context_path={quote_value(str(result.get('context_path') or ''), 180)} failed_tests={len(failed_tests)} artifacts={len(result.get('artifacts') or [])}"
        if failed_tests:
            detail += f" failing={quote_value(single_line('; '.join(str(item) for item in failed_tests[:2]), 180), 180)}"
        return detail
    if name == "capture_visual_snapshot":
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        detail = (
            f"url={quote_value(str(result.get('url') or arguments.get('url') or ''), 180)} "
            f"screenshot_path={quote_value(str(result.get('screenshot_path') or ''), 180)} "
            f"coverage={metrics.get('viewportCoverage')} top_gap={metrics.get('topGapRatio')} "
            f"elements={metrics.get('visibleElementCount')}"
        )
        if metrics.get("headingInteractiveOverlapCount"):
            detail += f" heading_control_overlaps={metrics.get('headingInteractiveOverlapCount')}"
        console_messages = result.get("consoleMessages") if isinstance(result.get("consoleMessages"), list) else []
        if console_messages:
            detail += f" console_messages={len(console_messages)}"
        return detail
    if name in {"append_evidence_note", "append_evidence_command", "append_evidence_screenshot"}:
        detail = f"evidence_path={quote_value(str(result.get('evidence_path') or ''), 180)}"
        if result.get("command"):
            detail += f" command={quote_value(str(result.get('command') or ''), 180)} returncode={result.get('returncode')}"
        if result.get("screenshot_path"):
            detail += f" screenshot_path={quote_value(str(result.get('screenshot_path') or ''), 180)}"
        if result.get("output_path"):
            detail += f" output_path={quote_value(str(result.get('output_path') or ''), 180)}"
        return detail
    if name == "detect_project_environment":
        return f"package_manager={quote_value(str(result.get('package_manager') or ''), 80)} scripts={len(result.get('scripts') or {})} test_commands={len(result.get('test_commands') or [])}"
    if name in {"git_status", "git_diff", "git_show"}:
        stdout = single_line(str(result.get("stdout") or ""), 180)
        detail = f"returncode={result.get('returncode')}"
        if stdout:
            detail += f" stdout={quote_value(stdout, 180)}"
        return detail
    if name == "start_process":
        ports = ",".join(str(port) for port in result.get("ports") or [])
        requested_ports = ",".join(str(port) for port in result.get("requested_ports") or [])
        detail = (
            f"process_id={quote_value(str(result.get('process_id') or ''), 80)} pid={result.get('pid')} ready={result.get('ready')} command={quote_value(str(arguments.get('command') or ''), 180)}"
        )
        if requested_ports:
            detail += f" requested_ports={quote_value(requested_ports, 80)}"
        if result.get("allocated_port"):
            detail += f" allocated_port={result.get('allocated_port')}"
        if ports:
            detail += f" ports={quote_value(ports, 80)}"
        if result.get("url"):
            detail += f" url={quote_value(str(result.get('url')), 120)}"
        output = single_line(str(result.get("output") or ""), 180)
        if output:
            detail += f" output={quote_value(output, 180)}"
        return detail
    if name == "read_process":
        output = single_line(str(result.get("output") or ""), 180)
        detail = f"process_id={quote_value(str(arguments.get('process_id') or ''), 80)} running={result.get('running')} returncode={result.get('returncode')}"
        if output:
            detail += f" output={quote_value(output, 180)}"
        return detail
    if name == "stop_process":
        return f"process_id={quote_value(str(arguments.get('process_id') or ''), 80)} stopped={result.get('stopped')} returncode={result.get('returncode')}"
    if name == "list_processes":
        processes = result.get("processes") if isinstance(result.get("processes"), list) else []
        running = sum(1 for item in processes if isinstance(item, dict) and item.get("running") is True)
        ports = sorted({str(port) for item in processes if isinstance(item, dict) for port in item.get("ports") or []})
        detail = f"processes={len(processes)} running={running}"
        if ports:
            detail += f" ports={quote_value(','.join(ports), 120)}"
        return detail
    if name == "activate_skill":
        resources = result.get("resources") if isinstance(result.get("resources"), list) else []
        return f"name={quote_value(str(result.get('name') or arguments.get('name') or ''), 120)} resources={len(resources)} already_active={result.get('already_active')}"
    if name == "read_skill_resource":
        return f"name={quote_value(str(result.get('name') or arguments.get('name') or ''), 120)} path={quote_value(str(result.get('path') or arguments.get('path') or ''), 180)} bytes={result.get('bytes')}"
    if name in {"list_files", "find_files"}:
        entries = result.get("entries") if name == "list_files" else result.get("matches")
        count = len(entries) if isinstance(entries, list) else 0
        path = arguments.get("path")
        pattern = arguments.get("pattern")
        parts = [f"count={count}"]
        if path:
            parts.append(f"path={quote_value(str(path), 120)}")
        if pattern:
            parts.append(f"pattern={quote_value(str(pattern), 120)}")
        return " ".join(parts)
    if name == "search_files":
        matches = result.get("matches") if isinstance(result.get("matches"), list) else []
        return f"matches={len(matches)} pattern={quote_value(str(arguments.get('pattern') or ''), 120)}"
    if name in {"todo_read", "todo_write"}:
        items = result.get("items") if isinstance(result.get("items"), list) else []
        detail = f"items={len(items)}"
        if name == "todo_write":
            active = active_todo_label(items)
            if active:
                detail += f" active={quote_value(active, 180)}"
        return detail
    if name == "web_search":
        results = result.get("results") if isinstance(result.get("results"), list) else []
        return f"results={len(results)} query={quote_value(str(arguments.get('query') or ''), 160)}"
    if name == "fetch_url":
        return f"url={quote_value(str(result.get('url') or arguments.get('url') or ''), 180)} status={result.get('status')}"
    if name == "final_report":
        return "submitted=true"
    return ""


def format_duration(value: Any) -> str:
    if value is None:
        return "unknown"
    duration_ms = float(value)
    if duration_ms >= 1000:
        return f"{duration_ms / 1000:.2f}s"
    return f"{duration_ms:.2f}ms"


def canonical_tool_name(name: str, valid_names: Any) -> str:
    valid = set(str(item) for item in valid_names)
    if name in valid:
        return name
    for separator in ("<|channel|>", "."):
        if separator in name:
            candidate = name.split(separator, 1)[0]
            if candidate in valid:
                return candidate
    return name


def display_tool_call_name(name: str) -> str:
    for separator in ("<|channel|>", "."):
        if separator in name:
            return name.split(separator, 1)[0]
    return name


def active_todo_label(items: list[Any]) -> str | None:
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or item.get("state") or "").lower()
        if status in {"active", "in_progress", "in-progress", "doing"}:
            return todo_label(item)
    for item in items:
        if isinstance(item, dict) and item.get("completed") is False:
            return todo_label(item)
    return None


def todo_label(item: dict[str, Any]) -> str:
    value = item.get("text") or item.get("description") or item.get("content") or item.get("task") or item.get("title")
    if value:
        return str(value)
    return compact_json(item, 180)


def render_runtime_timeline_markdown(transcript: list[dict[str, Any]]) -> str:
    lines = ["# Runtime Timeline", ""]
    items = [item for item in transcript if item.get("role") in {"system", "user", "assistant", "tool", "runtime_notice", "compaction", "pre_compaction"}]
    if not items:
        lines.append("No runtime events recorded.")
        lines.append("")
        return "\n".join(lines)
    for index, item in enumerate(items, 1):
        role = item.get("role")
        if role == "assistant":
            message = item.get("message") if isinstance(item.get("message"), dict) else {}
            tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
            usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
            lines.append(f"## {index}. `assistant`")
            timing = summarize_tool_timing(item)
            details = timing + [
                f"tool calls requested: {len(tool_calls)}",
                f"cost: {usage.get('cost', 0)}",
                f"tokens: {usage.get('total_tokens', 0)}",
            ]
            names = [
                display_tool_call_name(str(call.get("function", {}).get("name")))
                for call in tool_calls
                if isinstance(call, dict) and isinstance(call.get("function"), dict) and call.get("function", {}).get("name")
            ]
            if names:
                details.append("tools requested: " + ", ".join(str(name) for name in names))
            reasoning = item.get("reasoning") if isinstance(item.get("reasoning"), dict) else None
            if reasoning:
                details.append(f"reasoning: present, {int(reasoning.get('chars') or 0)} chars")
            lines.extend(f"- {detail}" for detail in details)
            text = assistant_message_text(message).strip()
            if text:
                lines.extend(["", "Assistant message:", "", "```text", text, "```"])
            lines.append("")
        elif role == "tool":
            name = str(item.get("name") or "unknown")
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            status = "ok" if result.get("ok") is True else "error" if result.get("ok") is False else "unknown"
            arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            lines.append(f"## {index}. `tool:{name}` `{status}`")
            details = summarize_tool_timing(item) + summarize_tool_event(name, arguments, result)
            lines.extend(f"- {detail}" for detail in details)
            lines.append("")
        elif role in {"system", "user", "runtime_notice"}:
            kind = str(item.get("kind") or "message")
            lines.append(f"## {index}. `{role}` `{kind}`")
            details = summarize_tool_timing(item)
            if item.get("message"):
                details.append("message: " + single_line(str(item["message"]), 500))
            lines.extend(f"- {detail}" for detail in details)
            lines.append("")
        else:
            lines.append(f"## {index}. `{role}`")
            details = summarize_tool_timing(item)
            lines.extend(f"- {detail}" for detail in details)
            lines.append("")
    return "\n".join(lines)


def render_tool_calls_markdown(tool_events: list[dict[str, Any]]) -> str:
    lines = ["# Tool Calls", ""]
    if not tool_events:
        lines.append("No tool calls recorded.")
        lines.append("")
        return "\n".join(lines)
    for index, event in enumerate(tool_events, 1):
        name = str(event.get("name") or "unknown")
        arguments = event.get("arguments") if isinstance(event.get("arguments"), dict) else {}
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        status = "ok" if result.get("ok") is True else "error" if result.get("ok") is False else "unknown"
        lines.append(f"## {index}. `{name}` `{status}`")
        summary = summarize_tool_event(name, arguments, result)
        timing = summarize_tool_timing(event)
        if timing:
            summary = timing + summary
        if summary:
            lines.append("")
            lines.extend(f"- {item}" for item in summary)
        lines.append("")
    return "\n".join(lines)


def summarize_tool_timing(event: dict[str, Any]) -> list[str]:
    timing: list[str] = []
    if event.get("started_at"):
        timing.append(f"started: {event['started_at']}")
    if event.get("ended_at"):
        timing.append(f"ended: {event['ended_at']}")
    if event.get("duration_ms") is not None:
        timing.append(f"duration: {event['duration_ms']}ms")
    return timing


def summarize_tool_event(name: str, arguments: dict[str, Any], result: dict[str, Any]) -> list[str]:
    summary: list[str] = []
    display_args = display_tool_arguments(name, arguments)
    if display_args:
        summary.append("args: " + compact_json(display_args, 300))
    if result.get("error"):
        summary.append("error: " + str(result["error"])[:500])
    if name in {"read_files", "write_files", "edit_files"}:
        files = result.get("files") if isinstance(result.get("files"), list) else []
        summary.append(f"files: {len(files)}")
        if name == "edit_files":
            summary.append(f"edits: {sum(int(item.get('edits') or 0) for item in files if isinstance(item, dict))}")
        if files:
            summary.append("sample: " + ", ".join(str(item.get("path")) for item in files[:12] if isinstance(item, dict)))
    elif name == "list_files":
        entries = result.get("entries") if isinstance(result.get("entries"), list) else []
        summary.append(f"entries: {len(entries)}")
        if entries:
            summary.append("sample: " + ", ".join(str(item.get("path")) for item in entries[:12]))
    elif name == "find_files":
        matches = result.get("matches") if isinstance(result.get("matches"), list) else []
        summary.append(f"matches: {len(matches)}")
        if matches:
            summary.append("sample: " + ", ".join(str(item) for item in matches[:12]))
    elif name == "search_files":
        matches = result.get("matches") if isinstance(result.get("matches"), list) else []
        summary.append(f"matches: {len(matches)}")
        if matches:
            summary.append("sample: " + "; ".join(f"{item.get('path')}:{item.get('line')}" for item in matches[:8] if isinstance(item, dict)))
    elif name == "bash":
        summary.append(f"returncode: {result.get('returncode')}")
        stdout = str(result.get("stdout") or "").strip()
        stderr = str(result.get("stderr") or "").strip()
        if stdout:
            summary.append("stdout: " + single_line(stdout, 500))
        if stderr:
            summary.append("stderr: " + single_line(stderr, 500))
    elif name == "run_tests":
        summary.append(f"command: `{result.get('command')}`")
        summary.append(f"returncode: {result.get('returncode')}")
        summary.append(f"timed out: {result.get('timed_out')}")
        summary.append(f"output path: `{result.get('output_path')}`")
        parsed = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        summary.append(f"passed: {parsed.get('passed')}")
        summary.append(f"failed: {parsed.get('failed')}")
        failed_tests = parsed.get("failed_tests") if isinstance(parsed.get("failed_tests"), list) else []
        for failed in failed_tests[:8]:
            summary.append("failed test: " + single_line(str(failed), 300))
        output_tail = str(result.get("output_tail") or "").strip()
        if output_tail:
            summary.append("output tail: " + single_line(output_tail, 500))
    elif name == "latest_test_failure_context":
        summary.append(f"context path: `{result.get('context_path')}`")
        summary.append(f"command: `{result.get('command')}`")
        summary.append(f"returncode: {result.get('returncode')}")
        failed_tests = result.get("failed_tests") if isinstance(result.get("failed_tests"), list) else []
        for failed in failed_tests[:8]:
            summary.append("failed test: " + single_line(str(failed), 300))
        artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), list) else []
        for artifact in artifacts[:8]:
            if isinstance(artifact, dict):
                summary.append(f"artifact: `{artifact.get('path')}`")
    elif name == "capture_visual_snapshot":
        summary.append(f"url: `{result.get('url') or arguments.get('url')}`")
        summary.append(f"screenshot path: `{result.get('screenshot_path')}`")
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        summary.append(f"viewport coverage: {metrics.get('viewportCoverage')}")
        summary.append(f"top gap ratio: {metrics.get('topGapRatio')}")
        summary.append(f"left gap ratio: {metrics.get('leftGapRatio')}")
        summary.append(f"visible elements: {metrics.get('visibleElementCount')}")
        if metrics.get("headingInteractiveOverlapCount"):
            summary.append(f"heading/control overlaps: {metrics.get('headingInteractiveOverlapCount')}")
        console_messages = result.get("consoleMessages") if isinstance(result.get("consoleMessages"), list) else []
        if console_messages:
            summary.append("console messages: " + str(len(console_messages)))
    elif name in {"append_evidence_note", "append_evidence_command", "append_evidence_screenshot"}:
        summary.append(f"evidence path: `{result.get('evidence_path')}`")
        if result.get("command"):
            summary.append(f"command: `{result.get('command')}`")
            summary.append(f"returncode: {result.get('returncode')}")
            summary.append(f"output path: `{result.get('output_path')}`")
        if result.get("screenshot_path"):
            summary.append(f"screenshot path: `{result.get('screenshot_path')}`")
        output_tail = str(result.get("output_tail") or "").strip()
        if output_tail:
            summary.append("output tail: " + single_line(output_tail, 500))
    elif name == "detect_project_environment":
        summary.append(f"package manager: {result.get('package_manager')}")
        summary.append("lockfiles: " + ", ".join(str(item) for item in result.get("lockfiles") or []))
        test_commands = result.get("test_commands") if isinstance(result.get("test_commands"), list) else []
        for command in test_commands[:8]:
            summary.append("test command: `" + str(command) + "`")
    elif name in {"git_status", "git_diff", "git_show"}:
        summary.append(f"returncode: {result.get('returncode')}")
        stdout = str(result.get("stdout") or "").strip()
        stderr = str(result.get("stderr") or "").strip()
        if stdout:
            summary.append("stdout: " + single_line(stdout, 500))
        if stderr:
            summary.append("stderr: " + single_line(stderr, 500))
    elif name == "start_process":
        summary.append(f"process id: `{result.get('process_id')}`")
        summary.append(f"pid: {result.get('pid')}")
        summary.append(f"ready: {result.get('ready')}")
        if result.get("requested_ports"):
            summary.append("requested ports: " + ", ".join(str(port) for port in result.get("requested_ports") or []))
        if result.get("allocated_port"):
            summary.append(f"allocated port: {result.get('allocated_port')}")
        if result.get("ports"):
            summary.append("listening ports: " + ", ".join(str(port) for port in result.get("ports") or []))
        if result.get("url"):
            summary.append(f"url: {result.get('url')}")
        summary.append(f"log path: `{result.get('log_path')}`")
        output = str(result.get("output") or "").strip()
        if output:
            summary.append("output: " + single_line(output, 500))
    elif name == "read_process":
        summary.append(f"process id: `{arguments.get('process_id')}`")
        summary.append(f"running: {result.get('running')}")
        summary.append(f"returncode: {result.get('returncode')}")
        if result.get("ports"):
            summary.append("listening ports: " + ", ".join(str(port) for port in result.get("ports") or []))
        if result.get("url"):
            summary.append(f"url: {result.get('url')}")
        output = str(result.get("output") or "").strip()
        if output:
            summary.append("output: " + single_line(output, 500))
    elif name == "stop_process":
        summary.append(f"process id: `{arguments.get('process_id')}`")
        summary.append(f"stopped: {result.get('stopped')}")
        summary.append(f"returncode: {result.get('returncode')}")
    elif name == "list_processes":
        processes = result.get("processes") if isinstance(result.get("processes"), list) else []
        summary.append(f"processes: {len(processes)}")
        for process in processes[:8]:
            if isinstance(process, dict):
                summary.append(
                    "process: "
                    + single_line(
                        f"{process.get('process_id')} pid={process.get('pid')} running={process.get('running')} command={process.get('command')}",
                        220,
                    )
                )
                if process.get("ports"):
                    summary.append("ports: " + ", ".join(str(port) for port in process.get("ports") or []))
                if process.get("allocated_port"):
                    summary.append(f"allocated port: {process.get('allocated_port')}")
                if process.get("url"):
                    summary.append(f"url: {process.get('url')}")
    elif name == "todo_read":
        items = result.get("items") if isinstance(result.get("items"), list) else []
        summary.append(f"todo items: {len(items)}")
    elif name == "todo_write":
        items = result.get("items") if isinstance(result.get("items"), list) else []
        summary.append(f"todo items: {len(items)}")
        for item in items[:8]:
            if isinstance(item, dict):
                label = item.get("content") or item.get("task") or item.get("title") or compact_json(item, 120)
                status = item.get("status") or item.get("state")
                summary.append(f"todo: {status + ' ' if status else ''}{single_line(str(label), 160)}")
    elif name == "web_search":
        results = result.get("results") if isinstance(result.get("results"), list) else []
        summary.append(f"results: {len(results)}")
        for item in results[:5]:
            if isinstance(item, dict):
                summary.append(f"result: {single_line(str(item.get('title') or ''), 120)} {item.get('url') or ''}")
    elif name == "fetch_url":
        content = str(result.get("content") or "")
        summary.append(f"url: `{result.get('url') or arguments.get('url')}`")
        summary.append(f"status: {result.get('status')}")
        summary.append(f"bytes read: {len(content.encode('utf-8'))}")
    elif name == "activate_skill":
        summary.append(f"skill: `{result.get('name') or arguments.get('name')}`")
        summary.append(f"description: {result.get('description') or ''}")
        summary.append(f"skill path: `{result.get('skill_path')}`")
        resources = result.get("resources") if isinstance(result.get("resources"), list) else []
        summary.append(f"resources: {len(resources)}")
        for resource in resources[:12]:
            if isinstance(resource, dict):
                summary.append(f"resource: `{resource.get('path')}` ({resource.get('bytes')} bytes)")
    elif name == "read_skill_resource":
        content = str(result.get("content") or "")
        summary.append(f"skill: `{result.get('name') or arguments.get('name')}`")
        summary.append(f"path: `{result.get('path') or arguments.get('path')}`")
        summary.append(f"bytes read: {len(content.encode('utf-8'))}")
    elif name == "final_report":
        summary.append("final report submitted")
    return summary


def display_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "write_files":
        files = arguments.get("files") if isinstance(arguments.get("files"), list) else []
        return {"files": [{"path": item.get("path")} for item in files if isinstance(item, dict)]}
    if name == "edit_files":
        files = arguments.get("files") if isinstance(arguments.get("files"), list) else []
        return {
            "files": [
                {
                    "path": item.get("path"),
                    "edits": len(item.get("edits") or []) if isinstance(item, dict) else 0,
                }
                for item in files
                if isinstance(item, dict)
            ]
        }
    if name in {"read_files", "fetch_url", "read_skill_resource"}:
        return {key: value for key, value in arguments.items() if key != "content"}
    if name == "todo_write":
        return {}
    return arguments


def build_runtime_metadata(
    agent_name: str,
    model: str,
    selected_model: dict[str, Any],
    result: AgentRunResult,
    *,
    status: str = "success",
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "agent_name": agent_name,
        "model": model,
        "variant_id": selected_model.get("variant_id", model),
        "reasoning_request": selected_model.get("reasoning_request"),
        "started_at": result.started_at,
        "ended_at": result.ended_at,
        "written_at": utc_timestamp(),
        "error": error,
        "final_report_present": result.final_report is not None,
        "usage": result.usage,
        "events": {
            "tool_calls": len(result.tool_events),
            "compactions": len(result.compaction_events),
            "pre_compaction_archives": len(result.pre_compaction_archives),
        },
    }


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
