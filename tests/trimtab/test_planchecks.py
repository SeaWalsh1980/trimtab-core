"""The four plan-run checks that the hooks and `trimtab` subcommands call (ADR 0063 §5).

Each check joins the pure rows (`planreview`, `planrows`) to the facts and the ledger, and reports:
allowed, refused with the row's reason, or unreadable. Nothing here writes the ledger — the hook does
(§1) — so each check names the events the caller must append.

The facts are a fake of this codebase's own reader (TST-2: a fake, not a mock, and no interaction
assertions); the ledger is real, in a temporary config directory.
"""

import tempfile
import unittest
from pathlib import Path

from trimtab import planchecks, planledger, planreview, planrows

REPO = "owner/proj"
PLAN = "0008-slug"
BRANCH = f"plan/{PLAN}/item-02-part1"
MB, H1, H2 = "a" * 40, "b" * 40, "c" * 40
ZERO = "0" * 40
AGENT, REVIEWER = "claude-bot", "review-bot[bot]"
TAGS = {"fixed": "[fixed]", "wrong": "[wrong]", "intentional": "[intentional]", "moot": "[moot]",
        "deferred": "[deferred]"}
MARKERS = ("pytest.skip(",)
THRESHOLDS = (r"fail_under",)


def thread(ident=11, severity="high", state="open", by="-", path="src/a.py", line=2, key="1" * 64,
           commit=MB, run="42"):
    return planreview.Thread(run, ident, severity, state, by, path, line, "correctness", key, commit)


def review(head=H1, verdict="pass", tree="t" * 40, run="42"):
    return planreview.Review(run, head, tree, verdict)


def comment(ident, user, body, parent=None):
    return planreview.Comment(ident, user, body, parent)


class FakeFacts:
    """This codebase's own facts reader, faked. Any attribute left None raises, so a check that reads a
    fact a test did not set fails loudly rather than passing on a default."""

    def __init__(self, **kw):
        self.values = {
            "open_pr": planrows.PrState(7, True, "2026-11-12T10:00:00Z", "main"),
            "branch_head": H1, "secret_scan": "pass", "runs": [], "comments": [], "merge_base": MB,
            "tree_of": {MB: "m" * 40}, "line_text": {}, "branch_commits": (), "activity": (),
            "push_commits": {}, "last_answer": None, "on_default": (), "outside_diff": True,
            "fix_valid": True, "changes": (), "register_answer": False, "conflicted": False,
            "commit_of": H2, "pr_heads": {7: BRANCH}, "local_plan_branches": (),
            "progress_names": (), "pr_head_refs": (), "parents": (MB,), "base_runs": [],
            "change_from": None, "on_default": (MB,),
            "default_branch": "main", "failing_heads": set(), "facts_run_heads": set(), "pushes": [],
        }
        self.raises = kw.pop("raises", None)
        self.values.update(kw)

    def _value(self, name):
        if self.raises == name:
            raise planrows.FactsError(f"{name} could not be read")
        return self.values[name]

    def open_pr(self, branch): return self._value("open_pr")
    def branch_head(self, branch): return self._value("branch_head")
    def secret_scan(self, head, branch): return self._value("secret_scan")
    def facts_runs(self, branch, number): return self._value("runs")
    def review_comments(self, number): return self._value("comments")
    def merge_base(self, branch): return self._value("merge_base")
    def merge_base_of(self, rev): return self._value("merge_base")
    def branch_sha(self, branch): return self._value("branch_head")
    def parents(self, rev): return self._value("parents")
    def change_from(self, base, source):
        found = self._value("change_from")
        return self._value("changes") if found is None else found
    def facts_runs_for(self, head): return self._value("base_runs")
    def tree_of(self, commit): return self._value("tree_of")[commit]
    def line_text(self, commit, path, line): return self._value("line_text").get((path, line))
    def branch_commits(self, branch): return self._value("branch_commits")
    def activity(self, branch): return self._value("activity")
    def push_commits(self, old, new, base): return self._value("push_commits").get((old, new), frozenset())
    def last_answer(self, branch): return self._value("last_answer")
    def on_default(self, sha): return sha in self._value("on_default")
    def outside_diff(self, branch, head, path, line): return self._value("outside_diff")
    def fix_valid(self, branch, t, sha): return self._value("fix_valid")
    def changes(self, branch, source, head): return self._value("changes")
    def register_answer(self, branch, before, after): return self._value("register_answer")
    def conflicted(self, number): return self._value("conflicted")
    def default_branch(self): return self._value("default_branch")
    def failing_heads(self, branch): return self._value("failing_heads")
    def facts_run_heads(self, branch): return self._value("facts_run_heads")
    def pushes(self, branch): return self._value("pushes")
    def commit_of(self, rev): return self._value("commit_of")
    def pr_head(self, number, repo): return self._value("pr_heads").get(number)
    def pr_base(self, number, repo): return "main"
    def local_plan_branches(self): return self._value("local_plan_branches")
    def progress_names(self, plan): return self._value("progress_names")
    def pr_head_refs(self): return self._value("pr_head_refs")
    def repository(self): return REPO


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("subprocess").run, ["rm", "-rf", str(self.tmp)])
        self.clock = lambda: "2026-11-13T10:00:00+00:00"
        self.ledger = planledger.Ledger(self.tmp, REPO, PLAN, 2, 1, clock=self.clock)
        self.ledger.start(remote_branch_exists=False)
        self.limits = planchecks.Limits(markers=MARKERS, thresholds=THRESHOLDS, named_lines=frozenset())

    def context(self, facts, **kw):
        kw.setdefault("limits", self.limits)
        return planchecks.Context(repo=self.tmp, repository=REPO, branch=BRANCH, plan=PLAN, item=2, part=1,
                                  facts=facts, ledger=self.ledger, agent=AGENT, review_login=REVIEWER,
                                  tags=TAGS, **kw)

    def round(self, head, before_draft=True):
        for name in planledger.PASSES:
            self.ledger.round(head, name, before_draft=before_draft)


class ReadyCheck(Base):
    def facts(self, **kw):
        base = {"runs": [(review(head=H1), [thread()])],
                "comments": [comment(11, REVIEWER, "a finding"),
                             comment(12, AGENT, f"[fixed] {H2}", parent=11)]}
        return FakeFacts(**{**base, **kw})

    def test_a_reviewed_passing_head_with_every_thread_answered_and_closed_is_allowed(self):
        runs = [(review(head=H1), [thread()]),
                (review(head=H1, run="43"), [thread(state="resolved", by=REVIEWER, run="43")])]

        got = planchecks.ready(self.context(self.facts(runs=runs)), H1)

        self.assertEqual((got.refusal, got.unreadable), (None, None))

    def test_an_unlisted_comment_by_the_review_refuses_ready_and_stops_the_part(self):
        extra = [comment(21, REVIEWER, "another finding")]

        got = planchecks.ready(self.context(self.facts(comments=extra)), H1)

        self.assertIn("E5", got.refusal)
        self.assertIsNotNone(got.stop_reason)

    def test_a_head_no_run_passed_refuses(self):
        got = planchecks.ready(self.context(self.facts(runs=[(review(head=H2), [thread()])])), H1)

        self.assertIn("E7", got.refusal)

    def test_a_register_commit_counts_only_where_a_deferral_reply_names_each_file(self):
        # E7's alternative: without the naming clause a run could commit any content as a register
        # entry and have ready accept a head no review ever saw (ADR 0063 §5).
        entry = "docs/" + "plans/unauthorised/0008-slug/item-02-part1-01.md"
        change = planrows.Change(entry, "A", ("[x](y)",), 0, False, ())
        runs = [(review(head=H2), [thread(severity="low")]),
                (review(head=H2, run="43"), [thread(severity="low", state="resolved", by=REVIEWER,
                                                    run="43")])]
        named = [comment(11, REVIEWER, "a finding"),
                 comment(13, AGENT, f"[deferred] recorded in {entry}", parent=11)]
        unnamed = [comment(11, REVIEWER, "a finding"), comment(13, AGENT, "[deferred] elsewhere", parent=11)]

        with_name = planchecks.ready(self.context(self.facts(runs=runs, comments=named, changes=(change,),
                                                            register_answer=True)), H1)
        without = planchecks.ready(self.context(self.facts(runs=runs, comments=unnamed, changes=(change,),
                                                           register_answer=True)), H1)

        # With the file named, E7 is satisfied and the next row decides; without it, E7 itself refuses.
        self.assertNotIn("E7", with_name.refusal or "")
        self.assertIn("E7", without.refusal)

    def test_a_register_file_is_named_as_a_whole_path_not_as_a_piece_of_another(self):
        entry = "docs/" + "plans/unauthorised/0008-slug/item-02-part1-01.md"
        change = planrows.Change(entry, "A", ("[x](y)",), 0, False, ())
        runs = [(review(head=H2), [thread(severity="low")]),
                (review(head=H2, run="43"), [thread(severity="low", state="resolved", by=REVIEWER,
                                                    run="43")])]
        for naming in (f"see x/{entry}", f"{entry}.bak", f"{entry}x"):
            with self.subTest(naming):
                comments = [comment(11, REVIEWER, "a finding"),
                            comment(13, AGENT, f"[deferred] recorded in {naming}", parent=11)]

                got = planchecks.ready(self.context(self.facts(runs=runs, comments=comments,
                                                               changes=(change,), register_answer=True)), H1)

                self.assertIn("E7", got.refusal)

    def test_a_register_shaped_head_that_adds_no_register_file_is_not_an_answer(self):
        runs = [(review(head=H2), [])]
        facts = self.facts(runs=runs, comments=[], changes=(), register_answer=True)

        got = planchecks.ready(self.context(facts), H1)

        self.assertIn("E7", got.refusal)

    def test_a_thread_with_no_tagged_reply_refuses(self):
        got = planchecks.ready(self.context(self.facts(comments=[comment(11, REVIEWER, "a finding")])), H1)

        self.assertIn("E8", got.refusal)

    def test_a_fixed_reply_whose_commit_does_not_fix_the_finding_refuses(self):
        got = planchecks.ready(self.context(self.facts(fix_valid=False)), H1)

        self.assertIn("E9", got.refusal)

    def test_a_deferral_is_accepted_only_against_a_review_of_the_merge_bases_tree(self):
        key = planreview.thread_key("src/a.py", "correctness", "two")
        low = thread(severity="low", key=key)
        runs = [(review(head=H1, tree="m" * 40), [low]),
                (review(head=H1, run="43"), [thread(severity="low", key=key, state="resolved", by=REVIEWER,
                                                    run="43")])]
        comments = [comment(11, REVIEWER, "a finding"), comment(13, AGENT, "[deferred] out of scope", parent=11)]
        facts = self.facts(runs=runs, comments=comments, line_text={("src/a.py", 2): "two"})

        got = planchecks.ready(self.context(facts), H1)

        self.assertEqual((got.refusal, got.unreadable), (None, None))

    def test_a_deferral_is_accepted_against_a_review_that_ran_on_the_merge_base_commit(self):
        # Such a review runs on the default branch, so it is in `facts_runs_for`, never in the branch's
        # own runs (ADR 0063 §5, E8).
        key = planreview.thread_key("src/a.py", "correctness", "two")
        low = thread(severity="low", key=key)
        runs = [(review(head=H1, tree="b" * 40), [low]),
                (review(head=H1, run="43", tree="b" * 40),
                 [thread(severity="low", key=key, state="resolved", by=REVIEWER, run="43")])]
        base_runs = [(review(head=MB, run="7", tree="m" * 40), [thread(severity="low", key=key, run="7")])]
        comments = [comment(11, REVIEWER, "a finding"), comment(13, AGENT, "[deferred] out of scope", parent=11)]
        facts = self.facts(runs=runs, base_runs=base_runs, comments=comments,
                           line_text={("src/a.py", 2): "two"})

        got = planchecks.ready(self.context(facts), H1)

        self.assertEqual((got.refusal, got.unreadable), (None, None))

    def test_a_deferral_with_no_review_of_the_merge_base_refuses(self):
        key = planreview.thread_key("src/a.py", "correctness", "two")
        low = thread(severity="low", key=key)
        comments = [comment(11, REVIEWER, "a finding"), comment(13, AGENT, "[deferred] out of scope", parent=11)]
        facts = self.facts(runs=[(review(head=H1, tree="b" * 40), [low])], base_runs=[], comments=comments,
                           line_text={("src/a.py", 2): "two"})

        got = planchecks.ready(self.context(facts), H1)

        self.assertIn("E8", got.refusal)

    def test_a_deferral_whose_key_this_base_recomputes_differently_refuses(self):
        low = thread(severity="low", key="9" * 64)
        comments = [comment(11, REVIEWER, "a finding"), comment(13, AGENT, "[deferred] out of scope", parent=11)]
        facts = self.facts(runs=[(review(head=H1, tree="m" * 40), [low])], comments=comments,
                           line_text={("src/a.py", 2): "two"})

        got = planchecks.ready(self.context(facts), H1)

        self.assertIn("E8", got.refusal)

    def test_a_pending_or_failing_secret_scan_refuses_ready(self):
        closed = [(review(head=H1), [thread()]),
                  (review(head=H1, run="43"), [thread(state="resolved", by=REVIEWER, run="43")])]
        for state in ("pending", "fail", "unreadable"):
            with self.subTest(state=state):
                facts = self.facts(secret_scan=state, runs=closed)

                got = planchecks.ready(self.context(facts), H1)

                self.assertIn("E19", got.refusal)
                self.assertEqual(state != "pending", got.stop_reason is not None)

    def test_a_failed_read_is_unreadable_never_an_allow(self):
        got = planchecks.ready(self.context(FakeFacts(raises="runs")), H1)

        self.assertIsNone(got.refusal)
        self.assertIsNotNone(got.unreadable)

    def test_no_open_pr_refuses(self):
        got = planchecks.ready(self.context(self.facts(open_pr=None)), H1)

        self.assertIsNotNone(got.refusal)

    def test_an_instance_with_no_agent_login_refuses(self):
        ctx = planchecks.Context(repo=self.tmp, repository=REPO, branch=BRANCH, plan=PLAN, item=2, part=1,
                                 facts=self.facts(), ledger=self.ledger, agent=None, review_login=REVIEWER,
                                 tags=TAGS)

        got = planchecks.ready(ctx, H1)

        self.assertIsNotNone(got.unreadable)


class RerequestCheck(Base):
    def test_a_new_head_with_a_complete_round_and_one_push_after_it_is_allowed(self):
        self.round(H1, before_draft=False)
        self.ledger.push(BRANCH, MB, H1, run_env="1")
        facts = FakeFacts(runs=[], branch_commits=(H1,), push_commits={(MB, H1): frozenset({H1})})

        got = planchecks.rerequest(self.context(facts), H2)

        self.assertEqual((got.refusal, got.unreadable), (None, None))

    def test_a_head_already_reviewed_refuses(self):
        self.round(H1, before_draft=False)
        self.ledger.push(BRANCH, MB, H1, run_env="1")
        facts = FakeFacts(runs=[(review(head=H1, verdict="fail"), [])], branch_commits=(H1,),
                          push_commits={(MB, H1): frozenset({H1})})

        got = planchecks.rerequest(self.context(facts), H1)

        self.assertIn("E6", got.refusal)

    def test_a_fail_closed_verdict_may_be_rerequested_once(self):
        self.round(H1, before_draft=False)
        self.ledger.push(BRANCH, MB, H1, run_env="1")
        facts = FakeFacts(runs=[(review(head=H1, verdict="fail-closed"), [])], branch_commits=(H1,),
                          push_commits={(MB, H1): frozenset({H1})})
        ctx = self.context(facts)

        self.assertIsNone(planchecks.rerequest(ctx, H1).refusal)
        self.ledger.rerequest(H1, "fail-closed")
        self.assertIn("E6", planchecks.rerequest(ctx, H1).refusal)

    def test_a_recorded_push_that_never_landed_is_not_counted_as_one_after_the_round(self):
        self.round(H1, before_draft=False)
        self.ledger.push(BRANCH, MB, H1, run_env="1")
        self.ledger.push(BRANCH, H1, H2, run_env="1")  # never landed
        facts = FakeFacts(runs=[], branch_commits=(H1,),
                          push_commits={(MB, H1): frozenset({H1}), (H1, H2): frozenset({H2})})

        got = planchecks.rerequest(self.context(facts), "d" * 40)

        self.assertEqual((got.refusal, got.unreadable), (None, None))

    def test_no_complete_round_since_the_last_ci_answer_refuses(self):
        self.ledger.push(BRANCH, MB, H1, run_env="1")
        facts = FakeFacts(runs=[], branch_commits=(H1,), push_commits={(MB, H1): frozenset({H1})})

        got = planchecks.rerequest(self.context(facts), H2)

        self.assertIn("E10", got.refusal)


class PushCheck(Base):
    def change(self, path="tests/test_a.py", added=(), removed=(), status="M"):
        return planrows.Change(path, status, tuple(added), len(removed), False, tuple(removed))

    def test_an_ordinary_push_within_the_limits_is_allowed(self):
        facts = FakeFacts(changes=(self.change(added=("def test_b(): pass",)),))

        got = planchecks.push(self.context(facts), old=MB, new=H2)

        self.assertEqual((got.refusal, got.unreadable), (None, None))

    def test_a_push_adding_a_marker_the_plan_does_not_name_refuses(self):
        facts = FakeFacts(changes=(self.change(added=("    pytest.skip('flaky')",)),))

        got = planchecks.push(self.context(facts), old=MB, new=H2)

        self.assertIn("E18", got.refusal)

    def test_a_marker_the_plans_item_names_is_allowed(self):
        facts = FakeFacts(changes=(self.change(added=("    pytest.skip('flaky')",)),))
        limits = planchecks.Limits(MARKERS, THRESHOLDS, frozenset({"pytest.skip('flaky')"}))

        got = planchecks.push(self.context(facts, limits=limits), old=MB, new=H2)

        self.assertIsNone(got.refusal)

    def test_a_lowered_threshold_refuses(self):
        facts = FakeFacts(changes=(self.change(path="setup.cfg", added=("fail_under = 50",),
                                               removed=("fail_under = 90",)),))

        got = planchecks.push(self.context(facts), old=MB, new=H2)

        self.assertIn("E18", got.refusal)

    def test_an_instance_with_no_fix_limits_refuses_rather_than_skipping_e18(self):
        got = planchecks.push(self.context(FakeFacts(), limits=None), old=MB, new=H2)

        self.assertIsNotNone(got.refusal)
        self.assertIn("E18", got.refusal)

    def test_a_conflicted_head_is_named_for_the_ledger(self):
        facts = FakeFacts(conflicted=True)

        got = planchecks.push(self.context(facts), old=MB, new=H2)

        self.assertEqual(got.conflicted_head, H1)

    def test_the_ledgers_past_conflicts_count_towards_the_ci_fix_limit(self):
        heads = ["1" * 40, "2" * 40, "3" * 40]
        for head in heads:
            self.ledger.conflict(head)
        pushes = [planrows.Push(heads[0], heads[1]), planrows.Push(heads[1], heads[2]),
                  planrows.Push(heads[2], H1)]
        facts = FakeFacts(pushes=pushes, branch_head=H1, conflicted=True)

        got = planchecks.push(self.context(facts), old=heads[2], new=H2)

        self.assertIn("E12", got.refusal)

    def test_a_failed_read_is_unreadable(self):
        got = planchecks.push(self.context(FakeFacts(raises="changes")), old=MB, new=H2)

        self.assertIsNotNone(got.unreadable)

    def test_the_push_that_creates_the_branch_is_still_judged_by_e18(self):
        facts = FakeFacts(branch_head=None, open_pr=None,
                          changes=(self.change(added=("    pytest.skip('flaky')",)),))

        got = planchecks.push(self.context(facts), old=ZERO, new=H2)

        self.assertIn("E18", got.refusal)

    def test_a_clean_creating_push_is_allowed_and_counts_no_round(self):
        facts = FakeFacts(branch_head=None, open_pr=None, changes=(self.change(added=("def test_b(): pass",)),))

        got = planchecks.push(self.context(facts), old=ZERO, new=H2)

        self.assertEqual((got.refusal, got.unreadable), (None, None))

    def test_a_merge_of_the_default_branch_is_judged_from_its_default_parent(self):
        # ADR 0063 §5a: main's own lowered threshold is not the run's change.
        lowered = self.change(path="setup.cfg", added=("fail_under = 50",), removed=("fail_under = 90",))
        clean = self.change(path="src/a.py", added=("x = 1",))
        facts = FakeFacts(parents=(H1, MB), changes=(lowered,), change_from=(clean,))

        got = planchecks.push(self.context(facts), old=MB, new=H2)

        self.assertEqual((got.refusal, got.unreadable), (None, None))

    def test_the_runs_own_resolution_in_a_merge_is_still_judged(self):
        lowered = self.change(path="setup.cfg", added=("fail_under = 50",), removed=("fail_under = 90",))
        facts = FakeFacts(parents=(H1, MB), changes=(self.change(),), change_from=(lowered,))

        got = planchecks.push(self.context(facts), old=MB, new=H2)

        self.assertIn("E18", got.refusal)


class ResumeCheck(Base):
    def facts(self, **kw):
        events = (planreview.Activity(ZERO, H1, AGENT, "2026-11-12T11:00:00Z"),)
        base = {"branch_commits": (H1,), "activity": events, "merge_base": MB,
                "push_commits": {(ZERO, H1): frozenset({H1})}}
        return FakeFacts(**{**base, **kw})

    def test_a_branch_whose_every_commit_came_from_a_recorded_plan_run_push_passes(self):
        self.ledger.push(BRANCH, ZERO, H1, run_env="1")

        got = planchecks.resume(self.context(self.facts()), H1, scope_ok=True)

        self.assertEqual((got.refusal, got.unreadable), (None, None))

    def test_a_commit_no_ledger_push_covers_refuses(self):
        got = planchecks.resume(self.context(self.facts()), H1, scope_ok=True)

        self.assertIn("E13", got.refusal)

    def test_a_push_made_outside_a_plan_run_refuses(self):
        self.ledger.push(BRANCH, ZERO, H1, run_env="")

        got = planchecks.resume(self.context(self.facts()), H1, scope_ok=True)

        self.assertIn("E13", got.refusal)

    def test_a_push_the_ledger_recorded_but_that_never_landed_does_not_strand_the_part(self):
        # The entry is written before the push runs; a denied prompt, a rejected push or the network
        # leaves one the server never saw, and E13's pair equality used to refuse every later write.
        self.ledger.push(BRANCH, ZERO, H2, run_env="1")  # recorded, never landed: H2 is not on the branch
        self.ledger.push(BRANCH, ZERO, H1, run_env="1")

        got = planchecks.resume(self.context(self.facts(push_commits={(ZERO, H1): frozenset({H1}),
                                                                      (ZERO, H2): frozenset({H2})})),
                                H1, scope_ok=True)

        self.assertEqual((got.refusal, got.unreadable), (None, None))

    def test_a_push_that_landed_still_has_to_match_an_event(self):
        self.ledger.push(BRANCH, ZERO, H1, run_env="1")

        got = planchecks.resume(self.context(self.facts(activity=())), H1, scope_ok=True)

        self.assertIn("E13", got.refusal)

    def test_an_event_by_another_account_refuses(self):
        self.ledger.push(BRANCH, ZERO, H1, run_env="1")
        events = (planreview.Activity(ZERO, H1, "someone-else", "2026-11-12T11:00:00Z"),)

        got = planchecks.resume(self.context(self.facts(activity=events)), H1, scope_ok=True)

        self.assertIn("E13", got.refusal)

    def test_a_diff_outside_the_plans_scope_refuses(self):
        self.ledger.push(BRANCH, ZERO, H1, run_env="1")

        got = planchecks.resume(self.context(self.facts()), H1, scope_ok=False)

        self.assertIn("E13", got.refusal)

    def test_a_part_with_no_ledger_is_unreadable_never_an_allow(self):
        ledger = planledger.Ledger(self.tmp, REPO, PLAN, 2, 9, clock=self.clock)
        ctx = planchecks.Context(repo=self.tmp, repository=REPO, branch=BRANCH, plan=PLAN, item=2, part=9,
                                 facts=self.facts(), ledger=ledger, agent=AGENT, review_login=REVIEWER,
                                 tags=TAGS)

        got = planchecks.resume(ctx, H1, scope_ok=True)

        self.assertIsNotNone(got.unreadable)


class NamedLines(unittest.TestCase):
    """E18's exception: the lines the authorised plan's item section names."""

    PLAN = """# 0008 slug

A plan.

1. Do the first thing.
   It touches `fail_under = 50` on purpose.
2. Do the second thing, which names a marker:

   ```
   pytest.skip('flaky')
   ```

3. Do the third thing.
"""

    def test_the_items_own_section_names_its_lines(self):
        got = planreview.named_lines(self.PLAN, 2)

        self.assertIn("pytest.skip('flaky')", got)
        self.assertNotIn("fail_under = 50", got)

    def test_an_inline_code_span_in_the_item_counts(self):
        self.assertIn("fail_under = 50", planreview.named_lines(self.PLAN, 1))

    def test_an_item_the_plan_does_not_hold_names_nothing(self):
        self.assertEqual(planreview.named_lines(self.PLAN, 9), frozenset())

    def test_a_heading_shaped_item_section_is_read_too(self):
        text = "## Item 03\n\nSet `fail_under = 10`.\n\n## Item 04\n\nNothing.\n"

        self.assertIn("fail_under = 10", planreview.named_lines(text, 3))
        self.assertNotIn("fail_under = 10", planreview.named_lines(text, 4))


if __name__ == "__main__":
    unittest.main()
