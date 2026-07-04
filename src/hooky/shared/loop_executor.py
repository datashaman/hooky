#!/usr/bin/env python3
"""Role executor adapters for the Hooky loop."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

from hooky.runtime import (
    AgentRunError,
    AgentRunResult,
    ToolRuntime,
    append_live_event,
    run_tool_agent,
    single_line,
    utc_timestamp,
)
from hooky.runtime.mcp_server import McpServerConfig

MCP_EXECUTORS = {"codex", "claude"}

VALID_EXECUTORS = {"native", "shell", "codex", "claude"}
EXECUTOR_ENV = "HOOKY_EXECUTOR"


@dataclass(frozen=True)
class RoleInvocation:
    role: str
    agent_name: str
    model: str
    system: str
    user: str
    runtime: ToolRuntime
    model_metadata: dict[str, Any]


def selected_executor(explicit: str | None = None) -> str:
    raw = (explicit or os.environ.get(EXECUTOR_ENV) or "native").strip().lower()
    if raw not in VALID_EXECUTORS:
        allowed = ", ".join(sorted(VALID_EXECUTORS))
        raise ValueError(f"unknown Hooky executor {raw!r}; expected one of: {allowed}")
    return raw


def run_role(invocation: RoleInvocation, *, executor: str | None = None) -> AgentRunResult:
    selected = selected_executor(executor)
    if selected == "native":
        return run_tool_agent(model=invocation.model, system=invocation.system, user=invocation.user, runtime=invocation.runtime)
    return run_external_executor(invocation, executor=selected)


def run_external_executor(invocation: RoleInvocation, *, executor: str) -> AgentRunResult:
    started_at = utc_timestamp()
    live_root = invocation.runtime.live_log_root or (invocation.runtime.working_folder / ".hooky/runs/local")
    executor_dir = live_root / "executor" / safe_segment(invocation.role)
    executor_dir.mkdir(parents=True, exist_ok=True)
    input_path = executor_dir / "input.md"
    output_path = executor_dir / "output.json"
    stdout_path = executor_dir / "stdout.log"
    stderr_path = executor_dir / "stderr.log"
    metadata_path = executor_dir / "metadata.json"

    mcp_config_path: Path | None = None
    mcp_events_path: Path | None = None
    if executor in MCP_EXECUTORS:
        mcp_config_path = executor_dir / "mcp_config.json"
        mcp_events_path = executor_dir / "mcp_tool_events.jsonl"
        mcp_events_path.write_text("", encoding="utf-8")
        mcp_server_config(invocation.runtime, mcp_events_path).to_json_file(mcp_config_path)

    prompt = executor_prompt(invocation, output_path)
    input_path.write_text(prompt, encoding="utf-8")
    command, stdin_text = command_for_executor(
        executor,
        invocation.runtime.working_folder,
        input_path,
        output_path,
        executor_dir,
        prompt,
        mcp_config_path=mcp_config_path,
    )
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "executor": executor,
        "role": invocation.role,
        "agent_name": invocation.agent_name,
        "model": invocation.model,
        "selected_model": invocation.model_metadata,
        "input": input_path.as_posix(),
        "output": output_path.as_posix(),
        "command": printable_command(command),
        "started_at": started_at,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    append_live_event(invocation.runtime, f"{started_at} run start executor={executor} command={json.dumps(printable_command(command))}")
    completed, timed_out = run_streaming_command(
        command,
        stdin_text=stdin_text,
        cwd=invocation.runtime.working_folder,
        timeout_seconds=invocation.runtime.max_seconds,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        runtime=invocation.runtime,
        executor=executor,
    )
    ended_at = utc_timestamp()
    metadata.update(
        {
            "ended_at": ended_at,
            "returncode": completed.returncode,
            "timed_out": timed_out,
            "stdout": stdout_path.as_posix(),
            "stderr": stderr_path.as_posix(),
            "output_present": output_path.exists(),
        }
    )
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report: dict[str, Any] | None = None
    error: str | None = None
    if output_path.exists():
        try:
            loaded = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                report = loaded
            else:
                error = "executor output JSON must be an object"
        except json.JSONDecodeError as exc:
            error = f"executor output JSON is invalid: {exc}"
    elif executor_error := extract_executor_error(completed.stdout, completed.stderr):
        error = executor_error
    elif completed.returncode == 0:
        error = f"executor did not write required output file: {output_path}"
    else:
        error = f"executor exited with status {completed.returncode} before writing output: {output_path}"

    if report is not None and invocation.runtime.final_validator is not None:
        try:
            invocation.runtime.final_validator(report)
        except Exception as exc:  # noqa: BLE001 - preserve validator message for the role transcript.
            error = str(exc)

    usage = external_usage(completed.stdout, executor=executor)
    usage.update(
        {
            "returncode": completed.returncode,
            "timed_out": timed_out,
            "stdout_bytes": len((completed.stdout or "").encode("utf-8")),
            "stderr_bytes": len((completed.stderr or "").encode("utf-8")),
        }
    )
    result = AgentRunResult(
        final_report=report if error is None else None,
        usage=usage,
        transcript=[
            {"role": "system", "message": invocation.system, "started_at": started_at, "ended_at": started_at},
            {"role": "user", "kind": "initial", "message": invocation.user, "started_at": started_at, "ended_at": started_at},
            {
                "role": "runtime_notice",
                "kind": "executor",
                "message": f"{executor} executor exited with status {completed.returncode}",
                "metadata": metadata,
                "started_at": started_at,
                "ended_at": ended_at,
            },
        ],
        tool_events=load_mcp_tool_events(mcp_events_path) if mcp_events_path is not None else [],
        compaction_events=[],
        pre_compaction_archives=[],
        started_at=started_at,
        ended_at=ended_at,
    )
    if error is not None:
        append_live_event(invocation.runtime, f"{ended_at} run error executor={executor} error={json.dumps(single_line(error, 300))}")
        raise AgentRunError(error, result)
    append_live_event(invocation.runtime, f"{ended_at} run success executor={executor} returncode={completed.returncode}")
    return result


def executor_prompt(invocation: RoleInvocation, output_path: Path) -> str:
    schema_json = json.dumps(invocation.runtime.final_report_schema, indent=2, sort_keys=True)
    model_json = json.dumps(invocation.model_metadata, indent=2, sort_keys=True)
    return f"""# Hooky role invocation

You are running as the Hooky `{invocation.role}` role.

Workspace: `{invocation.runtime.working_folder}`
Required JSON report path: `{output_path}`

Write exactly one JSON object matching this schema to the required report path:

```json
{schema_json}
```

Selected model metadata:

```json
{model_json}
```

Do not rely on conversational final text for the report. Hooky will read only the JSON file above and validate it.

## System instructions

{invocation.system}

## User instructions

{invocation.user}
"""


def command_for_executor(
    executor: str,
    workspace: Path,
    input_path: Path,
    output_path: Path,
    executor_dir: Path,
    prompt: str,
    *,
    mcp_config_path: Path | None = None,
) -> tuple[list[str], str | None]:
    if executor == "shell":
        return shell_command(workspace, input_path, output_path, executor_dir), None
    if executor == "codex":
        return codex_command(workspace, executor_dir, mcp_config_path=mcp_config_path), prompt
    if executor == "claude":
        return claude_command(executor_dir, mcp_config_path=mcp_config_path), prompt
    raise ValueError(f"unsupported executor: {executor}")


def mcp_server_config(runtime: ToolRuntime, events_path: Path) -> McpServerConfig:
    return McpServerConfig(
        working_folder=runtime.working_folder.as_posix(),
        events_path=events_path.as_posix(),
        max_seconds=runtime.max_seconds,
        bash_timeout_seconds=runtime.bash_timeout_seconds,
        read_allowed_prefixes=list(runtime.read_allowed_prefixes),
        read_blocked_prefixes=list(runtime.read_blocked_prefixes),
        bash_protected_prefixes=list(runtime.bash_protected_prefixes),
        bash_blocked_substrings=list(runtime.bash_blocked_substrings),
        live_log_root=runtime.live_log_root.as_posix() if runtime.live_log_root else None,
        live_event_prefix=runtime.live_event_prefix,
    )


def mcp_codex_config_args(mcp_config_path: Path) -> list[str]:
    args_toml = json.dumps(["mcp-serve", "--config", mcp_config_path.as_posix()])
    return [
        "--config",
        'mcp_servers.hooky.command="hooky"',
        "--config",
        f"mcp_servers.hooky.args={args_toml}",
    ]


def write_claude_mcp_config_file(executor_dir: Path, mcp_config_path: Path) -> Path:
    claude_mcp_config = {
        "mcpServers": {
            "hooky": {
                "command": "hooky",
                "args": ["mcp-serve", "--config", mcp_config_path.as_posix()],
            }
        }
    }
    path = executor_dir / "claude_mcp_servers.json"
    path.write_text(json.dumps(claude_mcp_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_mcp_tool_events(events_path: Path) -> list[dict[str, Any]]:
    if not events_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for raw_line in events_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def shell_command(workspace: Path, input_path: Path, output_path: Path, executor_dir: Path) -> list[str]:
    template = os.environ.get("HOOKY_EXECUTOR_COMMAND")
    if not template:
        raise RuntimeError("HOOKY_EXECUTOR_COMMAND is required when HOOKY_EXECUTOR=shell")
    values = {
        "workspace": shlex.quote(workspace.as_posix()),
        "input": shlex.quote(input_path.as_posix()),
        "output": shlex.quote(output_path.as_posix()),
        "executor_dir": shlex.quote(executor_dir.as_posix()),
    }
    command = Template(template).safe_substitute(values)
    return ["/bin/sh", "-lc", command]


def codex_command(workspace: Path, executor_dir: Path, *, mcp_config_path: Path | None = None) -> list[str]:
    command = [
        "codex",
        "exec",
        "-C",
        workspace.as_posix(),
        "--json",
        "--output-last-message",
        (executor_dir / "last_message.txt").as_posix(),
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    if model := os.environ.get("HOOKY_CODEX_MODEL"):
        command.extend(["--model", model])
    if effort := os.environ.get("HOOKY_CODEX_REASONING_EFFORT"):
        command.extend(["--config", f'model_reasoning_effort="{effort}"'])
    if mcp_config_path is not None:
        command.extend(mcp_codex_config_args(mcp_config_path))
    command.append("-")
    return command


def claude_command(executor_dir: Path, *, mcp_config_path: Path | None = None) -> list[str]:
    command = [
        "claude",
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
        "--debug-file",
        (executor_dir / "debug.log").as_posix(),
        "--permission-mode",
        "bypassPermissions",
        "--setting-sources",
        "user,project,local",
    ]
    if model := os.environ.get("HOOKY_CLAUDE_MODEL"):
        command.extend(["--model", model])
    if effort := os.environ.get("HOOKY_CLAUDE_EFFORT"):
        command.extend(["--effort", effort])
    if mcp_config_path is not None:
        claude_mcp_config_path = write_claude_mcp_config_file(executor_dir, mcp_config_path)
        command.extend(["--mcp-config", claude_mcp_config_path.as_posix()])
    return command


def printable_command(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def run_streaming_command(
    command: list[str],
    *,
    stdin_text: str | None,
    cwd: Path,
    timeout_seconds: int,
    stdout_path: Path,
    stderr_path: Path,
    runtime: ToolRuntime,
    executor: str,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    next_heartbeat = started + min(20, max(1, timeout_seconds))

    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
    except OSError as exc:
        message = str(exc)
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(message + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 127, stdout="", stderr=message), False

    if process.stdin is not None:
        try:
            if stdin_text is not None:
                process.stdin.write(stdin_text)
            process.stdin.close()
        except BrokenPipeError:
            pass

    def consume(stream: Any, path: Path, parts: list[str], stream_name: str) -> None:
        with path.open("w", encoding="utf-8") as handle:
            if stream is None:
                return
            for line in iter(stream.readline, ""):
                parts.append(line)
                handle.write(line)
                handle.flush()
                event = external_stream_event(executor, stream_name, line)
                if event:
                    append_live_event(runtime, event)
            stream.close()

    stdout_thread = threading.Thread(target=consume, args=(process.stdout, stdout_path, stdout_parts, "stdout"), daemon=True)
    stderr_thread = threading.Thread(target=consume, args=(process.stderr, stderr_path, stderr_parts, "stderr"), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    while process.poll() is None:
        now = time.monotonic()
        elapsed = int(now - started)
        if elapsed >= timeout_seconds:
            timed_out = True
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            timeout_line = f"executor timed out after {timeout_seconds}s"
            stderr_parts.append(timeout_line + "\n")
            with stderr_path.open("a", encoding="utf-8") as handle:
                handle.write(timeout_line + "\n")
            append_live_event(runtime, f"{utc_timestamp()} heartbeat source=local executor={executor} elapsed_seconds={elapsed} timed_out=true")
            break
        if now >= next_heartbeat:
            append_live_event(runtime, f"{utc_timestamp()} heartbeat source=local executor={executor} elapsed_seconds={elapsed}")
            next_heartbeat = now + 20
        time.sleep(0.25)

    returncode = process.wait()
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    if timed_out and returncode == 0:
        returncode = 124
    return subprocess.CompletedProcess(command, returncode, stdout="".join(stdout_parts), stderr="".join(stderr_parts)), timed_out


def external_stream_event(executor: str, stream_name: str, line: str) -> str | None:
    text = line.strip()
    if not text:
        return None
    timestamp = utc_timestamp()
    if stream_name == "stderr":
        return f"{timestamp} executor stream=stderr name={executor} message={json.dumps(single_line(text, 240))}"
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        return f"{timestamp} executor stream=stdout name={executor} message={json.dumps(single_line(text, 240))}"
    if not isinstance(event, dict):
        return f"{timestamp} executor stream=stdout name={executor} message={json.dumps(single_line(text, 240))}"
    event_type = event.get("type")
    if event_type == "system":
        model = event.get("model")
        subtype = event.get("subtype")
        session_id = event.get("session_id")
        return f"{timestamp} executor name={executor} event=system subtype={json.dumps(str(subtype))} model={json.dumps(str(model))} session_id={json.dumps(str(session_id))}"
    if event_type == "rate_limit_event":
        info = event.get("rate_limit_info") if isinstance(event.get("rate_limit_info"), dict) else {}
        return f"{timestamp} executor name={executor} event=rate_limit status={json.dumps(str(info.get('status')))} type={json.dumps(str(info.get('rateLimitType')))}"
    if event_type == "assistant":
        return external_assistant_event(timestamp, executor, event)
    if event_type == "user":
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        is_error = any(isinstance(item, dict) and item.get("is_error") for item in content) if isinstance(content, list) else False
        return f"{timestamp} executor name={executor} event=tool_result status={'error' if is_error else 'ok'}"
    if event_type == "result":
        is_error = bool(event.get("is_error"))
        duration = event.get("duration_ms")
        cost = event.get("total_cost_usd")
        result = event.get("result")
        message = f" message={json.dumps(single_line(result, 240))}" if isinstance(result, str) and result.strip() else ""
        return f"{timestamp} executor name={executor} event=result status={'error' if is_error else 'success'} duration_ms={duration} cost=${float(cost or 0):.8f}{message}"
    return f"{timestamp} executor name={executor} event={json.dumps(str(event_type))}"


def external_assistant_event(timestamp: str, executor: str, event: dict[str, Any]) -> str:
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    content = message.get("content") if isinstance(message, dict) else []
    model = message.get("model") if isinstance(message, dict) else None
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                return f"{timestamp} assistant executor={executor} model={json.dumps(str(model))} message={json.dumps(single_line(item['text'], 260))}"
            if item.get("type") == "tool_use":
                name = item.get("name")
                return f"{timestamp} tool executor={executor} name={json.dumps(str(name))} status=called"
            if item.get("type") == "thinking":
                thinking = item.get("thinking")
                suffix = f" reasoning={json.dumps(single_line(thinking, 180))}" if isinstance(thinking, str) and thinking.strip() else ""
                return f"{timestamp} assistant executor={executor} model={json.dumps(str(model))} thinking=true{suffix}"
    return f"{timestamp} assistant executor={executor} model={json.dumps(str(model))}"


def extract_executor_error(stdout: str | None, stderr: str | None) -> str | None:
    stderr_text = (stderr or "").strip()
    if stderr_text:
        return stderr_text.splitlines()[-1]
    for raw_line in reversed((stdout or "").splitlines()):
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result" and event.get("is_error"):
            result = event.get("result")
            if isinstance(result, str) and result.strip():
                return result.strip()
        message = event.get("message") if isinstance(event, dict) else None
        if isinstance(message, dict) and event.get("error"):
            content = message.get("content")
            if isinstance(content, list):
                text = " ".join(str(item.get("text")) for item in content if isinstance(item, dict) and item.get("type") == "text" and item.get("text")).strip()
                if text:
                    return text
    return None


def external_usage(stdout: str | None, *, executor: str) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "executor": executor,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost": 0.0,
    }
    result_event = last_result_event(stdout)
    if result_event is None:
        return usage
    if isinstance(result_event.get("total_cost_usd"), int | float):
        usage["cost"] = float(result_event["total_cost_usd"])
    raw_usage = result_event.get("usage")
    if isinstance(raw_usage, dict):
        prompt_tokens = int_value(raw_usage.get("input_tokens")) + int_value(raw_usage.get("cache_creation_input_tokens")) + int_value(raw_usage.get("cache_read_input_tokens"))
        completion_tokens = int_value(raw_usage.get("output_tokens"))
        usage["prompt_tokens"] = prompt_tokens
        usage["completion_tokens"] = completion_tokens
        usage["total_tokens"] = prompt_tokens + completion_tokens
        usage["raw_usage"] = raw_usage
    model_usage = result_event.get("modelUsage")
    if isinstance(model_usage, dict):
        usage["model_usage"] = model_usage
        if not usage["cost"]:
            usage["cost"] = sum(float(item.get("costUSD") or 0) for item in model_usage.values() if isinstance(item, dict))
        if not usage["total_tokens"]:
            prompt_tokens = 0
            completion_tokens = 0
            for item in model_usage.values():
                if not isinstance(item, dict):
                    continue
                prompt_tokens += int_value(item.get("inputTokens")) + int_value(item.get("cacheCreationInputTokens")) + int_value(item.get("cacheReadInputTokens"))
                completion_tokens += int_value(item.get("outputTokens"))
            usage["prompt_tokens"] = prompt_tokens
            usage["completion_tokens"] = completion_tokens
            usage["total_tokens"] = prompt_tokens + completion_tokens
    return usage


def last_result_event(stdout: str | None) -> dict[str, Any] | None:
    for raw_line in reversed((stdout or "").splitlines()):
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


def int_value(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def safe_segment(value: str) -> str:
    segment = "".join(char if char.isalnum() or char in ("-", "_") else "-" for char in value.strip().lower())
    return segment.strip("-") or "role"
