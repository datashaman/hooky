# Hooky Loop Model

Hooky is organized around phases first and agents second.

Agents are role-specific workers. Phases describe what the loop is doing.

## Phases

1. Gather
   - Collect issue input, repository context, prior artifacts, traces, screenshots, test output, and runtime facts.
   - This phase is mostly deterministic tooling.
2. Reason
   - Turn gathered context into a contract, diagnosis, strategy, or next action.
   - The Spec Agent currently implements the default Reason phase for new work.
3. Act
   - Change the workspace or run setup commands under the approved contract.
   - The Builder Agent currently implements the default Act phase.
4. Verify
   - Prove the result against tests, artifacts, contracts, and visual evidence.
   - The Verifier Agent and deterministic tools implement this phase.
5. Repeat
   - Decide whether the loop passes, fails, resumes from a phase, restarts, or needs human input.
   - The Eval Agent and remediation controller implement this phase.

## Stage Compatibility

The current stage names remain compatibility handles:

- `spec` maps to `reason`
- `builder` maps to `act`
- `verifier` maps to `verify`
- `eval` maps to `repeat`

The legacy `test` stage maps to `act` because executable test creation is now part of Builder's TDD loop.

## State

Task state keeps both views:

- `stage_status` preserves existing command and artifact compatibility.
- `phase_status` exposes the loop sequence.

Example:

```json
{
  "phase_status": {
    "gather": {"status": "passed", "source": "workspace_task_context"},
    "reason": {"status": "passed", "stage": "spec", "agent": "spec"},
    "act": {"status": "running", "stage": "builder", "agent": "builder"},
    "verify": {"status": "not-run"},
    "repeat": {"status": "not-run"}
  }
}
```

Runtime logs include `phase=<name>` on stage events. Remediation plans include both root-cause stage and root-cause phase.
