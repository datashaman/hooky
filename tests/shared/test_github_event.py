from __future__ import annotations

import unittest

from hooky.shared.github_event import derive_run_from_event, should_trigger_event


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
        self.assertIn("Add feature", proposal)

    def test_pull_request_event_derives_pr_run_key_and_proposal(self) -> None:
        event = {"pull_request": {"number": 3, "title": "Refactor auth", "body": "cleanup"}}

        run_key, proposal = derive_run_from_event(event, "pull_request", "999")

        self.assertEqual(run_key, "pr-3")
        self.assertIn("Review and improve pull request #3", proposal)
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
