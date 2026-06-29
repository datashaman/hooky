# Hooky Agent Context

Hooky is an agentic SDLC loop. Work flows through explicit phases:

1. Gather
2. Reason
3. Act
4. Verify
5. Repeat

Agents are implementations of phases, not the top-level architecture. The current default agent mapping is:

1. Spec Agent
2. Human approval
3. Builder Agent
4. Verifier Agent
5. Eval Agent
6. System-owned change proposal, then optional hosted pull request or merge

The legacy Test Agent still exists for compatibility and eval work, but it is not part of the default pipeline. Builder owns the task-local TDD loop: it creates or updates executable tests from the approved spec, then implements production code until the suite passes.

See `docs/loop-model.md` for the phase model and stage compatibility mapping.

## Architecture Rules

- Each agent has one responsibility.
- Each agent consumes approved artifacts from the previous stage.
- Each agent emits a typed report or contract.
- Agents must not perform another agent's responsibility.
- Agents must operate within an explicit cost budget.
- Agent outputs must be observable and auditable.

## Current Repository Shape

- `.workflow/agents/common/` stores static runtime context shared by all agents.
- `.workflow/agents/` stores static agent context, templates, and eval configuration.
- `.workflow/eval-runs/` stores generated eval outputs and reports.
- `.workflow/eval-cache/` stores cached eval attempts.
- `.workflow/artifacts/change-proposals/` stores local git proposal artifacts created by Hooky after Builder runs.
- `scripts/` stores local executable agent harnesses.
- `tests/fixtures/` stores eval fixtures.

## Stage Boundaries

- The Spec Agent writes specification artifacts only.
- The Builder Agent writes task-local executable tests and production implementation from an approved spec.
- Hooky, not the Builder Agent, derives the git change proposal from the resulting workspace.
- Verifier and Eval agents do not edit code or tests.

## Required Runtime Basics

Each agent must receive:

- a working folder for the project/task
- read/write file tools
- list/find/grep file tools
- bash
- todo read/write tools

Agents must use the todo list for substantive work.
