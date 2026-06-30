# TodoMVC Agent Context

This fixture represents a React TodoMVC project used by the Hooky loop.

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
- Tests should be written so a normal React TodoMVC implementation can satisfy them rather than test-only hooks.
- Do not choose a different test runner unless the approved spec or project files explicitly replace Playwright.

## Available Local Tooling

- `python3` is available for simple static hosting through `python3 -m http.server`.
- `node`, `npm`, and `npx` are available for project-defined JavaScript commands.
- `git` is available for repository inspection when needed.
- The project declares React, ReactDOM, Vite, the Vite React plugin, and Playwright in `package.json`.
- `npm run dev` starts the Vite development server on port 4173.
- `npm test` maps to `playwright test`.
- The Playwright config starts the Vite dev server and uses `http://127.0.0.1:4173` as `baseURL`.
- The generator may run setup commands and may add test-only dependencies if the accepted contract requires missing test tooling.
- The generator may run tests during development, but the evaluator decides whether the attempt passes.
- The generator must report every dependency manifest or lockfile change in its attempt report.
- The generator must not add production implementation dependencies unless the accepted contract or project context explicitly defines them as part of the target.

## Project Boundaries

- The generator may create project-native tests and implementation files after contract acceptance.
- The generator must not introduce unreported dependency changes.
- The generator must preserve the accepted contract criteria exactly.
