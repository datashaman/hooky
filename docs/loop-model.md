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

- `planner`: writes the initial proposal/contract boundary and never touches implementation code.
- `generator`: proposes done criteria during negotiation, then writes implementation/tests during attempts.
- `evaluator`: rejects weak contracts, grades attempts, reads traces and screenshots, and never edits code.

## State

Loop state is deliberately small and lives on disk:

- `.hooky/proposal.md`
- `.hooky/contract.md`
- `.hooky/feature_list.json`
- `.hooky/progress.md`
- `.hooky/log.md`
- `.hooky/state.json`
- `.hooky/attempts/<id>/`

Example:

```json
{
  "status": "attempt-running",
  "contract_accepted": true,
  "current_attempt": "001",
  "attempts": [
    {"id": "001", "status": "running", "path": ".hooky/attempts/001"}
  ]
}
```

Runtime logs, transcripts, tool events, OpenTelemetry-style spans, evaluator reports, and visual evidence live under the active attempt directory. Debug commands read those files directly instead of reconstructing what might have happened from model summaries.
