"""The wiring that plan-scope and merge-authority rely on, in their workflows.

A repository-consistency check, as test_workflow_pins is: it reads the tracked
workflow on purpose, because the workflow is what it checks. Each test names a
property whose loss would go unnoticed: the checker's tests cannot see how the
job calls it, and a broken wiring fails closed, so CI stays green.
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
WORKFLOW = WORKFLOWS / "plan-scope.yml"
RERUN = WORKFLOWS / "merge-authority-rerun.yml"


def load(path: Path = WORKFLOW) -> dict:
    # PyYAML reads the bare key `on` as True.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def step_envs(job: dict) -> str:
    return " ".join(str(step.get("env", {})) for step in job["steps"])


class PlanScopeWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = load()
        self.jobs = self.workflow["jobs"]

    def test_only_pull_request_events_start_the_workflow(self):
        # A review-event run would add a second required-check entry beside the
        # pull_request one, which never replaces it (ADR 0045).
        self.assertEqual(set(self.workflow[True]), {"pull_request"})

    def test_refs_come_from_the_event_pr_not_the_context(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertNotIn("github.head_ref", text)
        self.assertNotIn("github.base_ref", text)

    def test_plan_scope_runs_for_plan_heads_on_every_event(self):
        condition = self.jobs["plan-scope"]["if"]

        self.assertIn("github.event.pull_request.head.ref", condition)
        self.assertNotIn("event_name", condition)

    def test_merge_authority_runs_after_plan_scope_whatever_it_concluded(self):
        job = self.jobs["merge-authority"]

        self.assertEqual(job["needs"], "plan-scope")
        self.assertEqual(job["if"], "${{ !cancelled() }}")

    def test_merge_authority_only_reads(self):
        permissions = self.jobs["merge-authority"]["permissions"]

        self.assertEqual(permissions, {"contents": "read", "pull-requests": "read", "checks": "read"})

    def test_no_concurrency_group_can_cancel_a_plan_scope_run(self):
        self.assertNotIn("concurrency", self.workflow)
        for job in self.jobs.values():
            self.assertNotIn("concurrency", job)

    def test_logins_come_from_repository_variables(self):
        envs = step_envs(self.jobs["merge-authority"])

        self.assertIn("vars.TRIMTAB_ADMIN", envs)
        self.assertIn("vars.TRIMTAB_APPROVER", envs)

    def test_merge_authority_runs_the_checker_from_the_base_branch(self):
        checkout = self.jobs["merge-authority"]["steps"][0]

        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["ref"], "${{ github.event.pull_request.base.ref }}")

    def test_both_jobs_install_the_same_python_steps(self):
        def python_steps(name):
            steps = self.jobs[name]["steps"]
            return [s for s in steps if "setup-python" in s.get("uses", "") or "PyYAML" in s.get("name", "")]

        self.assertEqual(len(python_steps("plan-scope")), 2)
        self.assertEqual(python_steps("merge-authority"), python_steps("plan-scope"))


class MergeAuthorityRerunWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = load(RERUN)
        self.job = self.workflow["jobs"]["rerun"]
        self.script = self.job["steps"][0]["run"]

    def test_only_reviews_that_can_change_the_outcome_start_it(self):
        triggers = self.workflow[True]

        self.assertEqual(set(triggers), {"pull_request_review"})
        self.assertEqual(set(triggers["pull_request_review"]["types"]), {"submitted", "dismissed"})
        self.assertEqual(self.job["if"], "${{ github.event.review.state != 'commented' }}")

    def test_its_check_is_not_the_required_one(self):
        self.assertNotEqual(self.job["name"], "merge-authority")

    def test_it_may_only_re_run_jobs(self):
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.job["permissions"], {"actions": "write"})

    def test_it_re_runs_the_merge_authority_job_of_a_pull_request_run_of_plan_scope(self):
        self.assertIn("actions/workflows/plan-scope.yml/runs", self.script)
        self.assertIn("?event=pull_request&head_sha=$HEAD_SHA", self.script)
        self.assertIn('select(.name == "merge-authority")', self.script)
        self.assertIn("actions/jobs/$job_id/rerun", self.script)

    def test_reviews_on_one_pr_queue_rather_than_cancel(self):
        concurrency = self.workflow["concurrency"]

        self.assertIn("github.event.pull_request.number", concurrency["group"])
        self.assertIs(concurrency["cancel-in-progress"], False)

    @unittest.skipUnless(shutil.which("jq"), "jq is not installed")
    def test_the_run_filter_picks_this_prs_latest_run(self):
        # The workflow's own filter, run by jq on a runs payload. gh evaluates
        # it with gojq; both read `env` and `any/2` the same way.
        runs_filter = re.search(r"--jq '(\(env\.PR_NUMBER.*?)'\)", self.script, re.S).group(1)
        payload = {"workflow_runs": [
            {"id": 1, "status": "completed", "created_at": "2026-10-26T10:00:00Z", "pull_requests": [{"number": 7}]},
            {"id": 2, "status": "in_progress", "created_at": "2026-10-26T10:05:00Z",
             "pull_requests": [{"number": 8}, {"number": 7}]},
            {"id": 3, "status": "completed", "created_at": "2026-10-26T10:09:00Z", "pull_requests": [{"number": 9}]},
            {"id": 4, "status": "completed", "created_at": "2026-10-26T10:10:00Z", "pull_requests": []},
        ]}

        def pick(number: str) -> str:
            env = {**os.environ, "PR_NUMBER": number}
            out = subprocess.run(["jq", "-r", runs_filter], input=json.dumps(payload), env=env,
                                 capture_output=True, text=True, check=True)
            return out.stdout.strip()

        self.assertEqual(pick("7"), "2 in_progress")
        self.assertEqual(pick("5"), "")

    def test_it_picks_this_prs_run_with_the_number_from_the_environment(self):
        self.assertEqual(self.job["steps"][0]["env"]["PR_NUMBER"], "${{ github.event.pull_request.number }}")
        self.assertIn("(env.PR_NUMBER | tonumber) as $pr", self.script)
        self.assertIn(".number == $pr", self.script)

    def test_a_failed_api_call_fails_the_step(self):
        self.assertTrue(self.script.startswith("set -euo pipefail"))
        self.assertNotIn("< <(", self.script)


if __name__ == "__main__":
    unittest.main()
