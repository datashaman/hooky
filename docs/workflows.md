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

- **File**: [`src/hooky/cli/commands/listen.py`](../src/hooky/cli/commands/listen.py) (`hooky listen`), sharing its event-derivation logic with `hooky.yml` via [`src/hooky/shared/github_event.py`](../src/hooky/shared/github_event.py).
- **Trigger**: `gh webhook forward` (or an equivalent GitHub webhook relay) streams repo events — issues, PRs, comments, labels — to `hooky listen`'s local HTTP endpoint instead of (or alongside) GitHub Actions. GitHub itself never contacts your machine directly: `gh webhook forward` opens an outbound connection from your machine to a GitHub-hosted relay and forwards deliveries down that connection, so no inbound port needs to be reachable from the internet.
- **Trigger conditions and run_key/proposal derivation match workflow #1 exactly** — `should_trigger_event`/`derive_run_from_event` in `shared/github_event.py` is a direct port of `hooky.yml`'s inline Python, used by both. Keep them in sync if either changes.
- **What's different from workflow #1**: no GitHub-hosted runner, no git publishing. `hooky listen` runs the loop as a `hooky run` subprocess against the local workspace (one at a time, serialized behind a lock) and stops there — it never commits, pushes, or opens a PR. Role timeouts (`runtime/models.py`) use `SIGALRM`, which only works on a process's main thread; shelling out to a subprocess avoids that restriction and avoids mutating this process's environment from a background thread.
- **Why it's useful**: lets a user watch/debug a run with `hooky watch`/`hooky trace` in real time, iterate on the loop or prompts without pushing to GitHub, or use an executor (e.g. local `claude`/`codex`) that isn't available/authorized in CI.
- **Security**: `hooky listen --secret <shared-secret>` (or `HOOKY_WEBHOOK_SECRET`) verifies `X-Hub-Signature-256` before triggering anything; without it, any local process that can reach the bound port can start a run.
