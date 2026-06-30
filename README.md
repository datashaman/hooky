# Hooky

Hooky is an agentic SDLC loop: gather context, negotiate a contract, implement, evaluate, and repeat until the contract passes or the loop exposes the next bottleneck.

The CLI has one public pipeline. The normal command surface is top-level:

```bash
uv run hooky -C /tmp/todomvc init --proposal-file proposal.md
uv run hooky -C /tmp/todomvc run --proposal "Build a TodoMVC app"
uv run hooky watch
uv run hooky status
uv run hooky trace
uv run hooky report
```

`hooky init` initializes loop files and creates a local git baseline by default when the target folder is not already a worktree. Use `--no-git` when Hooky should not touch git setup.

`hooky start` is the background-friendly variant of `run`: it records the workspace in `/tmp/hooky-last-run-path` before starting so `hooky watch` and `hooky status` can keep using the latest run without repeating `-C`.

## Agent Context

Loop roles are separated:

- `planner`: turns the proposal into the initial contract boundary.
- `generator`: proposes testable done criteria, then changes the workspace during implementation attempts.
- `evaluator`: reviews the contract and implementation attempts. It does not edit the workspace.

The roles have separate prompts and context windows. They communicate through durable files under `.hooky/`, not through hidden in-memory handoffs.

## Local CLI Workflow

Hooky provides a Typer CLI for local workspaces. Commands default to the current directory; use `-C` to run against another workspace.

Start and watch:

```bash
uv run hooky -C /tmp/todomvc start --proposal-file proposal.md
uv run hooky watch
uv run hooky status
```

Run foreground with inline text:

```bash
uv run hooky -C /tmp/todomvc run --proposal "Build a TodoMVC app"
```

Run foreground from stdin:

```bash
uv run hooky -C /tmp/todomvc run <<'PROPOSAL'
Build a TodoMVC app

Use the official TodoMVC behavior and styling conventions.
PROPOSAL
```

Inspect what happened:

```bash
uv run hooky status
uv run hooky watch --no-follow
uv run hooky trace
uv run hooky transcript
uv run hooky inspect
uv run hooky trace-grep TodoMVC
```

Durable loop files:

- `.hooky/proposal.md`
- `.hooky/contract.md`
- `.hooky/feature_list.json`
- `.hooky/progress.md`
- `.hooky/log.md`
- `.hooky/attempts/<id>/`

## Agent Skills

Hooky supports agent skills using progressive disclosure. At run start, agents see only a catalog of available skill names and descriptions. They can call `activate_skill` to load a selected `SKILL.md`, and `read_skill_resource` to read specific referenced files from that skill directory.

Skill discovery includes:

- bundled repo skills under `.agents/skills`
- workspace skills under `<workspace>/.agents/skills`
- common user roots `~/.agents/skills`, `~/.codex/skills`, and `~/.claude/skills`
- extra roots from `HOOKY_SKILL_ROOTS`, `AGENT_SKILL_ROOTS`, or `SKILLS_PATH`

Any external skill collection works if it has the shape `<root>/<skill-name>/SKILL.md`:

```bash
HOOKY_SKILL_ROOTS=/path/to/skills uv run hooky -C /tmp/todomvc skills list
HOOKY_SKILL_ROOTS=/path/to/skills uv run hooky -C /tmp/todomvc skills show visual-ui-review
HOOKY_SKILL_ROOTS=/path/to/skills uv run hooky -C /tmp/todomvc start --skill visual-ui-review
```

`--skill` may be repeated. It preloads those selected skill instructions for the run; agents may still activate other catalogued skills dynamically.
