"""The merge-authority check: whose approval a PR needs, from its branch, its paths, its reviews and plan-scope."""

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from gh_fake import FakeGh, path_of, query_of
from test_scrub import isolated_git_env
from trimtab import mergeauthority, planscope
from trimtab.capture.sources import GhApi
from trimtab.mergeauthority import (Accounts, Activity, CheckRun, Containment, MergeAuthorityError, Pr, RequiredCheck,
                                    Review, RouteFacts)

ROOT = Path(__file__).resolve().parents[2]
TRIMTAB = ROOT / "bin" / "trimtab"
REPOSITORY = "owner/repo"
FORK = "someone/repo"
ADMIN = "the-admin"
APPROVER = "the-approver"
AGENT = "the-agent"
WHO = Accounts(ADMIN, APPROVER, AGENT)
HEAD = "a" * 40
EARLIER = "b" * 40
BASE = "c" * 40
DEFAULT = "main"
PLAN_REF = "plan/0019-y/item-01-part1"
FEATURE_REF = "feat/something"
NUMBER = 7
SOURCE = ["src/app.py"]
AUTHORISED_PLAN = f"{planscope.AUTHORISED}/0020-z.md"
OTHER_APP = "some-other-app"
NO_COMMIT = "0" * 40
ACTIONS_ID = 15368
SCANNER_ID = 46505
REVIEW_ID = 5531184
OTHER_ID = 99
REQUIRED = {"lint-test": ACTIONS_ID, "plan-scope": ACTIONS_ID, "merge-authority": ACTIONS_ID,
            "claude-review": REVIEW_ID, "GitGuardian Security Checks": SCANNER_ID}
SETTINGS = ("require_code_owner_review", "require_last_push_approval", "dismiss_stale_reviews_on_push",
            "require_extra_approval_for_unattributed_changes", "required_review_thread_resolution")


def pr(head_ref=PLAN_REF, head_repo=REPOSITORY, base_ref=DEFAULT):
    return Pr(repository=REPOSITORY, head_repo=head_repo, head_ref=head_ref, head=HEAD, base_ref=base_ref,
              default_branch=DEFAULT)


def review(login, state="APPROVED", commit=HEAD, id=0):
    """A review; id 0 means "in the order given", which decided() fills in."""
    return Review(id, login, state, commit)


def run(conclusion="success", status="completed", started="2026-10-25T10:00:00Z", app="github-actions", id=1):
    return CheckRun(id, mergeauthority.CHECK_NAME, app, status, conclusion, started)


PASSED = [run()]
# The approver route with every guard satisfied, so the tests of the rest of the decision are not about it.
OPEN_ROUTE = RouteFacts(
    author=AGENT,
    activities=(Activity(1, "branch_creation", AGENT, NO_COMMIT, HEAD),),
    containment=Containment(frozenset(SETTINGS), 1, True,
                            tuple(RequiredCheck(c, a) for c, a in REQUIRED.items())),
    runs=tuple(CheckRun(i, c, "", "completed", "success", "", a) for i, (c, a) in enumerate(REQUIRED.items(), 1)),
    statuses=())


def decided(*reviews, head_ref=PLAN_REF, runs=PASSED, paths=SOURCE, checker_in_tree=False, who=WHO, **kwargs):
    ordered = [r if r.id else replace(r, id=i) for i, r in enumerate(reviews, start=1)]
    return mergeauthority.decide(pr(head_ref, **kwargs), who, list(paths), checker_in_tree, ordered, list(runs),
                                 OPEN_ROUTE)


class NonPlanPrs(unittest.TestCase):
    def test_an_admin_approval_passes(self):
        result = decided(review(ADMIN), head_ref=FEATURE_REF, runs=[])

        self.assertEqual(result, mergeauthority.Result(ADMIN, ()))

    def test_an_approver_approval_alone_fails(self):
        result = decided(review(APPROVER), head_ref=FEATURE_REF, runs=[])

        self.assertIn("not a plan branch", result.problems[0])

    def test_no_plan_scope_run_is_needed(self):
        result = decided(review(ADMIN), head_ref=FEATURE_REF, runs=[])

        self.assertEqual(result.problems, ())

    def test_another_accounts_approval_does_not_count(self):
        result = decided(review(AGENT), head_ref=FEATURE_REF, runs=[])

        self.assertEqual(len(result.problems), 1)


class PlanPrs(unittest.TestCase):
    def test_an_approver_approval_passes_while_plan_scope_passes(self):
        result = decided(review(APPROVER))

        self.assertEqual(result, mergeauthority.Result(APPROVER, ()))

    def test_an_admin_approval_passes_while_plan_scope_passes(self):
        self.assertEqual(decided(review(ADMIN)).approved_by, ADMIN)

    def test_a_failed_plan_scope_run_fails_it(self):
        result = decided(review(APPROVER), runs=[run("failure")])

        self.assertIn("concluded failure", result.problems[0])

    def test_a_failed_plan_scope_run_fails_it_even_for_the_admin(self):
        self.assertEqual(len(decided(review(ADMIN), runs=[run("failure")]).problems), 1)

    def test_no_plan_scope_run_fails_it(self):
        self.assertIn("no plan-scope check run", decided(review(APPROVER), runs=[]).problems[0])

    def test_a_skipped_plan_scope_run_fails_it(self):
        self.assertIn("concluded skipped", decided(review(APPROVER), runs=[run("skipped")]).problems[0])

    def test_a_run_still_in_progress_fails_it(self):
        runs = [run(), run(None, status="in_progress", started="2026-10-25T11:00:00Z", id=2)]

        self.assertIn("not completed", decided(review(APPROVER), runs=runs).problems[0])

    def test_a_later_failure_outweighs_an_earlier_success(self):
        runs = [run(id=1), run("failure", started="2026-10-25T11:00:00Z", id=2)]

        self.assertIn("concluded failure", decided(review(APPROVER), runs=runs).problems[0])

    def test_a_later_success_does_not_clear_an_earlier_failure(self):
        runs = [run("failure", id=1), run(started="2026-10-25T11:00:00Z", id=2)]

        self.assertIn("a new commit is needed", decided(review(APPROVER), runs=runs).problems[0])

    def test_a_run_from_another_app_does_not_count(self):
        result = decided(review(APPROVER), runs=[run(app=OTHER_APP)])

        self.assertIn("no plan-scope check run", result.problems[0])

    def test_a_failure_from_another_app_is_ignored(self):
        runs = [run(), run("failure", started="2026-10-25T11:00:00Z", app=OTHER_APP, id=2)]

        self.assertEqual(decided(review(APPROVER), runs=runs).problems, ())


class AdminOnly(unittest.TestCase):
    def test_a_plan_pr_adding_an_authorised_plan_needs_the_admin(self):
        result = decided(review(APPROVER), paths=[AUTHORISED_PLAN])

        self.assertIn("admin-only paths", result.problems[0])

    def test_the_admin_may_approve_a_plan_pr_adding_an_authorised_plan(self):
        self.assertEqual(decided(review(ADMIN), paths=[AUTHORISED_PLAN]).problems, ())

    def test_a_workflow_change_needs_the_admin(self):
        self.assertEqual(len(decided(review(APPROVER), paths=[".github/workflows/ci.yml"]).problems), 1)

    def test_either_pin_file_needs_the_admin(self):
        for pin in mergeauthority.PIN_FILES:
            with self.subTest(pin=pin):
                self.assertEqual(len(decided(review(APPROVER), paths=[pin]).problems), 1)

    def test_checker_code_needs_the_admin_where_the_checker_runs_from_the_tree(self):
        for path in ("trimtab/planscope.py", "bin/trimtab", "hooks/run_module.py"):
            with self.subTest(path=path):
                self.assertEqual(len(decided(review(APPROVER), paths=[path], checker_in_tree=True).problems), 1)

    def test_the_same_paths_are_not_checker_code_elsewhere(self):
        self.assertEqual(decided(review(APPROVER), paths=["bin/tool"], checker_in_tree=False).problems, ())

    def test_admin_only_paths_are_compared_without_case(self):
        self.assertEqual(len(decided(review(APPROVER), paths=["Instance.JSON"]).problems), 1)

    def test_a_long_list_of_admin_only_paths_is_cut_short(self):
        paths = [f".github/workflows/w{i}.yml" for i in range(mergeauthority.SHOWN_PATHS + 3)]

        self.assertIn("and 3 more", decided(review(APPROVER), paths=paths).problems[0])

    def test_a_path_with_a_newline_is_printed_escaped(self):
        result = decided(review(APPROVER), paths=[".github/x\n::error::forged"])

        self.assertNotIn("\n", result.problems[0])

    def test_a_malformed_plan_branch_needs_the_admin(self):
        result = decided(review(APPROVER), head_ref="plan/0019-y")

        self.assertIn("not plan/<plan>/item-NN-partK", result.problems[0])

    def test_a_fork_needs_the_admin(self):
        self.assertIn(FORK, decided(review(APPROVER), head_repo=FORK).problems[0])

    def test_a_pr_targeting_another_branch_needs_the_admin(self):
        self.assertIn("not the default branch", decided(review(APPROVER), base_ref="release").problems[0])

    def test_with_no_approver_configured_only_the_admin_counts(self):
        result = decided(review(APPROVER), who=Accounts(ADMIN, "", AGENT))

        self.assertIn(f"from {ADMIN} on", result.problems[0])


class Reviews(unittest.TestCase):
    def test_a_stale_approval_does_not_count(self):
        result = decided(review(APPROVER, commit=EARLIER))

        self.assertIn("approved an earlier commit", result.problems[0])

    def test_a_dismissed_review_does_not_count(self):
        result = decided(review(APPROVER, state="DISMISSED"))

        self.assertEqual(len(result.problems), 1)

    def test_a_dismissal_hides_an_earlier_approval(self):
        result = decided(review(APPROVER, id=1), review(APPROVER, state="DISMISSED", id=2))

        self.assertEqual(len(result.problems), 1)

    def test_a_later_comment_leaves_an_approval_standing(self):
        result = decided(review(APPROVER, id=1), review(APPROVER, state="COMMENTED", commit=EARLIER, id=2))

        self.assertEqual(result.problems, ())

    def test_reviews_are_ordered_by_id_not_by_list_position(self):
        result = decided(review(APPROVER, state="DISMISSED", id=2), review(APPROVER, id=1))

        self.assertEqual(len(result.problems), 1)

    def test_a_later_request_for_changes_voids_an_approval(self):
        result = decided(review(APPROVER, id=1), review(APPROVER, state="CHANGES_REQUESTED", id=2))

        self.assertTrue(any("requested changes" in p for p in result.problems))

    def test_the_admins_request_for_changes_fails_an_approver_approval(self):
        result = decided(review(APPROVER, id=2), review(ADMIN, state="CHANGES_REQUESTED", id=1))

        self.assertIn(f"{ADMIN} requested changes", result.problems[0])

    def test_logins_are_compared_without_case(self):
        self.assertEqual(decided(review(APPROVER.upper())).problems, ())

    def test_a_pending_review_does_not_count(self):
        self.assertEqual(len(decided(review(APPROVER, state="PENDING")).problems), 1)


class NoFacts(unittest.TestCase):
    def test_facts_never_read_close_the_route_rather_than_open_it(self):
        ordered = [replace(review(APPROVER), id=1)]

        result = mergeauthority.decide(pr(), WHO, SOURCE, False, ordered, PASSED, None)

        self.assertIn("facts were not read", result.problems[0])


class AgreesWithTheSkill(unittest.TestCase):
    """The wrap-merged skill's approver file test reads the same lists after the merge; they must not drift."""

    def test_the_skills_approver_file_test_names_every_admin_only_pin_and_checker_path(self):
        skill = (ROOT / "skills" / "wrap-merged" / "SKILL.md").read_text(encoding="utf-8")
        start = skill.index("the PR changes neither `.github/`")
        section = skill[start:skill.index("the `plan-scope` job runs its checker", start)]
        paths = (*mergeauthority.PIN_FILES, *mergeauthority.CHECKER_DIRS, *mergeauthority.CHECKER_FILES, ".github/")

        self.assertEqual([p for p in paths if f"`{p}`" not in section], [])


class Configuration(unittest.TestCase):
    def test_no_admin_login_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            mergeauthority.accounts("  ", APPROVER)

    def test_the_same_login_for_both_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            mergeauthority.accounts(ADMIN, ADMIN.upper())

    def test_no_approver_means_none(self):
        self.assertEqual(mergeauthority.accounts(ADMIN, None), Accounts(ADMIN, ""))

    def test_the_approver_and_agent_being_one_login_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            mergeauthority.accounts(ADMIN, APPROVER, APPROVER.upper())

    def test_the_admin_and_agent_being_one_login_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            mergeauthority.accounts(ADMIN, APPROVER, ADMIN.upper())


def review_row(id, login, state="APPROVED", commit=HEAD):
    return {"id": id, "user": {"login": login}, "state": state, "commit_id": commit}


def run_row(id, conclusion="success", status="completed", app="github-actions"):
    return {"id": id, "name": mergeauthority.CHECK_NAME, "app": {"slug": app}, "status": status,
            "conclusion": conclusion, "started_at": f"2026-10-25T10:00:{id % 60:02d}Z"}


REVIEWS_PATH = f"repos/{REPOSITORY}/pulls/{NUMBER}/reviews"
RUNS_PATH = f"repos/{REPOSITORY}/commits/{HEAD}/check-runs"
PULL_PATH = f"repos/{REPOSITORY}/pulls/{NUMBER}"
ACTIVITY_PATH = f"repos/{REPOSITORY}/activity"
RULES_PATH = f"repos/{REPOSITORY}/rules/branches/{DEFAULT}"


def activity_row(id, kind, before, after, actor=AGENT, ref=PLAN_REF):
    return {"id": id, "activity_type": kind, "before": before, "after": after,
            "actor": {"login": actor} if actor else None, "ref": f"refs/heads/{ref}"}


def check_row(id, name, app_id, conclusion="success", status="completed"):
    slug = "github-actions" if app_id == ACTIONS_ID else OTHER_APP
    return {"id": id, "name": name, "app": {"id": app_id, "slug": slug}, "status": status,
            "conclusion": conclusion, "started_at": "2026-10-25T10:00:00Z"}


def rule_rows(checks=None, settings=SETTINGS, approvals=1, strict=True):
    pull_request = {name: True for name in settings} | {"required_approving_review_count": approvals}
    required = [{"context": c, **({"integration_id": a} if a else {})} for c, a in (checks or REQUIRED).items()]
    return [{"type": "pull_request", "parameters": pull_request},
            {"type": "required_status_checks",
             "parameters": {"required_status_checks": required, "strict_required_status_checks_policy": strict}}]


def route_answers(head):
    """The server's answers on a head only the agent pushed, with every required check green and no status."""
    return {f"repos/{REPOSITORY}/pulls/{NUMBER}": json.dumps({"user": {"login": AGENT}}),
            ACTIVITY_PATH: json.dumps([activity_row(2, "push", EARLIER, head),
                                       activity_row(1, "branch_creation", NO_COMMIT, EARLIER)]),
            RULES_PATH: json.dumps(rule_rows()),
            f"repos/{REPOSITORY}/commits/{head}/check-runs": json.dumps(
                {"check_runs": [check_row(i, c, a) for i, (c, a) in enumerate(REQUIRED.items(), 1)]}),
            f"repos/{REPOSITORY}/commits/{head}/statuses": "[]"}


class Rest(unittest.TestCase):
    def test_reviews_are_read_from_every_page(self):
        first = [review_row(i, AGENT, "COMMENTED") for i in range(1, 101)]
        gh = FakeGh({REVIEWS_PATH: [json.dumps(first), json.dumps([review_row(101, APPROVER)])]})

        reviews = mergeauthority.read_reviews(GhApi(gh), REPOSITORY, NUMBER)

        self.assertEqual((len(reviews), reviews[-1].login), (101, APPROVER))

    def test_a_review_by_a_deleted_account_is_read_as_no_login(self):
        gh = FakeGh({REVIEWS_PATH: json.dumps([{"id": 1, "user": None, "state": "APPROVED", "commit_id": HEAD}])})

        self.assertEqual(mergeauthority.read_reviews(GhApi(gh), REPOSITORY, NUMBER)[0].login, "")

    def test_a_malformed_review_is_an_error(self):
        gh = FakeGh({REVIEWS_PATH: json.dumps([{"id": "1", "user": {"login": APPROVER}, "state": "APPROVED"}])})

        with self.assertRaises(MergeAuthorityError):
            mergeauthority.read_reviews(GhApi(gh), REPOSITORY, NUMBER)

    def test_check_runs_are_read_from_every_page_with_every_run(self):
        first = {"total_count": 101, "check_runs": [run_row(i) for i in range(1, 101)]}
        second = {"total_count": 101, "check_runs": [run_row(101, "failure")]}
        gh = FakeGh({RUNS_PATH: [json.dumps(first), json.dumps(second)]})

        runs = mergeauthority.read_check_runs(GhApi(gh), REPOSITORY, HEAD)

        self.assertEqual((len(runs), runs[-1].conclusion), (101, "failure"))
        self.assertEqual(query_of(gh.argvs()[0])["filter"], "all")

    def test_too_many_check_runs_is_an_error(self):
        full = json.dumps({"check_runs": [run_row(i) for i in range(100)]})
        gh = FakeGh(default=full)

        with self.assertRaises(MergeAuthorityError):
            mergeauthority.read_check_runs(GhApi(gh), REPOSITORY, HEAD)

    def test_a_check_runs_answer_without_its_list_is_an_error(self):
        gh = FakeGh({RUNS_PATH: json.dumps([run_row(1)])})

        with self.assertRaises(MergeAuthorityError):
            mergeauthority.read_check_runs(GhApi(gh), REPOSITORY, HEAD)

    def test_a_failed_gh_call_is_an_error(self):
        gh = FakeGh(returncode=1, stderr="HTTP 500")

        with self.assertRaises(MergeAuthorityError):
            mergeauthority.read_reviews(GhApi(gh), REPOSITORY, NUMBER)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class GitRepository(unittest.TestCase):
    """Real git for the changed paths, on a plan branch cut from the default branch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.env = isolated_git_env({k: v for k, v in os.environ.items() if k != "TRIMTAB_INSTANCE"}, root)
        self.env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        subprocess.run(["git", "init", "-q", "-b", DEFAULT, str(self.repo)], check=True)
        self.write("src/app.py", "x = 1\n")
        self.base = self.commit("base")
        git(self.repo, "switch", "-q", "-c", PLAN_REF)

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")


class ApproverRoute(GitRepository):
    """Each guard through check(), from the server's JSON: every test starts on a passing PR and breaks one fact."""

    def setUp(self):
        super().setUp()
        self.write("src/app.py", "x = 2\n")
        self.head = self.commit("item")
        self.answers = {**route_answers(self.head),
                        REVIEWS_PATH: json.dumps([review_row(1, APPROVER, commit=self.head)])}

    def status_path(self):
        return f"repos/{REPOSITORY}/commits/{self.head}/statuses"

    def runs_path(self):
        return f"repos/{REPOSITORY}/commits/{self.head}/check-runs"

    def set_activity(self, *rows):
        self.answers[ACTIVITY_PATH] = json.dumps(list(rows))

    def set_runs(self, *rows):
        self.answers[self.runs_path()] = json.dumps({"check_runs": list(rows)})

    def green_runs_except(self, *names):
        return [check_row(i, c, a) for i, (c, a) in enumerate(REQUIRED.items(), 1) if c not in names]

    def judged(self, who=WHO, gh=None):
        the_pr = Pr(repository=REPOSITORY, head_repo=REPOSITORY, head_ref=PLAN_REF, head=self.head, base_ref=DEFAULT,
                    default_branch=DEFAULT)
        return mergeauthority.check(str(self.repo), the_pr, NUMBER, self.base, who, run=gh or FakeGh(self.answers))

    def assert_closed(self, says):
        result = self.judged()

        self.assertEqual(result.approved_by, "")
        self.assertTrue(any(says in p for p in result.problems), result.problems)

    def test_every_guard_satisfied_passes_the_approver(self):
        self.assertEqual(self.judged(), mergeauthority.Result(APPROVER, ()))

    # R1a-c: who pushed
    def test_a_head_the_approver_pushed_needs_the_admin(self):
        self.set_activity(activity_row(2, "push", EARLIER, self.head, actor=APPROVER),
                          activity_row(1, "branch_creation", NO_COMMIT, EARLIER))

        self.assert_closed(f"{APPROVER} pushed")

    def test_a_head_the_admin_pushed_needs_the_admins_approval(self):
        self.set_activity(activity_row(2, "push", EARLIER, self.head, actor=ADMIN),
                          activity_row(1, "branch_creation", NO_COMMIT, EARLIER))

        self.assert_closed(f"{ADMIN} pushed")

    def test_a_push_by_any_other_identity_closes_the_approver_route(self):
        self.set_activity(activity_row(2, "push", EARLIER, self.head),
                          activity_row(1, "branch_creation", NO_COMMIT, EARLIER, actor=OTHER_APP))

        self.assert_closed(f"{OTHER_APP} pushed")

    def test_a_push_with_no_actor_closes_the_approver_route(self):
        self.set_activity(activity_row(2, "push", EARLIER, self.head, actor=None),
                          activity_row(1, "branch_creation", NO_COMMIT, EARLIER))

        self.assert_closed("an unknown account pushed")

    # R1d, R1e: how the ref moved
    def test_a_force_push_on_the_ref_closes_the_approver_route(self):
        self.set_activity(activity_row(2, "force_push", EARLIER, self.head),
                          activity_row(1, "branch_creation", NO_COMMIT, EARLIER))

        self.assert_closed("force_push")

    def test_a_deleted_and_recreated_branch_closes_the_route(self):
        self.set_activity(activity_row(3, "branch_creation", NO_COMMIT, self.head),
                          activity_row(2, "branch_deletion", EARLIER, NO_COMMIT),
                          activity_row(1, "branch_creation", NO_COMMIT, EARLIER))

        self.assert_closed("branch_deletion")

    def test_activity_that_does_not_reach_the_head_closes_the_route(self):
        self.set_activity(activity_row(1, "branch_creation", NO_COMMIT, EARLIER))

        self.assert_closed("not the head")

    def test_a_gap_in_the_push_chain_closes_the_route(self):
        self.set_activity(activity_row(2, "push", BASE, self.head),
                          activity_row(1, "branch_creation", NO_COMMIT, EARLIER))

        self.assert_closed("gap")

    def test_activity_that_stops_short_of_the_branchs_creation_closes_the_route(self):
        self.set_activity(activity_row(1, "push", EARLIER, self.head))

        self.assert_closed("creation")

    def test_no_activity_closes_the_route(self):
        self.set_activity()

        self.assert_closed("no recorded activity")

    def test_rows_are_ordered_by_id_not_by_position(self):
        self.set_activity(activity_row(1, "branch_creation", NO_COMMIT, EARLIER),
                          activity_row(2, "push", EARLIER, self.head))

        self.assertEqual(self.judged().approved_by, APPROVER)

    def test_activity_for_another_ref_is_an_error(self):
        self.set_activity(activity_row(1, "branch_creation", NO_COMMIT, self.head, ref="plan/other"))

        with self.assertRaises(MergeAuthorityError):
            self.judged()

    def test_a_commit_authored_as_the_admin_earns_no_admin_route(self):
        git(self.repo, "-c", f"user.name={ADMIN}", "-c", "user.email=admin@example.invalid", "commit", "-q",
            "--allow-empty", "--author", f"{ADMIN} <admin@example.invalid>", "-m", "as the admin")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.answers.update({**route_answers(self.head),
                             REVIEWS_PATH: json.dumps([review_row(1, AGENT, commit=self.head)])})

        self.assertIn(f"no approval from {ADMIN} or {APPROVER}", self.judged().problems[0])

    # R2
    def test_a_pr_the_agent_did_not_author_needs_the_admin(self):
        self.answers[PULL_PATH] = json.dumps({"user": {"login": OTHER_APP}})

        self.assert_closed(f"opened by {OTHER_APP}")

    # R3
    def test_a_required_check_with_no_run_closes_the_route(self):
        self.set_runs(*self.green_runs_except("lint-test"))

        self.assert_closed("lint-test")

    def test_a_required_check_run_from_the_wrong_app_does_not_count(self):
        self.set_runs(*self.green_runs_except("lint-test"), check_row(50, "lint-test", OTHER_ID))

        self.assert_closed("lint-test")

    def test_a_red_latest_required_run_closes_the_route(self):
        self.set_runs(*self.green_runs_except("lint-test"), check_row(50, "lint-test", ACTIONS_ID),
                      check_row(51, "lint-test", ACTIONS_ID, "failure"))

        self.assert_closed("lint-test")

    def test_a_running_required_check_closes_the_route(self):
        self.set_runs(*self.green_runs_except("lint-test"), check_row(50, "lint-test", ACTIONS_ID, None, "in_progress"))

        self.assert_closed("lint-test")

    def test_a_skipped_required_check_closes_the_route(self):
        self.set_runs(*self.green_runs_except("claude-review"), check_row(50, "claude-review", REVIEW_ID, "skipped"))

        self.assert_closed("claude-review")

    def test_a_red_draft_run_before_a_green_ready_run_passes(self):
        self.set_runs(*self.green_runs_except("lint-test"), check_row(50, "lint-test", ACTIONS_ID, "failure"),
                      check_row(51, "lint-test", ACTIONS_ID))

        self.assertEqual(self.judged().approved_by, APPROVER)

    def test_the_checks_own_slot_is_not_required_of_itself(self):
        self.set_runs(*self.green_runs_except("merge-authority"))

        self.assertEqual(self.judged().approved_by, APPROVER)

    # R4
    def test_a_commit_status_named_for_a_required_check_closes_the_route(self):
        self.answers[self.status_path()] = json.dumps([{"context": "lint-test", "state": "success"}])

        self.assert_closed("commit status")

    def test_a_status_in_the_merge_authority_slot_closes_the_route(self):
        self.answers[self.status_path()] = json.dumps([{"context": "merge-authority", "state": "success"}])

        self.assert_closed("commit status")

    def test_a_status_under_another_name_is_ignored(self):
        self.answers[self.status_path()] = json.dumps([{"context": "something-else", "state": "failure"}])

        self.assertEqual(self.judged().approved_by, APPROVER)

    # R5
    def test_each_ruleset_containment_off_closes_the_route(self):
        for setting in SETTINGS:
            with self.subTest(setting=setting):
                self.answers[RULES_PATH] = json.dumps(rule_rows(settings=[s for s in SETTINGS if s != setting]))

                self.assert_closed(setting)

    def test_no_required_approval_closes_the_route(self):
        self.answers[RULES_PATH] = json.dumps(rule_rows(approvals=0))

        self.assert_closed("required_approving_review_count")

    def test_a_loose_strict_policy_closes_the_route(self):
        self.answers[RULES_PATH] = json.dumps(rule_rows(strict=False))

        self.assert_closed("strict")

    def test_an_unbound_required_check_closes_the_route(self):
        self.answers[RULES_PATH] = json.dumps(rule_rows(checks={**REQUIRED, "lint-test": None}))

        self.assert_closed("lint-test")

    def test_a_ruleset_not_requiring_merge_authority_closes_the_route(self):
        self.answers[RULES_PATH] = json.dumps(
            rule_rows(checks={c: a for c, a in REQUIRED.items() if c != "merge-authority"}))

        self.assert_closed("merge-authority")

    def test_plan_scope_bound_to_another_app_closes_the_route(self):
        self.answers[RULES_PATH] = json.dumps(rule_rows(checks={**REQUIRED, "plan-scope": OTHER_ID}))

        self.assert_closed("plan-scope")

    def test_no_rules_at_all_closes_the_route(self):
        self.answers[RULES_PATH] = "[]"

        self.assert_closed("require_code_owner_review")

    # R6
    def test_a_dismissed_admin_review_closes_the_approver_route(self):
        self.answers[REVIEWS_PATH] = json.dumps([review_row(1, ADMIN, "CHANGES_REQUESTED", self.head),
                                                 review_row(2, ADMIN, "DISMISSED", self.head),
                                                 review_row(3, APPROVER, commit=self.head)])

        self.assert_closed("dismissed")

    # R7
    def test_no_agent_login_closes_the_approver_route(self):
        result = self.judged(who=Accounts(ADMIN, APPROVER))

        self.assertIn("no agent login", result.problems[0])

    def test_no_agent_login_leaves_the_admins_approval_counting(self):
        self.answers[REVIEWS_PATH] = json.dumps([review_row(1, ADMIN, commit=self.head)])

        self.assertEqual(self.judged(who=Accounts(ADMIN, APPROVER)).approved_by, ADMIN)

    # R8
    def test_a_failed_activity_read_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            self.judged(gh=FakeGh(self.answers | {ACTIVITY_PATH: "not json"}))

    def test_too_many_activities_is_an_error(self):
        self.set_activity(*[activity_row(i, "push", EARLIER, self.head) for i in range(mergeauthority.ACTIVITY_LIMIT)])

        with self.assertRaises(MergeAuthorityError):
            self.judged()

    def test_each_malformed_read_is_an_error(self):
        for path in (PULL_PATH, ACTIVITY_PATH, RULES_PATH, self.runs_path(), self.status_path()):
            with self.subTest(path=path):
                with self.assertRaises(MergeAuthorityError):
                    self.judged(gh=FakeGh(self.answers | {path: json.dumps({"unexpected": 1})}))

    def test_a_rejected_activity_read_is_an_error(self):
        answering = FakeGh(self.answers)

        def forbidden(argv, stdin):
            if path_of(argv) == ACTIVITY_PATH:
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 403")
            return answering(argv, stdin)

        with self.assertRaises(MergeAuthorityError):
            self.judged(gh=forbidden)

    def test_a_failed_activity_read_leaves_the_admins_approval_counting(self):
        self.answers[REVIEWS_PATH] = json.dumps([review_row(1, ADMIN, commit=self.head),
                                                 review_row(2, APPROVER, commit=self.head)])

        result = self.judged(gh=FakeGh(self.answers | {ACTIVITY_PATH: "not json"}))

        self.assertEqual(result.approved_by, ADMIN)

    def test_a_closed_route_never_fails_the_admins_own_approval(self):
        self.set_activity(activity_row(1, "force_push", EARLIER, self.head, actor=ADMIN))
        self.answers[REVIEWS_PATH] = json.dumps([review_row(1, ADMIN, commit=self.head)])

        self.assertEqual(self.judged(), mergeauthority.Result(ADMIN, ()))


class Repository(GitRepository):
    """check() and the CLI end to end; a fake gh for the server."""

    def check(self, head, reviewer=APPROVER):
        gh = FakeGh({**route_answers(head),
                     f"repos/{REPOSITORY}/pulls/{NUMBER}/reviews": json.dumps([review_row(1, reviewer, commit=head)])})
        the_pr = Pr(repository=REPOSITORY, head_repo=REPOSITORY, head_ref=PLAN_REF, head=head, base_ref=DEFAULT,
                    default_branch=DEFAULT)
        return mergeauthority.check(str(self.repo), the_pr, NUMBER, self.base, WHO, run=gh), gh

    def test_an_approved_in_scope_plan_pr_passes(self):
        self.write("src/app.py", "x = 2\n")
        head = self.commit("item")

        result, gh = self.check(head)

        self.assertEqual(result.approved_by, APPROVER)

    def test_a_deleted_workflow_is_seen_and_needs_the_admin(self):
        git(self.repo, "switch", "-q", DEFAULT)
        self.write(".github/workflows/ci.yml", "on: push\n")
        self.base = self.commit("workflow")
        git(self.repo, "switch", "-q", "-C", PLAN_REF)
        git(self.repo, "rm", "-q", ".github/workflows/ci.yml")
        head = self.commit("item")

        result, _ = self.check(head)

        self.assertIn(".github/workflows/ci.yml", result.problems[0])

    def test_a_base_with_bin_trimtab_makes_checker_code_admin_only(self):
        git(self.repo, "switch", "-q", DEFAULT)
        self.write(mergeauthority.CHECKER_MARK, "#!/bin/sh\n")
        self.base = self.commit("checker")
        git(self.repo, "switch", "-q", "-C", PLAN_REF)
        self.write("trimtab/x.py", "y = 1\n")
        head = self.commit("item")

        result, _ = self.check(head)

        self.assertIn("trimtab/x.py", result.problems[0])

    def test_a_non_plan_pr_reads_no_check_runs(self):
        head = self.commit_change()
        gh = FakeGh({f"repos/{REPOSITORY}/pulls/{NUMBER}/reviews": json.dumps([review_row(1, ADMIN, commit=head)])})
        the_pr = Pr(repository=REPOSITORY, head_repo=REPOSITORY, head_ref=FEATURE_REF, head=head, base_ref=DEFAULT,
                    default_branch=DEFAULT)

        result = mergeauthority.check(str(self.repo), the_pr, NUMBER, self.base, WHO, run=gh)

        self.assertEqual((result.approved_by, [path_of(a) for a in gh.argvs()]),
                         (ADMIN, [f"repos/{REPOSITORY}/pulls/{NUMBER}/reviews"]))

    def test_a_non_plan_pr_needs_no_readable_diff(self):
        unknown = "d" * 40
        gh = FakeGh({f"repos/{REPOSITORY}/pulls/{NUMBER}/reviews": json.dumps([review_row(1, ADMIN, commit=unknown)])})
        the_pr = Pr(repository=REPOSITORY, head_repo=REPOSITORY, head_ref=FEATURE_REF, head=unknown,
                    base_ref=DEFAULT, default_branch=DEFAULT)

        result = mergeauthority.check(str(self.repo), the_pr, NUMBER, self.base, WHO, run=gh)

        self.assertEqual(result.approved_by, ADMIN)

    def test_an_unknown_head_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            self.check("d" * 40)

    def test_a_head_that_is_not_a_sha_is_refused_before_any_call(self):
        with self.assertRaises(MergeAuthorityError):
            self.check("HEAD")

    def commit_change(self):
        self.write("src/app.py", "x = 3\n")
        return self.commit("item")

    def fake_gh(self, head, reviewer):
        """A `gh` on PATH for the CLI, which runs the real command: the server's canned answers (TST-2)."""
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                      "print(json.loads(os.environ['FAKE_ANSWERS'])[sys.argv[4].split('?')[0]])\n",
                      encoding="utf-8")
        gh.chmod(0o755)
        return {"PATH": f"{bin_dir}{os.pathsep}{self.env['PATH']}",
                "FAKE_ANSWERS": json.dumps({**route_answers(head), REVIEWS_PATH: json.dumps(
                    [review_row(1, reviewer, commit=head)])})}

    def cli(self, *extra, reviewer=None):
        head = self.commit_change()
        env = {**self.env, **self.fake_gh(head, reviewer)} if reviewer else self.env
        return subprocess.run([str(TRIMTAB), "check-merge-authority", "--repo", str(self.repo),
                               "--repository", REPOSITORY, "--pr", str(NUMBER), "--base", self.base, "--head", head,
                               "--head-ref", PLAN_REF, "--head-repo", REPOSITORY, "--base-ref", DEFAULT,
                               "--default-branch", DEFAULT, *extra],
                              capture_output=True, text=True, env=env)

    def test_the_cli_exits_2_without_an_admin_login(self):
        done = self.cli("--admin", "")

        self.assertEqual(done.returncode, 2, done.stderr)

    def test_the_cli_exits_2_when_approver_and_admin_are_one_login(self):
        done = self.cli("--admin", ADMIN, "--approver", ADMIN)

        self.assertEqual(done.returncode, 2, done.stderr)

    def test_the_cli_exits_0_and_names_the_approver_on_a_pass(self):
        done = self.cli("--admin", ADMIN, "--approver", APPROVER, "--agent", AGENT, reviewer=APPROVER)

        self.assertEqual((done.returncode, f"approved by {APPROVER}" in done.stdout), (0, True), done.stderr)

    def test_the_cli_exits_2_when_agent_and_admin_are_one_login(self):
        done = self.cli("--admin", ADMIN, "--approver", APPROVER, "--agent", ADMIN)

        self.assertEqual(done.returncode, 2, done.stderr)

    def test_the_cli_exits_1_with_a_fail_line_when_unapproved(self):
        done = self.cli("--admin", ADMIN, "--approver", APPROVER, "--agent", AGENT, reviewer=AGENT)

        self.assertEqual((done.returncode, done.stderr.startswith("FAIL  ")), (1, True), done.stderr)

    def test_the_cli_refuses_to_run_without_an_admin_argument(self):
        self.assertEqual(self.cli().returncode, 2)


if __name__ == "__main__":
    unittest.main()
