# TodoMVC Agent Context

This fixture represents a plain browser TodoMVC project used to evaluate SDLC agents.

## Application Shape

- The app is a TodoMVC-compatible single page application.
- The UI uses the standard TodoMVC DOM conventions and class names.
- State is stored in browser `localStorage`.
- Routing uses hash fragments:
  - `#/` for all todos
  - `#/active` for active todos
  - `#/completed` for completed todos

## Expected Test Style

- Prefer behavior-focused browser or DOM tests.
- Tests should interact through visible TodoMVC controls instead of private implementation functions.
- Tests may assume standard selectors from TodoMVC markup, such as:
  - `.new-todo`
  - `.todo-list`
  - `.toggle-all`
  - `.toggle`
  - `.destroy`
  - `.edit`
  - `.todo-count`
  - `.clear-completed`
  - `.filters a`
- Tests should cover persistence by inspecting `localStorage` only at user-visible workflow boundaries.
- Tests should avoid depending on internal source file names.

## Project Boundaries

- The Test Agent may create test files and fixtures only.
- The Test Agent must not create or modify production implementation files.
- The Test Agent must not introduce new dependencies.
- The Test Agent must preserve the approved acceptance criteria exactly.

