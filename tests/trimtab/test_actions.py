"""Actions job records: every attempt, every page, a runner and steps, one snapshot (ADR 0058, ADR 0063).

gh, a true external system, is faked (TST-2)."""

import json
import unittest

from gh_fake import FakeGh, path_of
from trimtab import actions
from trimtab.actions import ActionsError
from trimtab.capture.sources import PER_PAGE, GhApi

REPO = "owner/core"
HEAD = "d" * 40
WORKFLOW = ".github/workflows/ci.yml"
RUNS = f"repos/{REPO}/actions/runs"


def run(run_id=1, head=HEAD, path=WORKFLOW, event="push", status="completed", branch="main"):
    return {"id": run_id, "head_sha": head, "head_branch": branch, "path": path, "event": event, "status": status}


def job(name="lint-test", head=HEAD, conclusion="success", status="completed", steps=1, runner_id=7,
        runner_name="runner-1"):
    return {"name": name, "head_sha": head, "status": status, "conclusion": conclusion,
            "steps": [{"name": "s"}] * steps, "runner_id": runner_id, "runner_name": runner_name}


def reader(runs, jobs):
    fake = FakeGh({RUNS: json.dumps({"workflow_runs": runs}),
                   **{f"{RUNS}/{rid}/jobs": json.dumps({"jobs": j}) for rid, j in jobs.items()}})
    return actions.Reader(GhApi(fake)), fake


def problem(runs, jobs, **kwargs):
    return reader(runs, jobs)[0].job_problem(REPO, HEAD, WORKFLOW, "lint-test", **kwargs)


class JudgesAJob(unittest.TestCase):
    def test_passes_when_every_attempt_of_every_run_passed(self):
        jobs = {1: [job(), job(), job("other", conclusion="failure")], 2: [job()]}

        self.assertIsNone(problem([run(1), run(2)], jobs))

    def test_refuses_when_one_attempt_failed(self):
        self.assertIn("not a completed, passing", problem([run()], {1: [job(), job(conclusion="failure")]}))

    def test_refuses_when_a_second_run_of_the_head_failed(self):
        self.assertIn("not a completed, passing", problem([run(1), run(2)], {1: [job()], 2: [job(conclusion="failure")]}))

    def test_refuses_a_job_with_no_runner_id(self):
        self.assertIn("with a runner", problem([run()], {1: [job(runner_id=None)]}))

    def test_refuses_a_job_with_no_runner_name(self):
        self.assertIn("with a runner", problem([run()], {1: [job(runner_name="")]}))

    def test_refuses_a_job_with_no_steps(self):
        self.assertIn("steps", problem([run()], {1: [job(steps=0)]}))

    def test_refuses_a_job_for_another_head(self):
        self.assertIn("not a completed, passing", problem([run()], {1: [job(head="e" * 40)]}))

    def test_refuses_with_no_run_of_the_workflow(self):
        self.assertIn("no push run", problem([run(path=".github/workflows/other.yml")], {}))

    def test_refuses_a_run_on_another_event(self):
        self.assertIn("no push run", problem([run(event="pull_request")], {}))

    def test_refuses_a_run_for_another_branch_when_a_branch_is_named(self):
        self.assertIn("another branch", problem([run(branch="plan/x")], {1: [job()]}, branch="main"))

    def test_refuses_an_unfinished_run(self):
        self.assertIn("not finished", problem([run(status="in_progress")], {}))

    def test_refuses_a_run_with_no_such_job(self):
        self.assertIn("lists no", problem([run()], {1: [job("other")]}))


class ReadsInFullOrNotAtAll(unittest.TestCase):
    def test_reads_every_page(self):
        first = [run(n, path=".github/workflows/other.yml") for n in range(1, PER_PAGE + 1)]
        fake = FakeGh({RUNS: [json.dumps({"workflow_runs": first}), json.dumps({"workflow_runs": [run(500)]})],
                       f"{RUNS}/500/jobs": json.dumps({"jobs": [job()]})})

        self.assertIsNone(actions.Reader(GhApi(fake)).job_problem(REPO, HEAD, WORKFLOW, "lint-test"))

    def test_refuses_past_its_bound(self):
        page = json.dumps({"workflow_runs": [run(n) for n in range(1, PER_PAGE + 1)]})
        fake = FakeGh({RUNS: [page] * (actions.LIMIT // PER_PAGE + 1)})

        with self.assertRaises(ActionsError):
            actions.Reader(GhApi(fake)).runs(REPO, HEAD, "push")

    def test_raises_on_unexpected_json(self):
        with self.assertRaises(ActionsError):
            problem([{"id": "x"}], {})

    def test_raises_on_a_job_with_no_steps_list(self):
        with self.assertRaises(ActionsError):
            problem([run()], {1: [{**job(), "steps": None}]})

    def test_raises_on_a_failed_read(self):
        with self.assertRaises(ActionsError):
            actions.Reader(GhApi(FakeGh(returncode=1, stderr="HTTP 403"))).runs(REPO, HEAD, "push")


class Memoises(unittest.TestCase):
    def test_reads_each_list_once_for_a_unit_of_work(self):
        r, fake = reader([run()], {1: [job(), job("other")]})

        r.job_problem(REPO, HEAD, WORKFLOW, "lint-test")
        r.job_problem(REPO, HEAD, WORKFLOW, "other")

        self.assertEqual(sorted(path_of(a) for a in fake.argvs()), [RUNS, f"{RUNS}/1/jobs"])


class RowsHelper(unittest.TestCase):
    def test_raises_the_callers_error(self):
        class Mine(Exception):
            pass

        with self.assertRaises(Mine):
            actions.rows(lambda p, w, q: {"x": []}, "repos/o/r/x", "rows", "list", 10, {}, error=Mine)


if __name__ == "__main__":
    unittest.main()
