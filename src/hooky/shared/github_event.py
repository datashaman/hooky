"""GitHub event -> (mode, run_key, proposal) derivation, shared by CI and local triggers.

Ported from the "Build proposal from event" step in .github/workflows/hooky.yml
so `hooky listen` (a local webhook listener, e.g. paired with `gh webhook
forward`) triggers runs the same way the GitHub Actions workflow does. Keep
both in sync if either the trigger conditions or the derivation logic change.

Modes, decided by how deliberate the trigger is - symmetric for issues and
PRs, except execution on a PR is scoped (light-implement) rather than
negotiated (implement):
- "implement": the full planner -> contract negotiation -> attempt loop.
  A `hooky:run` label on an issue, `workflow_dispatch`, or an explicit
  `/hooky run`/`/hooky go` comment on an issue.
- "light-implement": a scoped, already-worded ask against an existing PR. No
  contract negotiation - the comment itself is the ask. An explicit `/hooky
  run`/`/hooky go` comment on a PR.
- "review": read-only assessment, no writes, no branch/PR. A `hooky:run`
  label on a PR, or an explicit `/hooky review` comment on a PR.
- "refine": accumulate context for a future run; no model call, no run at
  all. Any other `/hooky <text>` comment on an issue or PR - folded into the
  proposal the next time implement/light-implement actually runs against
  that issue/PR. Plain comments with no `/hooky` prefix are never consumed
  by any mode, on either issues or PRs: only explicit `/hooky` text drives
  Hooky, so ordinary PR review conversation doesn't silently turn into code
  changes.
"""

from __future__ import annotations

from typing import Any

HOOKY_COMMENT_KEYWORDS = {"run", "go", "review"}


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_hooky_comment(body: str) -> tuple[str, str]:
    """Split a /hooky-prefixed comment into (keyword, remainder).

    keyword is "run", "go", "review", or "" for freeform text with no
    recognized keyword. remainder is whatever text follows the keyword (or
    the whole comment, if there's no keyword), stripped.
    """
    text = body[len("/hooky") :].strip() if body.startswith("/hooky") else body.strip()
    first_word, _, rest = text.partition(" ")
    if first_word.lower() in HOOKY_COMMENT_KEYWORDS:
        return first_word.lower(), rest.strip()
    return "", text


def determine_mode(event: dict[str, Any], event_name: str) -> str:
    """Return the run mode for an event, or "" if nothing should happen."""
    if event_name == "workflow_dispatch":
        return "implement"
    if event_name == "issues":
        label = as_dict(event.get("label"))
        return "implement" if str(label.get("name") or "") == "hooky:run" else ""
    if event_name == "pull_request":
        label = as_dict(event.get("label"))
        return "review" if str(label.get("name") or "") == "hooky:run" else ""
    if event_name == "issue_comment":
        comment = as_dict(event.get("comment"))
        body = str(comment.get("body") or "")
        if not body.startswith("/hooky"):
            return ""
        issue = as_dict(event.get("issue"))
        is_pr = bool(issue.get("pull_request"))
        keyword, _ = parse_hooky_comment(body)
        if is_pr:
            if keyword == "review":
                return "review"
            return "light-implement" if keyword in {"run", "go"} else "refine"
        return "implement" if keyword in {"run", "go"} else "refine"
    return ""


def should_trigger_event(event: dict[str, Any], event_name: str) -> bool:
    """Match the `if:` condition on the Hooky workflow's `run` job."""
    return determine_mode(event, event_name) != ""


def fold_issue_comments(proposal: str, comment_bodies: list[str]) -> str:
    """Fold /hooky-prefixed refinement comments into a proposal, kept terse.

    Only /hooky-prefixed comments count - Hooky's own bot status comments
    never start with /hooky, so they're naturally excluded and can't feed
    back into a future proposal as bloat. A bare "/hooky run"/"/hooky
    go"/"/hooky review" with no trailing text contributes nothing.
    """
    refinements = [remainder for body in comment_bodies if body.startswith("/hooky") for _, remainder in [parse_hooky_comment(body)] if remainder]
    if not refinements:
        return proposal
    bullets = "\n".join(f"- {item}" for item in refinements)
    return "\n\n".join(part for part in [proposal.strip(), f"Follow-up comments:\n{bullets}"] if part)


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
        is_pr = bool(issue.get("pull_request"))
        run_key = f"pr-{issue.get('number')}" if is_pr else f"issue-{issue.get('number')}"
        kind = "Pull request" if is_pr else "GitHub issue"
        comment = as_dict(event.get("comment"))
        body = str(comment.get("body") or "")
        if body.startswith("/hooky"):
            body = body[len("/hooky") :].strip()
        proposal = "\n\n".join(
            part
            for part in [
                f"{kind} #{issue.get('number')}: {issue.get('title', '')}".strip(),
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
                f"Pull request #{pr.get('number')}: {pr.get('title', '')}".strip(),
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
