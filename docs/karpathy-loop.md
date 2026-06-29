# Karpathy-Style Loop

This note records the intended shape of a simpler loop derived from
`docs/loops-field-notes.md`.

Use this vocabulary consistently when discussing this loop.

## Roles

The loop has three model roles, each with its own context window and system
prompt:

- `planner`: turns vague user input into a problem boundary. It never edits code.
- `generator`: changes the project to satisfy the contract. It does not grade its
  own work.
- `evaluator`: assumes the current attempt is broken, inspects diffs, runs tools,
  looks at visual evidence when relevant, and reports whether the attempt passes.

The `generator` may run tests as part of development, but its test results are
evidence, not authority. The `evaluator` is the authority for whether an attempt
passes.

The `loop-runner` is not a model role. It is harness code. It owns process flow
and decides whether to continue, resume, restart the attempt, restart the
contract, stop at a cap, or ask for human input.

## Contract Negotiation

The `planner` does not write the final grading contract. It writes the problem
boundary. The grading contract is negotiated before implementation:

1. The `planner` writes `planner_spec.md`.
2. The `generator` proposes `contract.md`, describing what done means.
3. The `evaluator` reviews `contract.md` and writes `contract_review.md`.
4. The `generator` revises `contract.md` until the `evaluator` accepts it.
5. The accepted contract is projected into `feature_list.json` as testable
   assertions.
6. Only after contract acceptance may the `generator` write implementation.

The original planner output is the boundary. The negotiated contract is the
grading instrument. The `generator` may propose criteria, but cannot approve
them. The `evaluator` may reject weak, vague, missing, or untestable criteria
before any code is written.

## Disk State

The loop must be resumable from durable files, not conversation context. The
primary state should be small enough to read directly:

```text
.workflow/loop/
  planner_spec.md
  contract_review.md
  feature_list.json
  contract.md
  progress.md
  log.md
```

Supporting artifacts such as patches, screenshots, test logs, and transcripts
may live under attempt-specific folders, but those artifacts are evidence. They
are not the primary loop state.

`log.md` is append-only. Entries should use a stable heading format:

```text
## [YYYY-MM-DD] op | title
```

## Resume And Restart

- `resume` handles accidental interruption: process disconnect, context loss,
  compaction, or a new model session. It keeps the current attempt and reads
  the disk state.
- `restart-attempt` handles a bad trajectory: the `evaluator` decides the
  current attempt has gone sideways. It discards the bad attempt while preserving
  durable loop memory.

The loop distinguishes three actions:

- `resume`: keep the current attempt and continue from disk state.
- `restart-attempt`: discard generated changes from the current attempt, keep
  `planner_spec.md`, `contract.md`, `contract_review.md`, `feature_list.json`,
  `progress.md`, and `log.md`.
- `restart-contract`: abandon or revise the contract. This requires the `planner`
  or a human because the problem statement changed.

The loop must not silently wipe the whole workspace. If a clean rebuild is
needed, the `loop-runner` records the reason in `log.md` and preserves the durable
loop files unless a human explicitly deletes them.

## Control Rule

Roles may recommend outcomes, but only the `loop-runner` performs control-flow
actions. In particular:

- The `generator` cannot declare success.
- The `evaluator` cannot edit code.
- The `evaluator` can recommend `restart-attempt` or `restart-contract`.
- The `loop-runner` applies the recommendation, enforces attempt limits, and logs
  the decision.
