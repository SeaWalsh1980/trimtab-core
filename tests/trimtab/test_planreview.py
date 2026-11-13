"""Rows E5-E10, E13, E18, E23 and K+1 as pure decisions (ADR 0063 §5, §6). Each refusal is its own test, so
the mutation test can show it fails once the row's function allows."""

import unittest

from trimtab import planreview as rv
from trimtab.planrows import Change, FactsError, Refusal

H1, H2, H3, H4 = (c * 40 for c in "1234")
AGENT, REVIEWER = "agent-bot", "review-bot[bot]"
PLANS = "docs/" + "plans/"  # in two pieces: this repository has no plan folders
TAGS = {"fixed": "[fixed]", "wrong": "[wrong]", "intentional": "[intentional]", "moot": "[moot]",
        "deferred": "[deferred]"}


def thread(id=1, severity="medium", state="open", resolved_by="-", key="k", commit=H1, run="r1"):
    return rv.Thread(run, id, severity, state, resolved_by, "a.py", 3, "bug", key, commit)


def reply(root, body, user=AGENT, id=100):
    return rv.Comment(id, user, body, in_reply_to=root)


def review(head, verdict="pass", run="r1", tree="t" * 40):
    return rv.Review(run, head, tree, verdict)


class E5(unittest.TestCase):
    def test_a_review_comment_outside_the_facts_refuses(self):
        with self.assertRaises(Refusal):
            rv.check_e5([rv.Comment(9, REVIEWER, "x")], [thread(id=1)], REVIEWER)

    def test_a_reply_by_the_review_takes_its_roots_id(self):
        rv.check_e5([rv.Comment(9, REVIEWER, "x", in_reply_to=1)], [thread(id=1)], REVIEWER)

    def test_comments_by_others_are_not_judged(self):
        rv.check_e5([rv.Comment(9, "human", "x")], [], REVIEWER)

    def test_the_login_compares_without_case(self):
        with self.assertRaises(Refusal):
            rv.check_e5([rv.Comment(9, REVIEWER.upper(), "x")], [], REVIEWER)


class E7(unittest.TestCase):
    def test_a_pass_on_the_exact_head_allows(self):
        rv.check_e7(H1, [review(H1)], lambda h: False)

    def test_a_failed_review_on_the_head_refuses(self):
        with self.assertRaises(Refusal):
            rv.check_e7(H1, [review(H1, "fail")], lambda h: False)

    def test_no_review_refuses(self):
        with self.assertRaises(Refusal):
            rv.check_e7(H1, [], lambda h: False)

    def test_a_register_commit_on_a_passed_head_allows(self):
        rv.check_e7(H2, [review(H1)], lambda h: h == H1)

    def test_a_register_commit_on_a_failed_head_refuses(self):
        with self.assertRaises(Refusal):
            rv.check_e7(H2, [review(H1, "fail")], lambda h: True)

    def test_a_later_failed_run_on_the_head_overrides_an_earlier_pass(self):
        with self.assertRaises(Refusal):
            rv.check_e7(H1, [review(H1, "pass"), review(H1, "fail", run="r2")], lambda h: True)

    def test_a_later_pass_overrides_an_earlier_failure(self):
        rv.check_e7(H1, [review(H1, "fail"), review(H1, "pass", run="r2")], lambda h: False)


class E8(unittest.TestCase):
    def run_row(self, t, replies, ok=lambda t: True):
        rv.check_e8([t], replies, AGENT, TAGS, ok)

    def test_an_unanswered_thread_refuses(self):
        with self.assertRaises(Refusal):
            self.run_row(thread(), [])

    def test_a_reply_by_someone_else_does_not_count(self):
        with self.assertRaises(Refusal):
            self.run_row(thread(), [reply(1, "[moot] x", user="human")])

    def test_a_reply_without_a_tag_does_not_count(self):
        with self.assertRaises(Refusal):
            self.run_row(thread(), [reply(1, "fine, thanks")])

    def test_a_tagged_reply_answers_a_medium_thread(self):
        self.run_row(thread(), [reply(1, "[wrong] it is fine")])

    def test_a_high_thread_accepts_only_the_fixed_tag(self):
        with self.assertRaises(Refusal):
            self.run_row(thread(severity="high"), [reply(1, "[wrong] no")])
        self.run_row(thread(severity="high"), [reply(1, f"[fixed] {H2}")])

    def test_a_critical_thread_refuses_a_deferral(self):
        with self.assertRaises(Refusal):
            self.run_row(thread(severity="critical"), [reply(1, "[deferred] later")], ok=lambda t: True)

    def test_a_deferral_needs_the_deferral_check(self):
        with self.assertRaises(Refusal):
            self.run_row(thread(), [reply(1, "[deferred] later")], ok=lambda t: False)
        self.run_row(thread(), [reply(1, "[deferred] later")], ok=lambda t: True)

    def test_every_thread_must_be_answered(self):
        with self.assertRaises(Refusal):
            rv.check_e8([thread(1), thread(2)], [reply(1, "[moot] x")], AGENT, TAGS, lambda t: True)


class Deferral(unittest.TestCase):
    def allowed(self, **kw):
        base = dict(outside_diff=True, merge_base_keys=frozenset({"k"}), recomputed_key="k")
        return rv.deferral_allowed(thread(), **{**base, **kw})

    def test_a_line_outside_the_diff_with_a_matching_merge_base_key_is_allowed(self):
        self.assertTrue(self.allowed())

    def test_a_line_inside_the_diff_is_not(self):
        self.assertFalse(self.allowed(outside_diff=False))

    def test_a_key_the_merge_base_review_never_listed_is_not(self):
        self.assertFalse(self.allowed(merge_base_keys=frozenset({"other"})))

    def test_a_key_that_does_not_recompute_is_not(self):
        self.assertFalse(self.allowed(recomputed_key="changed"))
        self.assertFalse(self.allowed(recomputed_key=None))

    def test_high_is_never_deferred(self):
        self.assertFalse(rv.deferral_allowed(thread(severity="high"), outside_diff=True,
                                             merge_base_keys=frozenset({"k"}), recomputed_key="k"))


class Key(unittest.TestCase):
    def test_whitespace_collapses_and_the_parts_matter(self):
        self.assertEqual(rv.thread_key("a.py", "bug", "x  =   1"), rv.thread_key("a.py", "bug", " x = 1 "))
        self.assertNotEqual(rv.thread_key("a.py", "bug", "x"), rv.thread_key("b.py", "bug", "x"))
        self.assertNotEqual(rv.thread_key("a.py", "bug", "x"), rv.thread_key("a.py", "bu", "gx"))


class E9(unittest.TestCase):
    def run_row(self, replies, lines, valid=lambda t, sha: True):
        rv.check_e9([thread()], lines, replies, AGENT, TAGS, valid)

    def test_a_fixed_reply_citing_no_sha_refuses(self):
        with self.assertRaises(Refusal):
            self.run_row([reply(1, "[fixed] done")], [thread(state="resolved", resolved_by="human")])

    def test_a_citation_that_does_not_fix_the_finding_refuses(self):
        with self.assertRaises(Refusal):
            self.run_row([reply(1, f"[fixed] {H2}")], [thread(state="resolved", resolved_by="human")],
                         valid=lambda t, sha: False)

    def test_a_fix_the_review_has_not_closed_refuses(self):
        with self.assertRaises(Refusal):
            self.run_row([reply(1, f"[fixed] {H2}")], [thread()])

    def test_a_resolution_by_the_agent_does_not_close(self):
        with self.assertRaises(Refusal):
            self.run_row([reply(1, f"[fixed] {H2}")], [thread(state="resolved", resolved_by=AGENT)])

    def test_a_later_resolution_by_another_closes(self):
        self.run_row([reply(1, f"[fixed] {H2}")],
                     [thread(), thread(state="resolved", resolved_by="human", run="r2")])

    def test_a_thread_reopened_by_a_later_line_is_not_closed(self):
        with self.assertRaises(Refusal):
            self.run_row([reply(1, f"[fixed] {H2}")],
                         [thread(state="resolved", resolved_by="human"), thread(run="r2")])

    def test_other_tags_are_not_judged(self):
        self.run_row([reply(1, "[moot] x")], [thread()])

    def test_a_fixed_reply_by_someone_else_is_not_judged(self):
        self.run_row([reply(1, "[fixed] done", user="human")], [thread()])

    def test_a_thread_with_no_anchor_commit_refuses(self):
        with self.assertRaises(Refusal):
            rv.check_e9([thread(commit="")], [thread(state="resolved", resolved_by="human")],
                        [reply(1, f"[fixed] {H2}")], AGENT, TAGS, lambda t, sha: True)

    def test_the_cited_sha_is_found_among_the_words(self):
        self.assertEqual(rv.fixed_commit(f"[fixed] in {H2} done"), H2)
        self.assertIsNone(rv.fixed_commit("[fixed] abc123"))


class E6(unittest.TestCase):
    def test_a_head_no_run_reviewed_allows(self):
        rv.check_e6(H2, [review(H1)], [])

    def test_a_reviewed_head_refuses(self):
        with self.assertRaises(Refusal):
            rv.check_e6(H1, [review(H1)], [])

    def test_a_fail_closed_head_not_yet_re_requested_allows(self):
        rv.check_e6(H1, [review(H1, "fail-closed")], [])

    def test_a_fail_closed_head_already_re_requested_refuses(self):
        with self.assertRaises(Refusal):
            rv.check_e6(H1, [review(H1, "fail-closed")], [H1])

    def test_only_the_latest_verdict_counts(self):
        with self.assertRaises(Refusal):
            rv.check_e6(H1, [review(H1, "fail-closed"), review(H1, "fail", run="r2")], [])


class E10(unittest.TestCase):
    COMMITS = [H1, H2, H3, H4]

    def push(self, new):
        return rv.LedgerPush(H1, new, "1")

    def test_a_round_at_or_after_the_last_answer_with_one_push_after_allows(self):
        rv.check_e10(self.COMMITS, [H2], H2, [self.push(H3)])

    def test_a_round_before_the_last_answer_refuses(self):
        with self.assertRaises(Refusal):
            rv.check_e10(self.COMMITS, [H1], H2, [])

    def test_two_pushes_after_the_round_refuse(self):
        with self.assertRaises(Refusal):
            rv.check_e10(self.COMMITS, [H2], H2, [self.push(H3), self.push(H4)])

    def test_no_complete_round_refuses(self):
        with self.assertRaises(Refusal):
            rv.check_e10(self.COMMITS, [], None, [])

    def test_a_round_on_a_head_off_the_branch_refuses(self):
        with self.assertRaises(Refusal):
            rv.check_e10(self.COMMITS, ["9" * 40], None, [])

    def test_an_answer_commit_off_the_branch_refuses(self):
        with self.assertRaises(Refusal):
            rv.check_e10(self.COMMITS, [H2], "9" * 40, [])

    def test_any_complete_head_that_qualifies_allows(self):
        rv.check_e10(self.COMMITS, [H1, H3], H2, [])

    def test_a_push_that_ends_at_the_round_head_is_not_after_it(self):
        rv.check_e10(self.COMMITS, [H2], None, [self.push(H2), self.push(H3)])

    def test_a_ledger_push_off_the_branch_refuses_rather_than_going_uncounted(self):
        with self.assertRaises(Refusal):
            rv.check_e10(self.COMMITS, [H2], None, [self.push(H3), self.push(H4), self.push("9" * 40)])

    def test_a_round_on_the_first_commit_with_no_answer_allows(self):
        rv.check_e10(self.COMMITS, [H1], None, [])


class E13(unittest.TestCase):
    def good(self, **kw):
        args = dict(branch_commits=[H2, H3], pushes=[rv.LedgerPush(H1, H3, "1", frozenset({H2, H3}))],
                    events=[rv.Activity(H1, H3, AGENT)], head=H3, merge_base=H1, agent=AGENT,
                    on_default=lambda s: False, scope_ok=True)
        args.update(kw)
        rv.check_e13(**args)

    def test_an_unbroken_agent_chain_matching_the_ledger_allows(self):
        self.good()

    def test_a_commit_outside_every_ledger_push_refuses(self):
        with self.assertRaises(Refusal):
            self.good(branch_commits=[H2, H3, H4])

    def test_a_push_made_without_the_run_flag_refuses(self):
        with self.assertRaises(Refusal):
            self.good(pushes=[rv.LedgerPush(H1, H3, "", frozenset({H2, H3}))])

    def test_a_chain_that_does_not_end_at_the_head_refuses(self):
        with self.assertRaises(Refusal):
            self.good(head=H4)

    def test_a_gap_in_the_chain_refuses(self):
        with self.assertRaises(Refusal):
            self.good(events=[rv.Activity(H1, H2, AGENT), rv.Activity(H3, H4, AGENT)], head=H4,
                      pushes=[rv.LedgerPush(H1, H2, "1", frozenset({H2})), rv.LedgerPush(H3, H4, "1")],
                      branch_commits=[H2])

    def test_a_chain_that_starts_elsewhere_refuses(self):
        with self.assertRaises(Refusal):
            self.good(events=[rv.Activity(H4, H3, AGENT)], pushes=[rv.LedgerPush(H4, H3, "1", frozenset({H2, H3}))])

    def test_a_chain_starting_on_the_default_branch_allows(self):
        self.good(events=[rv.Activity(H4, H3, AGENT)], pushes=[rv.LedgerPush(H4, H3, "1", frozenset({H2, H3}))],
                  on_default=lambda s: s == H4)

    def test_a_new_branch_chain_starts_at_zero(self):
        self.good(events=[rv.Activity(rv.ZERO, H3, AGENT)],
                  pushes=[rv.LedgerPush(rv.ZERO, H3, "1", frozenset({H2, H3}))])

    def test_the_agent_login_compares_without_case(self):
        self.good(events=[rv.Activity(H1, H3, AGENT.upper())])

    def test_a_zero_before_in_the_middle_of_the_chain_refuses(self):
        with self.assertRaises(Refusal):
            self.good(events=[rv.Activity(H1, H2, AGENT), rv.Activity(rv.ZERO, H3, AGENT)],
                      pushes=[rv.LedgerPush(H1, H2, "1", frozenset({H2})),
                              rv.LedgerPush(rv.ZERO, H3, "1", frozenset({H3}))])

    def test_a_merge_of_the_default_branch_is_not_the_runs_commit(self):
        # the default branch's own commits are not in origin/default..origin/branch, so none is judged
        self.good(branch_commits=[H3], pushes=[rv.LedgerPush(H1, H3, "1", frozenset({H3}))])

    def test_an_event_by_another_account_refuses(self):
        with self.assertRaises(Refusal):
            self.good(events=[rv.Activity(H1, H3, "someone")])

    def test_a_ledger_that_disagrees_with_the_events_refuses(self):
        with self.assertRaises(Refusal):
            self.good(pushes=[rv.LedgerPush(H2, H3, "1", frozenset({H2, H3}))])

    def test_no_events_refuses(self):
        with self.assertRaises(Refusal):
            self.good(events=[])

    def test_a_diff_out_of_scope_refuses(self):
        with self.assertRaises(Refusal):
            self.good(scope_ok=False)


class E18(unittest.TestCase):
    MARKERS = ("# noqa", "continue-on-error: true")
    THRESHOLDS = (r"fail_under",)

    def run_row(self, *changes, named=frozenset()):
        rv.check_e18(changes, self.MARKERS, self.THRESHOLDS, named)

    def test_an_added_marker_refuses(self):
        with self.assertRaises(Refusal):
            self.run_row(Change("a.py", "M", added=("x = 1  # noqa",)))

    def test_a_marker_the_plan_names_exactly_allows(self):
        self.run_row(Change("a.py", "M", added=("x = 1  # noqa",)), named=frozenset({"x = 1  # noqa"}))

    def test_a_marker_line_the_plan_does_not_name_refuses_even_when_another_is_named(self):
        with self.assertRaises(Refusal):
            self.run_row(Change("a.py", "M", added=("y = 2  # noqa",)), named=frozenset({"x = 1  # noqa"}))

    def test_a_deleted_test_file_refuses(self):
        with self.assertRaises(Refusal):
            self.run_row(Change("tests/test_a.py", "D"))

    def test_more_test_definitions_removed_than_added_refuses(self):
        with self.assertRaises(Refusal):
            self.run_row(Change("tests/test_a.py", "M", added=("def test_one(self):",),
                                removed_lines=("    def test_a(self):", "    def test_b(self):")))

    def test_swapping_a_test_for_another_allows(self):
        self.run_row(Change("tests/test_a.py", "M", added=("    def test_new(self):",),
                            removed_lines=("    def test_old(self):",)))

    def test_removing_a_definition_outside_a_test_file_is_not_judged(self):
        self.run_row(Change("src/a.py", "M", removed_lines=("def test_helper():",)))

    def test_lowering_a_threshold_number_refuses(self):
        with self.assertRaises(Refusal):
            self.run_row(Change("setup.cfg", "M", added=("fail_under = 80",), removed_lines=("fail_under = 90",)))

    def test_raising_or_keeping_a_threshold_allows(self):
        self.run_row(Change("setup.cfg", "M", added=("fail_under = 95",), removed_lines=("fail_under = 90",)))
        self.run_row(Change("setup.cfg", "M", added=("fail_under = 90",), removed_lines=("fail_under = 90",)))

    def test_removing_a_threshold_line_refuses(self):
        with self.assertRaises(Refusal):
            self.run_row(Change("setup.cfg", "M", removed_lines=("fail_under = 90",)))

    def test_a_threshold_line_the_plan_names_may_go(self):
        self.run_row(Change("setup.cfg", "M", removed_lines=("fail_under = 90",)),
                     named=frozenset({"fail_under = 90"}))

    def test_a_new_threshold_line_allows(self):
        self.run_row(Change("setup.cfg", "M", added=("fail_under = 50",)))

    def test_a_removed_threshold_hidden_by_an_unrelated_added_line_refuses(self):
        rows = Change("setup.cfg", "M", added=("cov_min = 95", "fail_under = 10"), removed_lines=("fail_under = 90",))
        with self.assertRaises(Refusal):
            rv.check_e18([rows], self.MARKERS, (r"fail_under|cov_min",), frozenset())

    def test_an_unrelated_added_line_with_higher_numbers_does_not_replace_a_removed_one(self):
        rows = Change("setup.cfg", "M", added=("min_x = 100",), removed_lines=("fail_under = 90",))
        with self.assertRaises(Refusal):
            rv.check_e18([rows], self.MARKERS, (r"fail_under|min_",), frozenset())

    def test_reordered_thresholds_that_are_not_lowered_allow(self):
        rows = Change("setup.cfg", "M", added=("cov_b = 90", "cov_a = 80"), removed_lines=("cov_a = 80", "cov_b = 90"))
        rv.check_e18([rows], self.MARKERS, (r"cov_",), frozenset())

    def test_a_replacement_the_plan_names_may_be_lower(self):
        rows = Change("setup.cfg", "M", added=("fail_under = 50",), removed_lines=("fail_under = 90",))
        self.run_row(rows, named=frozenset({"fail_under = 50"}))

    def test_a_negative_number_made_more_negative_refuses(self):
        rows = Change("setup.cfg", "M", added=("fail_under = -10",), removed_lines=("fail_under = -5",))
        with self.assertRaises(Refusal):
            self.run_row(rows)

    def test_a_different_count_of_numbers_refuses(self):
        rows = Change("setup.cfg", "M", added=("fail_under = 90",), removed_lines=("fail_under = 90 / 80",))
        with self.assertRaises(Refusal):
            self.run_row(rows)

    def test_markers_match_without_case_or_spacing(self):
        for text in ("x = 1  #noqa", "x = 1  # NOQA", "continue-on-error:true"):
            with self.subTest(text), self.assertRaises(Refusal):
                self.run_row(Change("a.py", "M", added=(text,)))

    def test_the_default_markers_are_exercised(self):
        from trimtab.instance import DEFAULT_FIX_MARKERS
        for text in ("x  # type:ignore", "@pytest.mark.xfail", "pytest.skip('x')", "  if: false", "git commit --no-verify"):
            with self.subTest(text), self.assertRaises(Refusal):
                rv.check_e18([Change("a.py", "M", added=(text,))], DEFAULT_FIX_MARKERS, (), frozenset())

    def test_a_marker_added_inside_a_test_file_refuses(self):
        with self.assertRaises(Refusal):
            self.run_row(Change("tests/test_a.py", "M", added=("    x = 1  # noqa",)))


class E23(unittest.TestCase):
    def test_a_complete_round_on_the_head_allows(self):
        rv.check_e23(H1, [H1], [H1])

    def test_no_round_on_the_exact_head_refuses(self):
        with self.assertRaises(Refusal):
            rv.check_e23(H2, [H1], [H1])

    def test_more_than_five_pre_draft_rounds_refuses(self):
        six = [str(i) * 40 for i in range(6)]
        with self.assertRaises(Refusal):
            rv.check_e23(H1, [H1], six)
        rv.check_e23(H1, [H1], six[:5])

    def test_rounds_are_counted_by_head_not_by_entry(self):
        rv.check_e23(H1, [H1], [H1] * 9)

    def test_a_sixth_pre_draft_head_refuses_at_its_first_pass(self):
        five = [str(i) * 40 for i in range(5)]
        with self.assertRaises(Refusal):
            rv.check_round_start(H4.replace("4", "a"), five)

    def test_a_pass_on_a_head_already_in_a_round_allows(self):
        five = [str(i) * 40 for i in range(5)]
        rv.check_round_start(five[0], five)

    def test_the_fifth_head_allows(self):
        rv.check_round_start(H1, [str(i) * 40 for i in range(4)])


class NextPart(unittest.TestCase):
    PLAN = "0008-slug"

    def test_one_past_the_highest_over_the_three_sources(self):
        self.assertEqual(rv.next_part({1}, {1, 2}, {3}), 4)

    def test_no_part_yet_is_part_one(self):
        self.assertEqual(rv.next_part(set(), set(), set()), 1)

    def test_a_source_that_failed_refuses(self):
        for sources in ((None, set(), set()), (set(), None, set()), (set(), set(), None)):
            with self.subTest(sources), self.assertRaises(FactsError):
                rv.next_part(*sources)

    def test_parts_are_read_from_record_names_and_head_refs(self):
        names = [PLANS + "progress/0008-slug/item-02-part1.md", PLANS + "progress/0008-slug/item-02-part3.md",
                 PLANS + "progress/0008-slug/item-03-part9.md", PLANS + "progress/0009-x/item-02-part7.md",
                 "plan/0008-slug/item-02-part2", "item-02-part4.jsonl"]
        self.assertEqual(rv.parts_in(names, self.PLAN, 2), {1, 2, 3, 4})


if __name__ == "__main__":
    unittest.main()
