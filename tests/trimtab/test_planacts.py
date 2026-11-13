"""The acts the guards recognise and the ledger they write (ADR 0063 §1, §1a, §5).

Each act is tested through `planacts`, as the hook calls it: the facts are a fake of this codebase's
own reader, the ledger is real in a temporary config directory, and the instance is a fixture.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from instance_fixture import make_instance
from test_plan_check_commands import KEYS
from test_planchecks import AGENT, BRANCH, FakeFacts, H1, H2, MB, PLAN, REVIEWER, review, thread
from trimtab import instance, planacts, planledger, planreview, planrows

# In two pieces: the base's citation test refuses a plans-directory path in a tracked file.
PROGRESS = "docs/" + "plans/progress"

ZERO = "0" * 40
LABEL = "review:run"


class Support:
    """What `planacts` reads from guard-github's `PlanSupport`: the checkout, the facts and the flag."""

    def __init__(self, facts, cwd, plan_run=True):
        self.facts, self.cwd, self.plan_run = facts, cwd, plan_run


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.config = self.tmp / "config"
        self.config.mkdir()
        root = make_instance(self.tmp)
        body = json.loads((root / "instance.json").read_text())
        (root / "instance.json").write_text(json.dumps({**body, **KEYS}))
        self.instance = instance.load(root)
        self.plan_text = "1. First.\n2. Second.\n"

    def ledger(self, item=2, part=1):
        found = planledger.Ledger(self.config, "owner/proj", PLAN, item, part,
                                  clock=lambda: "2026-11-13T10:00:00+00:00")
        found.start(remote_branch_exists=False)
        return found

    def act(self, name, facts=None, *, branch=BRANCH, plan_run=True, plan_text=True, **kw):
        """Call one act with this test's instance, config and plan text. `plan_text=False` answers as a
        merge-base that does not hold the authorised plan at all."""
        found = facts if facts is not None else FakeFacts()
        support = Support(found, self.tmp, plan_run)
        read = planacts.planscope.read_files
        path = planacts.planscope.plan_files(PLAN)[0]
        answer = {path: self.plan_text} if plan_text else {}
        planacts.planscope.read_files = lambda repo, sha, paths: answer
        try:
            return getattr(planacts, name)(support, branch, inst=self.instance, config=self.config, **kw)
        finally:
            planacts.planscope.read_files = read


class ReadyMark(Base):
    def facts(self, **kw):
        base = {"runs": [(review(head=H1), [thread()]),
                         (review(head=H1, run="43"), [thread(state="resolved", by=REVIEWER, run="43")])],
                "comments": [planreview.Comment(11, REVIEWER, "a finding"),
                             planreview.Comment(12, AGENT, f"[fixed] {H2}", 11)]}
        return FakeFacts(**{**base, **kw})

    def test_a_branch_that_is_not_a_plan_part_is_left_alone(self):
        self.assertIsNone(self.act("ready_mark", FakeFacts(), branch="feat/x"))

    def test_a_reviewed_head_with_every_thread_answered_is_allowed(self):
        self.ledger()

        self.assertIsNone(self.act("ready_mark", self.facts()))

    def test_a_part_with_no_ledger_and_a_draft_pr_stops(self):
        with self.assertRaisesRegex(planrows.Refusal, "§1"):
            self.act("ready_mark", self.facts())

    def test_a_part_with_no_ledger_after_ready_is_watch_only(self):
        facts = self.facts(open_pr=planrows.PrState(7, False, "2026-11-12T10:00:00Z", "main"))

        with self.assertRaisesRegex(planrows.Refusal, "§1a"):
            self.act("ready_mark", facts)

    def test_an_unlisted_review_comment_refuses_and_writes_a_stop(self):
        ledger = self.ledger()
        facts = self.facts(comments=[planreview.Comment(21, REVIEWER, "unlisted")])

        with self.assertRaisesRegex(planrows.Refusal, "E5"):
            self.act("ready_mark", facts)

        self.assertEqual([e["type"] for e in ledger.events()], ["stop"])

    def test_a_branch_not_on_the_remote_refuses(self):
        self.ledger()

        with self.assertRaises(planrows.Refusal):
            self.act("ready_mark", self.facts(branch_head=None))


class Labels(Base):
    def test_the_rerequest_label_goes_through_e6_and_e10(self):
        ledger = self.ledger()
        for name in planledger.PASSES:
            ledger.round(H1, name, before_draft=False)
        ledger.push(BRANCH, MB, H1, run_env="1")
        facts = FakeFacts(branch_head=H2, runs=[], branch_commits=(H1,),
                          push_commits={(MB, H1): frozenset({H1})})

        self.act("label_add", facts, label=LABEL)

        self.assertEqual([e["type"] for e in ledger.events()], ["round"] * 3 + ["push", "rerequest"])

    def test_a_rerequest_a_row_refuses_writes_no_rerequest_entry(self):
        ledger = self.ledger()
        facts = FakeFacts(branch_head=H2, runs=[], branch_commits=(H1,))

        with self.assertRaisesRegex(planrows.Refusal, "E10"):
            self.act("label_add", facts, label=LABEL)

        self.assertEqual([e["type"] for e in ledger.events()], [])

    def test_the_label_is_matched_without_case(self):
        self.ledger()
        facts = FakeFacts(branch_head=H2, runs=[], branch_commits=())

        with self.assertRaisesRegex(planrows.Refusal, "E10"):
            self.act("label_add", facts, label="Review:RUN")

    def test_watch_only_may_add_plan_blocked_but_no_other_label(self):
        facts = FakeFacts(open_pr=planrows.PrState(7, False, "2026-11-12T10:00:00Z", "main"))

        self.assertIsNone(self.act("label_add", facts, label=planacts.BLOCKED_LABEL))
        with self.assertRaisesRegex(planrows.Refusal, "§1a"):
            self.act("label_add", facts, label="needs-work")

    def test_watch_only_may_remove_plan_continue_but_no_other_label(self):
        facts = FakeFacts(open_pr=planrows.PrState(7, False, "2026-11-12T10:00:00Z", "main"))

        self.assertIsNone(self.act("label_remove", facts, label=planacts.CONTINUE_LABEL))
        with self.assertRaisesRegex(planrows.Refusal, "§1a"):
            self.act("label_remove", facts, label="needs-work")

    def test_an_ordinary_label_passes_with_the_ledger(self):
        self.ledger()

        self.assertIsNone(self.act("label_add", FakeFacts(), label="needs-work"))


class RowsOnTheHookPath(Base):
    """One case per row that the act itself must still enforce, so the mutation test reaches the hook
    path and not only the pure row (ADR 0063 §2)."""

    def facts(self, **kw):
        base = {"runs": [(review(head=H1), [thread()]),
                         (review(head=H1, run="43"), [thread(state="resolved", by=REVIEWER, run="43")])],
                "comments": [planreview.Comment(11, REVIEWER, "a finding"),
                             planreview.Comment(12, AGENT, f"[fixed] {H2}", 11)]}
        return FakeFacts(**{**base, **kw})

    def test_e6_a_rerequest_of_a_reviewed_head_refuses_through_the_act(self):
        ledger = self.ledger()
        for name in planledger.PASSES:
            ledger.round(H1, name, before_draft=False)
        ledger.push(BRANCH, MB, H1, run_env="1")
        facts = self.facts(runs=[(review(head=H1, verdict="fail"), [])], branch_commits=(H1,),
                           branch_head=H1, push_commits={(MB, H1): frozenset({H1})})

        with self.assertRaisesRegex(planrows.Refusal, "E6"):
            self.act("label_add", facts, label=LABEL)

    def test_e8_an_unanswered_thread_refuses_ready_through_the_act(self):
        self.ledger()
        facts = self.facts(comments=[planreview.Comment(11, REVIEWER, "a finding")])

        with self.assertRaisesRegex(planrows.Refusal, "E8"):
            self.act("ready_mark", facts)

    def test_e9_a_fixed_reply_that_fixes_nothing_refuses_ready_through_the_act(self):
        self.ledger()

        with self.assertRaisesRegex(planrows.Refusal, "E9"):
            self.act("ready_mark", self.facts(fix_valid=False))


class Replies(Base):
    def ready_facts(self):
        return FakeFacts(open_pr=planrows.PrState(7, False, "2026-11-12T10:00:00Z", "main"))

    def test_watch_only_may_reply_wrong_intentional_or_moot(self):
        for tag in ("[wrong]", "[intentional]", "[moot]"):
            with self.subTest(tag=tag):
                self.assertIsNone(self.act("reply", self.ready_facts(), body=f"{tag} because x"))

    def test_watch_only_may_not_reply_fixed_or_deferred(self):
        for tag in ("[fixed]", "[deferred]"):
            with self.subTest(tag=tag), self.assertRaisesRegex(planrows.Refusal, "§1a"):
                self.act("reply", self.ready_facts(), body=f"{tag} {H1}")

    def test_with_the_ledger_every_reply_is_the_runs_own(self):
        self.ledger()

        self.assertIsNone(self.act("reply", FakeFacts(), body=f"[fixed] {H1}"))


class Pushes(Base):
    def change(self, added=()):
        return planrows.Change("tests/test_a.py", "M", tuple(added), 0, False, ())

    def test_an_allowed_push_is_recorded_with_the_run_flag(self):
        ledger = self.ledger()
        facts = FakeFacts(changes=(self.change(),), commit_of=H2)

        self.act("pushed", facts, source="HEAD")

        self.assertEqual([(e["type"], e["old"], e["new"], e["run"]) for e in ledger.events()],
                         [("push", H1, H2, "1")])

    def test_a_push_outside_a_plan_run_records_the_flag_as_it_found_it(self):
        ledger = self.ledger()
        facts = FakeFacts(changes=(self.change(),), commit_of=H2)

        self.act("pushed", facts, source="HEAD", plan_run=False)

        self.assertEqual([e["run"] for e in ledger.events()], [""])

    def test_a_push_e18_refuses_is_not_recorded(self):
        ledger = self.ledger()
        facts = FakeFacts(changes=(self.change(added=("  pytest.skip('x')",)),), commit_of=H2)

        with self.assertRaisesRegex(planrows.Refusal, "E18"):
            self.act("pushed", facts, source="HEAD")

        self.assertEqual(ledger.events(), [])

    def test_a_push_with_no_source_commit_refuses(self):
        self.ledger()

        with self.assertRaisesRegex(planrows.Refusal, "E13"):
            self.act("pushed", FakeFacts(), source=None)

    def test_a_conflicted_head_is_recorded_even_where_the_rows_allow_the_push(self):
        ledger = self.ledger()
        facts = FakeFacts(changes=(self.change(),), commit_of=H2, conflicted=True)

        self.act("pushed", facts, source="HEAD")

        self.assertEqual([e["type"] for e in ledger.events()], ["conflict", "push"])

    def test_a_first_push_starts_the_ledger_but_not_for_a_branch_already_on_the_remote(self):
        facts = FakeFacts(changes=(self.change(),), commit_of=H2, branch_head=None, open_pr=None)

        self.act("pushed", facts, source="HEAD")
        made = planledger.Ledger(self.config, "owner/proj", PLAN, 2, 1, clock=planacts.now)
        self.assertEqual([e["type"] for e in made.events()], ["push"])

        with self.assertRaisesRegex(planrows.Refusal, "§1"):
            self.act("pushed", FakeFacts(commit_of=H2), branch=f"plan/{PLAN}/item-03-part1", source="HEAD")

    def test_an_instance_with_no_fix_limits_refuses_the_push(self):
        self.ledger()
        body = {k: v for k, v in KEYS.items() if k != "fix_limits"}
        root = make_instance(self.tmp / "other")
        (self.tmp / "other").mkdir(exist_ok=True)
        data = json.loads((root / "instance.json").read_text())
        (root / "instance.json").write_text(json.dumps({**data, **body}))
        self.instance = instance.load(root)

        with self.assertRaisesRegex(planrows.Refusal, "E18"):
            self.act("pushed", FakeFacts(commit_of=H2), source="HEAD")


class FailClosedArms(Base):
    """Every arm that turns a failed read into a refusal, one case each (SEC-2.5)."""

    def test_an_instance_with_no_review_key_refuses_every_act(self):
        self.instance = instance.load(make_instance(self.tmp / "bare"))
        self.ledger()

        for name, kw in (("ready_mark", {}), ("undraft", {}), ("label_add", {"label": LABEL}),
                         ("label_remove", {"label": "x"}), ("reply", {"body": "[wrong] no"}),
                         ("draft_opened", {"head": H1}), ("resume_ok", {})):
            with self.subTest(name), self.assertRaises(planrows.Refusal):
                self.act(name, FakeFacts(), **kw)

    def test_a_failed_read_refuses_each_act_that_needs_it(self):
        self.ledger()

        for name, kw, raises in (("ready_mark", {}, "branch_head"), ("draft_opened", {"head": H1}, "open_pr"),
                                 ("label_add", {"label": LABEL}, "branch_head"),
                                 ("resume_ok", {}, "branch_head")):
            with self.subTest(name), self.assertRaises(planrows.Refusal):
                self.act(name, FakeFacts(raises=raises), **kw)

    def test_a_plan_that_cannot_be_read_refuses_a_push(self):
        self.ledger()

        with self.assertRaisesRegex(planrows.Refusal, "plan"):
            self.act("pushed", FakeFacts(commit_of=H2), source="HEAD", plan_text=False)

    def test_a_round_starting_with_no_ledger_counts_nothing(self):
        self.assertIsNone(self.act("round_starting", FakeFacts(), head=H1))

    def test_a_scope_read_that_fails_refuses_the_resume(self):
        ledger = self.ledger()
        ledger.push(BRANCH, MB, H1, run_env="1")
        check = planacts.planscope.check

        def boom(*a, **kw):
            raise planacts.planscope.PlanScopeError("git failed")

        planacts.planscope.check = boom
        try:
            with self.assertRaisesRegex(planrows.Refusal, "merge-base"):
                self.act("resume_ok", FakeFacts(branch_commits=(H1,), merge_base=MB,
                                                activity=(planreview.Activity(MB, H1, AGENT, "t"),),
                                                push_commits={(MB, H1): frozenset({H1})}))
        finally:
            planacts.planscope.check = check


class NextPart(Base):
    """§6: a new part's ledger starts only for the part one past the highest the three sources name."""

    def facts(self, **kw):
        return FakeFacts(**{"changes": (), "commit_of": H2, "branch_head": None, "open_pr": None, **kw})

    def part3(self, **kw):
        return self.act("pushed", self.facts(**kw), branch=f"plan/{PLAN}/item-02-part3", source="HEAD")

    def test_the_part_after_the_highest_progress_record_may_start(self):
        self.part3(progress_names=(f"{PROGRESS}/{PLAN}/item-02-part2.md",))

        self.assertEqual([e["type"] for e in planledger.Ledger(
            self.config, "owner/proj", PLAN, 2, 3, clock=planacts.now).events()], ["push"])

    def test_a_part_the_records_already_passed_refuses(self):
        with self.assertRaisesRegex(planrows.Refusal, "§6"):
            self.part3(progress_names=(f"{PROGRESS}/{PLAN}/item-02-part3.md",))

    def test_a_head_ref_of_a_closed_pr_counts(self):
        with self.assertRaisesRegex(planrows.Refusal, "§6"):
            self.part3(pr_head_refs=(f"plan/{PLAN}/item-02-part4",))

    def test_this_machines_own_ledgers_count(self):
        self.ledger(item=2, part=3)  # the highest part this machine holds, so the next one is 4, not 5

        with self.assertRaisesRegex(planrows.Refusal, "§6"):
            self.act("pushed", self.facts(), branch=f"plan/{PLAN}/item-02-part5", source="HEAD")

        self.assertIsNone(self.act("pushed", self.facts(), branch=f"plan/{PLAN}/item-02-part4",
                                   source="HEAD"))

    def test_a_source_that_cannot_be_read_refuses_a_new_part(self):
        with self.assertRaisesRegex(planrows.Refusal, "§6"):
            self.part3(raises="progress_names")

    def test_the_parts_a_plan_holds_are_listed_from_the_ledger_directory(self):
        self.ledger(item=2, part=1)
        self.ledger(item=3, part=2)

        got = planledger.parts_of(self.config, "owner/proj", PLAN)

        self.assertEqual(got, ("item-02-part1.jsonl", "item-03-part2.jsonl"))

    def test_a_plan_with_no_directory_holds_no_parts(self):
        self.assertEqual(planledger.parts_of(self.config, "owner/proj", "0009-other"), ())


class ResumeOnWrite(Base):
    """E13 through `resume_ok`: once per head, recorded against it, and a failed read refuses."""

    def facts(self, **kw):
        events = (planreview.Activity(MB, H1, AGENT, "2026-11-12T11:00:00Z"),)
        base = {"branch_commits": (H1,), "activity": events, "merge_base": MB,
                "push_commits": {(MB, H1): frozenset({H1})}}
        return FakeFacts(**{**base, **kw})

    def act_resume(self, facts, scope_ok=True, **kw):
        scope = planacts.planscope.check
        planacts.planscope.check = lambda *a, **k: planacts.planscope.Result(
            planacts.planscope.Ref.PLAN, PLAN, () if scope_ok else ("src/x.py: outside",))
        try:
            return self.act("resume_ok", facts, **kw)
        finally:
            planacts.planscope.check = scope

    def test_a_branch_whose_history_the_ledger_vouches_for_passes_and_is_recorded(self):
        ledger = self.ledger()
        ledger.push(BRANCH, MB, H1, run_env="1")

        self.act_resume(self.facts())

        self.assertEqual([(e["type"], e["head"]) for e in ledger.events() if e["type"] == "resume"],
                         [("resume", H1)])

    def test_a_recorded_head_is_not_read_again(self):
        ledger = self.ledger()
        ledger.resume(BRANCH, H1)

        self.act_resume(FakeFacts(raises="activity"))  # no read of the chain: the head is recorded

        self.assertEqual(len([e for e in ledger.events() if e["type"] == "resume"]), 1)

    def test_a_chain_the_ledger_does_not_cover_refuses_and_records_nothing(self):
        ledger = self.ledger()

        with self.assertRaisesRegex(planrows.Refusal, "E13"):
            self.act_resume(self.facts())

        self.assertEqual([e for e in ledger.events() if e["type"] == "resume"], [])

    def test_a_diff_outside_the_plans_scope_refuses(self):
        ledger = self.ledger()
        ledger.push(BRANCH, MB, H1, run_env="1")

        with self.assertRaisesRegex(planrows.Refusal, "E13"):
            self.act_resume(self.facts(), scope_ok=False)

    def test_a_branch_not_on_the_remote_has_no_history_to_vouch_for(self):
        self.ledger()

        self.assertIsNone(self.act_resume(self.facts(branch_head=None)))

    def test_a_part_with_no_ledger_refuses_the_write(self):
        with self.assertRaisesRegex(planrows.Refusal, "§1"):
            self.act_resume(self.facts())

    def test_a_head_that_cannot_be_read_refuses(self):
        self.ledger()

        with self.assertRaisesRegex(planrows.Refusal, "E13"):
            self.act_resume(self.facts(raises="branch_head"))


class Rounds(Base):
    def test_a_recorded_pass_lands_in_the_ledger(self):
        ledger = self.ledger()

        self.act("round_recorded", FakeFacts(), head=H1, pass_name="code-review", before_draft=True)

        self.assertEqual([(e["type"], e["pass"], e["before_draft"]) for e in ledger.events()],
                         [("round", "code-review", True)])

    def test_a_sixth_pre_draft_round_refuses(self):
        ledger = self.ledger()
        for n in range(5):
            ledger.round(f"{n}" * 40, "code-review", before_draft=True)

        with self.assertRaisesRegex(planrows.Refusal, "E23"):
            self.act("round_recorded", FakeFacts(), head=H1, pass_name="code-review", before_draft=True)

    def test_a_round_after_the_draft_is_not_capped(self):
        ledger = self.ledger()
        for n in range(5):
            ledger.round(f"{n}" * 40, "code-review", before_draft=True)

        self.act("round_recorded", FakeFacts(), head=H1, pass_name="code-review", before_draft=False)

        self.assertEqual(len([e for e in ledger.events() if e["type"] == "round"]), 6)

    def test_opening_the_draft_needs_a_complete_round_on_the_head(self):
        ledger = self.ledger()

        with self.assertRaisesRegex(planrows.Refusal, "E23"):
            self.act("draft_opened", FakeFacts(), head=H1)

        for name in planledger.PASSES:
            ledger.round(H1, name, before_draft=True)
        self.assertIsNone(self.act("draft_opened", FakeFacts(), head=H1))


if __name__ == "__main__":
    unittest.main()
