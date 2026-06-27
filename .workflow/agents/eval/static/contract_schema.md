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
  "findings": ["string"],
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

- If Verifier status is not `pass`, `status` must be `fail` and `safe_to_merge` must be false.
- `needs_human_review` is appropriate when deterministic checks pass but qualitative risk remains.
- Explain every score below 8 in `findings` or `human_review_focus`.

