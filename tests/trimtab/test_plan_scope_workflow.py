"""The wiring that plan-scope and merge-authority rely on, in their workflows.

A repository-consistency check, as test_workflow_pins is: it reads the tracked
workflow on purpose, because the workflow is what it checks. Each test names a
property whose loss would go unnoticed: the checker's tests cannot see how the
job calls it, and a broken wiring fails closed, so CI stays green.
"""

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

    def test_review_events_start_it(self):
        triggers = self.workflow[True]

        self.assertEqual(set(triggers), {"pull_request_review"})
        self.assertEqual(set(triggers["pull_request_review"]["types"]), {"submitted", "edited", "dismissed"})

    def test_its_check_is_not_the_required_one(self):
        self.assertNotEqual(self.job["name"], "merge-authority")

    def test_it_may_only_re_run_jobs(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(self.job["permissions"], {"actions": "write"})

    def test_it_re_runs_the_merge_authority_job_of_a_pull_request_run_of_plan_scope(self):
        self.assertIn("actions/workflows/plan-scope.yml/runs", self.script)
        self.assertIn("?event=pull_request&head_sha=$HEAD_SHA", self.script)
        self.assertIn('select(.name == "merge-authority")', self.script)
        self.assertIn("actions/jobs/$job_id/rerun", self.script)

    def test_a_failed_api_call_fails_the_step(self):
        self.assertTrue(self.script.startswith("set -euo pipefail"))
        self.assertNotIn("< <(", self.script)


if __name__ == "__main__":
    unittest.main()
