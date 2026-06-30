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

The roles have separate prompts and context windows. They communicate through durable files under `.workflow/loop/`, not through hidden in-memory handoffs.

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

- `.workflow/loop/proposal.md`
- `.workflow/loop/contract.md`
- `.workflow/loop/feature_list.json`
- `.workflow/loop/progress.md`
- `.workflow/loop/log.md`
- `.workflow/loop/attempts/<id>/`

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

## Legacy Model Eval Scripts

These scripts remain for evaluating older role prompts and fixtures. They are not the public runtime pipeline and are not invoked by `hooky run`.

### Spec Role Eval

Run the older spec role against a moderately complex fixture. The runner fetches current OpenRouter model metadata, sorts configured candidate models by estimated cost, then moves up that priced ladder until one passes deterministic checks plus AI judge eval:

```bash
export OPENROUTER_API_KEY=...
python3 scripts/eval_spec_agent.py
```

The OpenRouter API supplies the model set. Filtering, profile, and cost-estimation assumptions live in `.workflow/model_ladder.json`.
Default profiles are configured for legacy eval roles:

- Spec role: `balanced_general`
- Test role fixture: `coding`
- Builder role fixture: `coding`
- Verifier role fixture: `cheap_general`
- Eval role fixture: `reasoning`

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

Generate a static HTML model-eval report from local eval reports:

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

### Test Role Fixture Eval

Run the legacy test role fixture against an approved TodoMVC spec fixture:

```bash
export OPENROUTER_API_KEY=...
uv run python scripts/eval_test_agent.py
```

The TodoMVC project context lives under `tests/fixtures/projects/todomvc/`. During eval, that folder is copied into each model-specific working folder:

`.workflow/eval-runs/test-agent/<timestamp>/<model>/`

Generated test files are written at project-native paths inside that copied working folder. Test role reports and runtime logs are written under `.workflow/artifacts/test-agent/`.

When a model passes eval, the runner updates `.workflow/agents/test/selected_model.json` unless `--no-update-selected-model` is passed.

### Builder Role Fixture Eval

Run the legacy builder role fixture against the approved TodoMVC tests produced by the test role fixture:

```bash
export OPENROUTER_API_KEY=...
uv run python scripts/eval_builder_agent.py
```

The builder fixture lives under `tests/fixtures/builder_agent/todomvc_approved_tests/`. It is a full working folder snapshot with project context, approved tests at project-native paths, and test-role sidecar artifacts.

During eval, each model receives an isolated copy under:

`.workflow/eval-runs/builder-agent/<timestamp>/<model>/`

The builder role may write production implementation files in that copied folder only. The eval hashes approved tests and test-role artifacts before generation and fails the attempt if any of those files change.
