# Spec Agent Contract Shape

Return only JSON matching this shape:

```json
{
  "summary": "string",
  "scope": ["string"],
  "non_goals": ["string"],
  "acceptance_criteria": ["string"],
  "affected_components": ["string"],
  "edge_cases": ["string"],
  "blocking_questions": ["string"],
  "test_plan": ["string"],
  "risks": ["string"],
  "cost_plan": {
    "complexity": "small|medium|large",
    "max_iterations": 3,
    "model_route": {
      "spec": "string",
      "test": "string",
      "builder": "string",
      "verifier": "string",
      "eval": "string"
    }
  },
  "requires_human_approval": true
}
```

## Required Contract Rules

- `requires_human_approval` must be `true`.
- `acceptance_criteria` must be non-empty.
- `test_plan` must be non-empty.
- `cost_plan` must be present.
- `non_goals` must include relevant stage-boundary exclusions.

