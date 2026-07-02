# Hooky Loop Model

Hooky is organized around the loop first and roles second.

Roles are planner, generator, and evaluator. Phases describe what the loop is doing at a given moment.

## Phases

1. Gather
   - Collect issue input, repository context, prior artifacts, traces, screenshots, test output, and runtime facts.
   - This phase is mostly deterministic tooling.
2. Reason
   - Turn gathered context into a contract, diagnosis, strategy, or next action.
   - Planner and evaluator contract review implement the contract side of this phase.
3. Act
   - Change the workspace or run setup commands under the approved contract.
   - Generator implements this phase after the contract is accepted.
4. Verify
   - Prove the result against tests, artifacts, contracts, and visual evidence.
   - Evaluator plus deterministic tools implement this phase.
5. Repeat
   - Decide whether the loop passes, fails, resumes from a phase, restarts, or needs human input.
   - Evaluator recommendations and loop orchestration implement this phase.

## Roles

- `planner`: writes initial proposal/contract material and never touches implementation code.
- `generator`: proposes done criteria during negotiation, then writes implementation/tests during attempts.
- `evaluator`: rejects weak contracts, grades attempts, reads traces and screenshots, and never edits code.

See [`agent-tools.md`](agent-tools.md) for the role tool surface and filesystem
guardrails.

## State

Loop state is deliberately small and lives on disk:

- `.hooky/runs/<key>/proposal.md`
- `.hooky/runs/<key>/contract.md`
- `.hooky/runs/<key>/feature_list.json`
- `.hooky/runs/<key>/progress.md`
- `.hooky/runs/<key>/log.md`
- `.hooky/runs/<key>/state.json`
- `.hooky/runs/<key>/attempts/<id>/`

The default local key is `local`. Event-driven automation should use stable keys
derived from the work item, such as `issue-12` or `pr-7`.

Example:

```json
{
  "status": "attempt-running",
  "contract_accepted": true,
  "current_attempt": "001",
  "attempts": [
    {"id": "001", "status": "running", "path": ".hooky/runs/issue-12/attempts/001"}
  ]
}
```

Runtime logs, transcripts, tool events, OpenTelemetry-style spans, evaluator reports, and visual evidence live under the active attempt directory. Debug commands read those files directly instead of reconstructing what might have happened from model summaries.
Human-readable proof artifacts live in `evidence.md`; command output and
screenshots referenced by that report are captured by Hooky evidence tools.

## Context Architecture

Hooky keeps three related records with different jobs:

- Live model context is the message list sent to the current model role. It
  includes the role system prompt, the user task, assistant messages, tool calls,
  tool results, runtime nudges, activated skill instructions, and any attached
  visual evidence. It is performance-sensitive and may be compacted.
- Runtime transcripts are append-only audit records written under the active
  trace directory. They include assistant messages even when `tool_calls=0`.
  They are never treated as a source of truth for continuing the model session;
  they exist for debugging, harness review, and post-run inspection.
- Durable loop files under `.hooky/runs/<key>/` hold proposal, contract,
  feature list, progress, log, state, attempts, evidence, and reports. These are
  the resumable state of the loop.

To preserve prompt-cache behavior and make traces comparable, a role run should
keep its stable prefix stable: same model, same current working directory, same
role system prompt shape, same tool schema ordering, and same sandbox/policy
surface. New runtime facts should be appended as later messages or durable files,
not by rewriting earlier context.

Compaction is allowed only for live model context. A compaction pass must retain:

- the original system/user prefix;
- an anchored summary with objective, current state, decisions, files/artifacts,
  tool failures, todo state, and next actions;
- active skill instructions;
- recent assistant/tool exchanges;
- all append-only transcript and tool-event artifacts on disk.

After compaction, read-before-write observations are intentionally invalidated.
Agents must call `read_files` again before overwriting or line-editing existing
files.

For typical run sequences, including auto-approve, human-in-the-loop, and GitHub
event-triggered flows, see [`usage-sequences.md`](usage-sequences.md).
