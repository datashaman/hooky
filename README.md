# Hooky

Hooky is an agentic SDLC loop: gather context, negotiate a contract, implement, evaluate, and repeat until the contract passes or the loop exposes the next bottleneck.

The CLI has one public pipeline. The normal command surface is top-level:

```bash
hooky -C /tmp/todomvc init --proposal-file proposal.md
hooky -C /tmp/todomvc run --proposal "Build a TodoMVC app"
hooky watch
hooky status
hooky trace
hooky report
```

Hooky is not published to PyPI yet. To use it from arbitrary project folders,
install the local checkout as an editable tool:

```bash
uv tool install --editable /path/to/hooky
```

For contributor work inside this repository, `uv run hooky ...` still works.
Outside this repository, do not use `uv run hooky` unless that project also
declares Hooky as a dependency.

Without installing, run Hooky from another folder by pointing `uv` at the Hooky
checkout and passing the target workspace explicitly:

```bash
uv run --directory /path/to/hooky hooky -C "$PWD" status
```

`hooky init` initializes loop files and creates a local git baseline by default when the target folder is not already a worktree. Use `--no-git` when Hooky should not touch git setup.

`hooky start` is the background-friendly variant of `run`: it records the workspace in `/tmp/hooky-last-run-path` before starting so `hooky watch` and `hooky status` can keep using the latest run without repeating `-C`.

## Environment

Local model-backed runs require `OPENROUTER_API_KEY`. Copy the root
`.env.example` to an uncommitted `.env`, or export the variables in your shell:

```bash
cp .env.example .env
```

GitHub Actions reads the same contract from repository secrets. At minimum,
configure `OPENROUTER_API_KEY` for the Hooky workflow. Optional model, budget,
web-search, and skill-root variables are listed in `.env.example`.

## Agent Context

Loop roles are separated:

- `planner`: turns the proposal into initial proposal/contract material.
- `generator`: proposes testable done criteria, then changes the workspace during implementation attempts.
- `evaluator`: reviews the contract and implementation attempts. It does not edit the workspace.

The roles have separate prompts and context windows. They communicate through durable files under `.hooky/`, not through hidden in-memory handoffs.

## Local CLI Workflow

Hooky provides a Typer CLI for local workspaces. Commands default to the current directory; use `-C` to run against another workspace.

Start and watch:

```bash
hooky -C /tmp/todomvc start --proposal-file proposal.md
hooky watch
hooky status
```

Run foreground with inline text:

```bash
hooky -C /tmp/todomvc run --proposal "Build a TodoMVC app"
```

Run foreground from stdin:

```bash
hooky -C /tmp/todomvc run <<'PROPOSAL'
Build a TodoMVC app

Use the official TodoMVC behavior and styling conventions.
PROPOSAL
```

Inspect what happened:

```bash
hooky status
hooky watch --no-follow
hooky trace
hooky transcript
hooky inspect
hooky trace-grep TodoMVC
```

Capture or review human-readable evidence:

```bash
hooky evidence init
hooky evidence note "What was checked" --body "Contract, tests, and UI state were reviewed."
hooky evidence exec "npm test" --title "Acceptance tests"
hooky evidence screenshot http://127.0.0.1:5173 --title "TodoMVC initial state"
hooky evidence show
```

Durable loop files:

- `.hooky/runs/<key>/proposal.md`
- `.hooky/runs/<key>/contract.md`
- `.hooky/runs/<key>/feature_list.json`
- `.hooky/runs/<key>/progress.md`
- `.hooky/runs/<key>/log.md`
- `.hooky/runs/<key>/evidence.md` before an attempt exists
- `.hooky/runs/<key>/attempts/<id>/`
- `.hooky/runs/<key>/attempts/<id>/evidence.md`

The default local run key is `local`. GitHub automation uses keys like
`issue-12`, `pr-7`, or `manual-<run-id>`, so independent flows can persist state
without colliding.

For sequence diagrams covering auto-approve, human-in-the-loop, and GitHub event
usage, see [`docs/usage-sequences.md`](docs/usage-sequences.md).

For the model-role tool surface, permissions, and guardrails, see
[`docs/agent-tools.md`](docs/agent-tools.md).

## Agent Skills

Hooky supports agent skills using progressive disclosure. At run start, agents see only a catalog of available skill names and descriptions. They can call `activate_skill` to load a selected `SKILL.md`, and `read_skill_resource` to read specific referenced files from that skill directory.

Skill discovery includes:

- bundled repo skills under `.agents/skills`
- workspace skills under `<workspace>/.agents/skills`
- common user roots `~/.agents/skills`, `~/.codex/skills`, and `~/.claude/skills`
- extra roots from `HOOKY_SKILL_ROOTS`, `AGENT_SKILL_ROOTS`, or `SKILLS_PATH`

Any external skill collection works if it has the shape `<root>/<skill-name>/SKILL.md`:

```bash
HOOKY_SKILL_ROOTS=/path/to/skills hooky -C /tmp/todomvc skills list
HOOKY_SKILL_ROOTS=/path/to/skills hooky -C /tmp/todomvc skills show visual-ui-review
HOOKY_SKILL_ROOTS=/path/to/skills hooky -C /tmp/todomvc start --skill visual-ui-review
```

`--skill` may be repeated. It preloads those selected skill instructions for the run; agents may still activate other catalogued skills dynamically.
