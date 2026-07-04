# Agent Executors

Hooky role execution is pluggable. The loop still has the same roles and durable
files; the executor only decides how each role prompt is run.

## Executors

- `native`: default. Uses Hooky's built-in OpenRouter/Ollama tool runtime.
- `codex`: runs `codex exec` and lets Codex use its own config, tools, MCPs,
  skills, and sandbox behavior.
- `claude`: runs `claude --print` and lets Claude Code use its own config,
  tools, MCPs, plugins, and settings.
- `shell`: runs a custom command from `HOOKY_EXECUTOR_COMMAND`.

Select an executor with CLI or environment:

```bash
hooky run --executor codex --proposal-file proposal.md
hooky start --executor claude --proposal "Build a TodoMVC app"
HOOKY_EXECUTOR=shell HOOKY_EXECUTOR_COMMAND='my-agent $input $output' hooky run
```

The CLI option wins over `HOOKY_EXECUTOR`. If neither is set, Hooky uses
`native`.

## File Contract

External executors receive a Markdown prompt through stdin and as a file:

```text
.hooky/runs/<key>/.../traces/executor/<role>/input.md
```

They must write the role report JSON object to:

```text
.hooky/runs/<key>/.../traces/executor/<role>/output.json
```

Hooky validates that JSON with the same role validators used by the native
runtime. Conversation text alone is not accepted as the report.

Each invocation also writes:

- `stdout.log`
- `stderr.log`
- `metadata.json`
- `last_message.txt` for Codex
- `debug.log` for Claude

## MCP: Hooky Tools Inside External Executors

The `codex`, `claude`, and `shell` executors all run their own file/bash
tools (or, for `shell`, whatever your own command provides), but none of
them know how to gather Hooky-specific evidence (browser screenshots, layout
metrics, run_tests/run_lint result parsing) on their own. To close that gap,
every external-executor invocation gets a per-run MCP server config,
served by `hooky mcp-serve`, exposing this curated tool subset over stdio:

- `detect_project_environment`
- `run_tests`, `run_lint`, `latest_test_failure_context`
- `capture_visual_snapshot`, `interact_and_snapshot`
- `append_evidence_note`, `append_evidence_command`,
  `append_evidence_screenshot`, `append_evidence_interaction`

File-editing, bash, git, and reporting tools (`read_files`/`write_files`/
`bash`/`git_status`/`git_diff`/`final_report`/...) are deliberately excluded:
codex and claude already have their own equivalents for those (a thin
read-only wrapper around `git diff` adds nothing they can't already do with
their own bash tool), and the JSON-report file contract above is unchanged.
Only tools that do something codex/claude can't already do themselves -
parsing test output into structured pass/fail evidence, driving a headless
browser for screenshots/interaction - are exposed this way.

Each call is dispatched through the same `ToolRuntime.run_tool` the native
executor uses, so the same evidence tools produce the same evidence shape
regardless of which executor ran the role. Calls are appended to
`executor/<role>/mcp_tool_events.jsonl` and folded back into that attempt's
`tool_events.json` after the executor exits, so the `capture_visual_snapshot`/
`interact_and_snapshot` evidence gates in `cli/validation.py` see them exactly
as they would from the native executor.

For codex and claude, Hooky registers the server itself, per-invocation, never
persisted to the user's global config:

- For codex, via `-c mcp_servers.hooky.command=...` /
  `-c mcp_servers.hooky.args=...` overrides (nothing is written to
  `~/.codex/config.toml`).
- For claude, via a generated `executor/<role>/claude_mcp_servers.json` passed
  with `--mcp-config` (the user's other MCP servers still load; this is
  additive, not `--strict-mcp-config`).

For `shell`, Hooky can't register anything on your command's behalf (it
doesn't know if your agent even speaks MCP), so it just always writes the
config file and exposes its path as `$mcp_config` (see the Shell section
below) — wiring it up is opt-in.

The server itself is spawned by codex/claude (or your own command, for
shell), not by Hooky; `hooky mcp-serve --config <path>` reads
`executor/<role>/mcp_config.json` (written before the executor process
starts) to reconstruct a `ToolRuntime` scoped to that role's workspace,
timeouts, and evidence directory.

## Codex

Default command shape:

```bash
codex exec -C <workspace> \
  --json \
  --output-last-message <trace>/executor/<role>/last_message.txt \
  --dangerously-bypass-approvals-and-sandbox \
  -
```

Hooky does not pass a model or reasoning effort by default. Codex should use the
user's normal `~/.codex/config.toml` and project config.

Optional overrides:

```bash
HOOKY_CODEX_MODEL=gpt-5.5
HOOKY_CODEX_REASONING_EFFORT=high
```

## Claude

Default command shape:

```bash
claude --print \
  --verbose \
  --output-format stream-json \
  --debug-file <trace>/executor/<role>/debug.log \
  --permission-mode bypassPermissions \
  --setting-sources user,project,local
```

Hooky does not pass a model or effort by default. Claude should use the user's
normal settings.

Optional overrides:

```bash
HOOKY_CLAUDE_MODEL=opusplan
HOOKY_CLAUDE_EFFORT=medium
```

## Shell

`shell` exists for local experiments and test adapters. The command is expanded
with these placeholders:

- `$workspace`
- `$input`
- `$output`
- `$executor_dir`
- `$mcp_config`: path to the same MCP server config codex/claude use (see
  above). Hooky always writes this file for the shell executor; your command
  isn't required to use it, but if your own agent understands MCP you can
  register `hooky mcp-serve --config $mcp_config` with it the same way codex
  and claude do, and get the same evidence tools and tool_events.json fold-in.

Example:

```bash
HOOKY_EXECUTOR=shell \
HOOKY_EXECUTOR_COMMAND='python ./run-role.py $input $output' \
hooky run --proposal-file proposal.md
```
