"""The retro's state lives on GitHub, not on disk (ADR 0006).

A routine has no durable disk, so its ledger is rebuilt each run from the PRs
already labelled `trimtab-ingested`, within the decay window; proposals are
opened as issues behind a confirmation, and never twice.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from instance_fixture import env_for, make_instance
from trimtab.capture.ingest import LABEL, PullRequest, rebuild
from trimtab.items import Item, Proposal, Strength
from trimtab.lint import structure
from trimtab.propose import (
    ISSUE_LABEL, Draft, StaleProposals, confirmation, open_issues, plan_issues, render,
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

    def __init__(self, titles=(), closed=(), destination="o/trimtab"):
        self.destination = destination
        self.issues = [{"title": t, "labels": [ISSUE_LABEL]} for t in titles]
        self.issues += [{"title": t, "labels": [ISSUE_LABEL], "closed_at": at} for t, at in closed]

    def recent_titles(self, label, closed_since):
        return [i["title"] for i in self.issues if label in i["labels"]
                and ("closed_at" not in i or (closed_since and i["closed_at"] >= closed_since))]

    def create(self, title, body, labels):
        self.issues.append({"title": title, "body": body, "labels": list(labels)})
        return f"issue {len(self.issues)}"


def draft(item_id, kind="issue"):
    return Draft(kind, item_id, f"[{item_id}] reported ambiguous in 2 PRs", "body", (ISSUE_LABEL,))


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

    def test_the_issue_names_the_reporting_project(self):
        proposal = Proposal("TST-2", "upstream", ("ambiguous",), ("e1", "e2"), (101, 102))

        body = render(proposal, REGISTRY, project="owner/consumer").body

        self.assertIn("project: owner/consumer", body)


class TwoWeeklyRuns(unittest.TestCase):
    """End to end: one PR per week, no ledger file, and the second week proposes."""

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

    def run_cli(self, *args, as_of):
        done = subprocess.run([str(TRIMTAB), *args, "--fixture", str(self.pulls), "--ledger-from-labels",
                               "--as-of", as_of], capture_output=True, text=True, cwd=self.project, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def ingest(self, as_of):
        token = self.run_cli("ingest", "--dry-run", as_of=as_of).split("confirm: ")[1].split()[0]
        self.run_cli("ingest", "--apply", "--confirm", token, as_of=as_of)

    def propose(self, *args, as_of):
        return self.run_cli("propose", "--issues-fixture", str(self.issues), *args, as_of=as_of)

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
