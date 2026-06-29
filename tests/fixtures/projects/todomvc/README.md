# TodoMVC Fixture Project

This is a runnable React/Vite starter fixture for evaluating the Test Agent.

It is intentionally separate from the Hooky repository. During eval, this folder is copied into each model-specific working folder under `.workflow/eval-runs/test-agent/<timestamp>/<model>/`.

The fixture supplies a coherent project shell: `index.html`, `src/main.jsx`, `src/App.jsx`, CSS, package scripts, and Playwright config. It is not a completed TodoMVC implementation. The Test Agent should generate executable tests against this project shape without repairing production code.
