"""E22: `trimtab plan-ready-check --server` re-checks every server-fact row on the whole branch (ADR 0063 §7).

E5, E7, E8, E9 and E19 are the ready mark's own rows (`planchecks.ready`); the server adds E12, E14, E15 and E16
through the activity events, and E18 on the whole diff from the merge-base. The facts are a fake of this
codebase's own reader (TST-2); nothing here touches a ledger."""

import dataclasses
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from test_plan_check_commands import FLOWS, KEYS, PLANS
from test_planchecks import (AGENT, BRANCH, H1, H2, MB, PLAN, REPO, REVIEWER, ZERO, Base, FakeFacts, comment, review,
                             thread)
from trimtab import cli, planchecks, planreview, planrows
from trimtab import instance as instance_file
from trimtab.enforcement.gate import GateError

H3, H4, H5 = "d" * 40, "e" * 40, "f" * 40
WHEN = "2026-11-12T11:00:00Z"


def chain(*heads, actor=AGENT, start=MB):
    """Activity events from `start` through each head in turn, oldest first."""
    out, before = [], start
    for after in heads:
        out.append(planreview.Activity(before, after, actor, WHEN, "push"))
        before = after
    return tuple(out)


class ServerCheck(Base):
    def facts(self, head=H1, **kw):
        base = {"branch_head": head,
                "runs": [(review(head=head), [thread()]),
                         (review(head=head, run="43"), [thread(state="resolved", by=REVIEWER, run="43")])],
                "comments": [comment(11, REVIEWER, "a finding"), comment(12, AGENT, f"[fixed] {H2}", parent=11)],
                "activity": chain(head)}
        return FakeFacts(**{**base, **kw})

    def check(self, facts, head=H1, **kw):
        return planchecks.server(self.context(facts, **kw), head)

    def refused(self, facts, head=H1, **kw):
        got = self.check(facts, head, **kw)
        self.assertEqual(got.unreadable, None)
        return got.refusal or ""  # an allowed head fails the caller's assertion, as a failure and not an error

    def test_a_clean_branch_is_allowed(self):
        got = self.check(self.facts())

        self.assertEqual((got.refusal, got.unreadable), (None, None))

    def test_the_ready_rows_still_refuse_here(self):
        self.assertIn("E19", self.refused(self.facts(secret_scan="fail")))

    def test_a_head_no_run_passed_refuses_through_e7(self):
        self.assertIn("E7", self.refused(self.facts(runs=[(review(head=H2), [thread()])])))

    def test_a_push_by_another_account_refuses_through_e14(self):
        self.assertIn("E14", self.refused(self.facts(activity=chain(H1, actor="someone-else"))))

    def test_the_agent_login_compares_without_case(self):
        self.assertIsNone(self.check(self.facts(activity=chain(H1, actor=AGENT.upper()))).refusal)

    def test_a_force_push_refuses_through_e15(self):
        events = (planreview.Activity(MB, H1, AGENT, WHEN, "force_push"),)

        self.assertIn("E15", self.refused(self.facts(activity=events)))

    def test_an_event_of_a_kind_nobody_listed_refuses_through_e15(self):
        for kind in ("merge_queue_merge", "pr_merge", ""):
            events = (planreview.Activity(MB, H1, AGENT, WHEN, kind),)

            self.assertIn("E15", self.refused(self.facts(activity=events)), kind)

    def test_only_the_first_event_may_be_a_branch_creation(self):
        events = (planreview.Activity(ZERO, H1, AGENT, WHEN, "branch_creation"),
                  planreview.Activity(H1, H2, AGENT, WHEN, "branch_creation"))

        self.assertIn("E15", self.refused(self.facts(head=H2, activity=events), H2))

    def test_a_branch_created_then_pushed_is_a_chain(self):
        events = (planreview.Activity(ZERO, H1, AGENT, WHEN, "branch_creation"),
                  planreview.Activity(H1, H2, AGENT, WHEN, "push"))

        self.assertIsNone(self.check(self.facts(head=H2, activity=events), H2).refusal)

    def test_a_broken_chain_refuses_through_e15(self):
        events = (planreview.Activity(MB, H2, AGENT, WHEN, "push"), planreview.Activity(H3, H1, AGENT, WHEN, "push"))

        self.assertIn("E15", self.refused(self.facts(activity=events)))

    def test_a_chain_that_ends_before_the_head_refuses_through_e15(self):
        self.assertIn("E15", self.refused(self.facts(activity=chain(H2))))

    def test_a_chain_that_starts_off_the_default_branch_refuses_through_e15(self):
        self.assertIn("E15", self.refused(self.facts(activity=chain(H1, start=H3))))

    def test_a_branch_created_by_its_first_push_is_a_chain(self):
        self.assertIsNone(self.check(self.facts(activity=chain(H1, start=ZERO))).refusal)

    def test_a_branch_with_no_events_refuses(self):
        self.assertIn("E15", self.refused(self.facts(activity=())))

    def test_a_deletion_event_refuses_through_e16(self):
        events = (*chain(H1), planreview.Activity(H1, ZERO, AGENT, WHEN, "branch_deletion"))

        self.assertIn("E16", self.refused(self.facts(activity=events)))

    def test_a_branch_with_no_open_pr_refuses(self):
        self.assertIn("no open PR", self.refused(self.facts(open_pr=None)))

    def rounds(self, failing):
        heads = [H1, H2, H3, H4, H5]
        events = chain(*heads)
        pushes = [planrows.Push(e.before, e.after) for e in events]
        return self.facts(head=H5, activity=events, pushes=pushes, failing_heads=set(failing))

    def test_three_answered_failing_heads_are_allowed(self):
        self.assertIsNone(self.check(self.rounds([H1, H2, H3]), H5).refusal)

    def test_a_fourth_answered_failing_head_refuses_through_e12(self):
        self.assertIn("E12", self.refused(self.rounds([H1, H2, H3, H4]), H5))

    def test_a_head_that_was_reviewed_counts_as_a_round(self):
        facts = self.rounds([H1, H2, H3])
        facts.values["facts_run_heads"] = {H4}

        self.assertIn("E12", self.refused(facts, H5))

    def test_a_round_answered_only_by_register_commits_is_not_counted(self):
        facts = self.rounds([H1, H2, H3, H4])
        facts.values["register_answer"] = True

        self.assertIsNone(self.check(facts, H5).refusal)

    def test_an_added_skip_marker_in_the_whole_diff_refuses_through_e18(self):
        diff = (planrows.Change("tests/test_a.py", "M", added=("    pytest.skip('x')",)),)

        self.assertIn("E18", self.refused(self.facts(change_from=diff)))

    def test_a_deleted_test_file_in_the_whole_diff_refuses_through_e18(self):
        diff = (planrows.Change("tests/test_a.py", "D"),)

        self.assertIn("E18", self.refused(self.facts(change_from=diff)))

    def test_the_plans_named_line_is_allowed_through_e18(self):
        diff = (planrows.Change("tests/test_a.py", "M", added=("pytest.skip('flaky')",)),)
        limits = planchecks.Limits(("pytest.skip(",), (), frozenset({"pytest.skip('flaky')"}))

        self.assertIsNone(self.check(self.facts(change_from=diff), limits=limits).refusal)

    def test_an_instance_with_no_fix_limits_refuses_e18(self):
        self.assertIn("E18", self.refused(self.facts(), limits=None))

    def test_an_unreadable_activity_is_a_failed_read_not_a_pass(self):
        got = self.check(self.facts(raises="activity"))

        self.assertIsNotNone(got.unreadable)

    def test_an_instance_with_no_agent_is_a_failed_read(self):
        got = planchecks.server(dataclasses.replace(self.context(self.facts()), agent=None), H1)

        self.assertIsNotNone(got.unreadable)

    def test_a_branch_head_that_moved_since_the_job_started_is_a_failed_read(self):
        got = self.check(self.facts(branch_head=H2))

        self.assertIsNotNone(got.refusal or got.unreadable)


class Command(Base):
    """`trimtab plan-ready-check --server`: plumbing and exit codes; the rows are tested above."""

    facts = ServerCheck.facts

    def run_command(self, facts, argv=(), found="keys", plan_text="1. a\n2. b\n"):
        keys = instance_file.parse({"schema_version": 1, "repo": REPO, "base": {"repo": "o/b", "sha": "0" * 40},
                                    **KEYS}) if found == "keys" else found
        out, err = io.StringIO(), io.StringIO()
        text = {f"{PLANS}authorised/{PLAN}.md": plan_text}
        with mock.patch.object(cli.enforcement_gate, "server_instance", lambda *a, **kw: keys), \
                mock.patch.object(cli.planguard, "build_facts", lambda repo, **kw: facts), \
                mock.patch.object(cli.planscope, "read_files", lambda repo, sha, paths: text), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp)}), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["plan-ready-check", "--server", "--repository", REPO, "--repo", str(self.tmp),
                             "--branch", BRANCH, "--head", H1, *argv])
        return code, err.getvalue()

    def test_exits_0_when_every_row_allows_it(self):
        code, _ = self.run_command(self.facts())

        self.assertEqual(code, 0)

    def test_exits_1_naming_the_row_that_refuses(self):
        code, err = self.run_command(self.facts(activity=chain(H1, actor="someone-else")))

        self.assertEqual((code, "E14" in err), (1, True))

    def test_exits_2_on_a_failed_read(self):
        code, _ = self.run_command(self.facts(raises="activity"))

        self.assertEqual(code, 2)

    def test_exits_2_when_the_lock_cannot_give_the_instance(self):
        with mock.patch.object(cli.enforcement_gate, "server_instance", side_effect=GateError("no lock")):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["plan-ready-check", "--server", "--repository", REPO, "--branch", BRANCH,
                                 "--head", H1])

        self.assertEqual(code, 2)

    def test_exits_2_when_no_repository_is_named(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli.main(["plan-ready-check", "--server", "--branch", BRANCH, "--head", H1])

        self.assertEqual((code, "--repository" in err.getvalue()), (2, True))

    def test_exits_2_when_the_checkout_is_another_repository(self):
        facts = self.facts()
        facts.repository = lambda: "someone/else"

        code, _ = self.run_command(facts)

        self.assertEqual(code, 2)


class ActivityKind(unittest.TestCase):
    def test_an_activity_event_carries_its_kind(self):
        self.assertEqual(planreview.Activity(MB, H1, AGENT, WHEN, "force_push").kind, "force_push")

    def test_the_kind_defaults_to_nothing_for_older_callers(self):
        self.assertEqual(planreview.Activity(MB, H1, AGENT).kind, "")


if __name__ == "__main__":
    unittest.main()
