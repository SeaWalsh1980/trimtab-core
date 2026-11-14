"""What a plan has in flight, from real git and a faked gh (ADR 0081)."""

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import test_planauth as tpa
import test_planlabels as tpl
from gh_fake import FakeGh, path_of
from instance_fixture import make_instance
from trimtab import cli, planinflight, planlabels
from trimtab.planinflight import InflightError, Resume

REPOSITORY = tpl.REPOSITORY
PLAN = tpl.PLAN
ADMIN = tpl.ADMIN
AGENT = tpl.AGENT
APPROVER = tpl.APPROVER
STRANGER = tpl.STRANGER
WHO = tpl.WHO
BLOCKED = tpl.BLOCKED
event = tpl.event
at = tpl.at
ITEM_REF = tpl.ITEM_REF
SECOND_REF = f"plan/{PLAN}/item-02-part1"
HEAD = "c" * 40
OTHER_HEAD = "d" * 40
REFS_PATH = f"repos/{REPOSITORY}/git/matching-refs/heads/plan/{PLAN}/"
PULLS_PATH = f"repos/{REPOSITORY}/pulls"


def pull(number, ref, author=AGENT, repo=REPOSITORY, labels=(), state="open", sha=HEAD):
    """A row of the PR list, as the server returns it."""
    return {"number": number, "state": state, "labels": [{"name": n} for n in labels],
            "user": None if author is None else {"login": author},
            "head": {"ref": ref, "sha": sha, "repo": None if repo is None else {"full_name": repo}}}


class Inflight(tpl.Restart):
    """A default branch where PR 7 added the plan, a scripted server, and no item PR or branch yet."""

    def setUp(self):
        super().setUp()
        self.refs = []
        self.answers[REFS_PATH] = json.dumps(self.refs)

    def open_pr(self, number, ref=ITEM_REF, events=(), labels=(), **kw):
        self.pulls.append(pull(number, ref, labels=labels, **kw))
        self.put(number, list(events), labels)

    def push(self, name=ITEM_REF, sha=HEAD):
        self.refs.append({"ref": f"refs/heads/{name}", "object": {"sha": sha, "type": "commit"}})
        self.answers[REFS_PATH] = json.dumps(self.refs)

    def inflight(self, who=WHO, gh=None):
        return planinflight.check(str(self.repo), REPOSITORY, PLAN, who, gh or FakeGh(self.answers))


class WhatToResume(Inflight):
    def test_nothing_in_flight_is_the_answer_for_a_plan_with_no_item_pr_or_branch(self):
        verdict = self.inflight()

        self.assertEqual((verdict.problems, verdict.resume), ((), None))

    def test_one_open_item_pr_is_the_thing_to_resume(self):
        self.open_pr(8)

        verdict = self.inflight()

        self.assertEqual((verdict.problems, verdict.resume), ((), Resume(ITEM_REF, HEAD, 8)))

    def test_a_pr_the_admin_opened_may_be_resumed(self):
        self.open_pr(8, author=ADMIN)

        self.assertEqual(self.inflight().resume, Resume(ITEM_REF, HEAD, 8))

    def test_logins_and_the_repository_are_compared_without_case(self):
        self.open_pr(8, author=AGENT.upper(), repo=REPOSITORY.upper())

        self.assertEqual(self.inflight().problems, ())

    def test_a_pushed_branch_that_never_had_a_pr_is_resumed_with_its_head(self):
        self.push(sha=OTHER_HEAD)

        verdict = self.inflight()

        self.assertEqual((verdict.problems, verdict.resume), ((), Resume(ITEM_REF, OTHER_HEAD, None)))

    def test_the_line_for_a_pr_names_the_pr_its_branch_and_its_head(self):
        self.assertEqual(Resume(ITEM_REF, HEAD, 8).line(), f"resume pr 8 {ITEM_REF} {HEAD}")

    def test_the_line_for_a_branch_names_it_and_its_head(self):
        self.assertEqual(Resume(ITEM_REF, HEAD, None).line(), f"resume branch {ITEM_REF} {HEAD}")

    def test_a_branch_that_a_closed_pr_of_this_repository_had_is_not_in_flight(self):
        self.pulls.append(pull(8, ITEM_REF, state="closed"))
        self.put(8, [])
        self.push()

        self.assertEqual(self.inflight().resume, None)

    def test_a_closed_pr_from_a_fork_does_not_hide_a_branch_with_the_same_name(self):
        self.pulls.append(pull(8, ITEM_REF, state="closed", repo="someone/fork"))
        self.put(8, [])
        self.push()

        self.assertEqual(self.inflight().resume, Resume(ITEM_REF, HEAD, None))

    def test_a_branch_an_open_pr_has_is_the_pr_not_a_second_thing(self):
        self.open_pr(8)
        self.push()

        self.assertEqual(self.inflight().resume, Resume(ITEM_REF, HEAD, 8))

    def test_other_plans_lookalike_branches_and_non_item_branches_are_not_in_flight(self):
        self.open_pr(30, ref="plan/0002-other/item-01-part1")
        self.open_pr(31, ref=f"plan/{PLAN}-x/item-01-part1")
        self.open_pr(32, ref=f"plan/{PLAN}/notes")
        self.open_pr(33, ref=f"feature/plan/{PLAN}/item-01-part1")
        self.push(f"plan/{PLAN}/notes")

        self.assertEqual((self.inflight().problems, self.inflight().resume), ((), None))

    def test_a_stop_that_the_admin_cleared_on_the_pr_lets_it_resume(self):
        self.open_pr(8, events=[event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, ADMIN)])

        self.assertEqual(self.inflight().resume, Resume(ITEM_REF, HEAD, 8))

    def test_a_later_stop_that_the_admin_cleared_too_lets_it_resume(self):
        self.open_pr(8, events=[event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, STRANGER),
                                event(3, "labeled", BLOCKED, AGENT), event(4, "unlabeled", BLOCKED, ADMIN)])

        self.assertEqual(self.inflight().problems, ())

    def test_a_branch_with_no_pr_resumes_when_the_plans_newest_stop_was_cleared(self):
        self.pulls.append(pull(9, SECOND_REF, state="closed"))
        self.put(9, [event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, ADMIN)])
        self.push()

        self.assertEqual(self.inflight().resume, Resume(ITEM_REF, HEAD, None))

    def test_a_branch_with_no_pr_resumes_when_the_plan_has_no_stop_at_all(self):
        self.push()

        self.assertEqual(self.inflight().problems, ())


class Refusals(Inflight):
    def test_two_open_item_prs_are_refused_naming_both(self):
        self.open_pr(8)
        self.open_pr(9, ref=SECOND_REF)

        verdict = self.inflight()

        self.assertEqual((len(verdict.problems), verdict.resume), (1, None))
        self.assertIn("2 things are in flight (PR #8, PR #9)", verdict.problems[0])

    def test_a_pr_and_a_branch_with_no_pr_are_refused_naming_both(self):
        self.open_pr(8)
        self.push(SECOND_REF)

        self.assertIn(f"(PR #8, branch {SECOND_REF})", self.inflight().problems[0])

    def test_two_branches_with_no_pr_are_refused(self):
        self.push()
        self.push(SECOND_REF)

        self.assertIn("2 things are in flight", self.inflight().problems[0])

    def test_a_fork_head_is_refused_naming_the_fork(self):
        self.open_pr(8, repo="someone/fork")

        verdict = self.inflight()

        self.assertEqual((len(verdict.problems), verdict.resume), (1, None))
        self.assertIn("PR #8: head is in someone/fork", verdict.problems[0])

    def test_a_head_repository_that_was_deleted_is_refused(self):
        self.open_pr(8, repo=None)

        self.assertIn("a deleted repository", self.inflight().problems[0])

    def test_an_author_that_is_neither_the_agent_nor_the_admin_is_refused_naming_it(self):
        self.open_pr(8, author=STRANGER)

        verdict = self.inflight()

        self.assertEqual(len(verdict.problems), 1)
        self.assertIn(f"PR #8: its author is {STRANGER}", verdict.problems[0])

    def test_the_approver_is_not_an_author_a_run_may_resume(self):
        self.open_pr(8, author=APPROVER)

        self.assertIn(APPROVER, self.inflight().problems[0])

    def test_an_author_that_was_deleted_is_refused(self):
        self.open_pr(8, author=None)

        self.assertIn("deleted account", self.inflight().problems[0])

    def test_an_instance_naming_no_agent_leaves_only_the_admin(self):
        self.open_pr(8, author=AGENT)

        verdict = self.inflight(who=tpl.Accounts(ADMIN, APPROVER, ""))

        self.assertIn(f"its author is {AGENT}", verdict.problems[0])

    def test_a_head_branch_of_the_wrong_shape_is_refused(self):
        for number, ref in enumerate((f"plan/{PLAN}/item-1-part1", f"plan/{PLAN}/item-01-part0",
                                      f"plan/{PLAN}/item-01-part01", f"plan/{PLAN}/item-01-part1/more",
                                      f"plan/{PLAN}/item-01-part"), start=8):
            with self.subTest(ref=ref):
                self.pulls.clear()
                self.open_pr(number, ref=ref)

                verdict = self.inflight()

                self.assertEqual(len(verdict.problems), 1)
                self.assertIn(f"head branch {ref} is not plan/{PLAN}/item-NN-partK", verdict.problems[0])

    def test_an_open_pr_with_a_stop_and_no_removal_is_refused_and_a_branch_does_not_replace_it(self):
        self.open_pr(8, events=[event(1, "labeled", BLOCKED, AGENT)], labels=(BLOCKED,))

        verdict = self.inflight()

        self.assertEqual(verdict.resume, None)
        self.assertEqual(verdict.problems[0], f"{BLOCKED} is still on PR #8")
        self.assertIn("no removal of plan:blocked follows its stop on PR #8", verdict.problems[1])

    def test_a_removal_by_someone_other_than_the_admin_leaves_the_stop_standing(self):
        for remover in (STRANGER, AGENT, APPROVER, None):
            with self.subTest(remover=remover):
                self.pulls.clear()
                self.open_pr(8, events=[event(1, "labeled", BLOCKED, AGENT),
                                        event(2, "unlabeled", BLOCKED, remover)])

                verdict = self.inflight()

                self.assertEqual((len(verdict.problems), verdict.resume), (1, None))
                self.assertIn("not the admin", verdict.problems[0])

    def test_the_label_still_on_the_pr_after_the_admins_removal_is_refused(self):
        self.open_pr(8, events=[event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, ADMIN)],
                     labels=(BLOCKED,))

        self.assertEqual(self.inflight().problems, (f"{BLOCKED} is still on PR #8",))

    def test_a_stop_the_agent_set_again_after_the_admins_removal_stands(self):
        self.open_pr(8, events=[event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, ADMIN),
                                event(3, "labeled", BLOCKED, AGENT)], labels=(BLOCKED,))

        self.assertIn("no removal", self.inflight().problems[1])

    def test_the_label_present_with_no_event_to_explain_it_is_refused(self):
        self.open_pr(8, labels=(BLOCKED,))

        self.assertIn("the events and the label disagree", self.inflight().problems[0])

    def test_a_removal_with_no_stop_before_it_is_refused(self):
        self.open_pr(8, events=[event(1, "unlabeled", BLOCKED, ADMIN)])

        self.assertIn("its stop cannot be read", self.inflight().problems[0])

    def test_other_labels_do_not_stop_a_pr(self):
        self.open_pr(8, events=[event(1, "labeled", "plan:continue", AGENT)], labels=("plan:continue",))

        self.assertEqual(self.inflight().problems, ())

    def test_a_branch_with_no_pr_is_refused_while_the_plans_newest_stop_stands(self):
        self.pulls.append(pull(9, SECOND_REF, state="closed"))
        self.put(9, [event(1, "labeled", BLOCKED, AGENT)], labels=(BLOCKED,))
        self.push()

        verdict = self.inflight()

        self.assertEqual(verdict.resume, None)
        self.assertEqual(verdict.problems[0], f"branch {ITEM_REF} has no PR: {BLOCKED} is still on PR #9")

    def test_a_branch_with_no_pr_is_refused_when_the_stop_was_cleared_by_someone_else(self):
        self.put(tpa.PLAN_PR, [event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, STRANGER)])
        self.push()

        self.assertIn(STRANGER, self.inflight().problems[0])

    def test_a_branch_with_no_pr_and_the_wrong_shape_is_refused_naming_it(self):
        name = f"plan/{PLAN}/item-1-part1"
        self.push(name)

        verdict = self.inflight()

        self.assertEqual(verdict.problems, (f"branch {name} has no PR and is not plan/{PLAN}/item-NN-partK",))

    def test_a_wrong_shape_branch_that_had_a_pr_is_left_to_that_prs_own_checks(self):
        name = f"plan/{PLAN}/item-1-part1"
        self.pulls.append(pull(8, name, state="closed"))
        self.put(8, [])
        self.push(name)

        self.assertEqual(self.inflight().problems, ())

    def test_every_problem_is_listed_not_only_the_first(self):
        self.open_pr(8, repo="someone/fork", author=STRANGER)
        self.open_pr(9, ref=SECOND_REF)
        self.push(f"plan/{PLAN}/item-x")

        verdict = self.inflight()

        self.assertEqual(len(verdict.problems), 3)
        self.assertEqual([p.split(":")[0] for p in verdict.problems[:2]], ["PR #8", "PR #8"])
        self.assertIn("item-x has no PR", verdict.problems[2])

    def test_a_refusal_alongside_one_good_pr_is_still_a_refusal(self):
        self.open_pr(8)
        self.open_pr(9, ref=SECOND_REF, author=STRANGER)

        verdict = self.inflight()

        self.assertEqual((len(verdict.problems), verdict.resume), (1, None))

    def test_a_plan_that_is_not_one_is_refused_never_none_in_flight(self):
        self.answers[f"repos/{REPOSITORY}/git/matching-refs/heads/plan/0009-nope/"] = "[]"

        verdict = planinflight.check(str(self.repo), REPOSITORY, "0009-nope", WHO, FakeGh(self.answers))

        self.assertEqual(verdict.resume, None)
        self.assertIn("0009-nope is not a plan", verdict.problems[0])

    def test_pr_problems_are_listed_with_the_plan_problem_not_dropped(self):
        self.answers[f"repos/{REPOSITORY}/git/matching-refs/heads/plan/0009-nope/"] = "[]"
        self.pulls.append(pull(8, "plan/0009-nope/item-01-part1", repo="someone/fork"))
        self.put(8, [])

        verdict = planinflight.check(str(self.repo), REPOSITORY, "0009-nope", WHO, FakeGh(self.answers))

        self.assertIn("PR #8: head is in someone/fork", verdict.problems[0])
        self.assertIn("0009-nope is not a plan", verdict.problems[1])

    def test_a_name_that_is_not_one_is_refused_before_any_read(self):
        gh = FakeGh(self.answers)

        for repository, plan in (("nope", PLAN), (REPOSITORY, "a/b"), ("a/../b", PLAN)):
            with self.subTest(repository=repository, plan=plan):
                verdict = planinflight.check(str(self.repo), repository, plan, WHO, gh)

                self.assertEqual(len(verdict.problems), 1)
        self.assertEqual(gh.calls, [])


class ReadFailures(Inflight):
    def read_fails(self, **kw):
        with self.assertRaises(InflightError) as caught:
            self.inflight(**kw)
        return str(caught.exception)

    def test_a_failed_pr_list_raises(self):
        self.answers.pop(PULLS_PATH)

        self.assertIn("gh api", self.read_fails())

    def test_a_failed_branch_list_raises(self):
        self.answers.pop(REFS_PATH)

        self.assertIn("matching refs", self.read_fails())

    def test_a_failed_label_event_read_raises(self):
        self.open_pr(8)
        self.answers.pop(tpl.ALL_EVENTS)

        self.assertIn("gh api", self.read_fails())

    def test_a_pr_list_cut_off_at_its_bound_raises(self):
        for number in (8, 9, 10):
            self.open_pr(number, ref=f"plan/{PLAN}/item-{number:02d}-part1")

        with mock.patch.object(planinflight, "PULLS_LIMIT", 2):
            self.assertIn("more than 2 rows", self.read_fails())

    def test_a_branch_list_cut_off_at_its_bound_raises(self):
        self.push()
        self.push(SECOND_REF)

        with mock.patch.object(planinflight, "REFS_LIMIT", 1):
            self.assertIn("more than 1 rows", self.read_fails())

    def test_exactly_the_bound_is_read(self):
        self.open_pr(8)
        self.open_pr(9, ref=f"plan/{PLAN}/other")

        with mock.patch.object(planinflight, "PULLS_LIMIT", 2):
            self.assertEqual(self.inflight().resume, Resume(ITEM_REF, HEAD, 8))

    def test_a_pr_listed_twice_raises(self):
        self.open_pr(8)
        self.pulls.append(pull(8, SECOND_REF))
        self.put(8, [])

        self.assertIn("lists PR #8 twice", self.read_fails())

    def test_a_reply_that_is_not_a_list_raises(self):
        for path in (PULLS_PATH, REFS_PATH):
            with self.subTest(path=path):
                answers = {**self.answers, path: "{}"}

                with self.assertRaises(InflightError):
                    self.inflight(gh=FakeGh(answers))

    def test_a_pr_row_of_an_unexpected_shape_raises(self):
        good = pull(8, ITEM_REF)
        broken = {
            "no head": {k: v for k, v in good.items() if k != "head"},
            "no state": {k: v for k, v in good.items() if k != "state"},
            "state of merged": {**good, "state": "merged"},
            "number zero": {**good, "number": 0},
            "number a string": {**good, "number": "8"},
            "no user key": {k: v for k, v in good.items() if k != "user"},
            "user without login": {**good, "user": {}},
            "labels not a list": {**good, "labels": None},
            "label without name": {**good, "labels": [{}]},
            "head sha too short": {**good, "head": {**good["head"], "sha": "abc"}},
            "head repo without name": {**good, "head": {**good["head"], "repo": {}}},
            "no head repo key": {**good, "head": {k: v for k, v in good["head"].items() if k != "repo"}},
        }
        for name, row in broken.items():
            with self.subTest(name):
                answers = {**self.answers, PULLS_PATH: json.dumps([row])}

                with self.assertRaises(InflightError):
                    self.inflight(gh=FakeGh(answers))

    def test_a_branch_row_of_an_unexpected_shape_raises(self):
        good = {"ref": f"refs/heads/{ITEM_REF}", "object": {"sha": HEAD}}
        broken = {
            "no ref": {"object": good["object"]},
            "ref of another plan": {**good, "ref": "refs/heads/plan/0002-other/item-01-part1"},
            "ref not under heads": {**good, "ref": f"refs/tags/plan/{PLAN}/item-01-part1"},
            "no object": {"ref": good["ref"]},
            "sha too short": {**good, "object": {"sha": "abc"}},
        }
        for name, row in broken.items():
            with self.subTest(name):
                answers = {**self.answers, REFS_PATH: json.dumps([row])}

                with self.assertRaises(InflightError):
                    self.inflight(gh=FakeGh(answers))

    def test_a_branch_listed_twice_raises(self):
        self.push()
        self.push()

        self.assertIn("lists", self.read_fails())

    def test_a_stale_fetch_raises(self):
        self.answers[f"repos/{REPOSITORY}/branches/{self.default}"] = json.dumps(
            {"name": self.default, "commit": {"sha": "e" * 40}})

        self.assertIn("git fetch origin", self.read_fails())

    def test_no_admin_login_raises(self):
        with self.assertRaises(InflightError):
            self.inflight(who=tpl.Accounts("", APPROVER, AGENT))


class NoRequestPerRow(Inflight):
    def test_each_list_is_read_once_however_many_prs_and_branches_there_are(self):
        for number in range(8, 18):
            self.open_pr(number, ref=f"plan/{PLAN}/item-{number:02d}-part1")
            self.push(f"plan/{PLAN}/item-{number + 20:02d}-part1")
        gh = FakeGh(self.answers)

        self.inflight(gh=gh)

        paths = [path_of(argv) for argv in gh.argvs()]
        # Once for this command's own list, and twice more for the restart judgement's two snapshots (ADR 0081).
        self.assertEqual((paths.count(PULLS_PATH), paths.count(REFS_PATH)), (3, 1))
        self.assertEqual(paths.count(tpl.ALL_EVENTS), 3)  # the PRs' window, and the restart's two snapshots
        self.assertEqual([p for p in paths if p.startswith(f"repos/{REPOSITORY}/issues/") and p != tpl.ALL_EVENTS], [])

    def test_only_open_prs_need_one_events_read_and_no_branch_means_no_restart_read(self):
        for number in range(8, 12):
            self.open_pr(number, ref=f"plan/{PLAN}/item-{number:02d}-part1")
        gh = FakeGh(self.answers)

        self.inflight(gh=gh)

        paths = [path_of(argv) for argv in gh.argvs()]
        self.assertEqual((paths.count(PULLS_PATH), paths.count(REFS_PATH), paths.count(tpl.ALL_EVENTS)), (1, 1, 1))
        self.assertEqual([p for p in paths if "/issues/" in p and p != tpl.ALL_EVENTS], [])

    def test_a_plan_with_nothing_in_flight_reads_no_label_events(self):
        gh = FakeGh(self.answers)

        self.inflight(gh=gh)

        self.assertEqual([path_of(a) for a in gh.argvs() if "issues" in path_of(a)], [])


class Stops(Inflight):
    def test_a_pr_number_that_is_not_one_is_refused_before_any_read(self):
        gh = FakeGh(self.answers)

        stops = planlabels.check_stops(str(self.repo), REPOSITORY, PLAN, WHO, {0: frozenset()}, False, gh)

        self.assertEqual((stops.problems, gh.calls), (("0 is not a PR number",), []))

    def test_no_admin_login_raises_from_check_stops_itself(self):
        with self.assertRaises(planlabels.LabelCheckError):
            planlabels.check_stops(str(self.repo), REPOSITORY, PLAN, tpl.Accounts("", APPROVER, AGENT), {}, False,
                                   FakeGh(self.answers))

    def test_no_pr_and_no_branch_still_checks_the_plan(self):
        stops = planlabels.check_stops(str(self.repo), REPOSITORY, "0009-nope", WHO, {}, False,
                                       FakeGh(self.answers))

        self.assertIn("0009-nope is not a plan", stops.problems[0])

    def test_a_pr_that_is_not_in_the_window_has_no_stop_but_is_still_judged_on_its_label(self):
        self.put(8, [], labels=(BLOCKED,))

        stops = planlabels.check_stops(str(self.repo), REPOSITORY, PLAN, WHO, {8: frozenset({BLOCKED})}, False,
                                       FakeGh(self.answers))

        self.assertIn("events and the label disagree", stops.prs[8][0])

    def test_a_label_the_caller_did_not_casefold_is_still_a_stop(self):
        self.put(8, [], labels=("Plan:Blocked",))

        stops = planlabels.check_stops(str(self.repo), REPOSITORY, PLAN, WHO, {8: frozenset({"Plan:Blocked"})},
                                       False, FakeGh(self.answers))

        self.assertIn("events and the label disagree", stops.prs[8][0])


class Cli(Inflight):
    """`trimtab check-inflight`: the real CLI, real git and a scripted gh on PATH."""

    def setUp(self):
        super().setUp()
        self.inst = make_instance(self.root)
        path = self.inst / "instance.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**data, "accounts": {"admin": ADMIN, "agent": AGENT, "approver": APPROVER}}),
                        encoding="utf-8")
        self.answers[tpa.VARIABLE_PATH] = json.dumps({"name": "TRIMTAB_ADMIN", "value": ADMIN})

    def run_cli(self, *argv, repository=REPOSITORY, plan=PLAN):
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
            code = cli.main(["check-inflight", "--repository", repository, "--plan", plan,
                             "--repo", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_one_open_pr_exits_0_printing_what_to_resume(self):
        self.open_pr(8)

        code, out, err = self.run_cli()

        self.assertEqual((code, out), (0, f"resume pr 8 {ITEM_REF} {HEAD}\n"), err)

    def test_one_branch_with_no_pr_exits_0_printing_it(self):
        self.push()

        code, out, err = self.run_cli()

        self.assertEqual((code, out), (0, f"resume branch {ITEM_REF} {HEAD}\n"), err)

    def test_nothing_in_flight_exits_0_saying_so(self):
        code, out, err = self.run_cli()

        self.assertEqual((code, out), (0, "none in flight\n"), err)

    def test_none_in_flight_is_not_a_line_a_resume_can_print(self):
        self.open_pr(8)

        _, out, _ = self.run_cli()

        self.assertNotEqual(out.strip(), planinflight.NONE_IN_FLIGHT)
        self.assertTrue(out.startswith("resume "))

    def test_two_candidates_exit_1_naming_both_and_printing_nothing(self):
        self.open_pr(8)
        self.open_pr(9, ref=SECOND_REF)

        code, out, err = self.run_cli()

        self.assertEqual((code, out, "PR #8, PR #9" in err), (1, "", True), err)

    def test_a_refusal_exits_1_naming_every_problem(self):
        self.open_pr(8, repo="someone/fork", author=STRANGER)

        code, out, err = self.run_cli()

        self.assertEqual((code, out), (1, ""))
        self.assertEqual((err.count("FAIL"), "someone/fork" in err, STRANGER in err), (2, True, True))

    def test_a_blocked_pr_exits_1(self):
        self.open_pr(8, events=[event(1, "labeled", BLOCKED, AGENT)], labels=(BLOCKED,))

        code, out, err = self.run_cli()

        self.assertEqual((code, out, "no removal" in err), (1, "", True))

    def test_a_blocked_pr_the_admin_cleared_exits_0(self):
        self.open_pr(8, events=[event(1, "labeled", BLOCKED, AGENT), event(2, "unlabeled", BLOCKED, ADMIN)])

        code, out, err = self.run_cli()

        self.assertEqual((code, out.startswith("resume pr 8")), (0, True), err)

    def test_a_failed_read_exits_2(self):
        self.answers.pop(PULLS_PATH)

        code, out, err = self.run_cli()

        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("nothing is resumed", err)

    def test_a_list_cut_off_at_its_bound_exits_2(self):
        for number in (8, 9, 10):
            self.open_pr(number, ref=f"plan/{PLAN}/item-{number:02d}-part1")

        with mock.patch.object(planinflight, "PULLS_LIMIT", 2):
            code, out, err = self.run_cli()

        self.assertEqual((code, out, "more than 2 rows" in err), (2, "", True))

    def test_an_unexpected_row_exits_2(self):
        self.answers[PULLS_PATH] = json.dumps([{"number": 8}])

        code, out, err = self.run_cli()

        self.assertEqual((code, out), (2, ""), err)

    def test_a_stale_fetch_exits_2(self):
        self.answers[f"repos/{REPOSITORY}/branches/{self.default}"] = json.dumps(
            {"name": self.default, "commit": {"sha": "e" * 40}})

        code, out, err = self.run_cli()

        self.assertEqual((code, out, "git fetch origin" in err), (2, "", True))

    def test_an_instance_that_cannot_be_read_exits_2(self):
        (self.inst / "instance.json").write_text("not json", encoding="utf-8")

        code, out, err = self.run_cli()

        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("nothing is resumed", err)

    def test_an_admin_and_approver_that_are_the_same_account_exit_2(self):
        path = self.inst / "instance.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**data, "accounts": {"admin": ADMIN, "agent": AGENT, "approver": ADMIN}}),
                        encoding="utf-8")

        code, out, err = self.run_cli()

        self.assertEqual((code, out), (2, ""), err)

    def test_a_failure_nobody_foresaw_exits_2_never_1(self):
        with mock.patch.object(planinflight, "check", side_effect=RuntimeError("boom")):
            code, out, err = self.run_cli()

        self.assertEqual((code, out), (2, ""))
        self.assertIn("RuntimeError while reading what is in flight; nothing is resumed", err)

    def test_a_bad_name_exits_1_before_any_read(self):
        for repository, plan in (("nope", PLAN), (REPOSITORY, "a/b")):
            with self.subTest(repository=repository, plan=plan):
                code, out, err = self.run_cli(repository=repository, plan=plan)

                self.assertEqual((code, out), (1, ""), err)

    def test_an_admin_the_repository_does_not_name_exits_1(self):
        self.answers[tpa.VARIABLE_PATH] = json.dumps({"name": "TRIMTAB_ADMIN", "value": "someone-else"})

        code, out, err = self.run_cli()

        self.assertEqual((code, out), (1, ""), err)


class SkillAgreement(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[2]

    def text(self, *parts):
        return " ".join((self.ROOT / "skills" / Path(*parts)).read_text(encoding="utf-8").split())

    def test_in_flight_reference_runs_the_command_and_says_what_each_exit_code_means(self):
        text = self.text("plan-run", "references", "in-flight.md")

        self.assertIn("trimtab check-inflight --repository <owner>/<name> --plan <plan>", text)
        self.assertIn("| 0, printing `none in flight` |", text)
        self.assertIn("| 0, printing `resume pr <n> <branch> <sha>` or `resume branch <branch> <sha>` |", text)
        self.assertIn("| 1 | A stop: report **every** problem the command names", text)
        self.assertIn("run `git fetch origin` once", text)
        self.assertIn("A second exit 2 is a stop", text)
        self.assertIn("the logins come only from instance configuration, never from a PR", text)

    def test_in_flight_reference_no_longer_reads_labels_or_pull_lists_by_hand(self):
        text = self.text("plan-run", "references", "in-flight.md")

        self.assertNotIn("gh api 'repos/{owner}/{repo}/pulls", text)
        self.assertNotIn("label events", text)
        self.assertNotIn("check-label-actor", text)

    def test_the_skill_state_section_points_at_the_command(self):
        text = self.text("plan-run", "SKILL.md")

        self.assertIn("trimtab check-inflight", text)
