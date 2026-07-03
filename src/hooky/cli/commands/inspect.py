"""Loop debugging commands: runtime-log, transcript, stall, context, inspect, trace-grep, harness-review."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from hooky import roles, runtime
from hooky.cli.app import app
from hooky.cli.commands.setup import follow_runtime_log
from hooky.cli.loop_state import (
    append_loop_log,
    ensure_loop_initialized,
    latest_loop_attempt_id,
    loop_visible_bottleneck,
    read_loop_state,
)
from hooky.cli.paths import (
    loop_attempt_dir,
    loop_contract_path,
    loop_dir,
    loop_feature_list_path,
    loop_log_path,
    loop_progress_path,
    read_json,
    selected_run_key,
    workspace_from_ctx,
)
from hooky.cli.transcript import (
    load_loop_transcript,
    loop_context_stats,
    loop_debug_root,
    transcript_reasoning_text,
    transcript_role,
    transcript_text,
    transcript_tool_calls,
)
from hooky.cli.validation import (
    loop_attempt_requires_reference_visual_evidence,
    loop_attempt_tool_events,
    loop_attempt_visual_snapshot_count,
)


@app.command("runtime-log")
def loop_runtime_log(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt, or contract-level runtime if none exists.")] = None,
    follow: Annotated[bool, typer.Option("--follow/--no-follow", "-f", help="Follow the runtime event log.")] = False,
    tail_path: Annotated[bool, typer.Option("--tail-path", help="Only print the runtime event log path.")] = False,
    lines: Annotated[int, typer.Option(help="Number of lines to show when not following.")] = 80,
) -> None:
    """Show the live model/tool runtime log for the loop or an attempt."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    path = root / "runtime_events.log"
    if tail_path:
        typer.echo(path)
        return
    if follow:
        follow_runtime_log(workspace, "loop", state, path)
        return
    if not path.exists():
        raise typer.BadParameter(f"runtime log not found: {path}")
    content = path.read_text(encoding="utf-8").splitlines()
    typer.echo("\n".join(content[-lines:]))


@app.command("transcript")
def loop_transcript(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt, or contract-level runtime if none exists.")] = None,
    role: Annotated[str, typer.Option(help="Role filter: all, system, user, assistant, or tool.")] = "all",
    last: Annotated[int, typer.Option(help="Number of matching transcript entries to show.")] = 20,
    full: Annotated[bool, typer.Option("--full", help="Print full message text instead of a preview.")] = False,
    reasoning: Annotated[bool, typer.Option("--reasoning", help="Include attached assistant reasoning traces.")] = False,
) -> None:
    """Show actual persisted model conversation entries for loop debugging."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    entries = load_loop_transcript(root)
    allowed_roles = {"all", "system", "user", "assistant", "tool"}
    if role not in allowed_roles:
        raise typer.BadParameter("role must be one of: all, system, user, assistant, tool")
    selected = [entry for entry in entries if role == "all" or transcript_role(entry) == role]
    for index, entry in list(enumerate(selected, 1))[-last:]:
        entry_role = transcript_role(entry)
        calls = transcript_tool_calls(entry)
        kind = str(entry.get("kind") or "message")
        text = transcript_text(entry).strip()
        reasoning_text = transcript_reasoning_text(entry).strip()
        reasoning_suffix = f" reasoning_chars={len(reasoning_text)}" if reasoning_text else ""
        typer.echo(f"## {index}. {entry_role} kind={kind} tool_calls={len(calls)}{reasoning_suffix}")
        if calls:
            names = [str((call.get("function") or {}).get("name") or call.get("name") or "unknown") for call in calls if isinstance(call, dict)]
            typer.echo("tools: " + ", ".join(names))
        if entry_role == "tool":
            typer.echo("tool: " + str(entry.get("name") or "unknown"))
            result = entry.get("result")
            typer.echo(runtime.single_line(json.dumps(result, sort_keys=True) if isinstance(result, dict) else str(result), 1200))
        elif text:
            typer.echo(text if full else runtime.single_line(text, 1200))
        else:
            typer.echo("[empty]")
        if reasoning and reasoning_text:
            typer.echo("")
            typer.echo("reasoning:")
            typer.echo(reasoning_text if full else runtime.single_line(reasoning_text, 1200))
        typer.echo("")


@app.command("stall")
def loop_stall(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt.")] = None,
    last: Annotated[int, typer.Option(help="Number of recent assistant entries to inspect.")] = 20,
) -> None:
    """Summarize recent no-tool assistant responses and fake final_report text."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    entries = load_loop_transcript(root)
    assistants = [entry for entry in entries if transcript_role(entry) == "assistant"]
    recent = assistants[-last:]
    no_tool = [entry for entry in recent if not transcript_tool_calls(entry)]
    fake_final = [entry for entry in no_tool if "final_report" in transcript_text(entry)]
    trailing_no_tool = 0
    for entry in reversed(assistants):
        if transcript_tool_calls(entry):
            break
        trailing_no_tool += 1
    typer.echo(f"transcript: {root / 'runtime_transcript.json'}")
    typer.echo(f"assistant_entries_checked: {len(recent)}")
    typer.echo(f"no_tool_assistant_entries: {len(no_tool)}")
    typer.echo(f"fake_final_report_text_entries: {len(fake_final)}")
    typer.echo(f"trailing_no_tool_assistant_entries: {trailing_no_tool}")
    if no_tool:
        typer.echo("")
        typer.echo("recent no-tool assistant messages:")
        for entry in no_tool[-5:]:
            text = transcript_text(entry).strip()
            typer.echo("- " + (runtime.single_line(text, 500) if text else "[empty]"))


@app.command("context")
def loop_context(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt, or contract-level runtime if none exists.")] = None,
) -> None:
    """Show live-context hygiene signals beside persisted transcript artifacts."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    stats = loop_context_stats(root)
    typer.echo(f"debug_root: {root}")
    typer.echo(f"runtime_transcript: {stats['files']['runtime_transcript']}")
    typer.echo(f"tool_events_file: {stats['files']['tool_events']}")
    typer.echo(f"compaction_events_file: {stats['files']['compaction_events']}")
    typer.echo(f"pre_compaction_archives_file: {stats['files']['pre_compaction_archives']}")
    typer.echo(f"transcript_entries: {stats['transcript_entries']}")
    typer.echo(f"assistant_entries: {stats['assistant_entries']}")
    typer.echo(f"no_tool_assistant_entries: {stats['no_tool_assistant_entries']}")
    typer.echo(f"trailing_no_tool_assistant_entries: {stats['trailing_no_tool_assistant_entries']}")
    typer.echo(f"tool_events: {stats['tool_events']}")
    typer.echo(f"compactions: {stats['compactions']}")
    typer.echo(f"pre_compaction_archives: {stats['pre_compaction_archives']}")
    typer.echo(f"archived_older_messages: {stats['archived_older_messages']}")
    typer.echo("tool_schema_order: " + ", ".join(stats["tool_schema_order"]))


@app.command("inspect")
def loop_inspect(
    ctx: typer.Context,
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt.")] = None,
    last: Annotated[int, typer.Option(help="Number of recent transcript entries to summarize.")] = 8,
) -> None:
    """Summarize status, runtime log, transcript, tool use, and stall signals."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    attempt_id = attempt or latest_loop_attempt_id(state) or "contract"
    typer.echo(f"loop: {loop_dir(workspace)}")
    typer.echo(f"run_key: {selected_run_key(workspace)}")
    typer.echo(f"status: {state.get('status')}")
    typer.echo(f"attempt: {attempt_id}")
    typer.echo(f"runtime_log: {root / 'runtime_events.log'}")
    typer.echo(f"transcript: {root / 'runtime_transcript.json'}")
    typer.echo(f"tool_events: {root / 'tool_events.json'}")
    report_path = loop_attempt_dir(workspace, attempt_id) / "evaluator_report.json" if attempt_id != "contract" else None
    if report_path and report_path.exists():
        report = read_json(report_path)
        typer.echo(f"evaluator_status: {report.get('status')}")
        typer.echo(f"recommendation: {report.get('recommendation')}")
        typer.echo(f"bottleneck: {report.get('bottleneck') or 'none'}")
        typer.echo(f"score: {report.get('score')}")
    try:
        entries = load_loop_transcript(root)
    except typer.BadParameter:
        entries = []
    if entries:
        stats = loop_context_stats(root)
        typer.echo(f"transcript_entries: {stats['transcript_entries']}")
        typer.echo(f"assistant_entries: {stats['assistant_entries']}")
        typer.echo(f"no_tool_assistant_entries: {stats['no_tool_assistant_entries']}")
        typer.echo(f"trailing_no_tool_assistant_entries: {stats['trailing_no_tool_assistant_entries']}")
        typer.echo("")
        typer.echo("recent:")
        for entry in entries[-last:]:
            role = transcript_role(entry)
            calls = transcript_tool_calls(entry)
            text = transcript_text(entry).strip()
            preview = runtime.single_line(text, 300) if text else "[empty]"
            suffix = f" tools={len(calls)}" if role == "assistant" else ""
            typer.echo(f"- {role}{suffix}: {preview}")
    else:
        typer.echo("transcript_entries: 0")
    tool_events_path = root / "tool_events.json"
    if tool_events_path.exists():
        events = read_json(tool_events_path)
        if isinstance(events, list):
            names: dict[str, int] = {}
            for event in events:
                if not isinstance(event, dict):
                    continue
                name = str(event.get("name") or "unknown")
                names[name] = names.get(name, 0) + 1
            if names:
                typer.echo("")
                typer.echo("tools:")
                for name, count in sorted(names.items()):
                    typer.echo(f"- {name}: {count}")


@app.command("trace-grep")
def loop_trace_grep(
    ctx: typer.Context,
    pattern: Annotated[str, typer.Argument(help="Case-insensitive text to search for in loop transcripts and runtime logs.")],
    attempt: Annotated[str | None, typer.Option(help="Attempt id. Defaults to active/latest attempt.")] = None,
    lines: Annotated[int, typer.Option(help="Maximum matching lines to print.")] = 20,
) -> None:
    """Search persisted loop debug artifacts without ad hoc shell commands."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_debug_root(workspace, state, attempt)
    needle = pattern.lower()
    matches = 0
    files = [
        root / "runtime_events.log",
        root / "runtime_transcript.json",
        root / "tool_events.json",
    ]
    files.extend(sorted(root.glob("*.jsonl")))
    for path in files:
        if not path.exists():
            continue
        for index, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if needle not in line.lower():
                continue
            typer.echo(f"{path}:{index}: {runtime.single_line(line, 1000)}")
            matches += 1
            if matches >= lines:
                return
    if matches == 0:
        typer.echo("no matches")


@app.command("harness-review", hidden=True)
def loop_harness_review(ctx: typer.Context, write: Annotated[bool, typer.Option(help="Write .hooky/harness_review.md.")] = True) -> None:
    """Review the loop harness against the Karpathy loop principles."""
    workspace = workspace_from_ctx(ctx)
    ensure_loop_initialized(workspace)
    state = read_loop_state(workspace)
    root = loop_dir(workspace)
    files = {
        "feature_list": loop_feature_list_path(workspace).exists(),
        "progress": loop_progress_path(workspace).exists(),
        "contract": loop_contract_path(workspace).exists(),
        "log": loop_log_path(workspace).exists(),
    }
    attempts = state.get("attempts") if isinstance(state.get("attempts"), list) else []
    latest = latest_loop_attempt_id(state)
    trace_root = loop_attempt_dir(workspace, latest) / "traces" if latest else root
    trace_files = [trace_root / "runtime_events.log", trace_root / "runtime_transcript.json", trace_root / "tool_events.json"]
    visible_bottleneck = loop_visible_bottleneck(state)
    contract_text = loop_contract_path(workspace).read_text(encoding="utf-8", errors="ignore") if loop_contract_path(workspace).exists() else ""
    taste_rubric_present = roles.taste_rubric_is_substantive(contract_text)
    reference_visual_required = loop_attempt_requires_reference_visual_evidence(workspace)
    reference_visual_count = loop_attempt_visual_snapshot_count(loop_attempt_tool_events(workspace, latest)) if latest else 0
    context_stats = loop_context_stats(trace_root)
    has_transcript = bool(context_stats["transcript_entries"])
    no_tool_entries = int(context_stats["no_tool_assistant_entries"])
    compactions = int(context_stats["compactions"])
    archives = int(context_stats["pre_compaction_archives"])
    checks = [
        ("loop_procedure", True, "loop run drives planner, generator, evaluator, and control flow"),
        ("role_separation", True, "planner/generator/evaluator are separate model roles"),
        ("durable_state", all(files.values()), ", ".join(f"{name}={exists}" for name, exists in files.items())),
        ("contract_negotiation", bool(state.get("contract_accepted")) or state.get("status") in {"contract-rejected", "restart-contract"}, f"status={state.get('status')}"),
        ("restart_paths", True, "restart-attempt and restart-contract commands exist; review attempt history for use"),
        ("subjective_scoring", taste_rubric_present, "substantive Taste Rubric present in contract.md" if taste_rubric_present else "missing substantive Taste Rubric in contract.md"),
        (
            "reference_visual_states",
            not reference_visual_required or reference_visual_count >= 2,
            f"snapshots={reference_visual_count}" if reference_visual_required else "not required",
        ),
        ("trace_reading", any(path.exists() for path in trace_files), f"trace_root={trace_root}"),
        (
            "context_transcript",
            has_transcript,
            f"transcript_entries={context_stats['transcript_entries']}",
        ),
        (
            "context_no_tool_drift",
            no_tool_entries <= 5,
            f"no_tool_assistant_entries={no_tool_entries} trailing={context_stats['trailing_no_tool_assistant_entries']}",
        ),
        (
            "context_compaction_archives",
            compactions == 0 or archives >= compactions,
            f"compactions={compactions} pre_compaction_archives={archives}",
        ),
        (
            "tool_prefix_stability",
            bool(context_stats["tool_schema_order"]),
            "tool_schema_order=" + ",".join(context_stats["tool_schema_order"][:6]) + ",...",
        ),
        ("bottleneck_visible", bool(visible_bottleneck), f"bottleneck={visible_bottleneck or 'none'}"),
        ("harness_restraint", True, "manual review required; delete rules whose failure mode no longer exists"),
    ]
    lines = ["# Loop Harness Review", "", f"- workspace: {workspace}", f"- status: {state.get('status')}", f"- attempts: {len(attempts)}", ""]
    for name, passed, detail in checks:
        mark = "pass" if passed else "gap"
        lines.append(f"- {mark}: {name} - {detail}")
    gaps = [name for name, passed, _detail in checks if not passed]
    lines.extend(["", "## Recommendation", ""])
    if gaps:
        lines.append("Address gaps: " + ", ".join(gaps))
    else:
        lines.append("No structural gaps detected. Read traces before adding more harness.")
    output = "\n".join(lines) + "\n"
    if write:
        path = root / "harness_review.md"
        path.write_text(output, encoding="utf-8")
        append_loop_log(workspace, "harness-review", "reviewed loop harness", f"report: {path.relative_to(workspace).as_posix()}")
        typer.echo(f"report: {path}")
    typer.echo(output.rstrip())
