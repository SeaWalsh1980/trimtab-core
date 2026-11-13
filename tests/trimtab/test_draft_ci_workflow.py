"""The wiring that keeps a draft PR from running CI and cancels CI on return to draft (ADR 0053).

A repository-consistency check, as test_plan_scope_workflow is: it reads the
tracked workflows on purpose, because the workflows are what it checks. GitHub's
behaviour itself is not testable here; ADR 0053 records the trial on a real PR.
"""

import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def load(name: str) -> dict:
    # PyYAML reads the bare key `on` as True.
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


class CiWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = load("ci.yml")

    def test_marking_ready_starts_the_run_that_replaces_the_drafts_skip(self):
        self.assertIn("ready_for_review", self.workflow[True]["pull_request"]["types"])

    def test_returning_to_draft_starts_the_run_that_cancels(self):
        self.assertIn("converted_to_draft", self.workflow[True]["pull_request"]["types"])

    def test_a_new_event_cancels_the_prs_run_in_progress(self):
        concurrency = self.workflow["concurrency"]

        self.assertIn("github.event.pull_request.number", concurrency["group"])
        self.assertIs(concurrency["cancel-in-progress"], True)

    def test_a_push_to_main_is_never_in_a_shared_group(self):
        # Without the run id every push would share one group and cancel the last.
        self.assertIn("|| github.run_id", self.workflow["concurrency"]["group"])

    def test_the_required_job_skips_only_a_draft(self):
        self.assertEqual(self.workflow["jobs"]["lint-test"]["if"], "${{ !github.event.pull_request.draft }}")

    def test_the_skipped_job_never_reports_as_the_required_check(self):
        # The ruleset requires `lint-test` by name and a skip under that name
        # passes it; under another name the required check stays Expected.
        self.assertEqual(self.workflow["jobs"]["lint-test"]["name"],
                         "${{ github.event.pull_request.draft && 'lint-test (draft, not run)' || 'lint-test' }}")


class ClaudeReviewWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = load("claude-code-review.yml")

    def test_returning_to_draft_cancels_a_review_in_progress(self):
        self.assertIn("converted_to_draft", self.workflow[True]["pull_request"]["types"])
        self.assertIs(self.workflow["concurrency"]["cancel-in-progress"], True)

    def test_the_run_that_cancels_does_not_review(self):
        self.assertEqual(self.workflow["jobs"]["claude-review"]["if"], "github.event.pull_request.draft == false")

    def test_the_skipped_job_never_reports_as_the_required_check(self):
        self.assertEqual(self.workflow["jobs"]["claude-review"]["name"],
                         "${{ github.event.pull_request.draft && 'claude-review (draft, not run)' || 'claude-review' }}")

    def test_the_review_job_requests_no_oidc_token(self):
        # Without id-token the action cannot exchange for the Claude App's token (ADR 0071).
        job = self.workflow["jobs"]["claude-review"]

        self.assertNotIn("id-token", self.workflow.get("permissions", {}))
        self.assertNotIn("id-token", job.get("permissions", {}))

    def test_the_review_uses_the_jobs_own_token(self):
        steps = self.workflow["jobs"]["claude-review"]["steps"]
        action = [s for s in steps if str(s.get("uses", "")).startswith("anthropics/claude-code-action@")]

        self.assertEqual(len(action), 1)
        self.assertEqual(action[0]["with"]["github_token"], "${{ github.token }}")

    def test_the_review_token_is_comment_only(self):
        self.assertEqual(self.workflow["permissions"],
                         {"contents": "read", "pull-requests": "write", "issues": "read"})
        self.assertNotIn("permissions", self.workflow["jobs"]["claude-review"])


if __name__ == "__main__":
    unittest.main()
