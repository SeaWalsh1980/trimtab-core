"""Who set or cleared a plan label, from a faked gh and, for a restart, real git (ADR 0080)."""

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import test_planauth as tpa
from gh_fake import FakeGh, path_of, query_of
from instance_fixture import make_instance
from trimtab import cli, mergeauthority, planlabels
from trimtab.mergeauthority import Accounts
from trimtab.planlabels import LabelCheckError

REPOSITORY = tpa.REPOSITORY
PLAN = tpa.PLAN
PLAN_PR = tpa.PLAN_PR
ADMIN = tpa.ADMIN
AGENT = tpa.AGENT
APPROVER = "the-approver[bot]"
STRANGER = "someone-else"
WHO = Accounts(ADMIN, APPROVER, AGENT)
PR = 21
OTHER = 22
CONTINUE = planlabels.CONTINUE_LABEL
BLOCKED = planlabels.BLOCKED_LABEL
ITEM_REF = f"plan/{PLAN}/item-01-part1"


ALL_EVENTS = f"repos/{REPOSITORY}/issues/events"
BEFORE_MERGE = "2026-11-01T10:00:00Z"  # the plan PR merged at tpa.MERGED_AT, 2026-11-03


def at(n):
    """A time after the plan merged, rising with `n` (under a minute's worth)."""
    return f"2026-11-10T10:00:{n:02d}Z"


def event(ident, action, label, actor, when=None):
    return {"id": ident, "event": action, "created_at": when or at(ident),
            "label": {"name": label}, "actor": None if actor is None else {"login": actor}}


def issue(number, labels=(), pull=True):
    row = {"number": number, "labels": [{"name": n} for n in labels]}
    return json.dumps({**row, "pull_request": {"url": "x"}} if pull else row)


def issue_path(number):
    return f"repos/{REPOSITORY}/issues/{number}"


def events_path(number):
    return f"{issue_path(number)}/events"


def pull_row(number, ref):
    return {"number": number, "head": {"ref": ref}}


class Actor(unittest.TestCase):
    """Mode 1: who added plan:continue."""

    def check(self, events, labels=(CONTINUE,), who=WHO, number=PR, label=CONTINUE, gh=None):
        gh = gh or FakeGh({issue_path(number): issue(number, labels), events_path(number): json.dumps(events)})
        return planlabels.check_actor(REPOSITORY, number, label, who, gh)

    def test_a_label_the_agent_added_passes_naming_the_actor(self):
        verdict = self.check([event(1, "labeled", CONTINUE, AGENT)])

        self.assertEqual((verdict.problems, verdict.pr, verdict.actor), ((), PR, AGENT))

    def test_a_label_the_approver_added_passes(self):
        self.assertEqual(self.check([event(1, "labeled", CONTINUE, APPROVER)]).problems, ())

    def test_a_label_the_admin_added_passes(self):
        self.assertEqual(self.check([event(1, "labeled", CONTINUE, ADMIN)]).problems, ())

    def test_logins_are_compared_without_case(self):
        self.assertEqual(self.check([event(1, "labeled", CONTINUE, AGENT.upper())]).problems, ())

    def test_an_account_the_instance_does_not_name_is_refused_and_named(self):
        verdict = self.check([event(1, "labeled", CONTINUE, STRANGER)])

        self.assertEqual(len(verdict.problems), 1)
        self.assertIn(STRANGER, verdict.problems[0])

    def test_an_agent_the_instance_does_not_name_does_not_count(self):
        verdict = self.check([event(1, "labeled", CONTINUE, AGENT)], who=Accounts(ADMIN, APPROVER))

        self.assertIn(AGENT, verdict.problems[0])

    def test_a_label_a_stranger_added_after_the_agent_is_refused(self):
        verdict = self.check([event(1, "labeled", CONTINUE, AGENT), event(2, "unlabeled", CONTINUE, AGENT),
                              event(3, "labeled", CONTINUE, STRANGER)])

        self.assertIn(STRANGER, verdict.problems[0])

    def test_a_label_the_agent_added_after_a_stranger_removed_and_added_it_passes(self):
        verdict = self.check([event(1, "labeled", CONTINUE, STRANGER), event(2, "unlabeled", CONTINUE, ADMIN),
                              event(3, "labeled", CONTINUE, AGENT)])

        self.assertEqual((verdict.problems, verdict.actor), ((), AGENT))

    def test_a_label_that_is_not_on_the_pr_now_is_refused(self):
        verdict = self.check([event(1, "labeled", CONTINUE, AGENT), event(2, "unlabeled", CONTINUE, AGENT)],
                             labels=())

        self.assertEqual(verdict.problems, (f"{CONTINUE} is not on PR #{PR}",))

    def test_a_label_with_no_labeled_event_is_refused(self):
        verdict = self.check([])

        self.assertEqual(verdict.problems, (f"PR #{PR} has no labeled event for {CONTINUE}",))

    def test_a_label_present_while_its_last_event_is_a_removal_is_refused(self):
        verdict = self.check([event(1, "labeled", CONTINUE, AGENT), event(2, "unlabeled", CONTINUE, AGENT)])

        self.assertIn("disagree", verdict.problems[0])

    def test_a_label_a_deleted_account_added_is_refused(self):
        verdict = self.check([event(1, "labeled", CONTINUE, None)])

        self.assertIn("deleted account", verdict.problems[0])

    def test_every_problem_is_listed_not_only_the_first(self):
        verdict = self.check([event(1, "labeled", CONTINUE, STRANGER)], labels=())

        self.assertEqual(len(verdict.problems), 2)

    def test_events_of_other_labels_are_not_the_labels_adder(self):
        verdict = self.check([event(1, "labeled", CONTINUE, AGENT), event(2, "labeled", "bug", STRANGER),
                              {"id": 3, "event": "commented", "created_at": at(3)}])

        self.assertEqual((verdict.problems, verdict.actor), ((), AGENT))

    def test_a_name_that_is_not_one_is_refused_before_any_read(self):
        gh = FakeGh()

        for repository, pr, label in (("not a repo", PR, CONTINUE), (REPOSITORY, 0, CONTINUE),
                                      (REPOSITORY, PR, BLOCKED), ("a/b/../c", PR, CONTINUE)):
            with self.subTest(repository=repository, pr=pr, label=label):
                verdict = planlabels.check_actor(repository, pr, label, WHO, gh)
                self.assertEqual(len(verdict.problems), 1)
        self.assertEqual(gh.calls, [])

    def test_a_failed_read_raises(self):
        with self.assertRaises(LabelCheckError):
            self.check([], gh=FakeGh(returncode=1, stderr="boom"))

    def test_an_issue_that_is_not_a_pr_raises(self):
        gh = FakeGh({issue_path(PR): issue(PR, (CONTINUE,), pull=False), events_path(PR): "[]"})

        with self.assertRaises(LabelCheckError):
            self.check([], gh=gh)

    def test_an_issue_with_another_number_raises(self):
        gh = FakeGh({issue_path(PR): issue(PR + 1, (CONTINUE,)), events_path(PR): "[]"})

        with self.assertRaises(LabelCheckError):
            self.check([], gh=gh)

    def test_a_list_cut_off_at_its_bound_raises(self):
        events = [event(i, "labeled", "bug", AGENT) for i in range(1, 5)]

        with mock.patch.object(planlabels, "EVENTS_LIMIT", 3), self.assertRaises(LabelCheckError):
            self.check(events)

    def test_events_out_of_order_raise(self):
        events = [event(2, "labeled", CONTINUE, AGENT), event(1, "labeled", CONTINUE, STRANGER)]

        with self.assertRaises(LabelCheckError):
            self.check(events)

    def test_a_time_that_runs_backwards_raises(self):
        events = [event(1, "labeled", "bug", AGENT, at(5)), event(2, "labeled", CONTINUE, AGENT, at(4))]

        with self.assertRaises(LabelCheckError):
            self.check(events)

    def test_a_label_event_with_no_label_name_raises(self):
        bad = {"id": 1, "event": "labeled", "created_at": at(1), "label": {}, "actor": {"login": AGENT}}

        with self.assertRaises(LabelCheckError):
            self.check([bad])

    def test_a_label_event_with_no_actor_key_raises(self):
        bad = {"id": 1, "event": "labeled", "created_at": at(1), "label": {"name": CONTINUE}}

        with self.assertRaises(LabelCheckError):
            self.check([bad])

    def test_an_event_with_an_unreadable_time_raises(self):
        with self.assertRaises(LabelCheckError):
            self.check([event(1, "labeled", CONTINUE, AGENT, "yesterday")])

    def test_a_reply_that_is_not_a_list_raises(self):
        gh = FakeGh({issue_path(PR): issue(PR, (CONTINUE,)), events_path(PR): json.dumps({"message": "x"})})

        with self.assertRaises(LabelCheckError):
            self.check([], gh=gh)


class Restart(tpa.GitRepository):
    """Mode 2: a default branch where PR 7 added the plan, and a scripted server."""

    def setUp(self):
        super().setUp()
        self.pulls = []
        self.events = {}
        self.labels = {}
        self.put(PLAN_PR, [])  # the plan's own PR is always read

    def sync(self):
        self.answers[f"repos/{REPOSITORY}/pulls"] = json.dumps(self.pulls)
        rows = []
        for number, events in self.events.items():
            rows += [{**e, "issue": {"number": number}} for e in events]
            self.answers[events_path(number)] = json.dumps(events)  # mode 1 reads one PR's events
            self.answers[issue_path(number)] = issue(number, self.labels.get(number, ()))
        # The repository-wide list: every PR's events together, newest first.
        self.answers[ALL_EVENTS] = json.dumps(sorted(rows, key=lambda r: -r["id"]))

    def put(self, number, events, labels=(), ref=None):
        """PR `number`, with its head ref (none: the plan PR, which is not in the pulls this lists) and events."""
        if ref is not None:
            self.pulls.append(pull_row(number, ref))
        self.events[number] = events
        self.labels[number] = labels
        self.sync()

    def check(self, who=WHO, gh=None):
        return planlabels.check_restart(str(self.repo), REPOSITORY, PLAN, who, gh or FakeGh(self.answers))

    def stopped_and_cleared(self, number=8, ref=ITEM_REF, remover=ADMIN):
        self.put(number, [event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, remover)], ref=ref)


class RestartAuthorised(Restart):
    def test_an_admin_removal_after_the_stop_passes_naming_the_pr(self):
        self.stopped_and_cleared()

        verdict = self.check()

        self.assertEqual((verdict.problems, verdict.pr, verdict.actor), ((), 8, ADMIN))

    def test_a_stop_before_the_draft_opened_is_found_on_the_plan_pr(self):
        self.put(PLAN_PR, [event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, ADMIN)])

        verdict = self.check()

        self.assertEqual((verdict.problems, verdict.pr), ((), PLAN_PR))

    def test_a_closed_item_pr_is_a_candidate_too(self):
        self.pulls.append({**pull_row(8, ITEM_REF), "state": "closed"})
        self.put(8, [event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, ADMIN)])

        self.assertEqual(self.check().pr, 8)

    def test_an_older_uncleared_stop_does_not_hide_a_newer_cleared_one(self):
        self.put(8, [event(1, "labeled", BLOCKED, AGENT)], labels=(BLOCKED,), ref=ITEM_REF)
        self.put(9, [event(3, "labeled", BLOCKED, AGENT), event(4, "unlabeled", BLOCKED, ADMIN)],
                 ref=f"plan/{PLAN}/item-02-part1")

        verdict = self.check()

        self.assertEqual((verdict.problems, verdict.pr), ((), 9))

    def test_a_removal_in_the_same_second_as_the_stop_counts_by_event_order(self):
        self.put(8, [event(1, "labeled", BLOCKED, AGENT, at(1)), event(2, "unlabeled", BLOCKED, ADMIN, at(1))],
                 ref=ITEM_REF)

        self.assertEqual(self.check().problems, ())

    def test_other_plans_and_lookalike_branches_are_not_candidates(self):
        self.stopped_and_cleared()
        for number, ref in ((30, "plan/0002-other/item-01-part1"), (31, f"plan/{PLAN}-x/item-01-part1"),
                            (32, f"plan/{PLAN}/other"), (33, f"feature/plan/{PLAN}/item-01-part1")):
            self.put(number, [event(number, "labeled", BLOCKED, AGENT)], labels=(BLOCKED,), ref=ref)

        verdict = self.check()

        self.assertEqual((verdict.problems, verdict.pr), ((), 8))


class RestartRefused(Restart):
    def test_a_removal_by_a_non_admin_is_refused_and_named(self):
        self.stopped_and_cleared(remover=STRANGER)

        verdict = self.check()

        self.assertEqual(len(verdict.problems), 1)
        self.assertIn(STRANGER, verdict.problems[0])

    def test_a_removal_by_the_agent_is_refused(self):
        self.stopped_and_cleared(remover=AGENT)

        self.assertIn(AGENT, self.check().problems[0])

    def test_a_removal_by_the_approver_is_refused(self):
        self.stopped_and_cleared(remover=APPROVER)

        self.assertIn(APPROVER, self.check().problems[0])

    def test_a_removal_by_a_deleted_account_is_refused(self):
        self.stopped_and_cleared(remover=None)

        self.assertIn("deleted account", self.check().problems[0])

    def test_a_label_added_again_after_the_admins_removal_is_a_stop_with_no_removal(self):
        self.put(8, [event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, ADMIN),
                     event(3, "labeled", BLOCKED, AGENT)], labels=(BLOCKED,), ref=ITEM_REF)

        verdict = self.check()

        self.assertEqual(len(verdict.problems), 2)
        self.assertIn("still on PR #8", verdict.problems[0])
        self.assertIn("no removal", verdict.problems[1])

    def test_a_non_admin_removal_after_an_admin_removal_of_an_earlier_stop_is_refused(self):
        self.put(8, [event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, ADMIN),
                     event(3, "labeled", BLOCKED, AGENT), event(4, "unlabeled", BLOCKED, STRANGER)], ref=ITEM_REF)

        self.assertIn(STRANGER, self.check().problems[0])

    def test_a_label_still_on_the_pr_after_the_admins_removal_is_refused(self):
        self.put(8, [event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, ADMIN)],
                 labels=(BLOCKED,), ref=ITEM_REF)

        self.assertEqual(self.check().problems, (f"{BLOCKED} is still on PR #8",))

    def test_a_stop_with_no_removal_is_refused_naming_it(self):
        self.put(8, [event(1, "labeled", BLOCKED, AGENT)], labels=(BLOCKED,), ref=ITEM_REF)

        verdict = self.check()

        self.assertEqual(verdict.pr, 8)
        self.assertTrue(any("no removal" in p for p in verdict.problems))

    def test_the_newest_stop_is_the_one_judged_not_an_older_cleared_one(self):
        self.put(8, [event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, ADMIN)], ref=ITEM_REF)
        self.put(9, [event(3, "labeled", BLOCKED, AGENT)], labels=(BLOCKED,), ref=f"plan/{PLAN}/item-02-part1")

        verdict = self.check()

        self.assertEqual(verdict.pr, 9)
        self.assertTrue(verdict.problems)

    def test_no_stop_anywhere_is_nothing_to_restart(self):
        self.put(8, [event(1, "labeled", "bug", AGENT)], ref=ITEM_REF)
        self.put(PLAN_PR, [])

        verdict = self.check()

        # in-flight.md relies on this: the one problem, opening the line
        self.assertEqual((verdict.pr, len(verdict.problems), verdict.problems[0].startswith("nothing to restart")),
                         (None, 1, True))

    def test_two_stops_in_the_same_second_cannot_be_ordered_and_are_refused(self):
        self.put(8, [event(1, "labeled", BLOCKED, AGENT, at(3)), event(2, "unlabeled", BLOCKED, ADMIN, at(3))],
                 ref=ITEM_REF)
        self.put(9, [event(5, "labeled", BLOCKED, AGENT, at(3)), event(6, "unlabeled", BLOCKED, ADMIN, at(3))],
                 ref=f"plan/{PLAN}/item-02-part1")

        verdict = self.check()

        self.assertIn("same second", verdict.problems[0])

    def test_two_removals_after_one_stop_are_refused(self):
        self.put(8, [event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, ADMIN),
                     event(3, "unlabeled", BLOCKED, ADMIN)], ref=ITEM_REF)

        self.assertIn("2 events follow", self.check().problems[0])

    def test_a_plan_pr_the_adding_commit_does_not_resolve_to_is_refused(self):
        self.answers[tpa.pulls_path(self.merge)] = json.dumps([tpa.listed_pull(PLAN_PR), tpa.listed_pull(99)])
        self.stopped_and_cleared()

        self.assertIn("2 PRs", self.check().problems[0])

    def test_a_plan_not_on_the_default_branch_is_refused(self):
        verdict = planlabels.check_restart(str(self.repo), REPOSITORY, "0009-nope", WHO, FakeGh(self.answers))

        self.assertTrue(verdict.problems)

    def test_a_name_that_is_not_one_is_refused_before_any_read(self):
        gh = FakeGh()

        verdicts = [planlabels.check_restart(str(self.repo), repository, plan, WHO, gh)
                    for repository, plan in (("nope", PLAN), (REPOSITORY, "a/b"), (REPOSITORY, "x..y"))]

        self.assertEqual(([len(v.problems) for v in verdicts], gh.calls), ([1, 1, 1], []))


class RestartReadFailures(Restart):
    def test_a_stale_fetch_raises(self):
        self.stopped_and_cleared()
        self.answers[f"repos/{REPOSITORY}/branches/{tpa.DEFAULT}"] = json.dumps({"commit": {"sha": "e" * 40}})

        with self.assertRaises(LabelCheckError):
            self.check()

    def test_a_failed_read_raises(self):
        with self.assertRaises(LabelCheckError):
            self.check(gh=FakeGh(returncode=1, stderr="boom"))

    def test_a_pulls_list_cut_off_at_its_bound_raises(self):
        for number in (8, 9, 10):
            self.pulls.append(pull_row(number, f"plan/other/item-{number}"))
        self.sync()

        with mock.patch.object(planlabels, "PULLS_LIMIT", 2), self.assertRaises(LabelCheckError):
            self.check()

    def test_a_pull_with_no_head_ref_raises(self):
        self.pulls.append({"number": 8, "head": {}})
        self.sync()

        with self.assertRaises(LabelCheckError):
            self.check()

    def test_events_out_of_order_raise(self):
        self.stopped_and_cleared()
        rows = [{**event(1, "labeled", BLOCKED, AGENT), "issue": {"number": 8}},
                {**event(2, "unlabeled", BLOCKED, ADMIN), "issue": {"number": 8}}]  # oldest first: not newest first
        self.answers[ALL_EVENTS] = json.dumps(rows)

        with self.assertRaises(LabelCheckError):
            self.check()

    def test_a_time_that_rises_down_the_list_raises(self):
        self.stopped_and_cleared()
        rows = [{**event(2, "unlabeled", BLOCKED, ADMIN, at(1)), "issue": {"number": 8}},
                {**event(1, "labeled", BLOCKED, AGENT, at(2)), "issue": {"number": 8}}]
        self.answers[ALL_EVENTS] = json.dumps(rows)

        with self.assertRaises(LabelCheckError):
            self.check()

    def test_a_plan_pr_with_no_merge_time_raises(self):
        self.answers[tpa.pull_path(PLAN_PR)] = json.dumps({"number": PLAN_PR, "merged_at": None})

        with self.assertRaises(LabelCheckError):
            self.check()

    def test_a_candidate_that_is_not_a_pr_raises(self):
        self.stopped_and_cleared()
        self.answers[issue_path(8)] = issue(8, (), pull=False)

        with self.assertRaises(LabelCheckError):
            self.check()

    def test_a_missing_admin_raises(self):
        with self.assertRaises(LabelCheckError):
            self.check(who=Accounts("", APPROVER, AGENT))


class RestartSnapshot(Restart):
    def test_a_stop_labelled_while_the_events_were_read_raises(self):
        self.stopped_and_cleared()
        reads = []
        base = FakeGh(self.answers)

        def run(argv, stdin):
            if argv[4].split("?")[0] == ALL_EVENTS:
                reads.append(argv)
                if len(reads) > 1:  # the second read shows a newer, uncleared stop
                    base.answers[ALL_EVENTS] = json.dumps(sorted([
                        {**e, "issue": {"number": 8}} for e in (
                            event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, ADMIN),
                            event(3, "labeled", BLOCKED, AGENT))], key=lambda r: -r["id"]))
            return base(argv, stdin)

        with self.assertRaises(LabelCheckError):
            self.check(gh=run)

    def test_a_pr_opened_while_the_list_was_read_raises(self):
        self.stopped_and_cleared()
        base = FakeGh(self.answers)
        lists = []

        def run(argv, stdin):
            if argv[4].split("?")[0] == f"repos/{REPOSITORY}/pulls":
                lists.append(argv)
                if len(lists) > 1:
                    base.answers[f"repos/{REPOSITORY}/pulls"] = json.dumps(
                        [*self.pulls, pull_row(9, f"plan/{PLAN}/item-02-part1")])
                    base.answers[ALL_EVENTS] = json.dumps(sorted(
                        [*json.loads(base.answers[ALL_EVENTS]),
                         {**event(5, "labeled", BLOCKED, AGENT), "issue": {"number": 9}}], key=lambda r: -r["id"]))
                    base.answers[issue_path(9)] = issue(9, (BLOCKED,))
            return base(argv, stdin)

        with self.assertRaises(LabelCheckError):
            self.check(gh=run)


class RestartBoundaries(Restart):
    def pages(self, *pages):
        """The repository-wide list as the server pages it (two rows a page here), served for both snapshot reads.
        A last page that is full is followed by an empty one, as the server answers."""
        self.answers[ALL_EVENTS] = [*(json.dumps(p) for p in pages), "[]"] * 2

    def row(self, ident, action, number, actor=AGENT, label=BLOCKED, when=None):
        return {**event(ident, action, label, actor, when), "issue": {"number": number}}

    def test_a_stop_on_the_second_page_is_found(self):
        self.stopped_and_cleared()
        filler = [self.row(9, "labeled", 50, label="bug"), self.row(8, "labeled", 51, label="bug")]
        self.pages(filler, [self.row(2, "unlabeled", 8, ADMIN), self.row(1, "labeled", 8)])

        with mock.patch.object(planlabels, "EVENTS_PAGE", 2):
            verdict = self.check()

        self.assertEqual((verdict.problems, verdict.pr), ((), 8))

    def test_an_event_older_than_the_plans_merge_stops_the_paging(self):
        self.stopped_and_cleared()
        old = [self.row(3, "labeled", 8, when=BEFORE_MERGE), self.row(2, "labeled", 8, when=BEFORE_MERGE)]
        self.answers[ALL_EVENTS] = [json.dumps([self.row(5, "unlabeled", 8, ADMIN), self.row(4, "labeled", 8)]),
                                    json.dumps(old)] * 2
        gh = FakeGh(self.answers)

        with mock.patch.object(planlabels, "EVENTS_PAGE", 2):
            verdict = self.check(gh=gh)

        reads = [query_of(argv) for argv in gh.argvs() if path_of(argv) == ALL_EVENTS]
        self.assertEqual((verdict.problems, [q["page"] for q in reads]), ((), ["1", "2", "1", "2"]))

    def test_events_older_than_the_plans_merge_are_not_the_stop(self):
        self.put(8, [event(1, "labeled", BLOCKED, AGENT, BEFORE_MERGE)], labels=(BLOCKED,), ref=ITEM_REF)

        verdict = self.check()

        self.assertTrue(verdict.problems[0].startswith("nothing to restart"))

    def test_a_stop_just_at_the_merge_time_counts(self):
        self.put(8, [event(1, "labeled", BLOCKED, AGENT, tpa.MERGED_AT)], labels=(BLOCKED,), ref=ITEM_REF)

        self.assertTrue(any("no removal" in p for p in self.check().problems))

    def test_reaching_the_page_cap_before_the_plans_merge_raises(self):
        self.stopped_and_cleared()
        full = [self.row(9, "labeled", 50, label="bug"), self.row(8, "labeled", 51, label="bug")]
        self.answers[ALL_EVENTS] = [json.dumps(full)] * 4

        with mock.patch.object(planlabels, "EVENTS_PAGE", 2), mock.patch.object(planlabels, "WINDOW_PAGES_LIMIT", 1), \
                self.assertRaises(LabelCheckError):
            self.check()

    def test_a_row_with_a_bad_time_or_no_issue_number_raises(self):
        self.stopped_and_cleared()
        good = self.row(2, "unlabeled", 8, ADMIN)
        for name, row in {"bad time": {**good, "created_at": "yesterday"},
                          "no time": {k: v for k, v in good.items() if k != "created_at"},
                          "no issue": {k: v for k, v in good.items() if k != "issue"},
                          "issue without a number": {**good, "issue": {}},
                          "number 0": {**good, "issue": {"number": 0}}}.items():
            with self.subTest(name):
                self.answers[ALL_EVENTS] = json.dumps([row])
                with self.assertRaises(LabelCheckError):
                    self.check()

    def test_a_label_row_of_a_pr_that_is_not_a_candidate_is_not_judged(self):
        self.stopped_and_cleared()
        stranger = {**self.row(9, "labeled", 77), "label": "x"}  # a malformed label event on an unrelated issue
        self.answers[ALL_EVENTS] = json.dumps(
            sorted([stranger, *json.loads(self.answers[ALL_EVENTS])], key=lambda r: -r["id"]))

        self.assertEqual(self.check().problems, ())

    def test_a_non_candidate_prs_stop_is_ignored(self):
        self.stopped_and_cleared()
        self.put(77, [event(9, "labeled", BLOCKED, AGENT)], labels=(BLOCKED,))  # not a candidate: no plan head ref

        verdict = self.check()

        self.assertEqual((verdict.problems, verdict.pr), ((), 8))

    def test_the_admin_removal_is_compared_without_case(self):
        self.stopped_and_cleared(remover=ADMIN.upper())

        self.assertEqual(self.check().problems, ())

    def test_every_pull_is_listed_oldest_first_whatever_its_state(self):
        self.stopped_and_cleared()
        gh = FakeGh(self.answers)

        self.check(gh=gh)

        listed = [query_of(argv) for argv in gh.argvs() if path_of(argv) == f"repos/{REPOSITORY}/pulls"]
        self.assertTrue(listed)
        for query in listed:
            self.assertEqual({k: query[k] for k in ("state", "sort", "direction")},
                             {"state": "all", "sort": "created", "direction": "asc"})


class AddingPr(Restart):
    def listed(self, rows):
        self.answers[tpa.pulls_path(self.merge)] = json.dumps(rows)

    def test_a_commit_in_no_merged_pr_is_refused(self):
        self.listed([])

        self.assertIn("0 PRs", self.check().problems[0])

    def test_an_unmerged_pr_or_one_into_another_base_is_not_the_plan_pr(self):
        self.listed([tpa.listed_pull(PLAN_PR, merged_at=None), tpa.listed_pull(99, base="other")])

        self.assertIn("0 PRs", self.check().problems[0])

    def test_a_reply_that_is_not_a_list_raises(self):
        self.answers[tpa.pulls_path(self.merge)] = json.dumps({"message": "x"})

        with self.assertRaises(LabelCheckError):
            self.check()

    def test_a_full_page_raises(self):
        self.listed([tpa.listed_pull(PLAN_PR), tpa.listed_pull(99, merged_at=None)])

        with mock.patch.object(tpa.planauth, "PULLS_LIMIT", 2), self.assertRaises(LabelCheckError):
            self.check()

    def test_a_row_with_no_base_or_a_bad_number_raises(self):
        for row in ({"number": PLAN_PR, "merged_at": tpa.MERGED_AT}, {**tpa.listed_pull(PLAN_PR), "number": 0},
                    "x"):
            with self.subTest(row=row):
                self.listed([row])
                with self.assertRaises(LabelCheckError):
                    self.check()

    def test_a_plan_file_git_cannot_place_or_answer_readably_raises(self):
        for failure in (planlabels.planscope.PlanScopeError("x"), ValueError("x"),
                        UnicodeDecodeError("ascii", b"\xff", 0, 1, "x")):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(planlabels.planscope, "added_by", side_effect=failure), \
                        self.assertRaises(LabelCheckError):
                    self.check()


class Shapes(Restart):
    def test_event_rows_that_are_not_the_expected_shape_raise(self):
        self.stopped_and_cleared()
        good = {**event(1, "labeled", BLOCKED, AGENT), "issue": {"number": 8}}
        bad_rows = {"id zero": {**good, "id": 0}, "negative id": {**good, "id": -1},
                    "not a row": "x", "actor with no login": {**good, "actor": {}},
                    "empty label name": {**good, "label": {"name": ""}},
                    "label not an object": {**good, "label": "x"},
                    "no event kind": {k: v for k, v in good.items() if k != "event"},
                    "event kind not a string": {**good, "event": 3}}
        for name, row in bad_rows.items():
            with self.subTest(name):
                self.answers[ALL_EVENTS] = json.dumps([row])
                with self.assertRaises(LabelCheckError):
                    self.check()

    def test_a_repeated_event_id_raises(self):
        self.stopped_and_cleared()
        rows = [{**event(1, "labeled", BLOCKED, AGENT), "issue": {"number": 8}},
                {**event(1, "unlabeled", BLOCKED, ADMIN), "issue": {"number": 8}}]
        self.answers[ALL_EVENTS] = json.dumps(rows)

        with self.assertRaises(LabelCheckError):
            self.check()

    def test_a_reply_that_is_not_a_list_raises(self):
        self.stopped_and_cleared()
        self.answers[ALL_EVENTS] = json.dumps({"message": "x"})

        with self.assertRaises(LabelCheckError):
            self.check()

    def test_an_issue_reply_that_is_not_the_expected_shape_raises(self):
        self.stopped_and_cleared()
        for reply in ("[]", json.dumps({"number": 8, "pull_request": {}, "labels": "x"}),
                      json.dumps({"number": 8, "pull_request": {}, "labels": [{}]}),
                      json.dumps({"number": 8, "pull_request": {}, "labels": ["x"]})):
            with self.subTest(reply=reply):
                self.answers[issue_path(8)] = reply
                with self.assertRaises(LabelCheckError):
                    self.check()

    def test_a_pull_number_that_is_not_a_positive_integer_raises(self):
        self.pulls.append({"number": 0, "head": {"ref": ITEM_REF}})
        self.sync()

        with self.assertRaises(LabelCheckError):
            self.check()

    def test_label_names_are_compared_without_case(self):
        self.put(8, [event(1, "labeled", "Plan:Blocked", AGENT), event(2, "unlabeled", "Plan:Blocked", ADMIN)],
                 labels=("Plan:Blocked",), ref=ITEM_REF)

        self.assertEqual(self.check().problems, (f"{BLOCKED} is still on PR #8",))

    def test_a_label_whose_name_differs_only_in_case_is_the_label_the_actor_check_reads(self):
        gh = FakeGh({issue_path(PR): issue(PR, ("Plan:Continue",)),
                     events_path(PR): json.dumps([event(1, "labeled", "Plan:Continue", STRANGER)])})

        verdict = planlabels.check_actor(REPOSITORY, PR, CONTINUE, WHO, gh)

        self.assertIn(STRANGER, verdict.problems[0])


class SkillAgreement(unittest.TestCase):
    def test_restart_reference_runs_the_command_and_names_each_exit_code(self):
        ref = Path(__file__).resolve().parents[2] / "skills" / "plan-run" / "references" / "restart.md"
        text = " ".join(ref.read_text(encoding="utf-8").split())

        self.assertIn("trimtab check-label-actor --repository <owner>/<name> --restart --plan <plan>", text)
        self.assertIn("Exit 0 prints the stopped PR's number alone", text)
        self.assertIn("Exit 1 names each problem", text)
        self.assertIn("On exit 2 run `git fetch origin` once", text)
        self.assertNotIn("gh api repos/{owner}/{repo}/issues", text)

    def test_in_flight_reference_says_what_each_exit_code_means_for_a_branch_with_no_pr(self):
        ref = Path(__file__).resolve().parents[2] / "skills" / "plan-run" / "references" / "in-flight.md"
        text = " ".join(ref.read_text(encoding="utf-8").split())

        self.assertIn("trimtab check-label-actor --repository <owner>/<name> --restart --plan <plan>", text)
        self.assertIn("Exit 1 whose only problem starts `nothing to restart`: the plan has no stop", text)
        self.assertIn("a second exit 2 is a stop", text)


class Cli(Restart):
    """`trimtab check-label-actor`: the real CLI, real git and a scripted gh on PATH."""

    def setUp(self):
        super().setUp()
        self.inst = make_instance(self.root)
        path = self.inst / "instance.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**data, "accounts": {"admin": ADMIN, "agent": AGENT, "approver": APPROVER}}),
                        encoding="utf-8")
        self.answers[tpa.VARIABLE_PATH] = json.dumps({"name": "TRIMTAB_ADMIN", "value": ADMIN})

    def run_cli(self, *argv, repository=REPOSITORY):
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        (self.root / "answers.json").write_text(json.dumps(self.answers), encoding="utf-8")
        gh = bin_dir / "gh"
        gh.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                      f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
                      "from gh_fake import path_of\n"
                      "answers = json.load(open(os.environ['FAKE_ANSWERS']))\n"
                      "path = path_of(sys.argv)\n"
                      "if path not in answers:\n    sys.exit('gh: Not Found (HTTP 404)')\n"
                      "print(answers[path])\n", encoding="utf-8")
        gh.chmod(0o755)
        env = {"TRIMTAB_INSTANCE": str(self.inst), "FAKE_ANSWERS": str(self.root / "answers.json"),
               "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", env), contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = cli.main(["check-label-actor", "--repository", repository, *argv])
        return code, out.getvalue(), err.getvalue()

    def actor(self, *extra, **kw):
        return self.run_cli("--label", CONTINUE, "--pr", str(PR), *extra, **kw)

    def restart(self, *extra):
        return self.run_cli("--restart", "--plan", PLAN, "--repo", str(self.repo), *extra)

    def continue_label(self, actor):
        self.put(PR, [event(1, "labeled", CONTINUE, actor)], labels=(CONTINUE,))

    def test_an_actor_the_instance_names_exits_0(self):
        self.continue_label(AGENT)

        code, out, err = self.actor()

        self.assertEqual((code, out), (0, f"PR #{PR}: {CONTINUE} added by {AGENT}\n"), err)

    def test_a_stranger_exits_1_naming_it(self):
        self.continue_label(STRANGER)

        code, out, err = self.actor()

        self.assertEqual((code, out, STRANGER in err), (1, "", True))

    def test_a_failed_read_exits_2(self):
        code, out, err = self.actor()

        self.assertEqual((code, out), (2, ""), err)

    def test_a_restart_exits_0_printing_the_stopped_pr_alone(self):
        self.stopped_and_cleared()

        code, out, err = self.restart()

        self.assertEqual((code, out), (0, "8\n"), err)

    def test_a_restart_with_no_removal_exits_1(self):
        self.put(8, [event(1, "labeled", BLOCKED, AGENT)], labels=(BLOCKED,), ref=ITEM_REF)

        code, out, err = self.restart()

        self.assertEqual((code, out, "no removal" in err), (1, "", True))

    def test_a_restart_with_nothing_to_restart_exits_1(self):
        self.put(PLAN_PR, [])

        code, out, err = self.restart()

        self.assertEqual((code, out, "nothing to restart" in err), (1, "", True))

    def test_a_restart_read_failure_exits_2(self):
        self.answers.pop(f"repos/{REPOSITORY}/pulls")

        code, out, err = self.restart()

        self.assertEqual((code, out), (2, ""), err)

    def test_a_bad_repository_exits_1_before_any_read(self):
        self.continue_label(AGENT)  # a read of any real path would answer; only a read of "nope" would 404 to exit 2

        code, out, err = self.run_cli("--label", CONTINUE, "--pr", str(PR), repository="nope")

        self.assertEqual((code, out, "owner/name" in err), (1, "", True))

    def test_the_two_modes_together_exit_2_naming_the_restart_rule(self):
        code, out, err = self.run_cli("--label", CONTINUE, "--pr", str(PR), "--restart", "--plan", PLAN)

        self.assertEqual((code, out, "--restart takes" in err), (2, "", True))

    def test_every_mixed_or_incomplete_argument_set_exits_2_naming_its_rule(self):
        restart_rule, actor_rule = "--restart takes", "give --label and --pr"
        for argv, rule in ((("--restart",), restart_rule), (("--restart", "--plan", PLAN, "--pr", "3"), restart_rule),
                           (("--restart", "--plan", PLAN, "--label", CONTINUE), restart_rule),
                           (("--label", CONTINUE), actor_rule), (("--pr", "3"), actor_rule),
                           (("--label", CONTINUE, "--pr", "3", "--plan", PLAN), actor_rule)):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out, rule in err), (2, "", True), err)

    def test_every_bad_name_exits_1_through_the_cli(self):
        for argv in (("--label", BLOCKED, "--pr", "3"), ("--label", CONTINUE, "--pr", "0"),
                     ("--restart", "--plan", "a/b")):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out), (1, ""), err)

    def test_an_unexpected_failure_in_a_restart_exits_2_not_1(self):
        err = io.StringIO()

        with mock.patch.dict("os.environ", {"TRIMTAB_INSTANCE": str(self.inst)}), \
                mock.patch.object(mergeauthority, "plan_admin_problems", return_value=()), \
                mock.patch.object(cli.planlabels, "check_restart", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(["check-label-actor", "--repository", REPOSITORY, "--restart", "--plan", PLAN])

        self.assertEqual((code, out.getvalue(), "RuntimeError" in err.getvalue()), (2, "", True))

    def test_neither_mode_exits_2_naming_the_actor_rule(self):
        code, out, err = self.run_cli()

        self.assertEqual((code, out, "give --label and --pr" in err), (2, "", True))

    def test_an_admin_the_repository_variable_disagrees_with_exits_1(self):
        self.answers[tpa.VARIABLE_PATH] = json.dumps({"name": "TRIMTAB_ADMIN", "value": STRANGER})
        self.continue_label(AGENT)

        code, out, err = self.actor()

        self.assertEqual((code, out, "TRIMTAB_ADMIN" in err), (1, "", True))

    def test_an_instance_naming_no_admin_exits_1_in_both_modes(self):
        path = self.inst / "instance.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("accounts", None)
        path.write_text(json.dumps(data), encoding="utf-8")
        self.continue_label(AGENT)
        self.stopped_and_cleared()

        for argv in (("--label", CONTINUE, "--pr", str(PR)), ("--restart", "--plan", PLAN, "--repo", str(self.repo))):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out, "names no admin" in err), (1, "", True), err)

    def test_a_malformed_instance_exits_2(self):
        path = self.inst / "instance.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**data, "accounts": {"admin": ADMIN, "agent": ADMIN}}), encoding="utf-8")
        self.continue_label(AGENT)

        code, out, err = self.actor()

        self.assertEqual((code, out), (2, ""), err)

    def test_an_unexpected_failure_exits_2_not_1(self):
        err = io.StringIO()

        with mock.patch.dict("os.environ", {"TRIMTAB_INSTANCE": str(self.inst)}), \
                mock.patch.object(mergeauthority, "plan_admin_problems", return_value=()), \
                mock.patch.object(cli.planlabels, "check_actor", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(["check-label-actor", "--repository", REPOSITORY, "--label", CONTINUE, "--pr", "1"])

        self.assertEqual((code, out.getvalue(), "RuntimeError" in err.getvalue()), (2, "", True))


if __name__ == "__main__":
    unittest.main()
