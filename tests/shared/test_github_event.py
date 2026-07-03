from __future__ import annotations

import unittest

from hooky.shared.github_event import derive_run_from_event, determine_mode, fold_issue_comments, parse_hooky_comment, should_trigger_event


class ShouldTriggerEventTests(unittest.TestCase):
    def test_workflow_dispatch_always_triggers(self) -> None:
        self.assertTrue(should_trigger_event({}, "workflow_dispatch"))

    def test_issues_requires_hooky_run_label(self) -> None:
        self.assertTrue(should_trigger_event({"label": {"name": "hooky:run"}}, "issues"))
        self.assertFalse(should_trigger_event({"label": {"name": "bug"}}, "issues"))
        self.assertFalse(should_trigger_event({}, "issues"))

    def test_pull_request_requires_hooky_run_label(self) -> None:
        self.assertTrue(should_trigger_event({"label": {"name": "hooky:run"}}, "pull_request"))
        self.assertFalse(should_trigger_event({"label": {"name": "bug"}}, "pull_request"))

    def test_issue_comment_requires_hooky_prefix(self) -> None:
        self.assertTrue(should_trigger_event({"comment": {"body": "/hooky do it"}}, "issue_comment"))
        self.assertFalse(should_trigger_event({"comment": {"body": "not a command"}}, "issue_comment"))
        self.assertFalse(should_trigger_event({}, "issue_comment"))

    def test_unknown_event_name_does_not_trigger(self) -> None:
        self.assertFalse(should_trigger_event({"comment": {"body": "/hooky"}}, "push"))


class ParseHookyCommentTests(unittest.TestCase):
    def test_recognizes_run_go_review_keywords_case_insensitively(self) -> None:
        self.assertEqual(parse_hooky_comment("/hooky run"), ("run", ""))
        self.assertEqual(parse_hooky_comment("/hooky Go please"), ("go", "please"))
        self.assertEqual(parse_hooky_comment("/hooky REVIEW the tests too"), ("review", "the tests too"))

    def test_freeform_text_has_no_keyword(self) -> None:
        self.assertEqual(parse_hooky_comment("/hooky also handle nulls"), ("", "also handle nulls"))
        self.assertEqual(parse_hooky_comment("/hooky"), ("", ""))


class DetermineModeTests(unittest.TestCase):
    def test_workflow_dispatch_is_implement(self) -> None:
        self.assertEqual(determine_mode({}, "workflow_dispatch"), "implement")

    def test_issue_label_is_implement(self) -> None:
        self.assertEqual(determine_mode({"label": {"name": "hooky:run"}}, "issues"), "implement")
        self.assertEqual(determine_mode({"label": {"name": "bug"}}, "issues"), "")

    def test_pull_request_label_is_review_only(self) -> None:
        self.assertEqual(determine_mode({"label": {"name": "hooky:run"}}, "pull_request"), "review")
        self.assertEqual(determine_mode({"label": {"name": "bug"}}, "pull_request"), "")

    def test_freeform_comment_on_issue_is_refine(self) -> None:
        event = {"issue": {"number": 1}, "comment": {"body": "/hooky also handle nulls"}}
        self.assertEqual(determine_mode(event, "issue_comment"), "refine")

    def test_run_or_go_comment_on_issue_is_implement(self) -> None:
        for keyword in ("run", "go"):
            event = {"issue": {"number": 1}, "comment": {"body": f"/hooky {keyword}"}}
            self.assertEqual(determine_mode(event, "issue_comment"), "implement")

    def test_review_comment_on_issue_is_still_refine(self) -> None:
        # "review" only has meaning against a PR's diff; on a bare issue it's
        # just freeform text to fold into a future run.
        event = {"issue": {"number": 1}, "comment": {"body": "/hooky review this approach"}}
        self.assertEqual(determine_mode(event, "issue_comment"), "refine")

    def test_freeform_comment_on_pull_request_is_refine(self) -> None:
        # Symmetric with issues: a suggestion left as plain /hooky text just
        # accumulates until an explicit run/go comment fires the light-implement pass.
        event = {"issue": {"number": 7, "pull_request": {"url": "..."}}, "comment": {"body": "/hooky also handle nulls"}}
        self.assertEqual(determine_mode(event, "issue_comment"), "refine")

    def test_run_or_go_comment_on_pull_request_is_light_implement(self) -> None:
        for keyword in ("run", "go"):
            event = {"issue": {"number": 7, "pull_request": {"url": "..."}}, "comment": {"body": f"/hooky {keyword} also handle nulls"}}
            self.assertEqual(determine_mode(event, "issue_comment"), "light-implement")

    def test_review_comment_on_pull_request_is_review(self) -> None:
        event = {"issue": {"number": 7, "pull_request": {"url": "..."}}, "comment": {"body": "/hooky review"}}
        self.assertEqual(determine_mode(event, "issue_comment"), "review")

    def test_non_hooky_comment_does_not_trigger(self) -> None:
        self.assertEqual(determine_mode({"comment": {"body": "not a command"}}, "issue_comment"), "")

    def test_unknown_event_name_does_not_trigger(self) -> None:
        self.assertEqual(determine_mode({}, "push"), "")


class FoldIssueCommentsTests(unittest.TestCase):
    def test_folds_only_hooky_prefixed_comments_with_remainder(self) -> None:
        comments = [
            "Hooky run finished with loop status: passed.",  # bot status comment, no prefix
            "/hooky run",  # bare keyword, no remainder
            "/hooky also handle the empty-list case",
            "unrelated human chatter",
            "/hooky and log a warning",
        ]

        proposal = fold_issue_comments("Fix the crash on save.", comments)

        self.assertIn("Fix the crash on save.", proposal)
        self.assertIn("Follow-up comments:", proposal)
        self.assertIn("- also handle the empty-list case", proposal)
        self.assertIn("- and log a warning", proposal)
        self.assertNotIn("Hooky run finished", proposal)
        self.assertNotIn("unrelated human chatter", proposal)

    def test_no_refining_comments_returns_proposal_unchanged(self) -> None:
        self.assertEqual(fold_issue_comments("Fix the crash on save.", ["/hooky run", "not a command"]), "Fix the crash on save.")


class DeriveRunFromEventTests(unittest.TestCase):
    def test_issue_comment_on_issue_derives_issue_run_key(self) -> None:
        event = {
            "issue": {"number": 12, "title": "Broken link", "body": "The footer link 404s."},
            "comment": {"body": "/hooky please fix"},
        }

        run_key, proposal = derive_run_from_event(event, "issue_comment", "999")

        self.assertEqual(run_key, "issue-12")
        self.assertIn("Broken link", proposal)
        self.assertIn("please fix", proposal)

    def test_issue_comment_on_pull_request_derives_pr_run_key(self) -> None:
        event = {
            "issue": {"number": 7, "title": "Add feature", "pull_request": {"url": "..."}},
            "comment": {"body": "/hooky"},
        }

        run_key, proposal = derive_run_from_event(event, "issue_comment", "999")

        self.assertEqual(run_key, "pr-7")
        self.assertIn("Pull request #7", proposal)
        self.assertIn("Add feature", proposal)

    def test_pull_request_event_derives_pr_run_key_and_proposal(self) -> None:
        event = {"pull_request": {"number": 3, "title": "Refactor auth", "body": "cleanup"}}

        run_key, proposal = derive_run_from_event(event, "pull_request", "999")

        self.assertEqual(run_key, "pr-3")
        self.assertIn("Pull request #3", proposal)
        self.assertIn("cleanup", proposal)

    def test_issues_event_derives_issue_run_key_and_proposal(self) -> None:
        event = {"issue": {"number": 21, "title": "Crash on save", "body": "Stack trace attached."}}

        run_key, proposal = derive_run_from_event(event, "issues", "999")

        self.assertEqual(run_key, "issue-21")
        self.assertIn("Crash on save", proposal)
        self.assertIn("Stack trace attached.", proposal)

    def test_workflow_dispatch_without_payload_falls_back_to_manual_run_key(self) -> None:
        run_key, proposal = derive_run_from_event({}, "workflow_dispatch", "555")

        self.assertEqual(run_key, "manual-555")
        self.assertIn("workflow_dispatch event 555", proposal)

    def test_issues_event_without_issue_number_falls_back_to_generic_proposal(self) -> None:
        run_key, proposal = derive_run_from_event({"issue": {}}, "issues", "555")

        self.assertEqual(run_key, "manual-555")
        self.assertEqual(proposal, "Run Hooky for issues event 555.")

    def test_explicit_proposal_short_circuits_derivation_and_keeps_manual_run_key(self) -> None:
        # Matches hooky.yml: an explicit proposal (only ever set on workflow_dispatch)
        # skips every event-specific branch, so run_key stays manual-<run_id> even
        # if the payload happens to carry issue/PR data.
        event = {"issue": {"number": 21, "title": "Crash on save"}}

        run_key, proposal = derive_run_from_event(event, "issues", "555", explicit_proposal="Do something specific")

        self.assertEqual(run_key, "manual-555")
        self.assertEqual(proposal, "Do something specific")


if __name__ == "__main__":
    unittest.main()
