# Verifier Agent Output Contract

Return a single JSON object with this structure:

```json
{
  "status": "pass|fail",
  "summary": "string",
  "checks_run": [
    {
      "name": "string",
      "command": "string",
      "status": "pass|fail|not_applicable",
      "evidence": "string"
    }
  ],
  "scope_violations": ["string"],
  "test_integrity_findings": ["string"],
  "acceptance_coverage_findings": ["string"],
  "security_findings": ["string"],
  "required_actions": ["string"],
  "safe_to_open_pr": false
}
```

Rules:

- `status` must be `fail` if any deterministic check fails.
- `safe_to_open_pr` may be `true` only when `status` is `pass`.
- `checks_run` must include every deterministic command or file-integrity check actually performed.
- `required_actions` must be non-empty for a failing result.
- Do not omit test integrity, scope, or command failures.

