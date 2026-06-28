# TodoMVC Agent Context

This fixture represents a React TodoMVC project used to evaluate SDLC agents.

## Application Shape

- The app is a TodoMVC-compatible React single page application.
- Use a Vite-style React shape unless the approved spec says otherwise:
  - `index.html` as the browser entry document.
  - `src/main.jsx` as the React mount entry point.
  - `src/App.jsx` for TodoMVC application behavior.
  - `src/styles.css` or equivalent CSS for TodoMVC presentation.
- Mount the React app into `#root`.
- The UI uses the standard TodoMVC DOM conventions and class names.
- State is stored in browser `localStorage`.
- Routing uses hash fragments:
  - `#/` for all todos
  - `#/active` for active todos
  - `#/completed` for completed todos

## Expected Test Style

- Use Playwright end-to-end tests as the project-native test strategy.
- Put executable browser tests under `tests/` using the existing `playwright.config.cjs`.
- Use CommonJS test files unless existing project files indicate otherwise:
  - `const { test, expect } = require('@playwright/test');`
- Navigate to `/` through Playwright's configured `baseURL`; do not hardcode absolute local filesystem paths or arbitrary server ports.
- Prefer behavior-focused browser tests over component tests or private unit tests.
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
- Tests should be written so the Builder Agent can satisfy them with a normal React TodoMVC implementation rather than test-only hooks.
- Do not choose a different test runner unless the approved spec or project files explicitly replace Playwright.

## Available Local Tooling

- `python3` is available for simple static hosting through `python3 -m http.server`.
- `node`, `npm`, and `npx` are available for project-defined JavaScript commands.
- `git` is available for repository inspection when needed.
- The project declares `@playwright/test` in `package.json` and `npm test` maps to `playwright test`.
- The Playwright config starts a local static server on port 4173 and uses `http://127.0.0.1:4173` as `baseURL`.
- Dependency installation is outside the Test Agent role. If dependencies are missing in a particular working folder, report discovery as skipped instead of installing them.

## Project Boundaries

- The Test Agent may create test files and fixtures only.
- The Test Agent must not create or modify production implementation files.
- The Test Agent must not introduce new dependencies.
- The Test Agent must preserve the approved acceptance criteria exactly.
