"""Ingest by watermark, tally per ID, and propose (ADR 0005)."""

import unittest

from trimtab.capture.ingest import (
    LABEL, InMemoryLedgerStore, Ledger, PullRequest, StalePlan, Unlabelled, apply, confirmation, plan,
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

    def unlabelled_merged(self, label, merged_since, limit, above=None):
        todo = [p for n, p in sorted(self.pulls.items()) if label not in self.labels[n]
                and (merged_since is None or p.merged_at[:10] >= merged_since)]
        below = tuple(p.number for p in todo if above is not None and p.number <= above)
        todo = [p for p in todo if p.number not in below]
        return Unlabelled(tuple(todo[:limit]), below, max(len(todo) - limit, 0))

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
        github = FakeGitHub([PullRequest(9, body(), "2026-09-27T00:00:00Z"),
                             PullRequest(10, body(), "2026-09-27T00:00:00Z"),
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


class IngestFloor(unittest.TestCase):
    """A run reads PRs merged since the later of the adoption and its floor, so each run's read stays bounded."""

    def setUp(self):
        self.github = FakeGitHub([PullRequest(11, body(), "2026-09-28T00:00:00Z"),
                                  PullRequest(12, body(), "2026-10-05T00:00:00Z")])

    def test_a_pr_merged_before_the_floor_is_not_read(self):
        todo = plan(self.github, Ledger(), CONFIG, REGISTRY, limit=50, floor="2026-10-01")

        self.assertEqual([r.number for r in todo.records], [12])

    def test_a_floor_before_the_adoption_reads_from_the_adoption(self):
        github = FakeGitHub([PullRequest(11, body(), "2026-09-26T00:00:00Z"),
                             PullRequest(12, body(), "2026-09-28T00:00:00Z")])

        todo = plan(github, Ledger(), CONFIG, REGISTRY, limit=50, floor="2026-09-01")

        self.assertEqual([r.number for r in todo.records], [12])

    def test_a_backfill_ignores_the_floor(self):
        todo = plan(self.github, Ledger(), CONFIG, REGISTRY, limit=50, backfill=True, floor="2026-10-01")

        self.assertEqual([r.number for r in todo.records], [11, 12])


LIMIT_THEN = 50  # the default --limit of the 10-24 run


def merged_prs(first, count):
    """`count` merged PRs numbered from `first`, all after the adoption."""
    return [PullRequest(n, body(), "2026-09-28T00:00:00Z") for n in range(first, first + count)]


def baseline_prs(count):
    """PRs at or below CONFIG's baseline, merged after the adoption: never labelled, so every read finds them."""
    return merged_prs(CONFIG.baseline_pr - count + 1, count)


def prs_after_baseline(count):
    return merged_prs(CONFIG.baseline_pr + 1, count)


class LimitAndBaseline(unittest.TestCase):
    """The 10-24 run: 9 baseline PRs took 9 of the 50 places, so 41 were recorded and 7 left unread, unreported."""

    BASELINE_SEEN = 9
    RECORDED_THEN = 41
    LEFT_OUT_THEN = 7

    def ingest_of(self, after_baseline):
        github = FakeGitHub(baseline_prs(self.BASELINE_SEEN) + prs_after_baseline(after_baseline))
        return plan(github, Ledger(), CONFIG, REGISTRY, limit=LIMIT_THEN)

    def test_baseline_prs_take_no_place_in_the_limit(self):
        todo = self.ingest_of(self.RECORDED_THEN + self.LEFT_OUT_THEN)

        self.assertEqual(len(todo.records), self.RECORDED_THEN + self.LEFT_OUT_THEN)

    def test_a_run_past_the_limit_records_the_limit_from_prs_after_the_baseline(self):
        todo = self.ingest_of(LIMIT_THEN + self.LEFT_OUT_THEN)

        self.assertEqual([r.number for r in todo.records],
                         [p.number for p in prs_after_baseline(LIMIT_THEN)])

    def test_a_run_past_the_limit_counts_the_prs_it_left(self):
        todo = self.ingest_of(LIMIT_THEN + self.LEFT_OUT_THEN)

        self.assertEqual(todo.left, self.LEFT_OUT_THEN)

    def test_a_run_within_the_limit_leaves_none(self):
        todo = self.ingest_of(self.RECORDED_THEN + self.LEFT_OUT_THEN)

        self.assertEqual(todo.left, 0)

    def test_every_baseline_pr_read_is_still_counted(self):
        todo = self.ingest_of(LIMIT_THEN + self.LEFT_OUT_THEN)

        self.assertEqual(todo.baseline_skipped, tuple(p.number for p in baseline_prs(self.BASELINE_SEEN)))


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
