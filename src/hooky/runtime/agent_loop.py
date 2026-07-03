"""The model/tool agent loop driver, compaction, and run-result types."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import threading
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hooky import agent_skills

from hooky.runtime.evidence import relative_to
from hooky.runtime.models import LocalDeadline, model_client, model_provider, model_request_options, openrouter_timeout_ms, relative_or_name, response_usage
from hooky.runtime.rendering import append_live_event, canonical_tool_name, format_runtime_event_line, utc_timestamp, write_runtime_log
from hooky.runtime.schemas import structured_response_format
from hooky.runtime.text import assistant_message_text, assistant_reasoning_trace, compaction_schema, compaction_system_prompt, compaction_user_prompt, estimate_tokens, extract_text_tool_actions, recover_text_final_report, single_line, trim_leading_tool_messages
from hooky.runtime.tool_runtime import ToolRuntime


def model_request_deadline_seconds(runtime: ToolRuntime, elapsed_seconds: float) -> int:
    remaining = max(1, int(runtime.max_seconds - elapsed_seconds))
    configured = max(1, openrouter_timeout_ms() // 1000)
    return max(1, min(configured, remaining))


def image_input_message(runtime: ToolRuntime, images: list[dict[str, str]], text: str) -> dict[str, Any] | None:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    attached: list[str] = []
    for image in images:
        relative = str(image.get("path") or "").strip()
        if not relative:
            continue
        try:
            path = runtime.resolve_path(relative)
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_data_url(path),
                    },
                }
            )
            label = str(image.get("label") or relative)
            content[0]["text"] += f"\n- {label}: {relative}"
            attached.append(relative)
        except (OSError, ValueError):
            continue
    if not attached:
        return None
    return {"role": "user", "content": content}


def image_data_url(path: Path) -> str:
    data = path.read_bytes()
    max_bytes = int(os.environ.get("AGENT_IMAGE_INPUT_MAX_BYTES", "5000000"))
    if len(data) > max_bytes:
        raise ValueError(f"image is too large to attach: {relative_or_name(path)} ({len(data)} bytes)")
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    if mime_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise ValueError(f"unsupported image type for model input: {mime_type}")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


@dataclass
class AgentRunResult:
    final_report: dict[str, Any] | None
    usage: dict[str, Any]
    transcript: list[dict[str, Any]]
    tool_events: list[dict[str, Any]]
    compaction_events: list[dict[str, Any]]
    pre_compaction_archives: list[dict[str, Any]]
    started_at: str
    ended_at: str


class AgentRunError(RuntimeError):
    def __init__(self, message: str, result: AgentRunResult):
        super().__init__(message)
        self.result = result


def run_tool_agent(
    *,
    model: str,
    system: str,
    user: str,
    runtime: ToolRuntime,
) -> AgentRunResult:
    transcript: list[dict[str, Any]] = []
    tool_events: list[dict[str, Any]] = []
    compaction_events: list[dict[str, Any]] = []
    pre_compaction_archives: list[dict[str, Any]] = []
    total_usage: dict[str, Any] = {"cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    started_at = utc_timestamp()
    soft_deadline_sent = False
    consecutive_no_tool_responses = 0
    messages: list[Any] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    transcript.extend(
        [
            {
                "role": "system",
                "message": system,
                "started_at": utc_timestamp(),
                "ended_at": utc_timestamp(),
            },
            {
                "role": "user",
                "kind": "initial",
                "message": user,
                "started_at": utc_timestamp(),
                "ended_at": utc_timestamp(),
            },
        ]
    )
    catalog_message = skill_catalog_message(runtime)
    if catalog_message:
        messages.append(catalog_message)
        transcript.append(
            {
                "role": "skill_catalog",
                "message": catalog_message["content"],
                "started_at": utc_timestamp(),
                "ended_at": utc_timestamp(),
            }
        )
    preselected_skill_message = preselected_skills_message(runtime)
    if preselected_skill_message:
        messages.append(preselected_skill_message)
        transcript.append(
            {
                "role": "skill_activation",
                "message": preselected_skill_message["content"],
                "started_at": utc_timestamp(),
                "ended_at": utc_timestamp(),
            }
        )
    if runtime.initial_image_paths:
        initial_images = [
            {"path": relative_to((path if path.is_absolute() else runtime.working_folder / path), runtime.working_folder), "label": "Initial visual evidence"}
            for path in runtime.initial_image_paths
        ]
        image_message = image_input_message(runtime, initial_images, "Initial visual evidence attached for inspection.")
        if image_message:
            messages.append(image_message)
            transcript.append(
                {
                    "role": "image_input",
                    "message": "Initial visual evidence attached for inspection.",
                    "images": initial_images,
                    "started_at": utc_timestamp(),
                    "ended_at": utc_timestamp(),
                }
            )

    def current_result() -> AgentRunResult:
        return AgentRunResult(
            runtime.final_report,
            total_usage,
            transcript,
            tool_events,
            compaction_events,
            pre_compaction_archives,
            started_at,
            utc_timestamp(),
        )

    def flush_live_log(status: str = "running", error: str | None = None) -> None:
        if not runtime.live_log_root:
            return
        result = current_result()
        write_runtime_log(
            runtime.live_log_root,
            result.transcript,
            result.tool_events,
            result.compaction_events,
            result.pre_compaction_archives,
            metadata={
                "schema_version": 1,
                "status": status,
                "model": model,
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
            },
        )

    append_live_event(runtime, f"{utc_timestamp()} run start model={model}")
    flush_live_log()
    stop_heartbeat = threading.Event()
    heartbeat_thread = start_heartbeat_thread(runtime, model, total_usage, stop_heartbeat)
    try:
        with model_client(model) as client:
            while runtime.final_report is None:
                elapsed_seconds = time.monotonic() - runtime.started_at
                if elapsed_seconds > runtime.max_seconds:
                    raise AgentRunError(f"agent runtime exceeded {runtime.max_seconds}s", current_result())
                if float(total_usage.get("cost") or 0) > runtime.max_cost_usd:
                    raise AgentRunError(f"agent runtime exceeded ${runtime.max_cost_usd:.4f} cost budget", current_result())
                remaining_seconds = max(0, int(runtime.max_seconds - elapsed_seconds))
                if not soft_deadline_sent and runtime.max_seconds >= 60 and elapsed_seconds >= runtime.max_seconds * 0.85:
                    soft_deadline_sent = True
                    warning = (
                        f"Runtime soft deadline: about {remaining_seconds}s remain before the hard timeout. "
                        "If recent tool results show useful progress and one concrete next step remains, you may call "
                        "request_time_extension with the failing tests, current status, and next command. Otherwise, "
                        "call final_report now with current status, concrete failures, and next steps instead of "
                        "starting another long debugging cycle."
                    )
                    messages.append({"role": "user", "content": warning})
                    transcript.append(
                        {
                            "role": "user",
                            "kind": "soft_deadline",
                            "message": warning,
                            "started_at": utc_timestamp(),
                            "ended_at": utc_timestamp(),
                        }
                    )
                    append_live_event(runtime, f"{utc_timestamp()} runtime_notice kind=soft_deadline remaining_seconds={remaining_seconds}")
                    flush_live_log()

                request_seconds = model_request_deadline_seconds(runtime, elapsed_seconds)
                try:
                    with LocalDeadline(request_seconds, f"agent model request exceeded {request_seconds}s"):
                        messages, compaction_event, pre_compaction_archive = maybe_compact_messages(client, model, runtime, messages)
                except TimeoutError as exc:
                    raise AgentRunError(str(exc), current_result()) from exc
                if pre_compaction_archive:
                    pre_compaction_archives.append(pre_compaction_archive)
                    transcript.append({"role": "pre_compaction", **pre_compaction_archive})
                    flush_live_log()
                if compaction_event:
                    runtime.advance_read_generation()
                    usage = compaction_event.get("usage") or {}
                    accumulate_usage(total_usage, usage)
                    compaction_events.append(compaction_event)
                    transcript.append({"role": "compaction", **compaction_event})
                    flush_live_log()
                    if float(total_usage.get("cost") or 0) > runtime.max_cost_usd:
                        raise AgentRunError(f"agent runtime exceeded ${runtime.max_cost_usd:.4f} cost budget after compaction", current_result())

                if runtime.pending_image_inputs:
                    attachments = list(runtime.pending_image_inputs)
                    runtime.pending_image_inputs.clear()
                    image_message = image_input_message(runtime, attachments, "Visual evidence attached. Inspect the image pixels directly before continuing.")
                    if image_message:
                        messages.append(image_message)
                        transcript.append(
                            {
                                "role": "image_input",
                                "images": [{"path": item["path"], "label": item.get("label", "")} for item in attachments],
                                "started_at": utc_timestamp(),
                                "ended_at": utc_timestamp(),
                            }
                        )
                        append_live_event(runtime, f"{utc_timestamp()} image_input count={len(attachments)} paths={','.join(item['path'] for item in attachments)}")
                        flush_live_log()

                assistant_started_at = utc_timestamp()
                assistant_start = time.monotonic()
                request_seconds = model_request_deadline_seconds(runtime, time.monotonic() - runtime.started_at)
                try:
                    with LocalDeadline(request_seconds, f"agent model request exceeded {request_seconds}s"):
                        completion = client.chat.send(
                            model=model,
                            messages=messages,
                            tools=runtime.tools(),
                            tool_choice="auto",
                            **model_request_options(model),
                        )
                except TimeoutError as exc:
                    raise AgentRunError(str(exc), current_result()) from exc
                assistant_ended_at = utc_timestamp()
                assistant_duration_ms = round((time.monotonic() - assistant_start) * 1000, 2)
                usage = response_usage(completion)
                accumulate_usage(total_usage, usage)
                message = completion.choices[0].message
                message_payload = message.model_dump(exclude_none=True) if hasattr(message, "model_dump") else message
                reasoning_trace = assistant_reasoning_trace(message_payload)
                messages.append(message_payload)
                assistant_entry = {
                    "role": "assistant",
                    "message": message_payload,
                    "usage": usage,
                    "started_at": assistant_started_at,
                    "ended_at": assistant_ended_at,
                    "duration_ms": assistant_duration_ms,
                }
                if reasoning_trace:
                    assistant_entry["reasoning"] = reasoning_trace
                transcript.append(assistant_entry)
                append_live_event(runtime, format_runtime_event_line(transcript[-1]))
                flush_live_log()

                tool_calls = getattr(message, "tool_calls", None) or []
                if not tool_calls:
                    assistant_text = assistant_message_text(message_payload)
                    text_actions = extract_text_tool_actions(assistant_text)
                    if text_actions:
                        recovered_results: list[dict[str, Any]] = []
                        for action in text_actions:
                            raw_name = str(action["name"])
                            name = runtime.canonical_tool_name(raw_name)
                            tool_started_at = utc_timestamp()
                            tool_start = time.monotonic()
                            args = dict(action["arguments"])
                            result = runtime.run_tool(name, args)
                            event = {
                                "tool_call_id": f"text-tool-{len(tool_events) + 1}",
                                "name": name,
                                "arguments": args,
                                "result": result,
                                "started_at": tool_started_at,
                                "ended_at": utc_timestamp(),
                                "duration_ms": round((time.monotonic() - tool_start) * 1000, 2),
                                "source": "assistant_text",
                            }
                            if raw_name != name:
                                event["raw_name"] = raw_name
                            tool_events.append(event)
                            runtime.tool_events.append(event)
                            transcript.append({"role": "tool", **event})
                            append_live_event(runtime, format_runtime_event_line(transcript[-1]))
                            recovered_results.append({"name": name, "result": result})
                            if name == "final_report" and runtime.final_report is not None:
                                break
                        transcript.append(
                            {
                                "role": "runtime_notice",
                                "kind": "recovered_text_tool_calls",
                                "message": f"Recovered {len(recovered_results)} tool call(s) from assistant text.",
                                "started_at": utc_timestamp(),
                                "ended_at": utc_timestamp(),
                            }
                        )
                        append_live_event(runtime, format_runtime_event_line(transcript[-1]))
                        flush_live_log()
                        if runtime.final_report is not None:
                            break
                        messages.append(
                            {
                                "role": "user",
                                "content": "Recovered text tool call results:\n" + json.dumps(recovered_results, sort_keys=True),
                            }
                        )
                        continue
                    recovered_report = recover_text_final_report(runtime, assistant_text)
                    if recovered_report is not None:
                        transcript.append(
                            {
                                "role": "runtime_notice",
                                "kind": "recovered_text_final_report",
                                "message": "Recovered final_report from assistant text because no tool call was emitted.",
                                "started_at": utc_timestamp(),
                                "ended_at": utc_timestamp(),
                            }
                        )
                        append_live_event(runtime, format_runtime_event_line(transcript[-1]))
                        flush_live_log()
                        break
                    consecutive_no_tool_responses += 1
                    if runtime.no_tool_response_limit > 0 and consecutive_no_tool_responses > runtime.no_tool_response_limit:
                        raise AgentRunError(
                            f"agent produced {consecutive_no_tool_responses} consecutive assistant messages without tool calls",
                            current_result(),
                        )
                    prompt = "Continue by using the available tools. Finish only by calling final_report."
                    messages.append({"role": "user", "content": prompt})
                    transcript.append(
                        {
                            "role": "user",
                            "kind": "no_tool_calls",
                            "message": prompt,
                            "started_at": utc_timestamp(),
                            "ended_at": utc_timestamp(),
                        }
                    )
                    append_live_event(runtime, format_runtime_event_line(transcript[-1]))
                    flush_live_log()
                    continue
                consecutive_no_tool_responses = 0

                for tool_call in tool_calls:
                    raw_name = tool_call.function.name
                    name = runtime.canonical_tool_name(raw_name)
                    tool_started_at = utc_timestamp()
                    tool_start = time.monotonic()
                    try:
                        args = json.loads(tool_call.function.arguments or "{}")
                    except json.JSONDecodeError as exc:
                        args = {}
                        result = {"ok": False, "error": f"invalid JSON tool arguments: {exc}"}
                    else:
                        result = runtime.run_tool(name, args)
                    event = {
                        "tool_call_id": tool_call.id,
                        "name": name,
                        "arguments": args,
                        "result": result,
                        "started_at": tool_started_at,
                        "ended_at": utc_timestamp(),
                        "duration_ms": round((time.monotonic() - tool_start) * 1000, 2),
                    }
                    if raw_name != name:
                        event["raw_name"] = raw_name
                    tool_events.append(event)
                    runtime.tool_events.append(event)
                    transcript.append({"role": "tool", **event})
                    append_live_event(runtime, format_runtime_event_line(transcript[-1]))
                    if name == "run_tests":
                        grace = runtime.grant_post_success_grace(result)
                        if grace:
                            notice = (
                                "Post-success grace: tests passed near the runtime deadline. "
                                f"Added {grace['added_seconds']}s for final todo/reporting."
                            )
                            transcript.append(
                                {
                                    "role": "runtime_notice",
                                    "message": notice,
                                    "started_at": utc_timestamp(),
                                    "ended_at": utc_timestamp(),
                                }
                            )
                            append_live_event(
                                runtime,
                                (
                                    f"{utc_timestamp()} runtime_notice kind=post_success_grace "
                                    f"added_seconds={grace['added_seconds']} max_seconds={int(grace['max_seconds'])}"
                                ),
                            )
                    flush_live_log()
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": name,
                            "content": json.dumps(result, sort_keys=True),
                        }
                    )
    except AgentRunError as exc:
        append_live_event(runtime, f"{utc_timestamp()} run error error={single_line(str(exc), 300)}")
        flush_live_log(status="error", error=str(exc))
        raise
    except Exception as exc:
        append_live_event(runtime, f"{utc_timestamp()} run error error={single_line(str(exc), 300)}")
        flush_live_log(status="error", error=str(exc))
        raise AgentRunError(str(exc), current_result()) from exc
    finally:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1)
        runtime.cleanup_processes()

    append_live_event(runtime, f"{utc_timestamp()} run success tool_calls={len(tool_events)} cost=${float(total_usage.get('cost') or 0):.8f}")
    flush_live_log(status="success")
    return current_result()


def start_heartbeat_thread(
    runtime: ToolRuntime,
    model: str,
    total_usage: dict[str, Any],
    stop_event: threading.Event,
) -> threading.Thread | None:
    if runtime.heartbeat_seconds <= 0:
        return None

    def emit_heartbeats() -> None:
        while not stop_event.wait(runtime.heartbeat_seconds):
            elapsed = int(time.monotonic() - runtime.started_at)
            append_live_event(runtime, f"{utc_timestamp()} heartbeat source=local model={model} elapsed_seconds={elapsed}")

    thread = threading.Thread(target=emit_heartbeats, name="hooky-agent-heartbeat", daemon=True)
    thread.start()
    return thread


def skill_catalog_message(runtime: ToolRuntime) -> dict[str, Any] | None:
    if not runtime.skills:
        return None
    return {"role": "user", "content": agent_skills.skill_catalog(runtime.skills)}


def preselected_skills_message(runtime: ToolRuntime) -> dict[str, Any] | None:
    if not runtime.preselected_skill_names:
        return None
    activated = []
    errors = []
    for name in runtime.preselected_skill_names:
        try:
            activated.append(runtime.activate_skill({"name": name}))
        except Exception as exc:  # noqa: BLE001 - invalid operator-selected skills should be visible.
            errors.append({"name": name, "error": str(exc)})
    if not activated and not errors:
        return None
    lines = ["Preselected Agent Skills loaded by the harness:"]
    for result in activated:
        lines.append(f"\n--- {result['name']} ({result['skill_path']}) ---")
        if result.get("description"):
            lines.append(str(result["description"]))
        lines.append(str(result["body"]))
        resources = result.get("resources") if isinstance(result.get("resources"), list) else []
        if resources:
            lines.append("\nResources available via read_skill_resource:")
            for resource in resources[:50]:
                if isinstance(resource, dict):
                    lines.append(f"- {resource.get('path')} ({resource.get('bytes')} bytes)")
    if errors:
        lines.append("\nSkill activation errors:")
        for error in errors:
            lines.append(f"- {error['name']}: {error['error']}")
    return {"role": "user", "content": "\n".join(lines)}


def maybe_compact_messages(
    client: Any,
    model: str,
    runtime: ToolRuntime,
    messages: list[Any],
) -> tuple[list[Any], dict[str, Any] | None, dict[str, Any] | None]:
    if not runtime.context_window_tokens:
        return messages, None, None
    estimated_tokens = estimate_tokens(messages)
    threshold_tokens = int(runtime.context_window_tokens * runtime.compaction_threshold)
    if estimated_tokens < threshold_tokens:
        return messages, None, None
    if len(messages) <= runtime.compaction_keep_recent_messages + 2:
        return messages, None, None

    keep_count = max(2, runtime.compaction_keep_recent_messages)
    prefix = messages[:2]
    older = messages[2:-keep_count]
    recent = trim_leading_tool_messages(messages[-keep_count:])
    if not older:
        return messages, None, None

    archive = {
        "reason": "pre_compaction_archive",
        "context_window_tokens": runtime.context_window_tokens,
        "threshold": runtime.compaction_threshold,
        "before_estimated_tokens": estimated_tokens,
        "older_messages_count": len(older),
        "recent_messages_count": len(recent),
        "older_messages": older,
    }

    previous_summary = runtime.anchored_summary
    prompt = compaction_user_prompt(previous_summary, older)
    compaction_model = os.environ.get("COMPACTION_MODEL", model)
    compaction_messages = [
        {"role": "system", "content": compaction_system_prompt(runtime)},
        {"role": "user", "content": prompt},
    ]
    compaction_kwargs = {
        "model": compaction_model,
        "messages": compaction_messages,
        "response_format": structured_response_format("context_compaction", compaction_schema()),
        **model_request_options(compaction_model),
    }
    if model_provider(compaction_model) == model_provider(model):
        completion = client.chat.send(**compaction_kwargs)
    else:
        with model_client(compaction_model) as compaction_client:
            completion = compaction_client.chat.send(**compaction_kwargs)
    content = completion.choices[0].message.content
    if not content:
        return messages, None, archive
    payload = json.loads(content)
    runtime.anchored_summary = payload["summary"]
    summary_messages = [
        {
            "role": "user",
            "content": "Anchored context summary for continuing this agent run:\n\n" + runtime.anchored_summary,
        }
    ]
    active_skill_message = activated_skills_compaction_message(runtime)
    if active_skill_message:
        summary_messages.append(active_skill_message)
    compacted = prefix + summary_messages + recent
    after_tokens = estimate_tokens(compacted)
    event = {
        "reason": "context_threshold",
        "context_window_tokens": runtime.context_window_tokens,
        "threshold": runtime.compaction_threshold,
        "before_estimated_tokens": estimated_tokens,
        "after_estimated_tokens": after_tokens,
        "older_messages_compacted": len(older),
        "recent_messages_kept": len(recent),
        "summary_chars": len(runtime.anchored_summary),
        "usage": response_usage(completion),
    }
    return compacted, event, archive


def activated_skills_compaction_message(runtime: ToolRuntime) -> dict[str, Any] | None:
    if not runtime.activated_skill_names:
        return None
    lines = ["Active Agent Skills that remain loaded after context compaction:"]
    for name in sorted(runtime.activated_skill_names):
        try:
            result = runtime.activate_skill({"name": name})
        except Exception:
            continue
        lines.append(f"\n--- {result['name']} ({result['skill_path']}) ---")
        if result.get("description"):
            lines.append(str(result["description"]))
        lines.append(str(result["body"]))
        resources = result.get("resources") if isinstance(result.get("resources"), list) else []
        if resources:
            lines.append("\nResources available via read_skill_resource:")
            for resource in resources[:50]:
                if isinstance(resource, dict):
                    lines.append(f"- {resource.get('path')} ({resource.get('bytes')} bytes)")
    return {"role": "user", "content": "\n".join(lines)}


def accumulate_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        total[key] = int(total.get(key) or 0) + int(usage.get(key) or 0)
    total["cost"] = round(float(total.get("cost") or 0) + float(usage.get("cost") or 0), 8)

