# Workflows

A running list of the automations that trigger Hooky, or that Hooky itself runs on a schedule/event. Each entry names the trigger, what it does, and where it lives. Add new entries here as they're introduced.

## 1. Hooky GitHub Actions loop

- **File**: [`.github/workflows/hooky.yml`](../.github/workflows/hooky.yml)
- **Triggers**: `workflow_dispatch` (manual, optional `proposal` input); `issues` labeled `hooky:run`; `pull_request` labeled `hooky:run`; `issue_comment` created and starting with `/hooky`.
- **Identity**: each trigger derives a `run_key` (`issue-<n>`, `pr-<n>`, or `manual-<run_id>`) from the event payload. All durable state, the eventual branch (`hooky/<run_key>`), and the PR title key off this — not the CI `run_id`, so reruns on the same issue/PR resume the same branch/PR instead of spawning new ones.
- **What it does**: checks out the repo fresh, builds a proposal from the event payload, runs `hooky run --run-key <run_key> --proposal-file event-proposal.md` (the planner → contract negotiation → generator/evaluator attempt loop), then packages the resulting working-tree diff (excluding `.hooky/`) into a commit on `hooky/<run_key>`, opens or reuses a PR, uploads `.hooky/runs/<run_key>/` as a workflow artifact, and comments the result back on the originating issue or PR. This applies uniformly across triggers, including `pull_request` events — the commit/PR step no longer skips them, so a `hooky:run`-labeled PR still gets its resulting diff committed and published rather than discarded when the runner tears down.
- **Failure modes**: retriggering the same issue/PR doesn't resume prior loop state — each run is a fresh checkout, so contract negotiation restarts and the final push force-overwrites `hooky/<run_key>`. If `gh pr create`/`gh pr list` fails after a passing loop, the issue/PR comment says so explicitly and points at the pushed branch rather than reporting a bare "passed" with no way to find the work.
- **See also**: [`docs/usage-sequences.md`](usage-sequences.md) for the full sequence diagram ("GitHub Event Flow").

## 2. Locally forwarded GitHub events

- **Not shipped by Hooky** — a pattern a user assembles themselves for local development, not a file in this repo.
- **Trigger**: `gh webhook forward` (or an equivalent GitHub webhook relay) streams repo events — issues, PRs, comments, labels — to a local HTTP listener instead of (or alongside) GitHub Actions.
- **What's different from workflow #1**: no GitHub-hosted runner. A local listener/script receives the same event payload `hooky.yml` parses, derives the same `run_key`/proposal from it, and calls `hooky run`/`hooky start` directly against the user's local checkout — with whatever `HOOKY_EXECUTOR` (native/codex/claude) and secrets they have configured locally. The loop, contract negotiation, and attempt state machine underneath are identical; only who drives the trigger and where the working tree lives changes.
- **Why it's useful**: lets a user watch/debug a run with `hooky watch`/`hooky trace` in real time, iterate on the loop or prompts without pushing to GitHub, or use an executor (e.g. local `claude`/`codex`) that isn't available/authorized in CI.
- **Gap**: there's currently no bundled listener script that replicates `hooky.yml`'s event-to-run_key/proposal parsing locally — a user doing this today re-implements that piece themselves.
