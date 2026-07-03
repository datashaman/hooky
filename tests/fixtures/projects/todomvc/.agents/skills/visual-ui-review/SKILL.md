---
name: visual-ui-review
description: Inspect browser UI screenshots and visual snapshot metrics for clipped content, overlap, overflow, missing controls, and other blocking layout defects.
---

# Visual UI Review

Use this skill whenever an agent verifies or evaluates a browser UI, visual surface, or screenshot artifact.

## Required Workflow

1. Start the application with the project-defined server command or a managed process tool.
2. Capture at least one representative viewport with `capture_visual_snapshot`.
3. Inspect the attached screenshot image pixels directly.
4. Cross-check the screenshot against the tool metrics.
5. Treat metric/image contradictions as failures to investigate, not as details to waive.

## Blocking Visual Findings

These findings are blocking unless the affected element is purely decorative:

- `contentBounds.y < 0`
- non-empty `sampleClippedElements` for headings, controls, links, buttons, inputs, or visible text
- `horizontalOverflow: true`
- non-empty `sampleHeadingInteractiveOverlaps`
- screenshot pixels showing primary content clipped, off-screen, hidden, or overlapping controls
- missing visible controls needed for the primary workflow
- large unintended whitespace that pushes primary content away from the usable viewport

## Reporting Rules

- Do not call clipped primary content, overlap, or overflow "minor", "non-critical", "acceptable", or "visually correct".
- If a blocking visual finding exists, Verifier must set `status: "fail"`, set `safe_to_open_pr: false`, include the visual defect in `visual_findings`, and include a concrete visual/layout fix in `required_actions`.
- If Verifier downgrades blocking visual evidence, Eval must fail the run with `safe_to_merge: false` and identify `verifier` as the root-cause stage unless an earlier stage clearly caused the same defect.
