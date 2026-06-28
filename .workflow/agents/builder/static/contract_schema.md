# Builder Agent Output Contract

Return a single JSON object with this structure:

```json
{
  "summary": "string",
  "file_writes": [
    {
      "path": "relative/path/from/working/folder",
      "purpose": "why this file is production implementation or required project wiring",
      "content": "complete file content"
    }
  ],
  "commands_to_run": ["string"],
  "tests_run": ["string"],
  "tests_passing": true,
  "failures_remaining": ["string"],
  "test_contract_findings": ["string"],
  "cost_actuals": {},
  "requires_verifier": true
}
```

Rules:

- `file_writes` must include complete replacement content.
- `path` must be relative to the working folder.
- Do not include paths under `tests/`, `.workflow/artifacts/test-agent/`, or approved spec/test sidecar folders.
- Include dependency manifests, lockfiles, build config, and toolchain config only when they are required by the approved spec or project context.
- `tests_passing` may be true only if the contract claims the generated implementation should pass the approved tests.
- If approved tests are invalid or unimplementable without editing tests, set `tests_passing` false and explain the evidence in `failures_remaining` and `test_contract_findings`.
- `requires_verifier` must be true.
