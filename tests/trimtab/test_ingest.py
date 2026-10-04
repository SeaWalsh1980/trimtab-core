"""Ingest by watermark, tally per ID, and propose (plan sections 6a and 11)."""

import unittest

from trimtab.capture.ingest import (
    LABEL, InMemoryLedgerStore, Ledger, PullRequest, StalePlan, apply, confirmation, plan,
)
from trimtab.config import ProjectConfig
from trimtab.items import Item, Strength
from trimtab.propose import render
from trimtab.score import candidates, tally

CONFIG = ProjectConfig(trimtab_sha="abc", id_prefix="TT",
                       adopted_at="2026-09-27T00:00:00Z", baseline_pr=10)
REGISTRY = {"TST-2": Item(id="TST-2", prefix="TST", local_id="2", type="rule", source="rules/Example.md",
                          heading="Mocking Policy", strength=Strength.PROHIBIT)}


def body(feedback_id="TST-2"):
    return f"""## Harness items applied
```yaml
- id: TST-2
  why: chose an in-memory fake
```
## Harness feedback
```yaml
- id: {feedback_id}
  kind: ambiguous
  scope: upstream
  evidence: fake vs mock undecided for our own HTTP client
```
## Process cost
```yaml
```
"""


class FakeGitHub:
    """A true external system (GitHub via gh), so a fake is allowed."""

    def __init__(self, pulls, fail_label_once=None):
        self.pulls = {p.number: p for p in pulls}
        self.labels = {p.number: set() for p in pulls}
        self.fail_label_once = fail_label_once

    def unlabelled_merged(self, label, merged_since, limit):
        todo = [p for n, p in sorted(self.pulls.items()) if label not in self.labels[n]]
        return todo[:limit]

    def add_label(self, number, label):
        if self.fail_label_once == number:
            self.fail_label_once = None
            raise ConnectionError("simulated failure after recording")
        self.labels[number].add(label)


def three_merged_prs():
    return [
        PullRequest(11, body(), "2026-09-28T00:00:00Z"),
        PullRequest(12, body(), "2026-09-29T00:00:00Z"),
        PullRequest(13, "No block at all.", "2026-09-30T00:00:00Z"),
    ]


class SliceAcceptance(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub(three_merged_prs())
        self.store = InMemoryLedgerStore()

    def run_ingest(self, limit=50):
        ledger = self.store.load()
        todo = plan(self.github, ledger, CONFIG, REGISTRY, limit=limit)
        apply(todo, ledger, self.store, self.github, confirm=confirmation(todo))
        return self.store.load()

    def test_coverage_is_the_share_of_prs_with_a_valid_block(self):
        ledger = self.run_ingest()

        self.assertEqual(tally(ledger.records.values()).coverage, (2, 3))

    def test_feedback_is_tallied_per_id_by_distinct_pr(self):
        ledger = self.run_ingest()

        self.assertEqual(tally(ledger.records.values()).feedback_prs("TST-2"), 2)

    def test_propose_renders_exactly_one_upstream_issue(self):
        ledger = self.run_ingest()

        proposals = candidates(tally(ledger.records.values()))
        drafts = [render(p, REGISTRY) for p in proposals]

        self.assertEqual([(d.kind, d.item_id) for d in drafts], [("issue", "TST-2")])

    def test_a_run_stopped_after_one_pr_resumes_without_double_counting(self):
        self.run_ingest(limit=1)
        ledger = self.run_ingest()

        self.assertEqual(sorted(ledger.records), [11, 12, 13])
        self.assertEqual(tally(ledger.records.values()).feedback_prs("TST-2"), 2)

    def test_a_crash_between_recording_and_labelling_counts_once(self):
        self.github.fail_label_once = 11
        with self.assertRaises(ConnectionError):
            self.run_ingest()

        ledger = self.run_ingest()

        self.assertEqual(tally(ledger.records.values()).feedback_prs("TST-2"), 2)
        self.assertEqual(self.github.labels[11], {LABEL})

    def test_an_immediate_rerun_has_nothing_to_ingest(self):
        self.run_ingest()

        todo = plan(self.github, self.store.load(), CONFIG, REGISTRY, limit=50)

        self.assertEqual(todo.records, ())


class Watermark(unittest.TestCase):
    def test_prs_at_or_below_the_baseline_count_as_ingested_without_reading(self):
        github = FakeGitHub([PullRequest(9, body(), "2026-09-26T00:00:00Z"),
                             PullRequest(10, body(), "2026-09-26T00:00:00Z"),
                             PullRequest(11, body(), "2026-09-28T00:00:00Z")])

        todo = plan(github, Ledger(), CONFIG, REGISTRY, limit=50)

        self.assertEqual([r.number for r in todo.records], [11])
        self.assertEqual(todo.baseline_skipped, (9, 10))

    def test_a_dry_run_records_and_labels_nothing(self):
        github = FakeGitHub(three_merged_prs())
        store = InMemoryLedgerStore()

        plan(github, store.load(), CONFIG, REGISTRY, limit=50)

        self.assertEqual(store.load().records, {})
        self.assertEqual(github.labels[11], set())

    def test_apply_refuses_a_confirmation_from_a_different_plan(self):
        github = FakeGitHub(three_merged_prs())
        store = InMemoryLedgerStore()
        stale = confirmation(plan(github, store.load(), CONFIG, REGISTRY, limit=1))
        todo = plan(github, store.load(), CONFIG, REGISTRY, limit=50)

        with self.assertRaises(StalePlan):
            apply(todo, store.load(), store, github, confirm=stale)

        self.assertEqual(store.load().records, {})


class BlockVersion(unittest.TestCase):
    """ADR 0007: a body in a newer block version waits, unlabelled, for a Trimtab that reads it."""

    def setUp(self):
        self.github = FakeGitHub([PullRequest(11, "<!-- trimtab-block: 1 -->\n" + body(), "2026-09-28T00:00:00Z"),
                                  PullRequest(12, "<!-- trimtab-block: 2 -->\n" + body(), "2026-09-29T00:00:00Z")])
        self.store = InMemoryLedgerStore()
        self.todo = plan(self.github, self.store.load(), CONFIG, REGISTRY, limit=50)

    def test_a_version_1_marker_is_recorded(self):
        self.assertEqual([r.number for r in self.todo.records], [11])

    def test_a_newer_version_is_neither_recorded_nor_labelled(self):
        apply(self.todo, self.store.load(), self.store, self.github, confirm=confirmation(self.todo))

        self.assertEqual(self.todo.newer_version, (12,))
        self.assertNotIn(12, self.store.load().records)
        self.assertEqual(self.github.labels[12], set())
        self.assertEqual(self.github.labels[11], {LABEL})


class Thresholds(unittest.TestCase):
    def test_one_report_is_below_the_threshold(self):
        github = FakeGitHub([PullRequest(11, body(), "2026-09-28T00:00:00Z")])
        store = InMemoryLedgerStore()
        todo = plan(github, store.load(), CONFIG, REGISTRY, limit=50)
        apply(todo, store.load(), store, github, confirm=confirmation(todo))

        self.assertEqual(candidates(tally(store.load().records.values())), [])


if __name__ == "__main__":
    unittest.main()
