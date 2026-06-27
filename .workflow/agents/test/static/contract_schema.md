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
  "acceptance_criteria_covered": ["string"],
  "acceptance_criteria_uncovered": ["string"],
  "untestable_requirements": ["string"],
  "requires_human_approval": true
}
```

## Required Contract Rules

- `requires_human_approval` must be `true`.
- `test_files` must be non-empty.
- Every approved acceptance criterion must be listed in either `acceptance_criteria_covered` or `acceptance_criteria_uncovered`.
- `acceptance_criteria_uncovered` must be empty for a passing output unless the requirement is genuinely untestable.
- Test file content must be executable test code, not prose.

