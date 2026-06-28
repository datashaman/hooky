# Test Agent Contract Shape

Return only JSON matching this shape:

```json
{
  "summary": "string",
  "test_files": [
    {
      "path": "string",
      "purpose": "string",
      "content": "string"
    }
  ],
  "fixtures": [
    {
      "path": "string",
      "purpose": "string",
      "content": "string"
    }
  ],
  "coverage_targets": ["string"],
  "test_execution_checks": [
    {
      "command": "string",
      "status": "passed|failed|skipped",
      "reason": "string"
    }
  ],
  "dependency_changes": [
    {
      "package": "string",
      "scope": "test|dev|runtime",
      "reason": "string",
      "files_changed": ["string"]
    }
  ],
  "acceptance_criteria_covered": ["string"],
  "acceptance_criteria_uncovered": ["string"],
  "untestable_requirements": ["string"],
  "requires_human_approval": true
}
```

## Required Contract Rules

- `requires_human_approval` must be `true`.
- `test_files` must be non-empty.
- `test_execution_checks` must list every setup, syntax, discovery, or test command the agent ran.
- `dependency_changes` must list every dependency addition, removal, or version change caused by the Test Agent.
- The Test Agent may add test tooling or test-only dependencies required by the approved test strategy.
- The Test Agent must not add production implementation dependencies unless the approved spec or project context explicitly defines them as part of the test target.
- Failing generated tests should be reported as `failed`; the Test Agent must not fix production implementation.
- A command that cannot run because project dependencies are still missing must be reported as `skipped` with a missing-dependency reason.
- A command may be reported as `passed` only when the matching shell command actually exited successfully.
- Every approved acceptance criterion must be listed in either `acceptance_criteria_covered` or `acceptance_criteria_uncovered`.
- `acceptance_criteria_uncovered` must be empty for a passing output unless the requirement is genuinely untestable.
- Test file content must be executable test code, not prose.
