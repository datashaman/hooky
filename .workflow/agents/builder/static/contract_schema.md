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
  "cost_actuals": {},
  "requires_verifier": true
}
```

Rules:

- `file_writes` must include complete replacement content.
- `path` must be relative to the working folder.
- Do not include paths under `tests/`, `.workflow/artifacts/test-agent/`, or approved spec/test sidecar folders.
- Do not include dependency manifest changes unless the dependency already exists in the approved project fixture.
- `tests_passing` may be true only if the contract claims the generated implementation should pass the approved tests.
- `requires_verifier` must be true.

