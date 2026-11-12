"""The plan rows' facts read GitHub through gh and the checkout through git (ADR 0063 §5).

gh, a true external system, is faked (TST-2); git is real, in temporary repositories."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from gh_fake import FakeGh, path_of
from test_enforcement_tools import commit, git
from test_scrub import isolate_git
from trimtab import instance, planfacts, planrows
from trimtab.actions import Job
from trimtab.capture.sources import GhApi
from trimtab.enforcement import tools
from trimtab.planrows import FactsError

PLANS = "docs/" + "plans/"

REPO = "owner/proj"
BRANCH = "plan/0008-slug/item-02-part1"
H1, H2 = "1" * 40, "2" * 40
FLOWS = ".github/" + "workflows/"
BASE = {"schema_version": 1, "repo": "owner/instance", "base": {"repo": "owner/core", "sha": "a" * 40}}
KEYS = {
    "secret_scan": {"workflow": FLOWS + "scan.yml", "job": "secret-scan"},
    "review": {
        "run": {"workflow": FLOWS + "review.yml", "job": "review", "event": "pull_request"},
        "facts": {"workflow": FLOWS + "facts.yml", "job": "facts"},
        "login": "review-bot[bot]", "rerequest_label": "review:run",
        "tags": {"fixed": "[fixed]", "wrong": "[wrong]", "intentional": "[intentional]", "moot": "[moot]",
                 "deferred": "[deferred]"},
    },
}
INSTANCE = instance.parse({**BASE, **KEYS})
PULLS = f"repos/{REPO}/pulls"
RUNS = f"repos/{REPO}/actions/runs"


class Pages(list):
    """Successive raw answers to one path (gh_fake serves a list one per call)."""


def facts(answers, inst=INSTANCE, checkout=Path("/nonexistent"), git_=None):
    fake = FakeGh({k: (v if isinstance(v, Pages) else json.dumps(v)) for k, v in answers.items()})
    sleeps = []
    out = planfacts.GitHubFacts(REPO, GhApi(fake), git_ or tools.Git(), checkout, inst, sleep=sleeps.append)
    out.fake, out.sleeps = fake, sleeps
    return out


def gh_404(missing, repo_readable=True):
    """A gh whose answer for each path in `missing` is 404; the repository itself answers only if readable."""
    def run(argv, stdin):
        path = path_of(argv)
        if path in missing or (path == f"repos/{REPO}" and not repo_readable):
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"full_name": REPO}), "")
    return run


def pr_row(number=7, draft=True, created="2026-11-12T10:00:00Z", ref=BRANCH):
    return {"number": number, "draft": draft, "created_at": created, "head": {"ref": ref}, "base": {"ref": "main"}}


def job(name="secret-scan", head=H1, status="completed", conclusion="success", steps=1, runner=7):
    return Job(name, head, status, conclusion, runner, "r" if runner else "", steps)


class ScanState(unittest.TestCase):
    def test_states(self):
        for name, found, jobs, want in (
                ("a passing job", True, [job()], "pass"),
                ("a failure", True, [job(conclusion="failure")], "fail"),
                ("any attempt failing", True, [job(), job(conclusion="failure")], "fail"),
                ("a skipped job", True, [job(conclusion="skipped")], "fail"),
                ("a success with no steps", True, [job(steps=0)], "fail"),
                ("a success with no runner", True, [job(runner=None)], "fail"),
                ("a running job", True, [job(status="in_progress", conclusion="")], "pending"),
                ("a running and a failed job", True, [job(status="queued", conclusion=""), job(conclusion="failure")], "fail"),
                ("no run yet", False, [], "pending"),
                ("a run with no such job", True, [], "unreadable")):
            with self.subTest(name):
                self.assertEqual(planfacts.scan_state(found, jobs), want)


class OpenPr(unittest.TestCase):
    def test_none_when_no_open_pr(self):
        self.assertIsNone(facts({PULLS: []}).open_pr(BRANCH))

    def test_reads_the_draft_flag_number_and_time(self):
        got = facts({PULLS: [pr_row(draft=False)]}).open_pr(BRANCH)
        self.assertEqual((got.number, got.draft, got.created), (7, False, "2026-11-12T10:00:00Z"))

    def test_asks_for_the_owner_qualified_head_once(self):
        f = facts({PULLS: [pr_row()]})
        f.open_pr(BRANCH)
        f.open_pr(BRANCH)
        self.assertEqual(len(f.fake.calls), 1)
        self.assertIn(f"head=owner%3A{BRANCH.replace('/', '%2F')}", f.fake.calls[0][0][4])

    def test_refuses_each_bad_shape(self):
        for name, answer in (("two PRs", [pr_row(), pr_row(number=8)]), ("not a list", {}),
                             ("a draft that is not a bool", [pr_row(draft="yes")]),
                             ("another head", [pr_row(ref="plan/other")])):
            with self.subTest(name), self.assertRaises(FactsError):
                facts({PULLS: answer}).open_pr(BRANCH)

    def test_a_failed_call_refuses(self):
        f = planfacts.GitHubFacts(REPO, GhApi(FakeGh(returncode=1, stderr="HTTP 500")), tools.Git(), Path("."),
                                  INSTANCE)
        with self.assertRaises(FactsError):
            f.open_pr(BRANCH)


class BranchHead(unittest.TestCase):
    PATH = f"repos/{REPO}/git/ref/heads/{BRANCH}"

    def test_reads_the_sha(self):
        self.assertEqual(facts({self.PATH: {"object": {"sha": H1}}}).branch_head(BRANCH), H1)

    def test_none_when_the_branch_is_absent_and_the_repository_answers(self):
        f = planfacts.GitHubFacts(REPO, GhApi(gh_404({self.PATH})), tools.Git(), Path("."), INSTANCE)
        self.assertIsNone(f.branch_head(BRANCH))

    def test_a_404_from_a_repository_the_token_cannot_read_is_not_absence(self):
        f = planfacts.GitHubFacts(REPO, GhApi(gh_404({self.PATH}, repo_readable=False)), tools.Git(), Path("."),
                                  INSTANCE)
        with self.assertRaisesRegex(FactsError, "not readable"):
            f.branch_head(BRANCH)

    def test_a_bad_shape_refuses(self):
        with self.assertRaises(FactsError):
            facts({self.PATH: {"object": {"sha": "zz"}}}).branch_head(BRANCH)


class Runs(unittest.TestCase):
    def run_row(self, head, conclusion, path=FLOWS + "ci.yml"):
        return {"head_sha": head, "path": path, "conclusion": conclusion, "id": 1}

    def test_failing_heads_are_the_failed_timed_out_and_cancelled(self):
        rows = [self.run_row(H1, "failure"), self.run_row(H2, "success"), self.run_row("3" * 40, "cancelled"),
                self.run_row("4" * 40, "timed_out"), self.run_row("5" * 40, None)]
        got = facts({RUNS: {"workflow_runs": rows}}).failing_heads(BRANCH)
        self.assertEqual(got, {H1, "3" * 40, "4" * 40})

    def test_facts_runs_are_the_runs_of_the_facts_workflow(self):
        rows = [self.run_row(H1, "success", FLOWS + "facts.yml"), self.run_row(H2, "success")]
        self.assertEqual(facts({RUNS: {"workflow_runs": rows}}).facts_run_heads(BRANCH), {H1})

    def test_without_a_review_key_the_facts_runs_are_unreadable(self):
        with self.assertRaises(FactsError):
            facts({RUNS: {"workflow_runs": []}}, inst=instance.parse(BASE)).facts_run_heads(BRANCH)

    def test_the_run_list_is_read_once_for_both(self):
        f = facts({RUNS: {"workflow_runs": []}})
        f.failing_heads(BRANCH)
        f.facts_run_heads(BRANCH)
        self.assertEqual(len(f.fake.calls), 1)

    def test_a_row_with_the_wrong_shape_refuses(self):
        with self.assertRaises(FactsError):
            facts({RUNS: {"workflow_runs": [{"head_sha": 1}]}}).failing_heads(BRANCH)


class SecretScan(unittest.TestCase):
    def answers(self, conclusion="success", path=FLOWS + "scan.yml", jobs=True):
        run = {"id": 5, "head_sha": H1, "head_branch": BRANCH, "path": path, "event": "push", "status": "completed"}
        job_row = {"name": "secret-scan", "head_sha": H1, "status": "completed", "conclusion": conclusion,
                   "steps": [{"name": "s"}], "runner_id": 3, "runner_name": "r"}
        return {RUNS: {"workflow_runs": [run]},
                f"{RUNS}/5/jobs": {"jobs": [job_row] if jobs else []}}

    def test_pass_and_fail(self):
        self.assertEqual(facts(self.answers()).secret_scan(H1, BRANCH), "pass")
        self.assertEqual(facts(self.answers("failure")).secret_scan(H1, BRANCH), "fail")

    def test_a_run_of_another_workflow_is_not_the_scan(self):
        # the branch has runs, but none of the scan workflow: it never triggers here, so E19 cannot be shown
        self.assertEqual(facts(self.answers(path=FLOWS + "other.yml")).secret_scan(H1, BRANCH), "unreadable")

    def test_a_branch_with_no_runs_yet_is_pending(self):
        answers = {**self.answers(), RUNS: {"workflow_runs": []}}
        self.assertEqual(facts(answers).secret_scan(H1, BRANCH), "pending")

    def test_a_run_with_no_such_job_is_unreadable(self):
        self.assertEqual(facts(self.answers(jobs=False)).secret_scan(H1, BRANCH), "unreadable")

    def test_no_key_refuses(self):
        with self.assertRaises(FactsError):
            facts(self.answers(), inst=instance.parse(BASE)).secret_scan(H1, BRANCH)

    def test_a_failed_read_refuses(self):
        f = planfacts.GitHubFacts(REPO, GhApi(FakeGh(returncode=1, stderr="HTTP 500")), tools.Git(), Path("."),
                                  INSTANCE)
        with self.assertRaises(FactsError):
            f.secret_scan(H1, BRANCH)


class Pushes(unittest.TestCase):
    ACTIVITY = f"repos/{REPO}/activity"

    def event(self, before, after, stamp="2026-11-12T11:00:00Z"):
        return {"before": before, "after": after, "timestamp": stamp, "ref": f"refs/heads/{BRANCH}",
                "activity_type": "push", "actor": {"login": "agent"}}

    def test_reads_pushes_since_the_pr_opened(self):
        rows = [self.event(H1, H2), self.event("3" * 40, H1, stamp="2026-11-12T09:00:00Z")]
        f = facts({PULLS: [pr_row()], self.ACTIVITY: rows})
        self.assertEqual(f.pushes(BRANCH), [planrows.Push(H1, H2)])

    def test_a_branch_creation_with_a_zero_before_is_not_a_push(self):
        f = facts({PULLS: [pr_row()], self.ACTIVITY: [self.event("0" * 40, H1)]})
        self.assertEqual(f.pushes(BRANCH), [])

    def test_no_open_pr_means_no_pushes_to_count(self):
        self.assertEqual(facts({PULLS: []}).pushes(BRANCH), ())

    def test_a_page_below_the_limit_is_read_whole(self):
        rows = [self.event(H1, H2)] * 99
        f = facts({PULLS: [pr_row()], self.ACTIVITY: rows})
        self.assertEqual(len(f.pushes(BRANCH)), 99)

    def test_a_full_page_refuses_because_the_rest_is_behind_a_cursor(self):
        f = facts({PULLS: [pr_row()], self.ACTIVITY: [self.event(H1, H2)] * 100})
        with self.assertRaisesRegex(FactsError, "or more"):
            f.pushes(BRANCH)

    def test_the_read_asks_for_one_page_without_a_page_number(self):
        f = facts({PULLS: [pr_row()], self.ACTIVITY: []})
        f.pushes(BRANCH)
        query = [a[4] for a in f.fake.argvs() if "activity" in a[4]][0]
        self.assertIn("per_page=100", query)
        self.assertNotIn("page=", query.replace("per_page=", ""))

    def test_a_bad_event_refuses(self):
        with self.assertRaises(FactsError):
            facts({PULLS: [pr_row()], self.ACTIVITY: [{"before": 1}]}).pushes(BRANCH)


class Conflicted(unittest.TestCase):
    PATH = f"repos/{REPO}/pulls/7"

    def test_reads_mergeable(self):
        self.assertFalse(facts({self.PATH: {"mergeable": True}}).conflicted(7))
        self.assertTrue(facts({self.PATH: {"mergeable": False}}).conflicted(7))

    def test_a_null_is_asked_again_then_refused(self):
        f = facts({self.PATH: {"mergeable": None}})
        with self.assertRaisesRegex(FactsError, "computed"):
            f.conflicted(7)
        self.assertEqual(f.sleeps, [1, 1, 1])

    def test_a_null_then_an_answer(self):
        f = facts({self.PATH: Pages([json.dumps({"mergeable": None}), json.dumps({"mergeable": False})])})
        self.assertTrue(f.conflicted(7))

    def test_a_bad_shape_refuses(self):
        with self.assertRaises(FactsError):
            facts({self.PATH: {"mergeable": "maybe"}}).conflicted(7)


class PrHead(unittest.TestCase):
    def test_reads_the_head_ref_of_this_or_another_repository(self):
        f = facts({f"repos/other/repo/pulls/7": {"head": {"ref": BRANCH}}})
        self.assertEqual(f.pr_head(7, "other/repo"), BRANCH)

    def test_a_number_that_is_not_a_pull_request_is_none(self):
        f = planfacts.GitHubFacts(REPO, GhApi(gh_404({f"repos/{REPO}/pulls/7"})), tools.Git(), Path("."), INSTANCE)
        self.assertIsNone(f.pr_head(7, None))

    def test_a_404_from_a_repository_the_token_cannot_read_is_not_an_issue(self):
        f = planfacts.GitHubFacts(REPO, GhApi(gh_404({f"repos/{REPO}/pulls/7"}, repo_readable=False)), tools.Git(),
                                  Path("."), INSTANCE)
        with self.assertRaisesRegex(FactsError, "not readable"):
            f.pr_base(7, None)

    def test_head_and_base_come_from_one_read(self):
        f = facts({f"repos/{REPO}/pulls/7": {"head": {"ref": BRANCH}, "base": {"ref": "main"}}})
        self.assertEqual((f.pr_head(7, None), f.pr_base(7, None), f.pr_head(7, None)), (BRANCH, "main", BRANCH))
        self.assertEqual(len(f.fake.calls), 1)

    def test_a_bad_shape_or_failed_read_refuses(self):
        with self.assertRaises(FactsError):
            facts({f"repos/{REPO}/pulls/7": {"head": {}}}).pr_head(7, None)
        f = planfacts.GitHubFacts(REPO, GhApi(FakeGh(returncode=1, stderr="HTTP 500")), tools.Git(), Path("."),
                                  INSTANCE)
        with self.assertRaises(FactsError):
            f.pr_head(7, None)


class Origin(unittest.TestCase):
    def test_reads_https_and_ssh_remotes(self):
        for url in ("https://github.com/owner/proj.git", "https://github.com/owner/proj", "git@github.com:owner/proj.git",
                    "ssh://git@github.com/owner/proj.git"):
            with self.subTest(url):
                self.assertEqual(planfacts.repository_of(url), "owner/proj")

    def test_refuses_another_host(self):
        for url in ("https://evil.example/owner/proj.git", "file:///tmp/x", "https://github.com.evil.example/o/p"):
            with self.subTest(url), self.assertRaises(FactsError):
                planfacts.repository_of(url)


class GitFacts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        isolate_git(self, self.tmp)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.base = commit(self.repo, "keep.txt", "one\ntwo\n")
        self.git = tools.Git(environ={"HOME": str(self.tmp)})

    def facts(self):
        return planfacts.GitHubFacts(REPO, GhApi(FakeGh()), self.git, self.repo, INSTANCE)

    def test_changes_lists_added_modified_and_deleted_paths_with_lines(self):
        (self.repo / "keep.txt").write_text("one\nthree\nfour\n")
        (self.repo / "new.md").write_text("[a](b.md)\n")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x")
        got = {c.path: c for c in self.facts().changes(BRANCH, "HEAD", self.base)}

        self.assertEqual(got["new.md"], planrows.Change("new.md", "A", ("[a](b.md)",), 0))
        self.assertEqual((got["keep.txt"].status, got["keep.txt"].added, got["keep.txt"].removed),
                         ("M", ("three", "four"), 1))

    def test_a_deleted_file_counts_its_removed_lines(self):
        git(self.repo, "rm", "-q", "keep.txt")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x")
        got = self.facts().changes(BRANCH, "HEAD", self.base)
        self.assertEqual([(c.path, c.status, c.removed) for c in got], [("keep.txt", "D", 2)])

    def test_an_added_line_that_looks_like_a_diff_header_is_still_a_line(self):
        (self.repo / "keep.txt").write_text("one\ntwo\n++ x\n")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-aqm", "x")
        got = self.facts().changes(BRANCH, "HEAD", self.base)
        self.assertEqual(got[0].added, ("++ x",))

    def test_a_path_with_a_space_or_a_unicode_name(self):
        (self.repo / "a b.md").write_text("x\n")
        (self.repo / "é.md").write_text("y\n")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x")
        got = {c.path: c.added for c in self.facts().changes(BRANCH, "HEAD", self.base)}
        self.assertEqual(got, {"a b.md": ("x",), "é.md": ("y",)})

    def test_an_external_diff_driver_in_the_repo_config_does_not_run(self):
        marker = self.tmp / "ran"
        git(self.repo, "config", "diff.external", f"touch {marker} ; true")
        (self.repo / "keep.txt").write_text("changed\n")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-aqm", "x")
        self.facts().changes(BRANCH, "HEAD", self.base)
        self.assertFalse(marker.exists())

    def test_no_source_refuses(self):
        with self.assertRaises(FactsError):
            self.facts().changes(BRANCH, None, self.base)

    def test_a_head_the_checkout_does_not_hold_refuses(self):
        with self.assertRaises(FactsError):
            self.facts().changes(BRANCH, "HEAD", "9" * 40)

    def test_a_register_answer_reads_the_diff_between_two_heads(self):
        folder = self.repo / (PLANS + "unauthorised/0008-slug")
        folder.mkdir(parents=True)
        (folder / "item-02-part1-01.md").write_text("entry\n")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x")
        after = git(self.repo, "rev-parse", "HEAD")
        self.assertTrue(self.facts().register_answer(BRANCH, self.base, after))

    def test_local_plan_branches_are_listed_whatever_their_case(self):
        git(self.repo, "branch", BRANCH)
        git(self.repo, "branch", "PLAN/upper")
        git(self.repo, "branch", "feature/x")
        self.assertEqual(sorted(self.facts().local_plan_branches()), sorted([BRANCH, "PLAN/upper"]))


if __name__ == "__main__":
    unittest.main()
