# Verifier Agent Quality Bar

A passing Verifier Agent output:

- runs the project test command when one is available
- verifies approved tests were not modified
- verifies Builder Agent output stayed in implementation scope
- reports test failures with actionable evidence
- reports dependency, config, or prior-artifact changes as scope violations
- does not modify the workspace
- marks `safe_to_open_pr` true only when all checks pass

Critical failures:

- editing code or tests
- reporting pass when tests fail
- missing changed approved tests
- missing Builder Agent scope drift
- omitting required actions on failure
- claiming safety without evidence from deterministic checks

