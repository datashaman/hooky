# Eval Agent Output Contract

Return a single JSON object with this structure:

```json
{
  "status": "pass|fail|needs_human_review",
  "scores": {
    "spec_alignment": 0,
    "maintainability": 0,
    "architecture_fit": 0,
    "risk_awareness": 0,
    "trajectory_quality": 0,
    "pr_summary_quality": 0
  },
  "root_cause_stage": "spec|test|builder|verifier|eval|pipeline|unknown",
  "role_boundary_findings": [
    {
      "role": "spec|builder|verifier|eval",
      "status": "kept|violated|unknown|not_run",
      "evidence": "string"
    }
  ],
  "findings": ["string"],
  "trajectory_findings": ["string"],
  "artifact_findings": ["string"],
  "tooling_findings": ["string"],
  "cost_findings": ["string"],
  "human_review_focus": ["string"],
  "safe_to_merge": false
}
```

Pass criteria:

- Verifier status is `pass`.
- Average score is at least 8.
- No category is below 6.
- No critical qualitative finding is present.
- `safe_to_merge` is true only when `status` is `pass`.

Rules:

- Eval must still run when prior stages failed or Verifier is missing.
- If Verifier status is missing or not `pass`, `status` must be `fail` and `safe_to_merge` must be false.
- `role_boundary_findings` must include exactly one entry for each role: `spec`, `builder`, `verifier`, and `eval`.
- Use `role_boundary_findings` to state whether each role stayed in bounds, crossed responsibilities, did not run, or cannot be determined from evidence.
- Do not contradict `deterministic_facts`; they are system-generated evidence.
- Distinguish "stage failed before final report" from "stage produced no files".
- `needs_human_review` is appropriate when deterministic checks pass but qualitative risk remains.
- Explain every score below 8 in `findings` or `human_review_focus`.
- Use `trajectory_findings` for agent-pathway issues: weak exploration, bad sequencing, failed tool calls, todo misuse, skipped checks, retry loops, or poor handoffs.
- Use `artifact_findings` for quality issues in Spec/Test/Builder/Verifier artifacts or generated project files.
- Use `tooling_findings` for missing, misused, blocked, or noisy tool behavior.
- Use `cost_findings` for excessive runtime, unexpected cost, compaction pressure, or model mismatch.
