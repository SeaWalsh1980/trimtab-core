"""The plan-run write rows E12, E14-E17 and E19 as pure decisions (ADR 0063 §5)."""

import unittest

from trimtab import planrows as pr
from trimtab.planrows import Change, Facts, Push, PrState, Refusal, Write

PLANS = "docs/" + "plans/"

BRANCH = "plan/0008-slug/item-02-part1"
H1, H2, H3, H4, H5 = (c * 40 for c in "12345")


def words(text):
    return [(w, False) for w in text.split()]


def read(text, current=lambda: BRANCH):
    return pr.read_push(words(text), current)


class FakeFacts(Facts):
    """An in-memory stand-in for what the guards read from GitHub and git (TST-2)."""

    def __init__(self, pr_state=None, scan="pass", head=H1, changes=(), pushes=(), failing=(), facts_runs=(),
                 conflicted_now=False, broken=(), register_answers=(), local=(), heads=None, bases=None,
                 repo="owner/proj"):
        self.pr_state, self.scan, self.head, self._changes = pr_state, scan, head, tuple(changes)
        self._pushes, self._failing, self._facts = tuple(pushes), set(failing), set(facts_runs)
        self.conflicted_now, self.broken = conflicted_now, set(broken)
        self.register_answers, self.local = set(register_answers), tuple(local)
        self.heads, self.bases, self.repo = dict(heads or {}), dict(bases or {}), repo
        self.reads = []
        self.looked_up = []

    def _read(self, name):
        self.reads.append(name)
        if name in self.broken:
            raise pr.FactsError(f"{name} unreadable")

    def open_pr(self, branch):
        self._read("open_pr")
        return self.pr_state

    def branch_head(self, branch):
        self._read("branch_head")
        return self.head

    def secret_scan(self, head, branch):
        self._read("secret_scan")
        return self.scan

    def changes(self, branch, source, head):
        self._read("changes")
        return self._changes

    def pushes(self, branch):
        self._read("pushes")
        return self._pushes

    def failing_heads(self, branch):
        self._read("failing_heads")
        return self._failing

    def facts_run_heads(self, branch):
        self._read("facts_run_heads")
        return self._facts

    def conflicted(self, number):
        self._read("conflicted")
        return self.conflicted_now

    def register_answer(self, branch, before, after):
        self._read("register_answer")
        return (before, after) in self.register_answers

    def local_plan_branches(self):
        self._read("local_plan_branches")
        return self.local

    def pr_head(self, number, repo):
        self._read("pr_head")
        self.looked_up.append((number, repo))
        return self.heads.get(number)

    def pr_base(self, number, repo):
        self._read("pr_base")
        return self.bases.get(number)

    def repository(self):
        self._read("repository")
        return self.repo


DRAFT = PrState(number=7, draft=True, base="main")
READY = PrState(number=7, draft=False, base="main")


class ReadPush(unittest.TestCase):
    def test_explicit_branch_is_a_write(self):
        self.assertEqual(read(f"origin {BRANCH}").writes, (Write(BRANCH, source=BRANCH),))

    def test_src_dst_refspec_names_the_destination(self):
        got = read(f"origin HEAD:refs/heads/{BRANCH}")
        self.assertEqual(got.writes, (Write(BRANCH, source="HEAD"),))

    def test_other_branches_are_not_plan_writes(self):
        self.assertEqual(read("origin feature/x").writes, (Write("feature/x", source="feature/x"),))

    def test_plus_refspec_forces(self):
        self.assertTrue(read(f"origin +HEAD:{BRANCH}").writes[0].force)

    def test_force_options_force(self):
        for opt in ("-f", "--force", "--force-with-lease", "--force-with-lease=plan/x:abc", "--force-if-includes",
                    "--forc", "-nf", "--for"):
            with self.subTest(opt):
                got = read(f"{opt} origin {BRANCH}")
                self.assertTrue(any(w.force for w in got.writes) or got.unreadable, got)

    def test_broad_pushes_are_named(self):
        for opt, kind in (("--mirror", "mirror"), ("--all", "all"), ("--branches", "all"), ("--prune", "prune")):
            with self.subTest(opt):
                got = read(f"{opt} origin")
                self.assertEqual((got.broad, got.writes), (kind, ()))

    def test_delete_flag_and_empty_source_delete(self):
        self.assertTrue(read(f"--delete origin {BRANCH}").writes[0].delete)
        self.assertTrue(read(f"-d origin {BRANCH}").writes[0].delete)
        self.assertTrue(read(f"origin :{BRANCH}").writes[0].delete)

    def test_bare_push_resolves_the_current_branch(self):
        self.assertEqual(read("").writes, (Write(BRANCH, source="HEAD"),))
        self.assertEqual(read("origin").writes, (Write(BRANCH, source="HEAD"),))

    def test_bare_push_that_cannot_resolve_is_unreadable(self):
        self.assertTrue(read("origin", current=lambda: None).unreadable)

    def test_head_refspec_resolves_like_a_bare_push(self):
        self.assertEqual(read("origin HEAD").writes, (Write(BRANCH, source="HEAD"),))

    def test_wildcard_and_dynamic_refspecs_are_unreadable(self):
        self.assertTrue(read("origin refs/heads/*:refs/heads/*").unreadable)
        self.assertTrue(pr.read_push([("origin", False), ("$B", True)], lambda: BRANCH).unreadable)

    def test_valued_options_do_not_become_refspecs(self):
        got = read(f"-o ci.skip --push-option=x --repo=origin {BRANCH}")
        self.assertEqual([w.branch for w in got.writes], [BRANCH])
        got = read(f"--receive-pack /bin/x origin {BRANCH}")
        self.assertEqual([w.branch for w in got.writes], [BRANCH])

    def test_short_cluster_with_valued_o(self):
        got = read(f"-uo opt origin {BRANCH}")
        self.assertEqual([w.branch for w in got.writes], [BRANCH])

    def test_unknown_option_is_unreadable(self):
        self.assertTrue(read(f"--no-such-thing origin {BRANCH}").unreadable)

    def test_tags_only_writes_no_branch(self):
        self.assertEqual(read("--tags origin").writes, ())
        self.assertEqual(read("origin tag v1").writes, ())

    def test_other_namespaces_are_not_branches(self):
        self.assertEqual(read("origin HEAD:refs/tags/v1").writes, ())

    def test_heads_shorthand_is_a_branch(self):
        self.assertEqual(read(f"origin HEAD:heads/{BRANCH}").writes[0].branch, BRANCH)

    def test_plan_branch_is_case_insensitive_and_exact_prefix(self):
        self.assertTrue(pr.plan_branch("plan/x"))
        self.assertTrue(pr.plan_branch("PLAN/x"))
        self.assertFalse(pr.plan_branch("planning/x"))
        self.assertFalse(pr.plan_branch("feature/plan/x"))


class Write14to17(unittest.TestCase):
    def judge(self, write, facts=None, run=True):
        pr.judge_write(write, plan_run=run, facts=facts or FakeFacts(pr_state=DRAFT))

    def test_non_plan_branch_reads_nothing(self):
        facts = FakeFacts()
        self.judge(Write("feature/x"), facts, run=False)
        self.assertEqual(facts.reads, [])

    def test_e14_refuses_without_plan_run(self):
        with self.assertRaisesRegex(Refusal, "E14"):
            self.judge(Write(BRANCH, source="HEAD"), run=False)

    def test_e14_allows_a_plan_run(self):
        self.judge(Write(BRANCH, source="HEAD"))

    def test_e15_refuses_force_even_in_a_run(self):
        with self.assertRaisesRegex(Refusal, "E15"):
            self.judge(Write(BRANCH, force=True))

    def test_e16_refuses_delete_in_any_mode(self):
        for run in (True, False):
            with self.subTest(run=run), self.assertRaisesRegex(Refusal, "E16"):
                self.judge(Write(BRANCH, delete=True), run=run)

    def test_e17_refuses_a_push_to_a_ready_pr(self):
        with self.assertRaisesRegex(Refusal, "E17"):
            self.judge(Write(BRANCH, source="HEAD"), FakeFacts(pr_state=READY))

    def test_e17_allows_a_push_with_no_open_pr(self):
        self.judge(Write(BRANCH, source="HEAD"), FakeFacts(pr_state=None, head=None))

    def test_e17_allows_the_register_commit_shape_to_a_ready_pr(self):
        link = "[a](../../unauthorised/0008-slug/item-02-part1-01.md)"
        changes = (Change(PLANS + "unauthorised/0008-slug/item-02-part1-01.md", "A", ("x",)),
                   Change(PLANS + "progress/0008-slug/item-02-part1.md", "M", (link,)))
        self.judge(Write(BRANCH, source="HEAD"), FakeFacts(pr_state=READY, changes=changes))

    def test_e17_refuses_a_register_commit_with_prose_in_the_record(self):
        changes = (Change(PLANS + "unauthorised/0008-slug/item-02-part1-01.md", "A", ("x",)),
                   Change(PLANS + "progress/0008-slug/item-02-part1.md", "M", ("done, mostly",)))
        with self.assertRaisesRegex(Refusal, "E17"):
            self.judge(Write(BRANCH, source="HEAD"), FakeFacts(pr_state=READY, changes=changes))

    def test_a_failed_read_refuses(self):
        for name in ("open_pr", "branch_head"):
            with self.subTest(name), self.assertRaisesRegex(Refusal, "unreadable"):
                self.judge(Write(BRANCH, source="HEAD"), FakeFacts(pr_state=DRAFT, broken=(name,)))


class RegisterShape(unittest.TestCase):
    ROOT = "" + PLANS + "unauthorised/0008-slug/"
    REC = "" + PLANS + "progress/0008-slug/item-02-part1.md"

    def shape(self, changes):
        return pr.register_shape(changes, BRANCH)

    def test_new_register_file_and_link_lines_only(self):
        link = "[x](" + PLANS + "unauthorised/0008-slug/item-02-part1-01.md)"
        self.assertTrue(self.shape([Change(self.ROOT + "item-02-part1-01.md", "A", ("a", "b")),
                                    Change(self.REC, "M", (link,))]))

    def test_register_file_may_be_added_without_a_record_change(self):
        self.assertTrue(self.shape([Change(self.ROOT + "item-02-part1-01.md", "A", ("a",))]))

    def test_a_modified_register_file_is_not_the_shape(self):
        self.assertFalse(self.shape([Change(self.ROOT + "item-02-part1-01.md", "M", ("a",))]))

    def test_a_deleted_line_or_other_path_is_not_the_shape(self):
        self.assertFalse(self.shape([Change("src/a.py", "M", ("x",))]))
        self.assertFalse(self.shape([Change(self.REC, "D", ())]))

    def test_a_link_to_a_file_not_in_the_commit_is_not_the_shape(self):
        link = "[x](" + PLANS + "unauthorised/0008-slug/item-02-part1-09.md)"
        self.assertFalse(self.shape([Change(self.ROOT + "item-02-part1-01.md", "A", ("a",)),
                                     Change(self.REC, "M", (link,))]))

    def test_a_link_line_with_extra_text_is_not_the_shape(self):
        link = "- [x](" + PLANS + "unauthorised/0008-slug/item-02-part1-01.md) fixed"
        self.assertFalse(self.shape([Change(self.ROOT + "item-02-part1-01.md", "A", ("a",)),
                                     Change(self.REC, "M", (link,))]))

    def test_another_plans_register_folder_is_not_the_shape(self):
        self.assertFalse(self.shape([Change(PLANS + "unauthorised/0009-other/item-02-part1-01.md", "A", ("a",))]))

    def test_no_change_is_not_the_shape(self):
        self.assertFalse(self.shape([]))

    def test_a_branch_that_is_not_an_item_branch_has_no_shape(self):
        self.assertFalse(pr.register_shape([Change(self.ROOT + "x.md", "A", ())], "plan/oddly-named"))


class SecretScan(unittest.TestCase):
    def run_write(self, scan, changes=(), head=H1):
        pr.judge_write(Write(BRANCH, source="HEAD"), plan_run=True,
                       facts=FakeFacts(pr_state=DRAFT, scan=scan, changes=changes, head=head))

    def test_pass_and_pending_allow_a_push(self):
        self.run_write("pass")
        self.run_write("pending")

    def test_fail_and_unreadable_refuse_an_ordinary_push(self):
        for state in ("fail", "unreadable"):
            with self.subTest(state), self.assertRaisesRegex(Refusal, "E19"):
                self.run_write(state, changes=(Change("src/a.py", "M", ("x",)),))

    def test_a_failed_scan_allows_a_push_touching_only_the_progress_record(self):
        rec = Change(PLANS + "progress/0008-slug/item-02-part1.md", "M", ("note",))
        self.run_write("fail", changes=(rec,))

    def test_a_failed_scan_refuses_a_push_touching_the_record_and_more(self):
        rec = Change(PLANS + "progress/0008-slug/item-02-part1.md", "M", ("note",))
        with self.assertRaisesRegex(Refusal, "E19"):
            self.run_write("fail", changes=(rec, Change("src/a.py", "M", ("x",))))

    def test_a_failed_scan_with_no_change_refuses(self):
        with self.assertRaisesRegex(Refusal, "E19"):
            self.run_write("fail", changes=())

    def test_an_unreadable_scan_read_refuses(self):
        with self.assertRaisesRegex(Refusal, "unreadable"):
            pr.judge_write(Write(BRANCH, source="HEAD"), plan_run=True,
                           facts=FakeFacts(pr_state=DRAFT, broken=("secret_scan",)))

    def test_a_first_push_has_nothing_to_scan(self):
        pr.judge_write(Write(BRANCH, source="HEAD"), plan_run=True,
                       facts=FakeFacts(pr_state=None, head=None, broken=("secret_scan",)))


class CiFixRounds(unittest.TestCase):
    def push(self, before, after):
        return Push(before=before, after=after)

    def test_an_answered_failing_head_counts_once(self):
        pushes = [self.push(H1, H2), self.push(H2, H3)]
        got = pr.counted_heads(pushes, failing={H1, H2}, facts=set(), register_only=set())
        self.assertEqual(got, {H1, H2})

    def test_a_head_never_answered_does_not_count(self):
        got = pr.counted_heads([self.push(H1, H2)], failing={H2}, facts=set(), register_only=set())
        self.assertEqual(got, set())

    def test_a_passing_answered_head_does_not_count(self):
        got = pr.counted_heads([self.push(H1, H2)], failing=set(), facts=set(), register_only=set())
        self.assertEqual(got, set())

    def test_an_e3_facts_run_counts_an_answered_head(self):
        got = pr.counted_heads([self.push(H1, H2)], failing=set(), facts={H1}, register_only=set())
        self.assertEqual(got, {H1})

    def test_a_register_commit_answer_is_excluded(self):
        got = pr.counted_heads([self.push(H1, H2)], failing={H1}, facts=set(), register_only={H1})
        self.assertEqual(got, set())

    def test_many_attempts_count_once(self):
        pushes = [self.push(H1, H2), self.push(H1, H3)]
        got = pr.counted_heads(pushes, failing={H1}, facts={H1}, register_only=set())
        self.assertEqual(got, {H1})

    def facts_with(self, **kw):
        return FakeFacts(pr_state=DRAFT, **kw)

    def judge(self, facts, head=H4):
        facts.head = head
        pr.judge_write(Write(BRANCH, source="HEAD"), plan_run=True, facts=facts)

    def test_the_fourth_round_refuses(self):
        pushes = [self.push(H1, H2), self.push(H2, H3), self.push(H3, H4)]
        with self.assertRaisesRegex(Refusal, "E12"):
            self.judge(self.facts_with(pushes=pushes, failing={H1, H2, H3, H4}))

    def test_the_third_round_is_allowed(self):
        pushes = [self.push(H1, H2), self.push(H2, H3)]
        self.judge(self.facts_with(pushes=pushes, failing={H1, H2, H3}), head=H3)

    def test_a_push_answering_a_passing_head_adds_no_round(self):
        pushes = [self.push(H1, H2), self.push(H2, H3), self.push(H3, H4)]
        self.judge(self.facts_with(pushes=pushes, failing={H1, H2, H3}), head=H4)

    def test_a_conflicted_head_being_answered_counts(self):
        pushes = [self.push(H1, H2), self.push(H2, H3), self.push(H3, H4)]
        facts = self.facts_with(pushes=pushes, failing={H1, H2, H3}, conflicted_now=True)
        with self.assertRaisesRegex(Refusal, "E12"):
            self.judge(facts, head=H4)

    def test_a_conflict_below_the_limit_is_allowed(self):
        pushes = [self.push(H1, H2)]
        self.judge(self.facts_with(pushes=pushes, failing={H1}, conflicted_now=True), head=H2)

    def test_a_register_commit_answer_adds_no_round(self):
        pushes = [self.push(H1, H2), self.push(H2, H3), self.push(H3, H4)]
        facts = self.facts_with(pushes=pushes, failing={H1, H2, H3, H4}, register_answers={(H3, H4)})
        self.judge(facts)

    def test_a_register_shaped_push_from_a_failing_head_is_excluded(self):
        link = "[a](" + PLANS + "unauthorised/0008-slug/item-02-part1-01.md)"
        changes = (Change(PLANS + "unauthorised/0008-slug/item-02-part1-01.md", "A", ("x",)),
                   Change(PLANS + "progress/0008-slug/item-02-part1.md", "M", (link,)))
        pushes = [self.push(H1, H2), self.push(H2, H3), self.push(H3, H4)]
        self.judge(self.facts_with(pushes=pushes, failing={H1, H2, H3, H4}, changes=changes))

    def test_the_head_being_answered_is_counted_once(self):
        pushes = [self.push(H1, H2), self.push(H2, H3)]
        # H2 is already counted as answered; pushing from it again adds nothing.
        self.judge(self.facts_with(pushes=pushes, failing={H1, H2}), head=H2)

    def test_a_failed_read_refuses(self):
        for name in ("pushes", "failing_heads", "facts_run_heads"):
            with self.subTest(name), self.assertRaisesRegex(Refusal, "unreadable"):
                self.judge(self.facts_with(pushes=[self.push(H1, H2)], broken=(name,)), head=H2)

    def test_a_first_push_counts_nothing(self):
        pr.judge_write(Write(BRANCH, source="HEAD"), plan_run=True,
                       facts=self.facts_with(head=None, broken=("pushes", "failing_heads")))


class Close(unittest.TestCase):
    def test_a_plan_head_refuses_and_others_pass(self):
        with self.assertRaisesRegex(Refusal, "E16"):
            pr.judge_close(BRANCH)
        with self.assertRaisesRegex(Refusal, "E16"):
            pr.judge_close("PLAN/x")
        pr.judge_close("feature/x")
        pr.judge_close(None)  # not a pull request


class HeadRefspec(unittest.TestCase):
    def test_head_resolves_through_its_own_callable(self):
        got = pr.read_push(words("origin HEAD"), current=lambda: "from-upstream", head=lambda: BRANCH)
        self.assertEqual(got.writes, (Write(BRANCH, source="HEAD"),))
        got = pr.read_push(words("origin"), current=lambda: "from-upstream", head=lambda: BRANCH)
        self.assertEqual(got.writes[0].branch, "from-upstream")


class Broad(unittest.TestCase):
    def test_mirror_and_prune_refuse_in_any_mode(self):
        for kind, row in (("mirror", "E15"), ("prune", "E16")):
            with self.subTest(kind), self.assertRaisesRegex(Refusal, row):
                pr.judge_broad(kind, plan_run=True, facts=FakeFacts())

    def test_all_with_no_local_plan_branch_passes(self):
        pr.judge_broad("all", plan_run=False, facts=FakeFacts(local=()))

    def test_all_with_a_local_plan_branch_is_judged_as_that_branch(self):
        with self.assertRaisesRegex(Refusal, "E14"):
            pr.judge_broad("all", plan_run=False, facts=FakeFacts(local=(BRANCH,)))
        pr.judge_broad("all", plan_run=True, facts=FakeFacts(local=(BRANCH,), pr_state=DRAFT))

    def test_all_that_cannot_list_branches_refuses(self):
        with self.assertRaisesRegex(Refusal, "unreadable"):
            pr.judge_broad("all", plan_run=True, facts=FakeFacts(broken=("local_plan_branches",)))


if __name__ == "__main__":
    unittest.main()
