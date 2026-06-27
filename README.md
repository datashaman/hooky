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

Static Spec Agent context:

- `.workflow/agents/spec/static/system.md`
- `.workflow/agents/spec/static/contract_schema.md`
- `.workflow/agents/spec/static/quality_bar.md`

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

Optional repository variable:

- `OPENROUTER_MODEL`, defaulting to `openai/gpt-4.1-mini`

There is no non-AI generation path. If OpenRouter is unavailable or the key is missing, the workflow fails without creating spec artifacts.

## Local Spec Agent Eval

Run the Spec Agent against a moderately complex fixture. The runner fetches current OpenRouter model metadata, sorts configured candidate models by estimated cost, then moves up that priced ladder until one passes deterministic checks plus AI judge eval:

```bash
export OPENROUTER_API_KEY=...
python3 scripts/eval_spec_agent.py
```

The OpenRouter API supplies the model set. Filtering and cost-estimation assumptions live in `.workflow/model_ladder.json`.

Each run writes artifacts and a costed report under `.workflow/eval-runs/<timestamp>/`.
Completed model attempts are cached under `.workflow/eval-cache/`, so rerunning the loop will not spend credits on the same fixture/model/eval combination again.

Useful overrides:

```bash
python3 scripts/eval_spec_agent.py --print-ladder
python3 scripts/eval_spec_agent.py --max-models 40
python3 scripts/eval_spec_agent.py --models openai/gpt-4.1-mini openai/gpt-4.1
python3 scripts/eval_spec_agent.py --judge-model openai/gpt-4.1
python3 scripts/eval_spec_agent.py --no-cache
```
