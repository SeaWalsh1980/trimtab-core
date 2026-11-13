"""The facts the review rows E5-E13 read (ADR 0063 §4, §5), 2b's readers.

gh is faked (TST-2); git is real, in temporary repositories. Every reader fails closed: a shape
it does not know, a paging limit or a failed read is a `FactsError`, never a shorter list.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from gh_fake import FakeGh, path_of, query_of
from test_enforcement_tools import commit, git
from test_planfacts import BRANCH, FLOWS, INSTANCE, REPO, Pages, facts, pr_row
from test_scrub import isolate_git
from trimtab import planfacts, planreview
from trimtab.capture.sources import GhApi
from trimtab.enforcement import tools
from trimtab.planrows import FactsError

ACTIVITY = f"repos/{REPO}/activity"
# In two pieces: the base's citation test refuses a plans-directory path in a tracked file.
RECORD = "docs/" + "plans/progress/0008-slug/item-02-part2.md"
A, B, C = "a" * 40, "b" * 40, "c" * 40
ZERO = "0" * 40
AGENT = "claude-bot"


def reply(body, link=None):
    head = ["HTTP/2.0 200 OK", "Content-Type: application/json"]
    if link:
        head.append(f"Link: {link}")
    return "\r\n".join(head) + "\r\n\r\n" + json.dumps(body)


def next_link(cursor):
    return f'<https://api.github.com/{ACTIVITY}?after={cursor}>; rel="next"'


def event(before, after, stamp, actor=AGENT, kind="push"):
    return {"before": before, "after": after, "timestamp": stamp, "activity_type": kind,
            "actor": {"login": actor}}


# Newest first, as the endpoint answers.
EVENTS = [event(A, B, "2026-11-12T12:00:00Z"), event(ZERO, A, "2026-11-12T11:00:00Z")]


def server_answers(default_sha, branch_sha=None, branch=BRANCH, extra=None):
    """What the server says about the refs a git-side reader checks its local ones against (ADR 0063 §5):
    the default branch's head, and the part's branch head."""
    out = {f"repos/{REPO}": {"default_branch": "main"},
           f"repos/{REPO}/branches/main": {"commit": {"sha": default_sha}}}
    if branch_sha is not None:
        out[f"repos/{REPO}/git/ref/heads/{branch}"] = {"object": {"sha": branch_sha}}
    out.update(extra or {})
    return out


class ActivityEvents(unittest.TestCase):
    """E13's chain: every event of the branch, oldest to newest, each with its actor (Amendment 1)."""

    def test_events_are_read_oldest_first_with_their_actor(self):
        f = facts({ACTIVITY: Pages([reply(EVENTS)])})

        got = f.activity(BRANCH)

        self.assertEqual([(e.before, e.after, e.actor) for e in got],
                         [(ZERO, A, AGENT), (A, B, AGENT)])

    def test_every_page_is_read_through_the_link_cursor_never_a_page_number(self):
        f = facts({ACTIVITY: Pages([reply([event(A, B, "2026-11-12T12:00:00Z")], link=next_link("c1")),
                                    reply([event(ZERO, A, "2026-11-12T11:00:00Z")])])})

        got = f.activity(BRANCH)

        self.assertEqual([e.after for e in got], [A, B])
        queries = [query_of(a) for a in f.fake.argvs()]
        self.assertEqual(queries[1]["after"], "c1")
        self.assertTrue(all("page" not in q for q in queries))
        self.assertEqual({q["ref"] for q in queries}, {f"refs/heads/{BRANCH}"})

    def test_the_branch_is_read_once(self):
        f = facts({ACTIVITY: Pages([reply(EVENTS)])})

        f.activity(BRANCH), f.activity(BRANCH)

        self.assertEqual(len(f.fake.calls), 1)

    def test_more_pages_than_the_limit_refuses(self):
        pages = [reply([event(A, B, "2026-11-12T12:00:00Z")], link=next_link(f"c{n}"))
                 for n in range(planfacts.EVENT_PAGES + 1)]

        with self.assertRaises(FactsError):
            facts({ACTIVITY: Pages(pages)}).activity(BRANCH)

    def test_a_deletion_refuses_rather_than_breaking_the_chain_silently(self):
        f = facts({ACTIVITY: Pages([reply([event(B, ZERO, "2026-11-12T13:00:00Z")])])})

        with self.assertRaises(FactsError):
            f.activity(BRANCH)

    def test_an_unexpected_shape_refuses(self):
        for row in ({"before": A, "after": B, "timestamp": "2026-11-12T12:00:00Z"},  # no actor
                    {"before": A, "after": B, "timestamp": "2026-11-12T12:00:00Z", "actor": {}},
                    {"before": "nope", "after": B, "timestamp": "2026-11-12T12:00:00Z", "actor": {"login": AGENT}},
                    {"before": A, "after": B, "timestamp": "not a time", "actor": {"login": AGENT}}):
            with self.subTest(row=row), self.assertRaises(FactsError):
                facts({ACTIVITY: Pages([reply([row])])}).activity(BRANCH)

    def test_events_out_of_time_order_refuse(self):
        rows = [event(A, B, "2026-11-12T11:00:00Z"), event(ZERO, A, "2026-11-12T12:00:00Z")]

        with self.assertRaises(FactsError):
            facts({ACTIVITY: Pages([reply(rows)])}).activity(BRANCH)

    def test_pushes_come_from_the_events_since_the_pr_opened(self):
        f = facts({f"repos/{REPO}/pulls": [pr_row(created="2026-11-12T11:30:00Z")],
                   ACTIVITY: Pages([reply(EVENTS)])})

        self.assertEqual([(p.before, p.after) for p in f.pushes(BRANCH)], [(A, B)])


class ReviewComments(unittest.TestCase):
    """E5, E8 and E9 read the PR's review comments; E9 takes each thread's commit from its root comment."""

    def setUp(self):
        self.rows = [
            {"id": 11, "user": {"login": INSTANCE.review.login}, "body": "a finding",
             "in_reply_to_id": None, "commit_id": A},
            {"id": 12, "user": {"login": AGENT}, "body": f"[fixed] {B}", "in_reply_to_id": 11, "commit_id": B},
        ]
        self.path = f"repos/{REPO}/pulls/7/comments"

    def test_comments_carry_their_author_body_and_reply_target(self):
        got = facts({self.path: self.rows}).review_comments(7)

        self.assertEqual([(c.id, c.user, c.in_reply_to) for c in got],
                         [(11, INSTANCE.review.login, None), (12, AGENT, 11)])

    def test_each_comment_id_maps_to_the_commit_the_review_anchored_it_on(self):
        self.assertEqual(facts({self.path: self.rows}).comment_commits(7), {11: A, 12: B})

    def test_the_pr_is_read_once(self):
        f = facts({self.path: self.rows})

        f.review_comments(7), f.comment_commits(7)

        self.assertEqual(len(f.fake.calls), 1)

    def test_an_unexpected_shape_refuses(self):
        for row in ({"id": "11", "user": {"login": AGENT}, "body": "x", "commit_id": A},
                    {"id": 11, "user": {}, "body": "x", "commit_id": A},
                    {"id": 11, "user": {"login": AGENT}, "body": 7, "commit_id": A},
                    {"id": 11, "user": {"login": AGENT}, "body": "x", "commit_id": "short"},
                    {"id": 11, "user": {"login": AGENT}, "body": "x", "commit_id": A, "in_reply_to_id": "11"}):
            with self.subTest(row=row), self.assertRaises(FactsError):
                facts({self.path: [row]}).review_comments(7)


REVIEW_LINE = f"TRIMTAB-REVIEW v1 run=42 head={B} tree={C} verdict=pass"
THREAD_LINE = ("TRIMTAB-THREAD v1 run=42 id=11 severity=high state=open resolved_by=- "
               f"path=src/a.py line=3 category=correctness key={'1' * 64}")


class FactsRuns(unittest.TestCase):
    """E3: each facts run's log, parsed by `planreview.parse_facts`, with `Thread.commit` filled (Amendment 2)."""

    def setUp(self):
        self.runs = {"total_count": 1, "workflow_runs": [
            {"id": 42, "head_sha": B, "head_branch": BRANCH, "path": FLOWS + "facts.yml",
             "event": "pull_request", "status": "completed"}]}
        self.jobs = {"total_count": 1, "jobs": [
            {"id": 99, "name": "facts", "head_sha": B, "status": "completed", "conclusion": "success",
             "runner_id": 7, "runner_name": "r", "steps": [{}]}]}
        self.answers = {
            f"repos/{REPO}/actions/runs": self.runs,
            f"repos/{REPO}/actions/runs/42/jobs": self.jobs,
            # A log is text, not JSON: Pages hands the body through as it is.
            f"repos/{REPO}/actions/jobs/99/logs": Pages([f"{REVIEW_LINE}\n{THREAD_LINE}\n"]),
            f"repos/{REPO}/pulls/7/comments": [
                {"id": 11, "user": {"login": INSTANCE.review.login}, "body": "a finding",
                 "in_reply_to_id": None, "commit_id": A}],
        }

    def test_a_runs_review_and_threads_are_read_with_the_threads_commit(self):
        got = facts(self.answers).facts_runs(BRANCH, 7)

        self.assertEqual([(r.head, r.verdict) for r, _ in got], [(B, "pass")])
        self.assertEqual([(t.id, t.severity, t.commit) for _, ts in got for t in ts], [(11, "high", A)])

    def test_a_thread_whose_root_comment_is_not_on_the_pr_refuses(self):
        self.answers[f"repos/{REPO}/pulls/7/comments"] = []

        with self.assertRaises(FactsError):
            facts(self.answers).facts_runs(BRANCH, 7)

    def test_a_log_that_breaks_the_line_shape_refuses(self):
        self.answers[f"repos/{REPO}/actions/jobs/99/logs"] = Pages(["TRIMTAB-REVIEW v2 run=42\n"])

        with self.assertRaises(FactsError):
            facts(self.answers).facts_runs(BRANCH, 7)

    def test_a_run_whose_latest_attempt_did_not_pass_holds_no_facts(self):
        # A superseded or cancelled run is the common case, and refusing on it would stop the part for
        # good; a head with no readable facts still refuses at the row that needs them.
        self.answers[f"repos/{REPO}/actions/runs/42/jobs"]["jobs"][0]["conclusion"] = "cancelled"

        self.assertEqual(facts(self.answers).facts_runs(BRANCH, 7), [])

    def test_a_later_attempt_of_the_facts_job_decides(self):
        jobs = self.answers[f"repos/{REPO}/actions/runs/42/jobs"]["jobs"]
        failed = {**jobs[0], "conclusion": "failure"}
        self.answers[f"repos/{REPO}/actions/runs/42/jobs"]["jobs"] = [failed, jobs[0]]

        got = facts(self.answers).facts_runs(BRANCH, 7)

        self.assertEqual([(r.head, r.verdict) for r, _ in got], [(B, "pass")])

    def test_a_run_with_no_job_of_that_name_refuses(self):
        self.answers[f"repos/{REPO}/actions/runs/42/jobs"]["jobs"][0]["name"] = "other"

        with self.assertRaises(FactsError):
            facts(self.answers).facts_runs(BRANCH, 7)

    def test_a_failed_log_read_refuses(self):
        def run(argv, stdin):
            if path_of(argv).endswith("/logs"):
                return subprocess.CompletedProcess(argv, 1, "", "gh: HTTP 500")
            answer = self.answers.get(path_of(argv), "")
            return subprocess.CompletedProcess(argv, 0, json.dumps(answer) if answer else "", "")

        f = planfacts.GitHubFacts(REPO, GhApi(run), tools.Git(), Path("/nonexistent"), INSTANCE)

        with self.assertRaises(FactsError):
            f.facts_runs(BRANCH, 7)

    def test_without_a_review_key_the_rows_refuse(self):
        with self.assertRaises(FactsError):
            facts(self.answers, inst=None).facts_runs(BRANCH, 7)


class GitReaders(unittest.TestCase):
    """The merge-base, its tree, the anchored line, the branch's commits and each push's commits."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        isolate_git(self, self.tmp)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "src").mkdir()
        self.base = commit(self.repo, "src/a.py", "one\ntwo\nthree\n")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        git(self.repo, "checkout", "-q", "-b", BRANCH)
        self.first = commit(self.repo, "src/b.py", "x\n")
        self.second = commit(self.repo, "src/c.py", "y\n")
        git(self.repo, "update-ref", f"refs/remotes/origin/{BRANCH}", self.second)
        self.git = tools.Git(environ={"HOME": str(self.tmp)})

    def facts(self, answers=None):
        answers = server_answers(self.base, self.second, extra=answers)
        fake = FakeGh({k: json.dumps(v) for k, v in answers.items()}, default=json.dumps({}))
        out = planfacts.GitHubFacts(REPO, GhApi(fake), self.git, self.repo, INSTANCE)
        out.fake = fake
        return out

    def test_the_default_branch_comes_from_the_repository_and_is_read_once(self):
        f = self.facts()

        self.assertEqual((f.default_branch(), f.default_branch()), ("main", "main"))
        self.assertEqual(len(f.fake.calls), 1)

    def test_the_merge_base_and_its_tree(self):
        f = self.facts()

        self.assertEqual(f.merge_base(BRANCH), self.base)
        self.assertEqual(f.tree_of(self.base), git(self.repo, "rev-parse", f"{self.base}^{{tree}}"))

    def test_the_branch_commits_run_oldest_to_newest(self):
        self.assertEqual(list(self.facts().branch_commits(BRANCH)), [self.first, self.second])

    def test_a_pushs_commits_are_the_range_it_added(self):
        f = self.facts()

        self.assertEqual(f.push_commits(self.first, self.second, self.base), frozenset({self.second}))

    def test_a_push_that_created_the_branch_is_bounded_by_the_merge_base(self):
        got = self.facts().push_commits(ZERO, self.second, self.base)

        self.assertEqual(got, frozenset({self.first, self.second}))

    def test_the_anchored_line_is_read_at_the_reviewed_commit(self):
        self.assertEqual(self.facts().line_text(self.base, "src/a.py", 2), "two")

    def test_a_line_or_path_that_is_not_there_reads_as_absent(self):
        f = self.facts()

        self.assertIsNone(f.line_text(self.base, "src/a.py", 99))
        self.assertIsNone(f.line_text(self.base, "src/missing.py", 1))

    def test_on_default_tells_a_default_branch_commit_from_a_branch_one(self):
        f = self.facts()

        self.assertTrue(f.on_default(self.base))
        self.assertFalse(f.on_default(self.second))

    def test_a_local_ref_the_server_does_not_agree_with_refuses(self):
        # Nothing refuses a session's own `git update-ref`, so a ref read without this check would be a
        # fact the agent can write (ADR 0063 §5).
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.second)

        with self.assertRaisesRegex(FactsError, "server"):
            self.facts().merge_base(BRANCH)

    def test_a_branch_ref_the_server_does_not_agree_with_refuses(self):
        git(self.repo, "update-ref", f"refs/remotes/origin/{BRANCH}", self.first)

        with self.assertRaisesRegex(FactsError, "server"):
            self.facts().branch_commits(BRANCH)

    def test_a_default_head_the_server_cannot_name_refuses(self):
        f = self.facts({f"repos/{REPO}/branches/main": {"commit": {}}})

        with self.assertRaises(FactsError):
            f.merge_base(BRANCH)

    def test_a_failed_git_read_refuses(self):
        f = self.facts()

        with self.assertRaises(FactsError):
            f.merge_base("plan/0008-slug/item-02-part9")

    def test_the_outside_of_the_diff_is_judged_from_the_merge_base(self):
        f = self.facts()

        self.assertTrue(f.outside_diff(BRANCH, self.second, "src/a.py", 2))
        self.assertFalse(f.outside_diff(BRANCH, self.second, "src/c.py", 1))


class LastAnswer(unittest.TestCase):
    """E10's floor: the last commit answering a CI review or a failing check."""

    def answers(self, runs):
        return {f"repos/{REPO}/pulls": [pr_row(created="2026-11-12T10:00:00Z")],
                f"repos/{REPO}/actions/runs": {"total_count": len(runs), "workflow_runs": runs},
                ACTIVITY: Pages([reply(EVENTS)])}

    def run_row(self, head, path=FLOWS + "ci.yml", conclusion="failure"):
        return {"id": 1, "head_sha": head, "head_branch": BRANCH, "path": path, "event": "push",
                "status": "completed", "conclusion": conclusion}

    def test_the_head_a_push_answering_a_failing_check_wrote(self):
        f = facts(self.answers([self.run_row(A)]))

        self.assertEqual(f.last_answer(BRANCH), B)

    def test_a_push_answering_a_facts_run_counts_too(self):
        f = facts(self.answers([self.run_row(A, path=FLOWS + "facts.yml", conclusion="success")]))

        self.assertEqual(f.last_answer(BRANCH), B)

    def test_no_push_answering_one_reads_as_none(self):
        f = facts(self.answers([self.run_row(C)]))

        self.assertIsNone(f.last_answer(BRANCH))


class RemovedLines(unittest.TestCase):
    """E18 judges removed line text, so a change carries it (Amendment 2)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        isolate_git(self, self.tmp)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.base = commit(self.repo, "setup.cfg", "fail_under = 90\nother = 1\n")
        self.git = tools.Git(environ={"HOME": str(self.tmp)})

    def test_a_change_carries_the_text_of_the_lines_it_removes(self):
        head = commit(self.repo, "setup.cfg", "fail_under = 50\nother = 1\n")
        f = planfacts.GitHubFacts(REPO, GhApi(FakeGh()), self.git, self.repo, INSTANCE)

        got = {c.path: c for c in f.changes(BRANCH, head, self.base)}

        self.assertEqual(got["setup.cfg"].removed_lines, ("fail_under = 90",))
        self.assertEqual(got["setup.cfg"].added, ("fail_under = 50",))


class PartSources(unittest.TestCase):
    """§6's two server-side sources: the progress records on the default branch, and every PR's head ref."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        isolate_git(self, self.tmp)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        folder = self.repo / "docs" / "plans" / "progress" / "0008-slug"
        folder.mkdir(parents=True)
        self.base = commit(self.repo, RECORD, "## Done\n")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        self.git = tools.Git(environ={"HOME": str(self.tmp)})

    def facts(self, answers=None):
        answers = server_answers(self.base, extra=answers)
        fake = FakeGh({k: json.dumps(v) for k, v in answers.items()}, default=json.dumps({}))
        return planfacts.GitHubFacts(REPO, GhApi(fake), self.git, self.repo, INSTANCE)

    def test_the_progress_records_on_the_default_branch_are_listed(self):
        got = self.facts().progress_names("0008-slug")

        self.assertEqual(got, (RECORD,))

    def test_a_plan_with_no_records_lists_nothing(self):
        self.assertEqual(self.facts().progress_names("0009-other"), ())

    def test_every_prs_head_ref_is_read_whatever_its_state(self):
        rows = [{"head": {"ref": BRANCH}}, {"head": {"ref": "feature/x"}}]
        f = self.facts({f"repos/{REPO}/pulls": rows})

        self.assertEqual(f.pr_head_refs(), (BRANCH, "feature/x"))

    def test_an_unexpected_shape_refuses(self):
        f = self.facts({f"repos/{REPO}/pulls": [{"head": {}}]})

        with self.assertRaises(FactsError):
            f.pr_head_refs()


class FixValidity(unittest.TestCase):
    """E9: a cited commit descends from the thread's commit, is on the branch, and changes a line in its hunk."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        isolate_git(self, self.tmp)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "src").mkdir()
        self.base = commit(self.repo, "src/a.py", "one\ntwo\nthree\n")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        git(self.repo, "checkout", "-q", "-b", BRANCH)
        self.fix = commit(self.repo, "src/a.py", "one\nfixed\nthree\n")
        self.other = commit(self.repo, "src/b.py", "z\n")
        git(self.repo, "update-ref", f"refs/remotes/origin/{BRANCH}", self.other)
        self.git = tools.Git(environ={"HOME": str(self.tmp)})
        answers = server_answers(self.base, self.other)
        fake = FakeGh({k: json.dumps(v) for k, v in answers.items()}, default=json.dumps({}))
        self.f = planfacts.GitHubFacts(REPO, GhApi(fake), self.git, self.repo, INSTANCE)

    def thread(self, line=2, path="src/a.py", commit_sha=None):
        return planreview.Thread("42", 11, "high", "open", "-", path, line, "correctness", "k" * 64,
                                 commit_sha or self.base)

    def test_a_commit_on_the_branch_that_changes_the_findings_line_is_valid(self):
        self.assertTrue(self.f.fix_valid(BRANCH, self.thread(), self.fix))

    def test_a_commit_that_changes_another_file_is_not(self):
        self.assertFalse(self.f.fix_valid(BRANCH, self.thread(), self.other))

    def test_a_commit_that_does_not_descend_from_the_threads_commit_is_not(self):
        self.assertFalse(self.f.fix_valid(BRANCH, self.thread(commit_sha=self.other), self.fix))

    def test_a_commit_that_is_not_on_the_branch_is_not(self):
        git(self.repo, "checkout", "-q", "main")
        stray = commit(self.repo, "src/a.py", "one\nstray\nthree\n")

        self.assertFalse(self.f.fix_valid(BRANCH, self.thread(), stray))

    def test_a_commit_git_cannot_read_refuses(self):
        with self.assertRaises(FactsError):
            self.f.fix_valid(BRANCH, self.thread(), "9" * 40)


if __name__ == "__main__":
    unittest.main()
