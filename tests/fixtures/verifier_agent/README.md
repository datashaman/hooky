# Verifier Agent Fixtures

These fixtures evaluate whether the Verifier Agent can independently check a Builder Agent workspace.

Each case is copied into a model-specific eval working folder. The Verifier Agent may inspect and run commands inside that copy, but must not edit project files, tests, dependency manifests, config, or prior-stage artifacts.

