"""The retro's state lives on GitHub, not on disk (ADR 0006).

A routine has no durable disk, so its ledger is rebuilt each run from the PRs
already labelled `trimtab-ingested`, within the decay window; proposals are
opened as issues behind a confirmation, and never twice.
"""

import contextlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from instance_fixture import env_for, make_instance
from trimtab.capture.ingest import LABEL, PullRequest, rebuild
from trimtab.capture.sources import FixtureSource, SourceError
from trimtab.items import Item, Proposal, Strength
from trimtab.lint import structure
from trimtab.propose import (
    ISSUE_LABEL, Draft, Filer, OpeningStopped, PullTitle, StaleProposals, confirmation, open_issues, plan_issues,
    plan_project_prs, render,
)
from trimtab.score import candidates, tally

REPO = Path(__file__).resolve().parents[2]
TRIMTAB = REPO / "bin" / "trimtab"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REGISTRY = {"TST-2": Item(id="TST-2", prefix="TST", local_id="2", type="rule", source="rules/Example.md",
                          heading="Mocking Policy", strength=Strength.PROHIBIT)}
AMBIGUOUS = (FIXTURES / "pr-tst2-ambiguous.md").read_text(encoding="utf-8")
CONFIG = {"trimtab_sha": "0" * 40, "id_prefix": "SCR",
          "adopted_at": "2026-09-01T00:00:00Z", "baseline_pr": 100, "schema_version": 2}


class FakeIssues:
    """GitHub issues: a true external system, so a fake is allowed."""

    def __init__(self, titles=(), closed=(), destination="o/trimtab", read_back_fails=None):
        self.destination = destination
        self.read_backs = 0
        self.read_back_fails = read_back_fails
        self.issues = [{"title": t, "labels": [ISSUE_LABEL]} for t in titles]
        self.issues += [{"title": t, "labels": [ISSUE_LABEL], "closed_at": at} for t, at in closed]

    def recent_titles(self, label, closed_since):
        return [i["title"] for i in self.issues if label in i["labels"]
                and ("closed_at" not in i or (closed_since and i["closed_at"] >= closed_since))]

    def create(self, title, body, labels):
        self.issues.append({"title": title, "body": body, "labels": list(labels)})
        return f"issue {len(self.issues)}"

    @contextlib.contextmanager
    def filing(self):
        try:
            yield Filer(self.create)
        finally:
            self.read_backs += 1
            if self.read_back_fails:
                raise self.read_back_fails


class FailingAt(FakeIssues):
    """Files issues until one whose title starts with `prefix`, which fails."""

    def __init__(self, prefix):
        super().__init__()
        self.prefix = prefix

    def create(self, title, body, labels):
        if title.startswith(self.prefix):
            raise RuntimeError("the sink failed")
        return super().create(title, body, labels)


def draft(item_id, kind="issue", prs=(), merged_at=()):
    return Draft(kind, item_id, f"[{item_id}] reported ambiguous in 2 PRs", "body", (ISSUE_LABEL,), prs, merged_at)


class FakePulls:
    """A project's PRs: GitHub, a true external system, so a fake is allowed."""

    def __init__(self, *pulls):
        self.pulls = list(pulls)

    def recent_pulls(self, closed_since):
        return self.pulls


class Rebuild(unittest.TestCase):
    def test_labelled_prs_are_the_ledger(self):
        pulls = [PullRequest(101, AMBIGUOUS, "2026-09-10T00:00:00Z"), PullRequest(102, AMBIGUOUS, "2026-09-20T00:00:00Z")]

        ledger = rebuild(pulls, REGISTRY)

        self.assertEqual(sorted(ledger.records), [101, 102])
        self.assertEqual([p.item_id for p in candidates(tally(ledger.records.values()))], ["TST-2"])

    def test_rebuilding_twice_gives_the_same_ledger(self):
        pulls = [PullRequest(101, AMBIGUOUS, "2026-09-10T00:00:00Z")]

        self.assertEqual(rebuild(pulls, REGISTRY).to_json(), rebuild(pulls + pulls, REGISTRY).to_json())

    def test_prs_at_or_below_the_baseline_are_not_evidence(self):
        ledger = rebuild([PullRequest(100, AMBIGUOUS, "2026-09-10T00:00:00Z")], REGISTRY, baseline_pr=100)

        self.assertEqual(ledger.records, {})


class Decay(unittest.TestCase):
    def test_evidence_older_than_the_window_no_longer_counts(self):
        ledger = rebuild([PullRequest(101, AMBIGUOUS, "2026-07-01T00:00:00Z"),
                          PullRequest(102, AMBIGUOUS, "2026-09-20T00:00:00Z")], REGISTRY)

        self.assertEqual(candidates(tally(ledger.records.values(), since="2026-08-01")), [])


class Issues(unittest.TestCase):
    def test_an_id_with_an_open_issue_is_skipped(self):
        todo = plan_issues([draft("TST-2"), draft("ENG-2")], FakeIssues(["[TST-2] reported gap in 3 PRs"]))

        self.assertEqual([d.item_id for d in todo.to_open], ["ENG-2"])
        self.assertEqual(todo.already_open, ("TST-2",))

    def test_an_issue_closed_within_the_window_is_not_refiled(self):
        sink = FakeIssues(closed=[("[TST-2] reported ambiguous in 2 PRs", "2026-09-15")])

        self.assertEqual(plan_issues([draft("TST-2")], sink, closed_since="2026-08-01").to_open, ())

    def test_an_issue_closed_before_the_window_no_longer_blocks(self):
        sink = FakeIssues(closed=[("[TST-2] reported ambiguous in 2 PRs", "2026-07-01")])

        self.assertEqual(len(plan_issues([draft("TST-2")], sink, closed_since="2026-08-01").to_open), 1)

    def test_project_drafts_are_never_opened_by_the_tool(self):
        todo = plan_issues([draft("SCR-X-1", kind="pr")], FakeIssues())

        self.assertEqual(todo.to_open, ())

    def test_opening_needs_the_dry_runs_token(self):
        sink = FakeIssues()
        todo = plan_issues([draft("TST-2")], sink)

        with self.assertRaises(StaleProposals):
            open_issues(todo, sink, confirm="000000000000")
        self.assertEqual(sink.issues, [])

    def test_a_token_from_a_dry_run_against_another_repository_is_refused(self):
        dry = plan_issues([draft("TST-2")], FakeIssues(destination="o/elsewhere"))
        sink = FakeIssues(destination="o/trimtab")

        with self.assertRaises(StaleProposals):
            open_issues(plan_issues([draft("TST-2")], sink), sink, confirmation(dry))
        self.assertEqual(sink.issues, [])

    def test_a_second_run_opens_nothing(self):
        sink = FakeIssues()
        first = plan_issues([draft("TST-2")], sink)
        open_issues(first, sink, confirmation(first))

        second = plan_issues([draft("TST-2")], sink)

        self.assertEqual(second.to_open, ())
        self.assertEqual(len(sink.issues), 1)

    def test_opening_that_stops_part_way_names_what_it_opened_and_what_it_did_not_try(self):
        sink = FailingAt("[ENG-2]")
        todo = plan_issues([draft("TST-2"), draft("ENG-2"), draft("SEC-1")], sink)

        with self.assertRaises(OpeningStopped) as caught:
            open_issues(todo, sink, confirmation(todo))

        stopped = caught.exception
        self.assertEqual((stopped.opened, stopped.stopped_at, stopped.not_opened, type(stopped.cause)),
                         (["issue 1"], "ENG-2", ["SEC-1"], RuntimeError))

    def test_every_issue_opened_is_read_back_once_after_all_are_filed(self):
        sink = FakeIssues()
        todo = plan_issues([draft("TST-2"), draft("ENG-2")], sink)

        open_issues(todo, sink, confirmation(todo))

        self.assertEqual((len(sink.issues), sink.read_backs), (2, 1))

    def test_what_was_opened_is_read_back_even_when_opening_stops_part_way(self):
        sink = FailingAt("[ENG-2]")
        todo = plan_issues([draft("TST-2"), draft("ENG-2")], sink)

        with self.assertRaises(OpeningStopped):
            open_issues(todo, sink, confirmation(todo))

        self.assertEqual(sink.read_backs, 1)

    def test_a_failed_read_back_still_names_what_was_opened(self):
        sink = FakeIssues(read_back_fails=RuntimeError("the body differs"))
        todo = plan_issues([draft("TST-2")], sink)

        with self.assertRaises(OpeningStopped) as caught:
            open_issues(todo, sink, confirmation(todo))

        self.assertEqual((caught.exception.opened, caught.exception.stopped_at), (["issue 1"], "the read-back"))

    def test_a_failed_create_followed_by_a_failed_read_back_keeps_both(self):
        sink = FailingAt("[ENG-2]")
        sink.read_back_fails = RuntimeError("the read-back failed")
        todo = plan_issues([draft("TST-2"), draft("ENG-2"), draft("SEC-1")], sink)

        with self.assertRaises(OpeningStopped) as caught:
            open_issues(todo, sink, confirmation(todo))

        stopped = caught.exception
        self.assertEqual((stopped.stopped_at, stopped.not_opened, stopped.also.stopped_at, str(stopped.also.cause)),
                         ("the read-back", ["SEC-1"], "ENG-2", "the sink failed"))

    def test_the_issue_names_the_reporting_project(self):
        proposal = Proposal("TST-2", "upstream", ("ambiguous",), ("e1", "e2"), (101, 102))

        body = render(proposal, REGISTRY, project="owner/consumer").body

        self.assertIn("project: owner/consumer", body)


class ProjectDrafts(unittest.TestCase):
    """A project draft is held back by a PR for its ID, and resolved only by a fix merged after all its evidence."""

    EVIDENCE = (915, 916, 918)
    # Evidence merged out of number order: #915 opened first and merged last.
    MERGED = ("2026-09-20T00:00:00Z", "2026-09-05T00:00:00Z", "2026-09-08T00:00:00Z")
    AFTER_ALL = "2026-09-25T00:00:00Z"
    BETWEEN = "2026-09-10T00:00:00Z"

    def held(self, *pulls, prs=EVIDENCE, merged_at=MERGED):
        return plan_project_prs([draft("SCR-X-2", kind="pr", prs=prs, merged_at=merged_at)], FakePulls(*pulls))

    def test_an_id_with_no_pr_is_proposed(self):
        self.assertEqual(self.held(PullTitle(940, "[SCR-Y-1] other", "open")), {})

    def test_an_open_pr_for_the_id_holds_the_draft(self):
        self.assertEqual(self.held(PullTitle(946, "[SCR-X-2] fix", "open")),
                         {"SCR-X-2": "SKIPPED: #946 for this ID is open"})

    def test_a_fix_merged_after_all_the_evidence_resolves_the_id(self):
        self.assertEqual(self.held(PullTitle(946, "[SCR-X-2] fix", "merged", self.AFTER_ALL)),
                         {"SCR-X-2": "RESOLVED: #946 merged after the evidence"})

    def test_a_fix_merged_before_some_evidence_counts_only_the_reports_since_it(self):
        self.assertEqual(self.held(PullTitle(946, "[SCR-X-2] fix", "merged", self.BETWEEN)),
                         {"SCR-X-2": "RESOLVED: #946 merged after the earlier evidence; 1 report(s) since, below the "
                                     "threshold of 2"})

    def test_reports_since_the_fix_that_reach_the_threshold_propose_the_id_again(self):
        prs, merged_at = (10, 11, 12, 21, 22, 23), ("2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z",
                                                    "2026-09-03T00:00:00Z", "2026-09-11T00:00:00Z",
                                                    "2026-09-12T00:00:00Z", "2026-09-13T00:00:00Z")
        fix = PullTitle(20, "[SCR-X-2] fix", "merged", "2026-09-10T00:00:00Z")

        found = plan_project_prs([draft("SCR-X-2", kind="pr", prs=prs, merged_at=merged_at)], FakePulls(fix),
                                 threshold=3)

        self.assertEqual(found, {"SCR-X-2": "REOPENED: #20 merged; 3 report(s) since: #21, #22, #23"})

    def test_one_report_since_the_fix_does_not_re_propose_a_fixed_rule(self):
        prs, merged_at = (10, 11, 12, 21), ("2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z",
                                            "2026-09-03T00:00:00Z", "2026-09-11T00:00:00Z")
        fix = PullTitle(20, "[SCR-X-2] fix", "merged", "2026-09-10T00:00:00Z")

        found = plan_project_prs([draft("SCR-X-2", kind="pr", prs=prs, merged_at=merged_at)], FakePulls(fix),
                                 threshold=3)

        self.assertEqual(found, {"SCR-X-2": "RESOLVED: #20 merged after the earlier evidence; 1 report(s) since, "
                                            "below the threshold of 3"})

    def test_a_fix_opened_before_the_evidence_but_merged_after_all_of_it_resolves_the_id(self):
        self.assertEqual(self.held(PullTitle(900, "[SCR-X-2] fix", "merged", self.AFTER_ALL)),
                         {"SCR-X-2": "RESOLVED: #900 merged after the evidence"})

    def test_a_fix_that_is_itself_evidence_resolves_the_id_when_it_merged_last(self):
        found = self.held(PullTitle(946, "[SCR-X-2] fix", "merged", self.AFTER_ALL),
                          prs=self.EVIDENCE + (946,), merged_at=self.MERGED + (self.AFTER_ALL,))

        self.assertEqual(found, {"SCR-X-2": "RESOLVED: #946 merged after the evidence"})

    def test_a_decline_older_than_the_latest_fix_does_not_hold_new_evidence_back(self):
        prs, merged_at = (21, 22), ("2026-09-11T00:00:00Z", "2026-09-12T00:00:00Z")
        decline = PullTitle(10, "[SCR-X-2] first try", "closed", closed_at="2026-09-02T00:00:00Z")
        fix = PullTitle(20, "[SCR-X-2] fix", "merged", "2026-09-10T00:00:00Z", "2026-09-10T00:00:00Z")

        found = plan_project_prs([draft("SCR-X-2", kind="pr", prs=prs, merged_at=merged_at)], FakePulls(decline, fix))

        self.assertEqual(found, {"SCR-X-2": "REOPENED: #20 merged; 2 report(s) since: #21, #22"})

    def test_a_decline_after_the_latest_fix_still_holds_the_draft(self):
        prs, merged_at = (21, 22), ("2026-09-11T00:00:00Z", "2026-09-12T00:00:00Z")
        decline = PullTitle(30, "[SCR-X-2] second try", "closed", closed_at="2026-09-13T00:00:00Z")
        fix = PullTitle(20, "[SCR-X-2] fix", "merged", "2026-09-10T00:00:00Z", "2026-09-10T00:00:00Z")

        found = plan_project_prs([draft("SCR-X-2", kind="pr", prs=prs, merged_at=merged_at)], FakePulls(decline, fix))

        self.assertEqual(found, {"SCR-X-2": "SKIPPED: #30 for this ID was closed unmerged within the window"})

    def test_a_draft_with_no_evidence_or_no_merge_times_is_never_resolved(self):
        fix = PullTitle(946, "[SCR-X-2] fix", "merged", self.AFTER_ALL)

        for prs, merged_at in (((), ()), (self.EVIDENCE, ())):
            with self.subTest(prs=prs, merged_at=merged_at):
                self.assertEqual(self.held(fix, prs=prs, merged_at=merged_at), {})

    def test_a_pr_closed_unmerged_within_the_window_holds_the_draft(self):
        self.assertEqual(self.held(PullTitle(950, "[SCR-X-2] fix", "closed")),
                         {"SCR-X-2": "SKIPPED: #950 for this ID was closed unmerged within the window"})

    def test_with_no_project_draft_the_prs_are_not_read(self):
        class Unreadable:
            def recent_pulls(self, closed_since):
                raise SourceError("more than 1000 closed PRs")

        self.assertEqual(plan_project_prs([draft("TST-2", prs=self.EVIDENCE)], Unreadable()), {})

    def test_a_pr_for_a_longer_id_sharing_the_prefix_is_not_this_ids(self):
        self.assertEqual(self.held(PullTitle(946, "[SCR-X-20] other", "open")), {})

    def test_an_issue_draft_is_left_to_plan_issues(self):
        found = plan_project_prs([draft("TST-2", prs=self.EVIDENCE)], FakePulls(PullTitle(946, "[TST-2] x", "open")))

        self.assertEqual(found, {})


class AFixtureRow(unittest.TestCase):
    """A scratch run's PRs: a row marked open is open, whatever else it carries."""

    def test_an_open_row_with_a_merge_time_is_open_and_never_merged_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pulls.json"
            path.write_text(json.dumps([{"number": 7, "title": "[X-1] fix", "state": "open", "body": AMBIGUOUS,
                                         "merged_at": "2026-09-10T00:00:00Z", "labels": []}]))
            source = FixtureSource(path)

            evidence = source.unlabelled_merged(LABEL, "2026-09-01", 10)
            states = [(p.number, p.state, p.merged_at) for p in source.recent_pulls("2026-09-01")]

        self.assertEqual((evidence, states), ([], [(7, "open", "")]))


class AFixtureDecline(unittest.TestCase):
    """A fixture decline is dated by when it closed, as GitHub's are."""

    def listed(self, closed_at):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pulls.json"
            path.write_text(json.dumps([{"number": 9, "title": "[X-1] declined", "state": "closed",
                                         "closed_at": closed_at, "labels": []}]))
            return [p.number for p in FixtureSource(path).recent_pulls("2026-09-01")]

    def test_one_closed_inside_the_window_is_listed(self):
        self.assertEqual(self.listed("2026-09-10T00:00:00Z"), [9])

    def test_one_closed_before_the_window_is_not(self):
        self.assertEqual(self.listed("2026-08-20T00:00:00Z"), [])


class AFixtureRowWithNoTitle(unittest.TestCase):
    def test_is_refused_as_github_would_refuse_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pulls.json"
            path.write_text(json.dumps([{"number": 7, "state": "open", "labels": []}]))

            with self.assertRaises(SourceError):
                FixtureSource(path).recent_pulls(None)


class RunsTheCli(unittest.TestCase):
    """A scratch project, its PRs and issues as fixture files, and the CLI run against them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.project, self.pulls, self.issues = tmp / "project", tmp / "pulls.json", tmp / "issues.json"
        self.env = env_for(make_instance(tmp))
        (self.project / ".claude").mkdir(parents=True)
        (self.project / ".claude" / "trimtab.json").write_text(json.dumps(CONFIG))
        self.write_pulls([{"number": 101, "body": AMBIGUOUS, "merged_at": "2026-09-10T00:00:00Z", "labels": []}])

    def tearDown(self):
        self._tmp.cleanup()

    def write_pulls(self, rows):
        self.pulls.write_text(json.dumps(rows))

    def run_cli(self, *args, as_of, code=0):
        done = subprocess.run([str(TRIMTAB), *args, "--fixture", str(self.pulls), "--ledger-from-labels",
                               "--as-of", as_of], capture_output=True, text=True, cwd=self.project, env=self.env)
        self.assertEqual(done.returncode, code, done.stderr)
        return done.stdout

    def ingest(self, as_of):
        token = self.run_cli("ingest", "--dry-run", as_of=as_of).split("confirm: ")[1].split()[0]
        self.run_cli("ingest", "--apply", "--confirm", token, as_of=as_of)

    def propose(self, *args, as_of, code=0):
        return self.run_cli("propose", "--issues-fixture", str(self.issues), *args, as_of=as_of, code=code)


class TheConfirmationToken(unittest.TestCase):
    """The token covers where the issues go and what they say, nothing else."""

    def plan(self, merged_at, body="body"):
        d = Draft("issue", "TST-2", "[TST-2] reported ambiguous in 2 PRs", body, (ISSUE_LABEL,), (101, 102), merged_at)
        return plan_issues([d], FakeIssues())

    def test_evidence_merge_times_do_not_change_it(self):
        first = confirmation(self.plan(("2026-09-10T00:00:00Z", "2026-09-18T00:00:00Z")))
        second = confirmation(self.plan(("2026-09-10T00:00:00.000Z", "2026-09-18T00:00:00.000Z")))

        self.assertEqual(first, second)

    def test_what_the_issue_says_does_change_it(self):
        merged = ("2026-09-10T00:00:00Z", "2026-09-18T00:00:00Z")

        self.assertNotEqual(confirmation(self.plan(merged)), confirmation(self.plan(merged, body="other")))


class TwoWeeklyRuns(RunsTheCli):
    """End to end: one PR per week, no ledger file, and the second week proposes."""

    def test_evidence_from_separate_runs_reaches_the_threshold_and_files_once(self):
        self.ingest(as_of="2026-09-12")
        self.assertIn("0 proposal(s)", self.propose("--check-open", as_of="2026-09-12"))
        rows = json.loads(self.pulls.read_text())
        self.write_pulls(rows + [{"number": 102, "body": AMBIGUOUS, "merged_at": "2026-09-18T00:00:00Z",
                                  "labels": []}])

        self.ingest(as_of="2026-09-19")
        dry = self.propose("--check-open", as_of="2026-09-19")
        token = dry.split("confirm: ")[1].split()[0]
        self.propose("--apply", "--confirm", token, as_of="2026-09-19")
        again = self.propose("--check-open", as_of="2026-09-19")

        self.assertIn("1 proposal(s)", dry)
        self.assertIn(f"--issues-fixture {self.issues}", dry.split("to open")[1])
        self.assertIn("--history-limit", dry.split("to open")[1])
        self.assertTrue(all(LABEL in r["labels"] for r in json.loads(self.pulls.read_text())))
        self.assertEqual([i["title"][:7] for i in json.loads(self.issues.read_text())], ["[TST-2]"])
        self.assertIn("SKIPPED: an issue for this ID is open or was closed", again)
        self.assertNotIn("confirm: ", again)


class AProjectDraftAlreadyFixed(RunsTheCli):
    """End to end: the dry run marks a project draft whose fix PR merged after its evidence."""

    def test_check_open_marks_the_draft_resolved_by_the_merged_fix(self):
        body = AMBIGUOUS.replace("scope: upstream", "scope: project")
        self.write_pulls([{"number": n, "title": f"change {n}", "body": body, "merged_at": at, "labels": []}
                          for n, at in ((101, "2026-09-10T00:00:00Z"), (102, "2026-09-18T00:00:00Z"))]
                         + [{"number": 103, "title": "[TST-2] settle fake vs mock", "body": "",
                             "merged_at": "2026-09-19T00:00:00Z", "labels": []}])
        self.ingest(as_of="2026-09-20")

        dry = self.propose("--check-open", as_of="2026-09-20")

        self.assertIn("draft PR in this project (session step), RESOLVED: #103 merged after the evidence", dry)


class APullListingThatCannotBeRead(RunsTheCli):
    """The PR check failing marks the project drafts unchecked, but never hides the issue plan or its token."""

    def test_the_issue_token_is_still_printed_and_the_run_ends_2(self):
        both = AMBIGUOUS.replace('''  evidence: "in-memory fake vs mock is undecided for our own HTTP client"
```''', '''  evidence: "in-memory fake vs mock is undecided for our own HTTP client"
- id: TST-2
  kind: gap
  scope: project
  evidence: "the project's rule is silent on it"
```''')
        self.write_pulls([{"number": n, "title": f"change {n}", "body": both, "merged_at": at, "labels": []}
                          for n, at in ((101, "2026-09-10T00:00:00Z"), (102, "2026-09-18T00:00:00Z"))]
                         + [{"number": 104, "state": "open", "body": "", "labels": []}])  # no title: unreadable
        self.ingest(as_of="2026-09-20")

        dry = self.propose("--check-open", as_of="2026-09-20", code=2)

        self.assertIn("confirm: ", dry)
        self.assertIn("draft PR in this project (session step), NOT CHECKED: the PR listing could not be read", dry)


class AnIssueListingThatCannotBeRead(RunsTheCli):
    """The issue listing failing marks the issue drafts unchecked, but the project's marks are still printed."""

    def test_the_project_marks_are_still_printed_and_the_run_ends_2(self):
        both = AMBIGUOUS.replace('''  evidence: "in-memory fake vs mock is undecided for our own HTTP client"
```''', '''  evidence: "in-memory fake vs mock is undecided for our own HTTP client"
- id: TST-2
  kind: gap
  scope: project
  evidence: "the project's rule is silent on it"
```''')
        self.write_pulls([{"number": n, "title": f"change {n}", "body": both, "merged_at": at, "labels": []}
                          for n, at in ((101, "2026-09-10T00:00:00Z"), (102, "2026-09-18T00:00:00Z"))]
                         + [{"number": 103, "title": "[TST-2] settle fake vs mock", "body": "",
                             "merged_at": "2026-09-19T00:00:00Z", "labels": []}])
        self.issues.write_text(json.dumps([{"labels": [ISSUE_LABEL], "state": "open"}]))  # no title: unreadable
        self.ingest(as_of="2026-09-20")

        dry = self.propose("--check-open", as_of="2026-09-20", code=2)

        self.assertIn("draft PR in this project (session step), RESOLVED: #103 merged after the evidence", dry)
        self.assertIn("NOT CHECKED: the issue listing could not be read", dry)
        self.assertNotIn("confirm: ", dry)


class NoIssueDraft(RunsTheCli):
    """With no issue draft the issues are not read, so an issue listing that cannot be read holds nothing up."""

    def test_an_unreadable_issue_listing_with_no_issue_draft_still_ends_0_with_the_project_marks(self):
        body = AMBIGUOUS.replace("scope: upstream", "scope: project")
        self.write_pulls([{"number": n, "title": f"change {n}", "body": body, "merged_at": at, "labels": []}
                          for n, at in ((101, "2026-09-10T00:00:00Z"), (102, "2026-09-18T00:00:00Z"))]
                         + [{"number": 103, "title": "[TST-2] settle fake vs mock", "body": "",
                             "merged_at": "2026-09-19T00:00:00Z", "labels": []}])
        self.issues.write_text(json.dumps([{"labels": [ISSUE_LABEL], "state": "open"}]))  # no title: unreadable
        self.ingest(as_of="2026-09-20")

        dry = self.propose("--check-open", as_of="2026-09-20")

        self.assertIn("RESOLVED: #103 merged after the evidence", dry)


class NoWindow(RunsTheCli):
    """With no window only open PRs are listed, and the dry run says so rather than imply the rest were checked."""

    def run_ledger(self, *args, window):
        ledger = self.pulls.parent / "ledger.json"
        done = subprocess.run([str(TRIMTAB), *args, "--fixture", str(self.pulls), "--ledger", str(ledger),
                               "--window-weeks", str(window), "--as-of", "2026-09-20"],
                              capture_output=True, text=True, cwd=self.project, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def dry_run(self, window):
        body = AMBIGUOUS.replace("scope: upstream", "scope: project")
        self.write_pulls([{"number": n, "title": f"change {n}", "body": body, "merged_at": at, "labels": []}
                          for n, at in ((101, "2026-09-10T00:00:00Z"), (102, "2026-09-18T00:00:00Z"))])
        token = self.run_ledger("ingest", "--dry-run", window=window).split("confirm: ")[1].split()[0]
        self.run_ledger("ingest", "--apply", "--confirm", token, window=window)
        return self.run_ledger("propose", "--issues-fixture", str(self.issues), "--check-open", window=window)

    def test_with_no_window_the_dry_run_says_merged_and_closed_prs_were_not_checked(self):
        self.assertIn("no window: merged and closed [<ID>] PRs were not checked", self.dry_run(window=0))

    def test_with_a_window_it_says_nothing_of_the_kind(self):
        self.assertNotIn("no window", self.dry_run(window=8))


class TheIngestFloor(unittest.TestCase):
    """Builds a fixture project, instance and PR file in a temporary directory.

    `ingest` reads PRs merged since the later of the adoption and its floor,
    `--ingest-weeks` before the run (default 2), and says where it starts.
    """

    MERGED_THREE_WEEKS_AGO = "2026-09-20T00:00:00Z"
    AS_OF = "2026-10-11"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.project, self.pulls = tmp / "project", tmp / "pulls.json"
        self.env = env_for(make_instance(tmp))
        (self.project / ".claude").mkdir(parents=True)
        (self.project / ".claude" / "trimtab.json").write_text(json.dumps(CONFIG))
        self.pulls.write_text(json.dumps([{"number": 101, "body": AMBIGUOUS,
                                           "merged_at": self.MERGED_THREE_WEEKS_AGO, "labels": []}]))

    def tearDown(self):
        self._tmp.cleanup()

    def dry_run(self, *args):
        done = subprocess.run([str(TRIMTAB), "ingest", "--dry-run", "--fixture", str(self.pulls),
                               "--ledger-from-labels", "--as-of", self.AS_OF, *args],
                              capture_output=True, text=True, cwd=self.project, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_a_pr_merged_before_the_default_two_week_floor_is_not_read(self):
        out = self.dry_run()

        self.assertIn("to record: 0 PRs", out)

    def test_the_report_names_the_floor(self):
        out = self.dry_run()

        self.assertIn("ingest floor: PRs merged before 2026-09-27 are not read", out)

    def test_a_wider_floor_reads_the_pr(self):
        out = self.dry_run("--ingest-weeks", "4")

        self.assertIn("to record: 1 PRs", out)

    def test_the_apply_command_keeps_the_floor(self):
        out = self.dry_run("--ingest-weeks", "4")

        self.assertIn("--ingest-weeks 4", out.split("to apply:")[1])

    def test_propose_names_the_floor_too(self):
        done = subprocess.run([str(TRIMTAB), "propose", "--dry-run", "--fixture", str(self.pulls),
                               "--ledger-from-labels", "--as-of", self.AS_OF,
                               "--issues-fixture", str(self.pulls.parent / "issues.json")],
                              capture_output=True, text=True, cwd=self.project, env=self.env)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ingest floor: PRs merged before 2026-09-27 are not read", done.stdout)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class StructureAgainstARef(unittest.TestCase):
    """Growth is measured against the committed tree, so a routine needs no stored report."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "CLAUDE.md").write_text("# Core\n\n## 1. Small\n\n- **PREFER** short files.\n")
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "base")
        self.base = git(self.root, "rev-parse", "HEAD")

    def tearDown(self):
        self._tmp.cleanup()

    def test_growth_since_a_ref_is_a_finding(self):
        with (self.root / "CLAUDE.md").open("a") as fh:
            fh.write("\n## 2. Procedure\n\n" + "Run the long procedure step by step.\n" * 20)

        previous = structure.measure_at(self.root, self.base, [], 50).to_dict()
        report = structure.measure(self.root, [], 50, previous)

        self.assertEqual([f.code for f in report.findings], ["growth"])

    def test_an_unknown_ref_raises(self):
        with self.assertRaises(structure.TreeError):
            structure.measure_at(self.root, "no-such-ref", [], 50)

    def test_sections_are_listed_largest_first_with_binding_marked(self):
        with (self.root / "CLAUDE.md").open("a") as fh:
            fh.write("\n## 2. Procedure\n\n" + "Run the long procedure step by step.\n" * 20)
            fh.write("\n## 3. Secrets\n\n- **PROHIBIT** logging tokens.\n")
        report = structure.measure(self.root, [], 50)

        found = structure.sections(self.root, report)

        self.assertEqual(found[0].heading, "2. Procedure")
        self.assertFalse(found[0].binding)
        self.assertTrue(next(s for s in found if s.heading == "3. Secrets").binding)


if __name__ == "__main__":
    unittest.main()
