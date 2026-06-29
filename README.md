# Hooky

Hooky is an agentic SDLC pipeline driven from issue events and system-owned change proposals.

## First Flow: Issue To Spec

Label an issue with `sdlc:spec` to start the Spec Agent flow.

The workflow:

1. Reads the labeled GitHub issue.
2. Generates specification artifacts only.
3. Writes auditable spec artifacts.
4. Waits for human approval before tests are generated.

The Spec Agent never edits production code.

## Change Proposals And Git

Hooky treats the Builder stage's main handoff as a change proposal. Locally, that proposal is a system-owned artifact with git metadata, not a model-authored pull request:

- `.workflow/artifacts/change-proposals/<task-id>/summary.md`
- `.workflow/artifacts/change-proposals/<task-id>/patch.diff`
- `.workflow/artifacts/change-proposals/<task-id>/proposal.json`

Hooky snapshots the project immediately before Builder runs and derives the proposal from the Builder delta. `proposal.json` records the suggested branch name, current branch, changed files, Builder summary, test claims, and hosted PR fields. `hosted_pr_url` is `null` unless a later GitHub integration actually publishes a remote pull request.

`hooky init` initializes a local git repository by default when the target folder is not already a git worktree. Use `--no-git` for workspaces where Hooky should not touch git setup.

## Agent Context

Agents run with both static and dynamic context.

Static project context:

- `AGENTS.md` describes repository architecture, patterns, and agent stage boundaries.
- Eval project fixtures, such as `tests/fixtures/projects/todomvc/`, provide isolated project context for agent evals.

Shared static runtime context:

- `.workflow/agents/common/static/runtime.md`

Static Spec Agent context:

- `.workflow/agents/spec/static/system.md`
- `.workflow/agents/spec/static/contract_schema.md`
- `.workflow/agents/spec/static/quality_bar.md`

Legacy Test Agent context:

- `.workflow/agents/test/static/system.md`
- `.workflow/agents/test/static/contract_schema.md`
- `.workflow/agents/test/static/quality_bar.md`

Static Builder Agent context:

- `.workflow/agents/builder/static/system.md`
- `.workflow/agents/builder/static/contract_schema.md`
- `.workflow/agents/builder/static/quality_bar.md`

Dynamic Spec Agent context:

- GitHub issue event data in production.
- Eval fixture issue data during local eval.

Each generated spec artifact folder includes:

- `dynamic_context.json`
- `context_snapshot.md`

Those files make the exact runtime context auditable.

## GitHub Setup

Create this label in the repository:

- `sdlc:spec`

Required repository secret:

- `OPENROUTER_API_KEY`

Selected model:

- `.workflow/agents/spec/selected_model.json`

`OPENROUTER_MODEL` may still be used as a temporary override, but normal runs use the selected model file.

There is no non-AI generation path. If OpenRouter is unavailable or the key is missing, the workflow fails without creating spec artifacts.

## Local CLI Workflow

Hooky provides a Typer CLI for local workspaces. Commands default to the current directory; use `-C` to run against another workspace.

The standard pipeline is:

1. Spec Agent
2. Human approval, or `--auto-approve` for local full runs
3. Builder Agent, including the task-local TDD loop
4. Verifier Agent
5. Eval Agent
6. System-owned change proposal

For the simple local loop, start the pipeline and watch the latest started workspace with stable commands:

```bash
uv run hooky -C /tmp/todomvc start
uv run hooky watch
uv run hooky status
```

`hooky start` records the workspace in `/tmp/hooky-last-run-path`. `hooky watch` reads that file and follows the pipeline event log, so the viewer command stays the same across runs.

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

```bash
uv run hooky init
uv run hooky task create --title "Implement TodoMVC" --body-file /tmp/todomvc-issue.md
uv run hooky run spec
uv run hooky approve spec
uv run hooky run builder
uv run hooky run verifier
uv run hooky run eval
uv run hooky status
uv run hooky trace pipeline --follow
uv run hooky trace builder
uv run hooky trace builder --follow
uv run hooky report
```

For a separate workspace:

```bash
uv run hooky -C /tmp/todomvc init
uv run hooky -C /tmp/todomvc task create --title "Implement TodoMVC" --body-file /tmp/todomvc-issue.md
uv run hooky -C /tmp/todomvc start
```

Task input is process input, not project context. Keep `--body-file` outside the workspace, or pass the body with `--body`; Hooky stores it under `.workflow/tasks/...` for the Spec Agent. The CLI rejects workspace-local body files so later agents cannot discover the original issue text as an ordinary project file.

The CLI stores task state under `.workflow/tasks/<task-id>/state.json` and tracks the current task in `.workflow/state.json`, so normal stage commands do not need task ids or artifact paths. `hooky status` shows the workspace, task, pipeline state, last event, next action, per-stage state, errors, and report paths. `hooky watch` follows the latest started workspace's stage-level pipeline log. `hooky trace <stage>` shows the readable runtime timeline for one stage and can be run while an agent is still in progress. `hooky trace <stage> --follow` tails that stage's append-only `runtime_events.log`; `--tail-path` prints the log path for external `tail -f`.

Hooky reports, contracts, context snapshots, and runtime transcripts stay under `.workflow`.

The Builder Agent owns the task-local TDD loop. It writes executable tests at project-native paths, writes production implementation, and may create or update the project toolchain files needed to run and verify that implementation. If the approved spec is invalid or unimplementable, Builder should stop with `tests_passing=false` and record the evidence instead of weakening the acceptance target or churning dependencies. After the Builder returns, Hooky creates the local change proposal artifact from git status and diff evidence.

## Local Spec Agent Eval

Run the Spec Agent against a moderately complex fixture. The runner fetches current OpenRouter model metadata, sorts configured candidate models by estimated cost, then moves up that priced ladder until one passes deterministic checks plus AI judge eval:

```bash
export OPENROUTER_API_KEY=...
python3 scripts/eval_spec_agent.py
```

The OpenRouter API supplies the model set. Filtering, profile, and cost-estimation assumptions live in `.workflow/model_ladder.json`.
Default profiles are configured per agent:

- Spec Agent: `balanced_general`
- Test Agent: `coding`
- Builder Agent: `coding`
- Verifier Agent: `cheap_general`
- Eval Agent: `reasoning`

The runner pushes these filters to `GET /api/v1/models` when no explicit `--models` override is used:

- `sort`, currently `pricing-low-to-high`
- `supported_parameters`, currently `response_format,structured_outputs` so agent and judge calls can use strict JSON Schema output
- `context`, for minimum context length
- optional `category`, with OpenRouter's current values: `programming`, `roleplay`, `marketing`, `marketing/seo`, `technology`, `science`, `translation`, `legal`, `finance`, `health`, `trivia`, `academia`
- optional `q`, for search terms such as `code` or `coder`

`category=programming` is intentionally limited to coding-oriented profiles because it excludes known passing general-purpose Spec/Test models.
OpenRouter does not allow `category` and `supported_parameters` in the same `/models` request, so the runner sends `category` server-side and then enforces structured-output support locally.

Available ladder profiles:

- `cheap_general`: lowest-cost general text models with strict structured output support.
- `balanced_general`: cheap general ladder plus Haiku-class anchor models for non-code product/spec work.
- `coding`: structured-output models from OpenRouter's `programming` category.
- `reasoning`: wider structured-output ladder with stronger anchor models for hard judgement or synthesis tasks.

Reasoning-capable models are expanded into separate eval variants when OpenRouter metadata advertises `reasoning.supported_efforts`.
Variants are tried from lowest to highest effort using this order: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`.
The runtime passes `reasoning: {"effort": "<level>", "exclude": true}` so reasoning can affect output quality without storing reasoning text in artifacts.

Tool-loop agents compact context when estimated prompt size approaches the selected model's context window.
The eval harness passes OpenRouter's `context_length` metadata into the runtime; normal runs use the selected-model file when it includes `context_length`.
By default, compaction triggers at 65% of the context window, keeps the newest 16 messages verbatim, and summarizes older messages with `.workflow/agents/common/static/compaction.md`.
Before compaction rewrites model context, the runtime archives the exact older message slice to `pre_compaction_archives.json`.
Compaction calls are included in cost accounting and logged to `compaction_events.json`.

Useful compaction overrides:

```bash
AGENT_COMPACTION_THRESHOLD=0.75
AGENT_COMPACTION_KEEP_RECENT_MESSAGES=24
COMPACTION_MODEL=openai/gpt-4.1-mini
```

Each run writes artifacts and a costed report under `.workflow/eval-runs/<timestamp>/`.
Completed model attempts are cached under `.workflow/eval-cache/`, so rerunning the loop will not spend credits on the same fixture/model/eval combination again.
When a model passes eval, the runner updates `.workflow/agents/spec/selected_model.json` unless `--no-update-selected-model` is passed.

Generate a static HTML pipeline health report from local eval reports:

```bash
uv run python scripts/generate_eval_report.py
```

The report is written to `.workflow/eval-runs/report.html` and scans existing `report.json` files directly, so it also works before `.workflow/eval-runs/index.json` has been created. It includes per-attempt filesystem snapshots with noisy dependency/build folders omitted.

Useful overrides:

```bash
python3 scripts/eval_spec_agent.py --print-ladder
python3 scripts/eval_spec_agent.py --profile balanced_general --print-ladder
python3 scripts/eval_spec_agent.py --profile coding --print-ladder
python3 scripts/eval_spec_agent.py --max-models 40
python3 scripts/eval_spec_agent.py --models openai/gpt-4.1-mini openai/gpt-4.1
python3 scripts/eval_spec_agent.py --judge-model openai/gpt-4.1
python3 scripts/eval_spec_agent.py --no-cache
python3 scripts/eval_spec_agent.py --no-update-selected-model
```

## Local Test Agent Eval

Run the Test Agent against an approved TodoMVC spec fixture:

```bash
export OPENROUTER_API_KEY=...
uv run python scripts/eval_test_agent.py
```

The TodoMVC project context lives under `tests/fixtures/projects/todomvc/`. During eval, that folder is copied into each model-specific working folder:

`.workflow/eval-runs/test-agent/<timestamp>/<model>/`

Generated test files are written at project-native paths inside that copied working folder. Test Agent reports and runtime logs are written under `.workflow/artifacts/test-agent/`.

When a model passes eval, the runner updates `.workflow/agents/test/selected_model.json` unless `--no-update-selected-model` is passed.

## Local Builder Agent Eval

Run the Builder Agent against the approved TodoMVC tests produced by the Test Agent:

```bash
export OPENROUTER_API_KEY=...
uv run python scripts/eval_builder_agent.py
```

The Builder fixture lives under `tests/fixtures/builder_agent/todomvc_approved_tests/`. It is a full working folder snapshot after the Test Agent stage: project context, approved tests at project-native paths, and Test Agent sidecar artifacts.

During eval, each model receives an isolated copy under:

`.workflow/eval-runs/builder-agent/<timestamp>/<model>/`

The Builder Agent may write production implementation files in that copied folder only. The eval hashes approved tests and Test Agent artifacts before generation and fails the attempt if any of those files change.
