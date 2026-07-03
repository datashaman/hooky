"""GitHub event -> (run_key, proposal) derivation, shared by CI and local triggers.

Ported from the "Build proposal from event" step in .github/workflows/hooky.yml
so `hooky listen` (a local webhook listener, e.g. paired with `gh webhook
forward`) triggers runs the same way the GitHub Actions workflow does. Keep
both in sync if either the trigger conditions or the derivation logic change.
"""

from __future__ import annotations

from typing import Any


def should_trigger_event(event: dict[str, Any], event_name: str) -> bool:
    """Match the `if:` condition on the Hooky workflow's `run` job."""
    if event_name == "workflow_dispatch":
        return True
    if event_name in {"issues", "pull_request"}:
        label = as_dict(event.get("label"))
        return str(label.get("name") or "") == "hooky:run"
    if event_name == "issue_comment":
        comment = as_dict(event.get("comment"))
        return str(comment.get("body") or "").startswith("/hooky")
    return False


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def derive_run_from_event(
    event: dict[str, Any],
    event_name: str,
    run_id: str,
    *,
    explicit_proposal: str = "",
) -> tuple[str, str]:
    """Return (run_key, proposal) for an event, matching hooky.yml's derivation exactly."""
    proposal = explicit_proposal.strip()
    run_key = f"manual-{run_id}"

    if not proposal and event_name == "issue_comment":
        issue = as_dict(event.get("issue"))
        run_key = f"pr-{issue.get('number')}" if issue.get("pull_request") else f"issue-{issue.get('number')}"
        comment = as_dict(event.get("comment"))
        body = str(comment.get("body") or "")
        if body.startswith("/hooky"):
            body = body[len("/hooky") :].strip()
        proposal = "\n\n".join(
            part
            for part in [
                f"Respond to GitHub issue #{issue.get('number')}: {issue.get('title', '')}".strip(),
                body or str(issue.get("body") or "").strip(),
            ]
            if part
        )

    if not proposal and event_name == "pull_request":
        pr = as_dict(event.get("pull_request"))
        run_key = f"pr-{pr.get('number')}"
        proposal = "\n\n".join(
            part
            for part in [
                f"Review and improve pull request #{pr.get('number')}: {pr.get('title', '')}".strip(),
                str(pr.get("body") or "").strip(),
            ]
            if part
        )

    if not proposal:
        issue = as_dict(event.get("issue"))
        if issue.get("number"):
            run_key = f"issue-{issue.get('number')}"
            proposal = "\n\n".join(
                part
                for part in [
                    f"Address GitHub issue #{issue.get('number')}: {issue.get('title', '')}".strip(),
                    str(issue.get("body") or "").strip(),
                ]
                if part
            )

    if not proposal:
        proposal = f"Run Hooky for {event_name} event {run_id}."

    return run_key, proposal
