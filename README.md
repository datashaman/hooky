# Hooky

Hooky is an agentic SDLC pipeline driven from GitHub issue and pull request events.

## First Flow: Issue To Spec

Label an issue with `sdlc:spec` to start the Spec Agent flow.

The workflow:

1. Reads the labeled GitHub issue.
2. Generates specification artifacts only.
3. Commits those artifacts to a branch named `sdlc/spec-issue-<number>`.
4. Opens a pull request for human approval.
5. Comments on the original issue with the PR link.

The Spec Agent never edits production code.

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

Static Test Agent context:

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

```bash
uv run hooky init
uv run hooky task create --title "Implement TodoMVC" --body-file issue.md
uv run hooky run spec
uv run hooky approve spec
uv run hooky run test
uv run hooky approve test
uv run hooky run builder
uv run hooky run verifier
uv run hooky run eval
uv run hooky status
uv run hooky trace test
uv run hooky report
```

For a separate workspace:

```bash
uv run hooky -C /tmp/todomvc init
uv run hooky -C /tmp/todomvc task create --title "Implement TodoMVC" --body-file issue.md
uv run hooky -C /tmp/todomvc run pipeline --auto-approve
```

The CLI stores task state under `.workflow/tasks/<task-id>/state.json` and tracks the current task in `.workflow/state.json`, so normal stage commands do not need task ids or artifact paths. `hooky status` shows per-stage state, errors, report paths, and approved test file paths. `hooky trace <stage>` shows the readable tool-call timeline for a stage.

The Test Agent writes executable tests and fixtures at project-native paths chosen from the workspace context. Hooky reports, contracts, context snapshots, and runtime transcripts stay under `.workflow`.

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

The report is written to `.workflow/eval-runs/report.html` and scans existing `report.json` files directly, so it also works before `.workflow/eval-runs/index.json` has been created. It includes per-attempt filesystem snapshots with noisy dependency folders such as `node_modules` omitted.

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
