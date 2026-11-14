"""Whether the latest record's merge authorises it, from a faked gh and, for the wiring, real git (ADR 0072)."""

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import test_mergeauthority as tma
import test_planauth as tpa
import test_planrecord as tpr
from gh_fake import FakeGh, path_of
from instance_fixture import make_instance
from trimtab import cli, mergeauthority, planrecord, planrecordauth, planscope
from trimtab.mergeauthority import Accounts, PlanScopeResult
from trimtab.planrecordauth import RecordAuthError

REPOSITORY = tma.REPOSITORY
DEFAULT = tma.DEFAULT
PLAN = "0019-y"
ITEM = "item-01-part1"
HEAD_REF = f"plan/{PLAN}/{ITEM}"
RECORD_PATH = f"{planscope.PLANS_DIR}/progress/{PLAN}/{ITEM}.md"
RECORD_SHA = "d" * 40
RECORD = planrecord.Latest(RECORD_PATH, RECORD_SHA, 1, 1)
HEAD = tma.HEAD
EARLIER = tma.EARLIER
BASE_SHA = tma.BASE
NUMBER = 7
OTHER_PR = 9
ADMIN = tma.ADMIN
APPROVER = "the-approver[bot]"
STRANGER = "someone-else"
WHO = Accounts(ADMIN, APPROVER)
MERGED_AT = "2026-11-10T12:00:00Z"
BEFORE_MERGE = "2026-11-01T00:00:00Z"
AFTER_MERGE = "2026-11-11T00:00:00Z"
RULESET = 42
SECOND_RULESET = 43
SOURCE_FILE = "docs/notes.md"
def PASSING_GATE(repository, head):
    return PlanScopeResult((), wait=False)

PULLS_PATH = f"repos/{REPOSITORY}/commits/{RECORD_SHA}/pulls"
PULL_PATH = f"repos/{REPOSITORY}/pulls/{NUMBER}"
REVIEWS_PATH = f"{PULL_PATH}/reviews"
FILES_PATH = f"{PULL_PATH}/files"
RULES_PATH = tma.RULES_PATH


def ruleset_path(ruleset=RULESET):
    return f"repos/{REPOSITORY}/rulesets/{ruleset}"


def listed(number=NUMBER, merged_at=MERGED_AT, base=DEFAULT):
    return {"number": number, "merged_at": merged_at, "base": {"ref": base}}


def pull(merged_by=ADMIN, head_ref=HEAD_REF, head_repo=REPOSITORY, base=DEFAULT, merged_at=MERGED_AT,
         merge_commit=RECORD_SHA, changed_files=None, files=None, number=NUMBER, base_sha=BASE_SHA):
    return {"number": number, "merged_at": merged_at, "merge_commit_sha": merge_commit,
            "merged_by": None if merged_by is None else {"login": merged_by},
            "head": {"ref": head_ref, "sha": HEAD, "repo": None if head_repo is None else {"full_name": head_repo}},
            "base": {"ref": base, "sha": base_sha}, "changed_files": len(files or [SOURCE_FILE])
            if changed_files is None else changed_files}


def review(login, state="APPROVED", commit=HEAD, id=1, at=BEFORE_MERGE):
    return {"id": id, "user": {"login": login}, "state": state, "commit_id": commit, "submitted_at": at}


def file_row(name, previous=None):
    return {"filename": name, **({"previous_filename": previous} if previous else {})}


def rules(ruleset=RULESET):
    rows = tma.rule_rows()
    for row in rows:
        row["ruleset_id"] = ruleset
    return rows


def ruleset(updated=BEFORE_MERGE, enforcement="active", id=RULESET):
    return {"id": id, "updated_at": updated, "enforcement": enforcement}


def answers(merged_by=ADMIN, reviews=None, files=None, pull_changes=None, listed_rows=None, rule_rows=None,
            rulesets=None, scope=None):
    """A record added by PR 7's merge commit, approved by the admin on its head; for the approver route, a plan
    branch for this record, one source file, a passing plan-scope job, and a ruleset unchanged since the merge."""
    files = [file_row(SOURCE_FILE)] if files is None else files
    found = {
        PULLS_PATH: json.dumps([listed()] if listed_rows is None else listed_rows),
        PULL_PATH: json.dumps(pull(merged_by=merged_by, files=files, **(pull_changes or {}))),
        REVIEWS_PATH: json.dumps([review(ADMIN)] if reviews is None else reviews),
        FILES_PATH: json.dumps(files),
        **(tma.plan_scope_answers() if scope is None else scope),
        RULES_PATH: json.dumps(rules() if rule_rows is None else rule_rows),
    }
    for id, row in (rulesets or {RULESET: ruleset()}).items():
        found[ruleset_path(id)] = json.dumps(row)
    return found


def no_checker(sha):
    return False


def judge(found=None, who=WHO, record=RECORD, checker_in_tree=False, plan_gate=None, plan=PLAN, gh=None):
    return planrecordauth.judge(REPOSITORY, DEFAULT, plan, record, who, checker_in_tree,
                                gh or FakeGh(answers() if found is None else found), plan_gate,
                                checker_at_base=no_checker)


def approver_merge(**changes):
    """An approver merge meeting every condition this base can check."""
    return answers(merged_by=APPROVER, **changes)


class TheRecordsPr(unittest.TestCase):
    def test_an_admin_merge_with_the_admins_approval_on_the_head_passes(self):
        verdict = judge()

        self.assertEqual((verdict.problems, verdict.pr, verdict.merged_by, verdict.approved_by),
                         ((), NUMBER, ADMIN, ADMIN))

    def test_a_commit_in_no_merged_pr_is_a_named_problem(self):
        verdict = judge(answers(listed_rows=[]))

        self.assertIn("belongs to 0 PRs merged", verdict.problems[0])

    def test_a_commit_in_two_merged_prs_is_a_named_problem(self):
        verdict = judge(answers(listed_rows=[listed(), listed(OTHER_PR)]))

        self.assertIn("belongs to 2 PRs merged", verdict.problems[0])

    def test_a_listed_pr_merged_elsewhere_or_unmerged_does_not_count(self):
        verdict = judge(answers(listed_rows=[listed(base="release"), listed(OTHER_PR, merged_at=None)]))

        self.assertIn("belongs to 0 PRs merged", verdict.problems[0])

    def test_a_pr_whose_merge_commit_is_not_the_records_commit_is_a_named_problem(self):
        verdict = judge(answers(pull_changes={"merge_commit": EARLIER}))

        self.assertIn("not the record's commit", verdict.problems[0])

    def test_a_pr_whose_own_row_targets_another_branch_is_a_named_problem(self):
        verdict = judge(answers(pull_changes={"base": "release"}))

        self.assertIn("targets 'release'", verdict.problems[0])

    def test_a_pr_whose_own_row_is_unmerged_is_a_named_problem(self):
        verdict = judge(answers(pull_changes={"merged_at": None}))

        self.assertIn("is not merged", verdict.problems[0])

    def test_the_pull_list_is_asked_for_a_page_as_long_as_its_bound(self):
        gh = FakeGh(answers())

        judge(gh=gh)

        listed_call = next(argv for argv in gh.argvs() if path_of(argv) == PULLS_PATH)
        self.assertIn(f"per_page={planrecordauth.PULLS_LIMIT}", listed_call[4])

    def test_a_full_page_of_pulls_is_an_error_not_a_verdict(self):
        rows = [listed(n) for n in range(1, planrecordauth.PULLS_LIMIT + 1)]

        with self.assertRaises(RecordAuthError):
            judge(answers(listed_rows=rows))


class CodeGate(unittest.TestCase):
    def test_the_approvers_approval_passes_the_code_gate_for_an_admin_merge(self):
        verdict = judge(answers(reviews=[review(APPROVER)]))

        self.assertEqual((verdict.problems, verdict.approved_by), ((), APPROVER))

    def test_no_approval_fails_the_code_gate(self):
        verdict = judge(answers(reviews=[]))

        self.assertEqual(len(verdict.problems), 1)
        self.assertIn("code gate: no approval", verdict.problems[0])

    def test_an_approval_of_an_earlier_commit_fails_the_code_gate(self):
        verdict = judge(answers(reviews=[review(ADMIN, commit=EARLIER)]))

        self.assertIn("code gate: no approval", verdict.problems[0])

    def test_an_approval_from_an_account_the_instance_does_not_name_fails_the_code_gate(self):
        verdict = judge(answers(reviews=[review(STRANGER)]))

        self.assertIn("code gate: no approval", verdict.problems[0])

    def test_a_dismissed_last_review_hides_an_earlier_approval(self):
        verdict = judge(answers(reviews=[review(ADMIN, id=1), review(ADMIN, "DISMISSED", id=2)]))

        self.assertIn("code gate: no approval", verdict.problems[0])

    def test_an_approval_submitted_after_the_merge_does_not_count(self):
        verdict = judge(answers(reviews=[review(ADMIN, at=AFTER_MERGE)]))

        self.assertEqual(len(verdict.problems), 1)
        self.assertIn("submitted after the merge", verdict.problems[0])

    def test_an_approval_in_the_merges_own_second_counts(self):
        """Times have one-second resolution and the approver approves then merges back to back (ADR 0072)."""
        verdict = judge(answers(reviews=[review(ADMIN, at=MERGED_AT)]))

        self.assertEqual(verdict.problems, ())

    def test_a_late_approval_is_not_blamed_when_the_approval_that_is_missing_was_never_eligible(self):
        late_stranger = review(STRANGER, at=AFTER_MERGE)
        stale = review(ADMIN, commit=EARLIER, id=2)

        verdict = judge(answers(reviews=[late_stranger, stale]))

        self.assertEqual(len(verdict.problems), 1)
        self.assertNotIn("after the merge", verdict.problems[0])

    def test_an_earlier_approval_stands_beside_a_later_one(self):
        verdict = judge(answers(reviews=[review(ADMIN, id=1), review(ADMIN, id=2, at=AFTER_MERGE)]))

        self.assertEqual(verdict.problems, ())

    def test_a_request_for_changes_after_the_merge_still_voids_the_approval(self):
        verdict = judge(answers(reviews=[review(ADMIN, id=1), review(ADMIN, "CHANGES_REQUESTED", id=2,
                                                                      at=AFTER_MERGE)]))

        self.assertIn("requested changes", verdict.problems[0])

    def test_an_approval_with_no_readable_time_is_an_error(self):
        for at in (None, "yesterday"):
            with self.subTest(at=at):
                with self.assertRaises(RecordAuthError):
                    judge(answers(reviews=[review(ADMIN, at=at)]))

    def test_a_comment_after_an_approval_does_not_hide_it(self):
        verdict = judge(answers(reviews=[review(ADMIN, id=1), review(ADMIN, "COMMENTED", id=2)]))

        self.assertEqual(verdict.problems, ())

    def test_a_standing_request_for_changes_fails_even_beside_an_approval(self):
        verdict = judge(answers(reviews=[review(ADMIN), review(APPROVER, "CHANGES_REQUESTED", commit=EARLIER, id=2)]))

        self.assertEqual(verdict.problems, (f"code gate: {APPROVER} requested changes; that voids every approval "
                                            "until they approve again",))

    def test_an_approval_after_a_request_for_changes_clears_it(self):
        verdict = judge(answers(reviews=[review(ADMIN, "CHANGES_REQUESTED", id=1), review(ADMIN, id=2)]))

        self.assertEqual(verdict.problems, ())

    def test_logins_are_compared_without_case(self):
        verdict = judge(answers(merged_by=ADMIN.upper(), reviews=[review(ADMIN.upper())]))

        self.assertEqual(verdict.problems, ())


class Merger(unittest.TestCase):
    def test_a_merge_by_an_account_the_instance_does_not_name_is_a_named_problem(self):
        verdict = judge(answers(merged_by=STRANGER))

        self.assertEqual(verdict.problems,
                         (f"PR #{NUMBER} was merged by {STRANGER}, not {ADMIN} or the approver {APPROVER}",))

    def test_a_merge_by_a_deleted_account_is_a_named_problem(self):
        verdict = judge(answers(merged_by=None))

        self.assertIn("merged by a deleted account", verdict.problems[0])

    def test_an_approver_merge_with_no_approver_configured_is_a_named_problem(self):
        verdict = judge(answers(merged_by=APPROVER), who=Accounts(ADMIN, ""))

        self.assertIn("the instance names no approver", verdict.problems[0])

    def test_every_failure_is_named_not_just_the_first(self):
        verdict = judge(answers(merged_by=STRANGER, reviews=[]))

        self.assertEqual(len(verdict.problems), 2)


class ApproverMerge(unittest.TestCase):
    def test_an_approver_merge_meeting_every_condition_passes_once_a_plan_gate_job_passes(self):
        verdict = judge(approver_merge(), plan_gate=PASSING_GATE)

        self.assertEqual((verdict.problems, verdict.merged_by), ((), APPROVER))

    def test_an_approver_merge_fails_while_the_base_ships_no_plan_gate_job(self):
        verdict = judge(approver_merge())

        self.assertEqual(len(verdict.problems), 1)
        self.assertIn(f"ships no {planrecordauth.PLAN_GATE_JOB} job", verdict.problems[0])

    def test_a_failing_plan_gate_job_is_named(self):
        def failing(repository, head):
            return PlanScopeResult(("the plan-gate job did not pass",), wait=False)

        verdict = judge(approver_merge(), plan_gate=failing)

        self.assertEqual(verdict.problems, ("approver merge: the plan-gate job did not pass",))

    def test_a_head_for_another_plan_is_a_named_problem(self):
        verdict = judge(approver_merge(pull_changes={"head_ref": f"plan/0020-z/{ITEM}"}), plan_gate=PASSING_GATE)

        self.assertIn("is not plan/0019-y/item-01-part1", verdict.problems[0])

    def test_a_head_for_another_item_or_part_is_a_named_problem(self):
        verdict = judge(approver_merge(pull_changes={"head_ref": f"plan/{PLAN}/item-01-part2"}),
                        plan_gate=PASSING_GATE)

        self.assertIn("the branch of the record it carries", verdict.problems[0])

    def test_a_head_outside_plan_or_malformed_under_it_is_a_named_problem(self):
        for ref in ("feat/x", f"plan/{PLAN}/notes"):
            with self.subTest(ref=ref):
                verdict = judge(approver_merge(pull_changes={"head_ref": ref}), plan_gate=PASSING_GATE)

                self.assertEqual(len(verdict.problems), 1, verdict.problems)
                self.assertIn(f"the head {ref!r} is not", verdict.problems[0])

    def test_a_head_in_a_fork_or_a_deleted_fork_is_a_named_problem(self):
        for repo in (tma.FORK, None):
            with self.subTest(repo=repo):
                verdict = judge(approver_merge(pull_changes={"head_repo": repo}), plan_gate=PASSING_GATE)

                self.assertIn("not this repository", verdict.problems[0])

    def test_a_workflow_file_is_a_named_problem(self):
        verdict = judge(approver_merge(files=[file_row(".github/workflows/ci.yml")]), plan_gate=PASSING_GATE)

        self.assertIn("admin-only paths: .github/workflows/ci.yml", verdict.problems[0])

    def test_a_rename_out_of_github_is_a_named_problem(self):
        verdict = judge(approver_merge(files=[file_row("old-ci.yml", previous=".github/workflows/ci.yml")]),
                        plan_gate=PASSING_GATE)

        self.assertIn(".github/workflows/ci.yml", verdict.problems[0])

    def test_a_pin_file_is_a_named_problem(self):
        verdict = judge(approver_merge(files=[file_row("instance.json")]), plan_gate=PASSING_GATE)

        self.assertIn("admin-only paths: instance.json", verdict.problems[0])

    def test_the_checkers_code_is_admin_only_where_the_repository_runs_it_from_its_tree(self):
        found = approver_merge(files=[file_row("trimtab/cli.py")])

        held = judge(found, checker_in_tree=True, plan_gate=PASSING_GATE)
        elsewhere = judge(approver_merge(files=[file_row("trimtab/cli.py")]), plan_gate=PASSING_GATE)

        self.assertEqual((len(held.problems), elsewhere.problems), (1, ()))

    def test_the_checker_is_admin_only_when_only_the_prs_base_commit_holds_it(self):
        """After a rebase merge the record commit's parent may be one of the PR's own commits, so the base counts."""
        asked = []

        def holds_checker(sha):
            asked.append(sha)
            return True

        found = approver_merge(files=[file_row("trimtab/cli.py")])

        verdict = planrecordauth.judge(REPOSITORY, DEFAULT, PLAN, RECORD, WHO, False, FakeGh(found), PASSING_GATE,
                                       checker_at_base=holds_checker)

        self.assertEqual((asked, len(verdict.problems)), ([BASE_SHA], 1))
        self.assertIn("admin-only paths: trimtab/cli.py", verdict.problems[0])

    def test_a_base_without_the_checker_leaves_the_checkers_paths_open(self):
        found = approver_merge(files=[file_row("trimtab/cli.py")])

        verdict = planrecordauth.judge(REPOSITORY, DEFAULT, PLAN, RECORD, WHO, False, FakeGh(found), PASSING_GATE,
                                       checker_at_base=lambda sha: False)

        self.assertEqual(verdict.problems, ())

    def test_the_base_is_not_asked_when_the_checker_is_already_held_or_the_admin_merged(self):
        asked = []

        def holds_checker(sha):
            asked.append(sha)
            return False

        planrecordauth.judge(REPOSITORY, DEFAULT, PLAN, RECORD, WHO, True, FakeGh(approver_merge()), PASSING_GATE,
                             checker_at_base=holds_checker)
        planrecordauth.judge(REPOSITORY, DEFAULT, PLAN, RECORD, WHO, False, FakeGh(answers()), PASSING_GATE,
                             checker_at_base=holds_checker)

        self.assertEqual(asked, [])

    def test_a_base_commit_that_cannot_be_read_is_an_error(self):
        def unreadable(sha):
            raise RecordAuthError("git rev-parse exited 128")

        with self.assertRaises(RecordAuthError):
            planrecordauth.judge(REPOSITORY, DEFAULT, PLAN, RECORD, WHO, False, FakeGh(approver_merge()),
                                 PASSING_GATE, checker_at_base=unreadable)

    def test_a_pull_whose_base_sha_is_not_a_commit_id_is_an_error(self):
        for bad in ("main", None, 7):
            with self.subTest(base_sha=bad):
                with self.assertRaises(RecordAuthError):
                    judge(approver_merge(pull_changes={"base_sha": bad}), plan_gate=PASSING_GATE)

    def test_the_approvers_login_is_compared_without_case(self):
        verdict = judge(answers(merged_by=APPROVER.upper(), reviews=[review(APPROVER.upper())]),
                        plan_gate=PASSING_GATE)

        self.assertEqual(verdict.problems, ())

    def test_more_files_than_the_prs_count_is_a_named_problem(self):
        verdict = judge(approver_merge(pull_changes={"changed_files": 1},
                                       files=[file_row("a.md"), file_row("b.md")]), plan_gate=PASSING_GATE)

        self.assertIn("refusing a partial list", verdict.problems[0])

    def test_more_files_than_github_lists_is_a_named_problem_without_reading_them(self):
        gh = FakeGh(approver_merge(pull_changes={"changed_files": planrecordauth.FILES_LIMIT + 1}))

        verdict = judge(gh=gh, plan_gate=PASSING_GATE)

        self.assertIn("GitHub lists at most", verdict.problems[0])
        self.assertNotIn(FILES_PATH, [path_of(argv) for argv in gh.argvs()])

    def test_a_file_list_shorter_than_the_prs_count_is_a_named_problem(self):
        verdict = judge(approver_merge(pull_changes={"changed_files": 2}), plan_gate=PASSING_GATE)

        self.assertIn("refusing a partial list", verdict.problems[0])

    def test_a_file_row_of_the_wrong_shape_is_an_error(self):
        with self.assertRaises(RecordAuthError):
            judge(approver_merge(files=[{"filename": 7}]), plan_gate=PASSING_GATE)

    def test_a_plan_scope_job_that_did_not_pass_is_a_named_problem(self):
        scope = tma.plan_scope_answers(jobs=[tma.plan_scope_job(conclusion="failure")])

        verdict = judge(approver_merge(scope=scope), plan_gate=PASSING_GATE)

        self.assertIn("is not a passing job", verdict.problems[0])

    def test_a_plan_scope_run_still_going_is_a_wait_alone(self):
        scope = tma.plan_scope_answers(jobs=[tma.plan_scope_job(status="in_progress", conclusion=None)])

        verdict = judge(approver_merge(scope=scope), plan_gate=PASSING_GATE)

        self.assertEqual((verdict.wait, len(verdict.problems)), (True, 1))

    def test_the_unshipped_plan_gate_line_alone_does_not_turn_a_wait_into_a_failure(self):
        scope = tma.plan_scope_answers(jobs=[tma.plan_scope_job(status="in_progress", conclusion=None)])

        verdict = judge(approver_merge(scope=scope))

        self.assertEqual((verdict.wait, len(verdict.problems)), (True, 2))

    def test_a_plan_scope_run_still_going_beside_another_failure_is_not_a_wait(self):
        scope = tma.plan_scope_answers(jobs=[tma.plan_scope_job(status="in_progress", conclusion=None)])

        verdict = judge(approver_merge(scope=scope, files=[file_row(".github/x.yml")]), plan_gate=PASSING_GATE)

        self.assertEqual((verdict.wait, len(verdict.problems)), (False, 2))

    def test_a_ruleset_that_does_not_bind_plan_scope_to_actions_is_a_named_problem(self):
        unbound = tma.rule_rows(checks={"plan-scope": None})
        scope = {**tma.plan_scope_answers(), RULES_PATH: json.dumps(unbound)}

        verdict = judge(approver_merge(scope=scope, rule_rows=unbound), plan_gate=PASSING_GATE)

        self.assertEqual(len(verdict.problems), 1, verdict.problems)
        self.assertIn("does not require plan-scope bound to GitHub Actions", verdict.problems[0])

    def test_a_ruleset_changed_after_the_merge_is_a_named_problem(self):
        verdict = judge(approver_merge(rulesets={RULESET: ruleset(updated=AFTER_MERGE)}), plan_gate=PASSING_GATE)

        self.assertIn("last changed before the merge", verdict.problems[0])

    def test_a_ruleset_changed_in_the_merges_second_cannot_vouch_for_it(self):
        verdict = judge(approver_merge(rulesets={RULESET: ruleset(updated=MERGED_AT)}), plan_gate=PASSING_GATE)

        self.assertIn("last changed before the merge", verdict.problems[0])

    def test_a_ruleset_not_enforced_cannot_vouch_for_the_merge(self):
        verdict = judge(approver_merge(rulesets={RULESET: ruleset(enforcement="evaluate")}), plan_gate=PASSING_GATE)

        self.assertIn("evaluate", verdict.problems[0])

    def test_one_ruleset_unchanged_since_before_the_merge_vouches_beside_a_newer_one(self):
        rows = rules(RULESET) + rules(SECOND_RULESET)
        found = approver_merge(rule_rows=rows, rulesets={RULESET: ruleset(updated=AFTER_MERGE),
                                                         SECOND_RULESET: ruleset(id=SECOND_RULESET)})

        verdict = judge(found, plan_gate=PASSING_GATE)

        self.assertEqual(verdict.problems, ())

    def test_a_rule_with_no_ruleset_id_is_an_error(self):
        rows = tma.rule_rows()

        with self.assertRaises(RecordAuthError):
            judge(approver_merge(rule_rows=rows), plan_gate=PASSING_GATE)

    def test_a_ruleset_time_of_the_wrong_shape_is_an_error(self):
        with self.assertRaises(RecordAuthError):
            judge(approver_merge(rulesets={RULESET: ruleset(updated="yesterday")}), plan_gate=PASSING_GATE)

    def test_every_failure_of_the_route_is_named(self):
        found = approver_merge(pull_changes={"head_ref": "feat/x"}, files=[file_row(".github/x.yml")],
                               rulesets={RULESET: ruleset(updated=AFTER_MERGE)})

        verdict = judge(found)

        self.assertEqual(len(verdict.problems), 4, verdict.problems)  # head, files, ruleset, plan-gate

    def test_each_endpoint_is_read_once_per_check(self):
        gh = FakeGh(approver_merge())

        judge(gh=gh, plan_gate=PASSING_GATE)

        reads = [path_of(argv) for argv in gh.argvs()]
        self.assertEqual((reads.count(tma.REPOSITORY_PATH), reads.count(RULES_PATH)), (1, 1))

    def test_an_admin_merge_reads_none_of_the_route(self):
        gh = FakeGh(answers())

        judge(gh=gh)

        self.assertEqual({path_of(argv) for argv in gh.argvs()}, {PULLS_PATH, PULL_PATH, REVIEWS_PATH})


class RouteEdges(unittest.TestCase):
    """The fail-closed branches of the approver route and of `judge`'s own inputs."""

    def approver(self, **changes):
        return judge(approver_merge(**changes), plan_gate=PASSING_GATE)

    def test_a_record_with_no_commit_or_a_bad_commit_is_an_error(self):
        for record in (planrecord.Latest(None, None, 1, 1), planrecord.Latest(RECORD_PATH, "abc", 1, 1)):
            with self.subTest(record=record):
                with self.assertRaises(RecordAuthError):
                    judge(record=record)

    def test_a_plan_gate_job_still_running_is_a_named_problem_not_a_wait(self):
        def running(repository, head):
            return PlanScopeResult(("the plan-gate job is queued",), wait=True)

        verdict = judge(approver_merge(), plan_gate=running)

        self.assertEqual((verdict.wait, len(verdict.problems)), (False, 2))
        self.assertIn("plan-gate job on the final head has not completed", verdict.problems[0])

    def test_more_admin_only_paths_than_are_shown_are_counted(self):
        names = [f".github/workflows/w{i}.yml" for i in range(planrecordauth.mergeauthority.SHOWN_PATHS + 2)]

        verdict = self.approver(files=[file_row(n) for n in names])

        self.assertIn("and 2 more", verdict.problems[0])

    def test_a_failed_files_read_is_an_error(self):
        found = approver_merge()
        del found[FILES_PATH]

        with self.assertRaises(RecordAuthError):
            judge(found, plan_gate=PASSING_GATE)

    def test_a_previous_filename_of_the_wrong_shape_is_an_error(self):
        with self.assertRaises(RecordAuthError):
            self.approver(files=[{"filename": "a.md", "previous_filename": 7}])

    def test_a_failed_rules_read_is_an_error(self):
        found = approver_merge()
        del found[RULES_PATH]

        with self.assertRaises(RecordAuthError):
            judge(found, plan_gate=PASSING_GATE)

    def test_a_rule_row_of_the_wrong_shape_is_an_error(self):
        for rows in (["x"], [{"type": 7}],
                     [{"type": "required_status_checks", "parameters": {"required_status_checks": "no"}}],
                     [{"type": "required_status_checks", "parameters": {"required_status_checks": [
                         {"context": "plan-scope", "integration_id": 15368}]}, "ruleset_id": "7"}]):
            with self.subTest(rows=rows):
                with self.assertRaises(RecordAuthError):
                    self.approver(rule_rows=rows)

    def test_a_failed_ruleset_read_is_an_error(self):
        found = approver_merge()
        del found[ruleset_path()]

        with self.assertRaises(RecordAuthError):
            judge(found, plan_gate=PASSING_GATE)

    def test_more_rulesets_than_the_bound_is_an_error(self):
        count = planrecordauth.RULESET_LIMIT + 1
        rows = [row for n in range(1, count + 1) for row in rules(n)]

        with self.assertRaises(RecordAuthError):
            self.approver(rule_rows=rows, rulesets={n: ruleset(id=n) for n in range(1, count + 1)})

    def test_a_failed_review_read_is_an_error(self):
        found = answers()
        del found[REVIEWS_PATH]

        with self.assertRaises(RecordAuthError):
            judge(found)

    def test_a_failed_plan_scope_read_is_an_error(self):
        found = approver_merge()
        del found[tma.ACTIONS_RUNS_PATH]

        with self.assertRaises(RecordAuthError):
            judge(found, plan_gate=PASSING_GATE)


class FailedReads(unittest.TestCase):
    def test_a_failed_gh_read_is_an_error(self):
        with self.assertRaises(RecordAuthError):
            judge(gh=FakeGh(returncode=1, stderr="gh: Not Found (HTTP 404)"))

    def test_a_pull_of_the_wrong_shape_is_an_error(self):
        for broken in ({"number": NUMBER}, {**pull(), "changed_files": "1"}, {**pull(), "merged_at": "soon"}):
            with self.subTest(broken=broken):
                with self.assertRaises(RecordAuthError):
                    judge({**answers(), PULL_PATH: json.dumps(broken)})

    def test_a_pull_list_of_the_wrong_shape_is_an_error(self):
        for broken in ({"rows": []}, [{"number": "7", "base": {"ref": DEFAULT}}], [{"number": NUMBER}]):
            with self.subTest(broken=broken):
                with self.assertRaises(RecordAuthError):
                    judge({**answers(), PULLS_PATH: json.dumps(broken)})

    def test_a_review_list_of_the_wrong_shape_is_an_error(self):
        with self.assertRaises(RecordAuthError):
            judge({**answers(), REVIEWS_PATH: json.dumps([{"id": 1}])})

    def test_a_repository_or_plan_that_is_not_a_name_is_refused_before_any_read(self):
        gh = FakeGh(answers())

        for repository, plan in (("owner/../x", PLAN), (REPOSITORY, "../x")):
            with self.subTest(repository=repository, plan=plan):
                with self.assertRaises(RecordAuthError):
                    planrecordauth.judge(repository, DEFAULT, plan, RECORD, WHO, False, gh,
                                         checker_at_base=no_checker)

        self.assertEqual(gh.calls, [])


class Wiring(tpr.Records):
    """`check` over real git: the latest record and its commit come from `planrecord.latest`."""

    def check(self, plan=tpr.PLAN, gh=None):
        return planrecordauth.check(str(self.repo), tpa.REPOSITORY, plan, Accounts(tpa.ADMIN, APPROVER),
                                    gh or FakeGh(self.answers))

    def record_pr(self, sha, merged_by=tpa.ADMIN, files=None):
        files = files or [file_row(tpr.record_path(1, 1))]
        self.answers.update({
            f"repos/{tpa.REPOSITORY}/commits/{sha}/pulls": json.dumps([listed()]),
            f"repos/{tpa.REPOSITORY}/pulls/{NUMBER}": json.dumps(
                pull(merged_by=merged_by, head_ref=f"plan/{tpr.PLAN}/item-01-part1", merge_commit=sha, files=files,
                     base_sha=tpa.git(self.repo, "rev-parse", f"{sha}^1"))),
            f"repos/{tpa.REPOSITORY}/pulls/{NUMBER}/reviews": json.dumps([review(tpa.ADMIN)]),
            f"repos/{tpa.REPOSITORY}/pulls/{NUMBER}/files": json.dumps(files),
        })

    def test_no_record_is_a_pass_with_nothing_judged(self):
        verdict = self.check()

        self.assertEqual((verdict.problems, verdict.record), ((), None))

    def test_the_latest_records_merge_commit_is_the_one_judged(self):
        sha = self.merge_record(1, 1)
        self.record_pr(sha)

        verdict = self.check()

        self.assertEqual((verdict.problems, verdict.record, verdict.pr), ((), tpr.record_path(1, 1), NUMBER))

    def test_a_plan_that_is_not_a_name_is_a_named_problem_before_any_read(self):
        gh = FakeGh(self.answers)

        verdict = self.check(plan="../x", gh=gh)

        self.assertEqual((len(verdict.problems), gh.calls), (1, []))

    def test_a_record_name_that_is_not_item_part_is_a_named_problem(self):
        self.merge_branch("odd", {f"{tpr.FOLDER}/notes.md": "x\n"})
        self.publish()

        verdict = self.check()

        self.assertIn("notes.md", verdict.problems[0])

    def test_a_commit_git_does_not_hold_is_an_error_when_asking_for_the_checker(self):
        with self.assertRaises(RecordAuthError):
            planrecordauth.checker_at(str(self.repo), "f" * 40)

    def test_the_checker_is_found_in_a_commits_tree(self):
        with_checker = self.merge_branch("checker", {mergeauthority.CHECKER_MARK: "#!/bin/sh\n"})

        self.assertEqual((planrecordauth.checker_at(str(self.repo), with_checker),
                          planrecordauth.checker_at(str(self.repo), f"{with_checker}^1")), (True, False))

    def test_a_stale_fetch_is_an_error(self):
        late = self.merge_branch("late", {"README.md": "changed\n"})  # the server moves; origin's copy does not
        self.answers[f"repos/{tpa.REPOSITORY}/branches/{tpa.DEFAULT}"] = json.dumps(
            {"name": tpa.DEFAULT, "commit": {"sha": late}})

        with self.assertRaises(RecordAuthError):
            self.check()

    def test_a_plan_scope_run_still_going_is_a_wait_through_check(self):
        sha = self.merge_record(1, 1)
        self.record_pr(sha, merged_by=APPROVER)
        scope = tma.plan_scope_answers(jobs=[tma.plan_scope_job(status="in_progress", conclusion=None)])
        self.answers.update({k: v for k, v in scope.items() if k != tma.REPOSITORY_PATH})
        self.answers[f"repos/{tpa.REPOSITORY}/rules/branches/{tpa.DEFAULT}"] = json.dumps(rules())
        self.answers[ruleset_path()] = json.dumps(ruleset())

        def gate(repository, head):
            return PlanScopeResult((), wait=False)

        verdict = planrecordauth.check(str(self.repo), tpa.REPOSITORY, tpr.PLAN, Accounts(tpa.ADMIN, APPROVER),
                                       FakeGh(self.answers), gate)

        self.assertEqual((verdict.wait, len(verdict.problems)), (True, 1), verdict.problems)

    def test_a_plan_not_on_the_default_branch_is_a_named_problem(self):
        verdict = self.check(plan="0099-missing")

        self.assertIn("is not a plan", verdict.problems[0])

    def test_the_checker_is_admin_only_when_the_merge_base_holds_bin_trimtab(self):
        self.merge_branch("checker", {mergeauthority.CHECKER_MARK: "#!/bin/sh\n"})
        self.publish()
        sha = self.merge_record(1, 1)
        self.record_pr(sha, merged_by=APPROVER, files=[file_row("trimtab/cli.py")])
        self.answers.update({k: v for k, v in tma.plan_scope_answers().items()
                             if k not in (tma.REPOSITORY_PATH,)})
        self.answers[f"repos/{tpa.REPOSITORY}/rules/branches/{tpa.DEFAULT}"] = json.dumps(rules())
        self.answers[ruleset_path()] = json.dumps(ruleset())

        verdict = self.check()

        self.assertTrue(any("admin-only paths: trimtab/cli.py" in p for p in verdict.problems), verdict.problems)

    def approver_merged_record(self, record_files, base_sha=None):
        """An approver-merged record PR over files that touch the checker's code, every other condition met."""
        sha = self.merge_branch("rec", {tpr.record_path(1, 1): tpr.record_text("item 1 complete"), **record_files})
        self.publish()
        self.record_pr(sha, merged_by=APPROVER, files=[file_row("trimtab/cli.py")])
        if base_sha:
            pulled = json.loads(self.answers[f"repos/{tpa.REPOSITORY}/pulls/{NUMBER}"])
            pulled["base"]["sha"] = base_sha
            self.answers[f"repos/{tpa.REPOSITORY}/pulls/{NUMBER}"] = json.dumps(pulled)
        self.answers.update({k: v for k, v in tma.plan_scope_answers().items() if k != tma.REPOSITORY_PATH})
        self.answers[f"repos/{tpa.REPOSITORY}/rules/branches/{tpa.DEFAULT}"] = json.dumps(rules())
        self.answers[ruleset_path()] = json.dumps(ruleset())

    def test_the_checker_is_admin_only_when_only_the_record_commit_holds_it(self):
        """The PR that adds the record also adds the checker, so the record commit's parent lacks it."""
        self.approver_merged_record({mergeauthority.CHECKER_MARK: "#!/bin/sh\n"})

        verdict = self.check()

        self.assertTrue(any("admin-only paths: trimtab/cli.py" in p for p in verdict.problems), verdict.problems)

    def test_the_checker_is_admin_only_when_only_the_prs_base_holds_it_after_a_rebase_merge(self):
        """The checker was on main at the PR's base, then removed by a commit the record's PR sits on top of."""
        holder = self.merge_branch("checker", {mergeauthority.CHECKER_MARK: "#!/bin/sh\n"})
        self.merge_branch("drop-checker", {}, removed=(mergeauthority.CHECKER_MARK,))
        self.approver_merged_record({}, base_sha=holder)

        verdict = self.check()

        self.assertTrue(any("admin-only paths: trimtab/cli.py" in p for p in verdict.problems), verdict.problems)

    def test_the_checker_is_admin_only_when_only_the_default_branch_tip_holds_it(self):
        """The checker landed on the branch after the merge, and the PR's `base.sha` lags: no tree the merge sat on
        holds it, but the tip does."""
        self.approver_merged_record({})
        mine = {k: v for k, v in self.answers.items() if k.startswith(f"repos/{tpa.REPOSITORY}/pulls/{NUMBER}")}
        self.merge_branch("checker-later", {mergeauthority.CHECKER_MARK: "#!/bin/sh\n"})
        self.publish()  # moves the fetched tip, and answers pull 7 as the plan's own PR
        self.answers.update(mine)

        verdict = self.check()

        self.assertTrue(any("admin-only paths: trimtab/cli.py" in p for p in verdict.problems), verdict.problems)

    def test_the_checker_paths_stay_open_when_none_of_the_four_trees_holds_it(self):
        self.approver_merged_record({})

        verdict = self.check()

        self.assertFalse(any("admin-only" in p for p in verdict.problems), verdict.problems)

    def test_a_pull_with_no_base_sha_is_an_error(self):
        sha = self.merge_record(1, 1)
        self.record_pr(sha, merged_by=APPROVER)
        pulled = json.loads(self.answers[f"repos/{tpa.REPOSITORY}/pulls/{NUMBER}"])
        del pulled["base"]["sha"]
        self.answers[f"repos/{tpa.REPOSITORY}/pulls/{NUMBER}"] = json.dumps(pulled)

        with self.assertRaises(RecordAuthError):
            self.check()


class Cli(tpa.GitRepository):
    """`trimtab check-record-authorised` against a fixture instance, real git and a scripted gh."""

    cli = tpa.Cli.cli

    def setUp(self):
        super().setUp()
        self.inst = make_instance(self.root)
        self.answers[tpa.VARIABLE_PATH] = json.dumps({"name": "TRIMTAB_ADMIN", "value": tpa.ADMIN})
        path = self.inst / "instance.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**data, "accounts": {"admin": tpa.ADMIN, "approver": APPROVER}}),
                        encoding="utf-8")
        self.merge_branch("plan2", {tpr.PLAN_FILE: tpr.PLAN_TEXT, tpr.WRITES_FILE: ""})
        self.publish()

    def run_cli(self, plan=tpr.PLAN, **kwargs):
        return self.cli(command=("check-record-authorised",), plan=plan, **kwargs)

    def merge_record(self, merged_by=tpa.ADMIN):
        sha = self.merge_branch("rec", {tpr.record_path(1, 1): tpr.record_text("item 1 complete")})
        self.publish()
        self.answers.update({
            f"repos/{tpa.REPOSITORY}/commits/{sha}/pulls": json.dumps([listed()]),
            f"repos/{tpa.REPOSITORY}/pulls/{NUMBER}": json.dumps(
                pull(merged_by=merged_by, head_ref=f"plan/{tpr.PLAN}/item-01-part1", merge_commit=sha,
                     base_sha=tpa.git(self.repo, "rev-parse", f"{sha}^1"))),
            f"repos/{tpa.REPOSITORY}/pulls/{NUMBER}/reviews": json.dumps([review(tpa.ADMIN)]),
        })
        return sha

    def test_no_record_exits_0(self):
        done = self.run_cli()

        self.assertEqual((done.returncode, done.stdout), (0, f"plan {tpr.PLAN}: no record; nothing to judge\n"),
                         done.stderr)

    def test_an_admin_merge_with_the_code_gate_exits_0_naming_the_pr(self):
        self.merge_record()

        done = self.run_cli()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"PR #{NUMBER} merged by {tpa.ADMIN}", done.stdout)

    def test_a_merge_by_another_account_exits_1_naming_it(self):
        self.merge_record(merged_by=STRANGER)

        done = self.run_cli()

        self.assertEqual((done.returncode, done.stdout, STRANGER in done.stderr), (1, "", True), done.stderr)

    def test_a_plan_that_is_not_a_name_exits_1(self):
        done = self.run_cli(plan="../x")

        self.assertEqual((done.returncode, done.stdout), (1, ""), done.stderr)

    def test_an_admin_the_repository_variable_does_not_match_exits_1(self):
        self.answers[tpa.VARIABLE_PATH] = json.dumps({"name": "TRIMTAB_ADMIN", "value": STRANGER})

        done = self.run_cli()

        self.assertEqual((done.returncode, done.stdout), (1, ""), done.stderr)

    def test_a_failed_read_exits_2(self):
        self.merge_record()
        del self.answers[f"repos/{tpa.REPOSITORY}/pulls/{NUMBER}"]

        done = self.run_cli()

        self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)

    def test_an_unreadable_instance_exits_2(self):
        done = self.run_cli(env_extra={"TRIMTAB_INSTANCE": str(self.root / "no-such-instance")})

        self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)

    def approver_merge_with_plan_scope(self, scope_job):
        """An approver-merged record on a plan branch whose every other route condition holds."""
        sha = self.merge_record(merged_by=APPROVER)
        scope = tma.plan_scope_answers(jobs=[scope_job])
        self.answers.update({k: v for k, v in scope.items() if k != tma.REPOSITORY_PATH})
        self.answers[f"repos/{tpa.REPOSITORY}/pulls/{NUMBER}/files"] = json.dumps([file_row(SOURCE_FILE)])
        self.answers[f"repos/{tpa.REPOSITORY}/rules/branches/{tpa.DEFAULT}"] = json.dumps(rules())
        self.answers[ruleset_path()] = json.dumps(ruleset())
        return sha

    def run_in_process(self, *extra, plan_gate=None):
        """The real CLI, real git and a scripted gh on PATH, with only the plan-gate reader swapped."""
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
        argv = ["check-record-authorised", "--repository", tpa.REPOSITORY, "--plan", tpr.PLAN, "--repo",
                str(self.repo), *extra]
        env = {"TRIMTAB_INSTANCE": str(self.inst), "FAKE_ANSWERS": str(self.root / "answers.json"),
               "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", env), mock.patch.object(planrecordauth, "PLAN_GATE", plan_gate), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_plan_scope_run_still_going_exits_3(self):
        self.approver_merge_with_plan_scope(tma.plan_scope_job(status="in_progress", conclusion=None))

        code, out, err = self.run_in_process(plan_gate=lambda repository, head: PlanScopeResult((), wait=False))

        self.assertEqual((code, out, "has not completed" in err), (3, "", True), err)

    def test_a_plan_scope_run_still_going_exits_3_while_the_base_ships_no_plan_gate_job(self):
        self.approver_merge_with_plan_scope(tma.plan_scope_job(status="in_progress", conclusion=None))

        code, out, err = self.run_in_process()

        self.assertEqual((code, out), (3, ""), err)

    def test_an_approver_merge_meeting_every_condition_exits_0_once_plan_gate_passes(self):
        self.approver_merge_with_plan_scope(tma.plan_scope_job())

        code, out, err = self.run_in_process(plan_gate=lambda repository, head: PlanScopeResult((), wait=False))

        self.assertEqual((code, f"merged by {APPROVER}" in out), (0, True), err)

    def test_an_approver_merge_exits_1_while_the_base_ships_no_plan_gate_job(self):
        self.approver_merge_with_plan_scope(tma.plan_scope_job())

        code, out, err = self.run_in_process()

        self.assertEqual((code, out, "plan-gate" in err), (1, "", True), err)

    def test_an_instance_naming_a_malformed_approver_exits_2(self):
        path = self.inst / "instance.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**data, "accounts": {"admin": tpa.ADMIN, "approver": tpa.ADMIN}}),
                        encoding="utf-8")

        done = self.run_cli()

        self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)

    def test_an_unexpected_failure_exits_2_not_1(self):
        argv = ["check-record-authorised", "--repository", tpa.REPOSITORY, "--plan", tpr.PLAN, "--repo",
                str(self.repo)]
        err = io.StringIO()

        with mock.patch.dict("os.environ", {"TRIMTAB_INSTANCE": str(self.inst)}), \
                mock.patch.object(mergeauthority, "plan_admin_problems", return_value=()), \
                mock.patch.object(planrecordauth, "check", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(argv)

        self.assertEqual((code, out.getvalue(), "RuntimeError" in err.getvalue()), (2, "", True))


if __name__ == "__main__":
    unittest.main()
