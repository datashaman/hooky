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

Example:

```bash
HOOKY_EXECUTOR=shell \
HOOKY_EXECUTOR_COMMAND='python ./run-role.py $input $output' \
hooky run --proposal-file proposal.md
```
