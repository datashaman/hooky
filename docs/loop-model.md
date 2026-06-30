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

For typical run sequences, including auto-approve, human-in-the-loop, and GitHub
event-triggered flows, see [`usage-sequences.md`](usage-sequences.md).
