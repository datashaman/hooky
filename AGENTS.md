# Hooky Agent Context

Hooky is an agentic SDLC pipeline. Work flows through explicit stages:

1. Spec Agent
2. Human approval
3. Test Agent
4. Human approval
5. Builder Agent
6. Verifier Agent
7. Eval Agent
8. System-owned change proposal, then optional hosted pull request or merge

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
- The Test Agent writes executable tests only.
- The Builder Agent writes production implementation only after approved tests exist.
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
