# Hooky Agent Context

Hooky is an agentic SDLC loop. The public pipeline is the loop pipeline.

The loop uses three roles, three context windows, and three system prompts:

1. Planner turns a proposal into a contract boundary and never edits product code.
2. Generator proposes testable done criteria, then changes the workspace during implementation attempts.
3. Evaluator reviews contracts and implementation attempts, uses trace/test/visual evidence, and never edits code.

The loop sequence is:

1. Gather proposal and repository context.
2. Reason by writing or revising `.workflow/loop/contract.md`.
3. Act by implementing an attempt in the workspace.
4. Verify by evaluating the attempt against contract, tests, traces, and visual evidence.
5. Repeat by continuing, restarting the attempt, restarting the contract, or surfacing a human review point.

See `docs/loop-model.md` for the loop model.

## Architecture Rules

- Each role has one responsibility.
- Each role consumes durable files from `.workflow/loop/` and the current workspace state.
- Each role emits a typed report, contract update, or attempt artifact.
- Roles must not perform another role's responsibility.
- Agents must operate within an explicit cost budget.
- Agent outputs must be observable and auditable.

## Current Repository Shape

- `.workflow/agents/common/` stores static runtime context shared by all agents.
- `.workflow/agents/` stores static agent context, templates, and eval configuration.
- `.workflow/loop/` stores durable loop state, contract, progress, log, attempt reports, and traces.
- `.workflow/eval-runs/` stores generated eval outputs and reports.
- `.workflow/eval-cache/` stores cached eval attempts.
- `scripts/` stores local executable agent harnesses.
- `tests/fixtures/` stores eval fixtures.

## Role Boundaries

- Planner writes proposal/contract material only.
- Generator may write implementation files and project-native tests only after the evaluator accepts the contract.
- Evaluator may read files, run tests, launch browsers, inspect screenshots, and write evaluator reports under `.workflow/loop/attempts/<id>/`.
- Evaluator must not edit implementation or test files.
- System code owns `.workflow/loop/state.json`, log append operations, attempt bookkeeping, and command orchestration.

## Required Runtime Basics

Each role must receive:

- a working folder for the project
- read/write file tools
- list/find/grep file tools
- bash
- todo read/write tools

Roles must use the todo list for substantive work.
