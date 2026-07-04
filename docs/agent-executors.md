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

Registration is additive with one exception: the server name itself, `hooky`,
is reserved. If a project's `.mcp.json` (or a user's global config) already
defines its own server named `hooky`, Hooky's injected entry wins outright
for that invocation — confirmed empirically for both codex (`-c` overrides
the existing `mcp_servers.hooky` table) and claude (`--mcp-config`'s `hooky`
entry is used, not merged with the project's). The user's config file itself
is never modified; their own `hooky` entry is just shadowed for that one run.
This only matters if a server happens to be named exactly `hooky`, which is
unlikely outside of this exact scenario.

The server itself is spawned by codex/claude (or your own command, for
shell), not by Hooky; `hooky mcp-serve --config <path>` reads
`executor/<role>/mcp_config.json` (written before the executor process
starts) to reconstruct a `ToolRuntime` scoped to that role's workspace,
timeouts, and evidence directory.

## MCP: Native Executor as an MCP Client

The sections above cover Hooky *serving* its own tools over MCP to codex and
claude. The `native` executor needs the opposite direction: it's Hooky's own
in-process OpenRouter/Ollama-driven agent, so it has no external tool config
of its own to fall back on the way codex/claude do — the only tools it ever
sees are whatever `ToolRuntime` hands it.

To close that gap, `native` reads the same `.mcp.json` file from the
workspace root that codex/claude already read
(`{"mcpServers": {"name": {"command", "args", "env"}}}`), connects to each
declared server, and merges their tools into the same `tools()`/
`tool_handlers()` surface the built-in tools use — so the model sees no
difference between a Hooky tool and an MCP one, and existing `enabled_tools`
allowlists (e.g. a role restricted to `["final_report"]`) govern MCP tools
identically, with no special-casing.

Tool names are synthesized as `mcp__<server-name>__<tool-name>` (matching the
convention Claude Code itself uses for its own MCP tool calls), so a tool
named `echo` from a server named `fixture` becomes `mcp__fixture__echo`. If a
synthesized name collides with one of Hooky's own built-in tool names, the
MCP tool is dropped (not exposed, never shadows the built-in) and a warning
is recorded — this should be structurally rare given the `mcp__` prefix, but
is checked defensively.

Connections are made once per role invocation (lazily, on first use, so
roles with no `.mcp.json` or a fully restrictive `enabled_tools` allowlist
pay no cost) and torn down after the role finishes, success or failure. Each
server runs on its own background thread so tool calls dispatch through the
async MCP client without making the rest of Hooky's runtime asynchronous.

**Trust boundary**: an MCP server's tools run outside Hooky's own guarded
handlers — none of `ToolRuntime`'s protected-path snapshotting,
`write_enabled` gate, or `bash_command_validator` apply to a third-party MCP
tool call, since Hooky is just forwarding the call to that server's own
process. This is the same trust level codex/claude already extend to
`.mcp.json` servers; Hooky has no additional way to sandbox an arbitrary
third-party subprocess beyond what the OS/user's own environment provides.

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
