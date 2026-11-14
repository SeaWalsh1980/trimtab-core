"""The wiring that keeps a draft PR from running CI and cancels CI on return to draft (ADR 0053).

A repository-consistency check, as test_plan_scope_workflow is: it reads the
tracked workflows on purpose, because the workflows are what it checks. GitHub's
behaviour itself is not testable here; ADR 0053 records the trial on a real PR.
"""

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

REVIEW_WORKFLOW = "claude-code-review.yml"
REVIEW_JOB = "claude-review"
REVIEW_ACTION_PREFIX = "anthropics/claude-code-action@"
JOB_TOKEN = "${{ github.token }}"
# Fragments of a key name through which a workflow asks for the Claude App's token
# again: the OIDC permission, the request variables the exchange reads, the review
# action's own App inputs, and the credentials an App-token action takes. Matched as
# substrings of a key, so an env name such as ACTIONS_ID_TOKEN_REQUEST_URL is caught
# too, in either spelling.
APP_TOKEN_KEY_FRAGMENTS = (
    "id-token",
    "id_token",
    "app-token",
    "app_token",
    "app-id",
    "app_id",
    "client-id",
    "client_id",
    "private-key",
    "private_key",
    "oidc",
)
# The action that mints an App token here, whatever the token is then passed to. One
# name, so another minting action would need adding; its App inputs are keys, so the
# fragments above would catch those.
APP_TOKEN_ACTION = "create-github-app-token"


def load(name: str) -> dict:
    # PyYAML reads the bare key `on` as True.
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def mapping_keys(node) -> list:
    """Every mapping key in a parsed workflow, lowercased.

    Keys only: the prompt and the messages are free to name OIDC in prose.
    """
    if isinstance(node, dict):
        keys = [str(key).lower() for key in node]
        for value in node.values():
            keys += mapping_keys(value)
        return keys
    if isinstance(node, list):
        return [key for item in node for key in mapping_keys(item)]
    return []


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
        self.workflow = load(REVIEW_WORKFLOW)

    def test_returning_to_draft_cancels_a_review_in_progress(self):
        self.assertIn("converted_to_draft", self.workflow[True]["pull_request"]["types"])
        self.assertIs(self.workflow["concurrency"]["cancel-in-progress"], True)

    def test_the_run_that_cancels_does_not_review(self):
        self.assertEqual(self.workflow["jobs"][REVIEW_JOB]["if"], "github.event.pull_request.draft == false")

    def test_the_skipped_job_never_reports_as_the_required_check(self):
        self.assertEqual(self.workflow["jobs"][REVIEW_JOB]["name"],
                         "${{ github.event.pull_request.draft && 'claude-review (draft, not run)' || 'claude-review' }}")

    def test_no_key_names_an_app_token_path(self):
        # Restoring the Claude App's token (ADR 0071) means a permission, an input or
        # an env name that asks for it. Those are keys, so a key naming one fails
        # here, anywhere in the file; prose in the prompt and the messages is free to
        # discuss OIDC. A token smuggled in as a VALUE is out of this test's reach:
        # test_the_review_uses_the_jobs_own_token pins the review action's own input,
        # but a `run:` step or a local composite action could still mint one unseen.
        keys = mapping_keys(self.workflow)

        for fragment in APP_TOKEN_KEY_FRAGMENTS:
            with self.subTest(fragment=fragment):
                named = [key for key in keys if fragment in key]

                self.assertEqual(named, [], f"{REVIEW_WORKFLOW} names {named}")

    def test_no_step_mints_an_app_token(self):
        # Every job, not just the reviewer's: a second job could mint the token and
        # pass it on.
        for name, job in self.workflow["jobs"].items():
            for step in job.get("steps", []):
                with self.subTest(job=name, step=step.get("name", step.get("uses"))):
                    self.assertNotIn(APP_TOKEN_ACTION, str(step.get("uses", "")))

    def test_the_review_uses_the_jobs_own_token(self):
        steps = self.workflow["jobs"][REVIEW_JOB]["steps"]
        action = [s for s in steps if str(s.get("uses", "")).startswith(REVIEW_ACTION_PREFIX)]

        self.assertEqual(len(action), 1)
        self.assertEqual(action[0]["with"]["github_token"], JOB_TOKEN)

    def test_the_review_token_is_comment_only(self):
        self.assertEqual(self.workflow["permissions"],
                         {"contents": "read", "pull-requests": "write", "issues": "read"})
        self.assertNotIn("permissions", self.workflow["jobs"][REVIEW_JOB])


ASSERTION_STEP = "Fail closed when no review was posted"
RUN_URL = "https://github.com/acme/repo/actions/runs/123"
FINISHED = "**Claude finished @bot's task** "


def finished_comment(cid, body):
    return {"id": cid, "created_at": "2026-11-14T10:00:00Z", "user": {"type": "Bot"}, "body": body}


class RunLinkedCommentMatch(unittest.TestCase):
    """The assertion's comment filter links this run exactly, not by prefix (ADR 0081).

    Runs the workflow's own `--jq` filter under jq, so the test fails when the
    filter is weakened, not when its spelling changes.
    """

    def setUp(self):
        self.assertTrue(shutil.which("jq"), "jq is required to test the assertion's filter")
        workflow = yaml.safe_load((WORKFLOWS / REVIEW_WORKFLOW).read_text(encoding="utf-8"))
        steps = workflow["jobs"][REVIEW_JOB]["steps"]
        run = next(s for s in steps if s.get("name") == ASSERTION_STEP)["run"]
        found = re.search(r"--jq '([^']*)'", run)
        self.assertIsNotNone(found, "the assertion step no longer has a single-quoted --jq filter")
        self.jq_filter = found.group(1)

    def matched_ids(self, *comments):
        env = {**os.environ, "SINCE": "2026-11-14T00:00:00Z", "RUN_URL": RUN_URL}
        out = subprocess.run(["jq", "-r", self.jq_filter],
                             input=json.dumps(list(comments)), env=env,
                             capture_output=True, text=True, check=True).stdout
        return out.split()

    def test_an_exact_link_matches(self):
        self.assertEqual(self.matched_ids(finished_comment(1, f"{FINISHED}[run]({RUN_URL}) done")), ["1"])

    def test_a_link_at_the_end_of_the_body_matches(self):
        self.assertEqual(self.matched_ids(finished_comment(1, f"{FINISHED}{RUN_URL}")), ["1"])

    def test_a_link_followed_by_a_closing_paren_or_slash_matches(self):
        for tail in (")", "/", "#job", "?x=1", "."):
            with self.subTest(tail=tail):
                self.assertEqual(self.matched_ids(finished_comment(1, f"{FINISHED}({RUN_URL}{tail}")), ["1"])

    def test_a_longer_run_id_does_not_match(self):
        for longer in ("4", "45", "0"):
            with self.subTest(longer=longer):
                self.assertEqual(self.matched_ids(finished_comment(1, f"{FINISHED}[run]({RUN_URL}{longer})")), [])

    def test_a_longer_run_id_at_the_end_of_the_body_does_not_match(self):
        self.assertEqual(self.matched_ids(finished_comment(1, f"{FINISHED}{RUN_URL}4")), [])

    def test_a_body_linking_both_runs_matches_on_the_exact_link(self):
        self.assertEqual(self.matched_ids(finished_comment(1, f"{FINISHED}{RUN_URL}4 and {RUN_URL})")), ["1"])

    def test_a_different_repository_with_the_same_run_id_does_not_match(self):
        other = RUN_URL.replace("acme/repo", "acme/other")

        self.assertEqual(self.matched_ids(finished_comment(1, f"{FINISHED}{other}")), [])

    def test_the_other_conditions_still_apply(self):
        working = finished_comment(1, f"{FINISHED}{RUN_URL} Claude Code is working")
        not_finished = finished_comment(2, f"**Claude encountered an error** {RUN_URL}")
        human = {**finished_comment(3, f"{FINISHED}{RUN_URL}"), "user": {"type": "User"}}
        stale = {**finished_comment(4, f"{FINISHED}{RUN_URL}"), "created_at": "2026-11-13T10:00:00Z"}

        self.assertEqual(self.matched_ids(working, not_finished, human, stale), [])


if __name__ == "__main__":
    unittest.main()
