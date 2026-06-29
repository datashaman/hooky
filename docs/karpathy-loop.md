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

1. The `planner` writes the problem boundary into `contract.md`.
2. The `generator` revises `contract.md` with proposed done criteria.
3. The `evaluator` reviews `contract.md`, records objections in `log.md`, and
   updates `progress.md` with the current contract-negotiation status.
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
  feature_list.json
  progress.md
  contract.md
  log.md
```

Supporting artifacts such as patches, screenshots, test logs, and transcripts
may live under attempt-specific folders, but those artifacts are evidence. They
are not the primary loop state.

`contract.md` carries both the problem boundary and the accepted grading
contract. `feature_list.json` is the structured checklist projected from the
accepted contract. `progress.md` records the current loop state and next action.
`log.md` records append-only decisions, objections, restarts, and notable events.

`log.md` is append-only. Entries should use a stable heading format:

```text
## [YYYY-MM-DD] op | title
```

## Subjective Scoring

Subjective quality is gradable only when the taste rubric is written into
`contract.md` before grading starts. The `evaluator` scores against that rubric;
the `generator` does not invent or approve the taste criteria.

For UI or product work, the default subjective axes are:

- `design`
- `originality`
- `craft`
- `functionality`

The contract should assign weights to those axes when subjective quality matters.
The weights must sum to `1.0`. The `evaluator` returns a score between `0.0` and
`1.0`, plus a short explanation of the gap between the attempt and the rubric.

Calibration examples belong in `contract.md`, not in conversation context. A
useful rubric names three positive references and three negative references, with
short notes about what each reference demonstrates.

If subjective quality matters but `contract.md` does not include a rubric, the
`evaluator` should not invent taste after the fact. It should either grade only
objective functionality or recommend `restart-contract` so the rubric can be
written explicitly.

## Trace Reading

Raw transcripts are supporting evidence. They do not replace the four durable
state files, but they must be available when the loop behaves badly.

Each attempt should keep grep-friendly raw transcripts for each model role:

```text
.workflow/loop/attempts/001/traces/
  planner.jsonl
  generator.jsonl
  evaluator.jsonl
```

Read traces before changing prompts, role instructions, or loop policy. A new
experiment is not a substitute for understanding where the previous attempt's
judgment diverged from the intended procedure.

When a prompt or loop policy changes because of trace evidence, record the
change in `log.md` with a reference to the attempt, role, and trace location.
The `evaluator` may cite transcript moments when explaining a bad trajectory.

## OpenTelemetry

OpenTelemetry is useful as supporting observability, not as primary loop memory.
The `loop-runner` may emit OpenTelemetry data for correlation, dashboards, and
machine-readable trace inspection. The model roles should not need to understand
OpenTelemetry.

Local runs must not require an external collector. If OpenTelemetry is enabled,
write a local artifact beside the raw transcripts:

```text
.workflow/loop/attempts/001/otel/
  spans.jsonl
```

Recommended span shape:

- one trace per loop run
- one span per role invocation
- child spans for tool calls
- events for `contract_proposed`, `contract_rejected`, `attempt_started`,
  `restart_attempt`, `restart_contract`, `evaluation_failed`, and
  `evaluation_passed`

Useful attributes include:

- `attempt`
- `role`
- `model`
- `cost`
- `tokens`
- `contract_version`
- `decision`

OpenTelemetry must not become the source of truth. The durable loop memory
remains `feature_list.json`, `progress.md`, `contract.md`, and `log.md`.

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
  `feature_list.json`, `progress.md`, `contract.md`, and `log.md`.
- `restart-contract`: abandon or revise the contract. This requires the `planner`
  or a human because the problem statement changed.

The loop must not silently wipe the whole workspace. If a clean rebuild is
needed, the `loop-runner` records the reason in `log.md` and preserves the durable
loop files unless a human explicitly deletes them.

## Restart Policy

Restart is expected behavior, not exceptional failure handling. The loop should
prefer a clean new attempt over patching a bad approach until the project becomes
hard to reason about.

The `evaluator` may recommend `restart-attempt` when:

- the implementation is accumulating patches without converging
- the `generator` is working around the contract instead of satisfying it
- the diff is larger or stranger than the task warrants
- repeated fixes create contradictory structure
- visual or behavioral failures suggest the approach is wrong

On `restart-attempt`, the `loop-runner` discards generated attempt changes and
starts a new attempt from `contract.md`, `feature_list.json`, `progress.md`, and
`log.md`.

The `loop-runner` asks for human input only on `restart-contract`, when the
`evaluator` says the contract itself is wrong, incomplete, or misleading.

## Control Rule

Roles may recommend outcomes, but only the `loop-runner` performs control-flow
actions. In particular:

- The `generator` cannot declare success.
- The `evaluator` cannot edit code.
- The `evaluator` can recommend `restart-attempt` or `restart-contract`.
- The `loop-runner` applies the recommendation, enforces attempt limits, and logs
  the decision.

## Harness Restraint

The harness exists only to enforce behavior the model cannot reliably provide on
its own. It should not grow monotonically.

Every loop rule, tool, file, or policy should have a clear reason to exist: the
failure it prevents, the evidence it preserves, or the decision it makes
auditable. If that reason disappears as models improve, delete the rule.

Prefer the smallest loop that works:

- three model roles
- four durable state files
- raw traces as supporting evidence
- optional OpenTelemetry as supporting observability
- a small `loop-runner` that owns control flow

Re-read the harness after model changes, not only after failed runs. A mechanism
that was load-bearing for one model may become friction for the next. The loop
should be allowed to get smaller.
