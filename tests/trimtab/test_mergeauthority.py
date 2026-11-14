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
from instance_fixture import env_for, make_instance
from test_scrub import isolated_git_env
from trimtab import mergeauthority, planscope
from trimtab.capture import sources
from trimtab.capture.sources import PER_PAGE, GhApi
from trimtab.mergeauthority import (Accounts, Activity, CheckRun, Containment, JobRecord, MergeAuthorityError, Pr,
                                    RequiredCheck, Review, RouteFacts)

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
CI_WORKFLOW = ".github/workflows/ci.yml"
SCOPE_WORKFLOW = ".github/workflows/plan-scope.yml"
WORKFLOWS = {"lint-test": CI_WORKFLOW, "plan-scope": SCOPE_WORKFLOW, "merge-authority": SCOPE_WORKFLOW}
WORKFLOW_PAIRS = ["=".join(pair) for pair in WORKFLOWS.items()]
LOCKED = ".github/**/*"
RUN_ID = 700
RUNNER = 1000000560
# The approver route with every guard satisfied, so the tests of the rest of the decision are not about it.
OPEN_ROUTE = RouteFacts(
    author=AGENT,
    activities=(Activity(1, "branch_creation", AGENT, NO_COMMIT, HEAD),),
    containment=Containment(frozenset(SETTINGS), 1,
                            tuple(RequiredCheck(c, a) for c, a in REQUIRED.items()), frozenset({LOCKED})),
    runs=tuple(CheckRun(i, c, "", "completed", "success", "", a) for i, (c, a) in enumerate(REQUIRED.items(), 1)),
    statuses=(),
    jobs=tuple(JobRecord(i, c, "completed", "success", RUNNER, "a runner", 3, HEAD, HEAD, WORKFLOWS[c], "pull_request")
               for i, (c, a) in enumerate(REQUIRED.items(), 1) if a == ACTIONS_ID and c != "merge-authority"),
    workflows=WORKFLOWS,
    freshness=())


def decided(*reviews, head_ref=PLAN_REF, runs=PASSED, paths=SOURCE, checker_in_tree=False, who=WHO, **kwargs):
    ordered = [r if r.id else replace(r, id=i) for i, r in enumerate(reviews, start=1)]
    return mergeauthority.decide(pr(head_ref, **kwargs), who, list(paths), checker_in_tree, ordered, list(runs),
                                 lambda: OPEN_ROUTE)


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

    def test_a_nested_claude_directory_needs_the_admin(self):
        self.assertEqual(len(decided(review(APPROVER), paths=["pkg/.claude/rules/x.md"]).problems), 1)

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


def unread():
    raise AssertionError("the approver route was read")


class RouteLeftUnread(unittest.TestCase):
    """Reading the route can fail, so where the approver isn't what passes the PR, it must never be read."""

    def judged(self, *reviews, head_ref=PLAN_REF, paths=SOURCE):
        ordered = [replace(r, id=i) for i, r in enumerate(reviews, start=1)]
        return mergeauthority.decide(pr(head_ref), WHO, list(paths), False, ordered, PASSED, unread)

    def test_when_the_admin_approved(self):
        self.assertEqual(self.judged(review(ADMIN)).approved_by, ADMIN)

    def test_when_both_approved(self):
        self.assertEqual(self.judged(review(APPROVER), review(ADMIN)).approved_by, ADMIN)

    def test_on_a_non_plan_branch(self):
        result = self.judged(review(APPROVER), review(ADMIN), head_ref=FEATURE_REF)

        self.assertEqual(result.approved_by, ADMIN)

    def test_when_the_approver_alone_approved_a_non_plan_branch(self):
        self.assertIn("not a plan branch", self.judged(review(APPROVER), head_ref=FEATURE_REF).problems[0])

    def test_when_admin_only_paths_leave_the_approver_ineligible(self):
        self.assertIn("admin-only paths", self.judged(review(APPROVER), paths=[AUTHORISED_PLAN]).problems[0])

    def test_when_nobody_approved(self):
        self.assertEqual(self.judged().approved_by, "")


class NoFacts(unittest.TestCase):
    def test_facts_never_read_close_the_route_rather_than_open_it(self):
        ordered = [replace(review(APPROVER), id=1)]

        result = mergeauthority.decide(pr(), WHO, SOURCE, False, ordered, PASSED, lambda: None)

        self.assertIn("facts were not read", result.problems[0])

    def test_a_checker_in_the_tree_closes_the_route_without_reading_it(self):
        ordered = [replace(review(APPROVER), id=1)]

        result = mergeauthority.decide(pr(), WHO, SOURCE, True, ordered, PASSED, unread)

        self.assertIn("runs its checker from its own tree", result.problems[0])

    def test_a_checker_in_the_tree_leaves_the_admins_approval_counting(self):
        ordered = [replace(review(ADMIN), id=1)]

        result = mergeauthority.decide(pr(), WHO, SOURCE, True, ordered, PASSED, unread)

        self.assertEqual(result.approved_by, ADMIN)

    def test_a_passing_run_with_no_job_record_closes_the_route(self):
        ordered = [replace(review(APPROVER), id=1)]

        result = mergeauthority.decide(pr(), WHO, SOURCE, False, ordered, PASSED, lambda: replace(OPEN_ROUTE, jobs=()))

        self.assertIn("not a passing job", result.problems[0])


class AgreesWithTheSkill(unittest.TestCase):
    """The plan-run skill's approver file test reads the same lists after the merge; they must not drift."""

    def test_the_skills_approver_file_test_names_every_admin_only_pin_and_checker_path(self):
        skill = (ROOT / "skills" / "plan-run" / "references" / "record-authority.md").read_text(encoding="utf-8")
        start = skill.index("- **The merger:**")
        section = skill[start:skill.index("- **A ruleset changed after the merge**", start)]
        paths = (*mergeauthority.PIN_FILES, *mergeauthority.CHECKER_DIRS, *mergeauthority.CHECKER_FILES, ".github/")

        self.assertEqual([p for p in paths if f"`{p}`" not in section], [])

    def plan_checks(self):
        """Checks 1-3 of the skill's authorisation check, which a plan run types (ADR 0059)."""
        skill = (ROOT / "skills" / "plan-run" / "references" / "authorisation.md").read_text(encoding="utf-8")
        start = skill.index("1. **The plan and its declaration are on the default branch.**")
        return skill[start:skill.index("4. **The enforcement is live", start)]

    def test_the_skills_plan_check_runs_check_plan_authorised_as_a_command_of_its_own(self):
        section = " ".join(self.plan_checks().split())

        self.assertIn("`trimtab check-plan-authorised --repository <owner>/<name> --plan <plan> --print-plan`",
                      section)
        self.assertIn("Take the plan's items from that text, never from the checkout", section)
        self.assertIn("Both verdict lines must name the same PR and merge commit", section)
        self.assertNotIn("Read tool", section)
        self.assertIn("as a command of its own with no pipe", section)
        self.assertNotIn("trimtab instance --json", section)

    def test_the_skills_record_check_states_its_own_pr_lookup(self):
        """Check 2's lookup moved into check-plan-authorised (ADR 0059), so section 3 can't point back at it."""
        # Section 3 now spans plan-run's SKILL.md and the top of record-authority.md (ADR 0068).
        entry = (ROOT / "skills" / "plan-run" / "SKILL.md").read_text(encoding="utf-8")
        start = entry.index("### 3. Work out the state")
        state = entry[start:entry.index("### 4. Gate the next work", start)]
        record = (ROOT / "skills" / "plan-run" / "references" / "record-authority.md").read_text(encoding="utf-8")
        record = record[:record.index("What it holds, so a `FAIL` line reads clearly")]
        section = " ".join((state + "\n" + record).split())

        self.assertIn("trimtab check-record-authorised --repository <owner>/<name> --plan <plan>", section)
        self.assertIn("finds its PR", section)
        self.assertNotIn("section 2, check 2", section)
        self.assertNotIn("section 2's check 2", section)

    def test_the_skills_plan_check_types_no_git_read_of_the_plans_folder(self):
        """guard-paths' layer 1 refuses any shell command naming that folder, so a plan run could not type one."""
        section = self.plan_checks()

        self.assertEqual([verb for verb in ("git ls-tree", "git log", "git diff", "gh api") if verb in section], [])

    def test_the_skills_in_flight_test_names_where_the_agents_login_comes_from(self):
        # The in-flight test moved to its own reference (ADR 0070), and the command decides it (ADR 0081): the
        # session no longer reads the agent's login itself, so the reference says the command takes it from the
        # instance and the session reads no PR by hand.
        skill = (ROOT / "skills" / "plan-run" / "references" / "in-flight.md").read_text(encoding="utf-8")
        start = skill.index("## Resumed or not")
        section = " ".join(skill[start:skill.index("## \"Only the plan run's own in-scope pushes\"", start)].split())

        self.assertIn("the logins come only from instance configuration, never from a PR", section)
        self.assertIn("its author is the agent or the admin account", section)
        self.assertNotIn("gh api", section)

    def test_the_skills_label_check_runs_the_command_that_reads_the_logins_from_the_instance(self):
        # The command reads accounts.agent, accounts.approver and the admin itself (ADR 0080).
        skill = (ROOT / "skills" / "plan-run" / "SKILL.md").read_text(encoding="utf-8")
        start = skill.index("**A session started by the `plan:continue` label**")
        section = skill[start:skill.index("**Restarting after a stop**", start)]

        said = " ".join(section.split())
        self.assertIn("trimtab check-label-actor --repository <owner>/<name> --label plan:continue --pr <n>", said)
        self.assertIn("the logins come only from instance configuration, never from the PR", said)
        self.assertNotIn("gh api", said)


class Configuration(unittest.TestCase):
    def test_no_admin_login_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            mergeauthority.accounts("  ", APPROVER)

    def test_the_same_login_for_both_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            mergeauthority.accounts(ADMIN, ADMIN.upper())

    def test_job_workflows_are_read_from_pairs(self):
        self.assertEqual(mergeauthority.job_workflows([f"lint-test={CI_WORKFLOW}"]), {"lint-test": CI_WORKFLOW})

    def test_no_job_workflows_means_none(self):
        self.assertEqual(mergeauthority.job_workflows(None), {})

    def test_a_bad_job_workflow_is_an_error(self):
        for pair in ("lint-test", f"={CI_WORKFLOW}", "lint-test=ci.yml", "lint-test=.github/workflows/../ci.yml"):
            with self.subTest(pair=pair):
                with self.assertRaises(MergeAuthorityError):
                    mergeauthority.job_workflows([pair])

    def test_a_check_named_twice_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            mergeauthority.job_workflows([f"lint-test={CI_WORKFLOW}", f"lint-test={SCOPE_WORKFLOW}"])

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
BRANCH_PATH = f"repos/{REPOSITORY}/branches/{DEFAULT}"


def activity_row(id, kind, before, after, actor=AGENT, ref=PLAN_REF):
    return {"id": id, "activity_type": kind, "before": before, "after": after,
            "actor": {"login": actor} if actor else None, "ref": f"refs/heads/{ref}"}


def check_row(id, name, app_id, conclusion="success", status="completed"):
    slug = "github-actions" if app_id == ACTIONS_ID else OTHER_APP
    return {"id": id, "name": name, "app": {"id": app_id, "slug": slug}, "status": status,
            "conclusion": conclusion, "started_at": "2026-10-25T10:00:00Z"}


def rule_rows(checks=None, settings=SETTINGS, approvals=1, strict=True, locked=(LOCKED, "CODEOWNERS")):
    pull_request = {name: True for name in settings} | {"required_approving_review_count": approvals}
    required = [{"context": c, **({"integration_id": a} if a else {})} for c, a in (checks or REQUIRED).items()]
    return [{"type": "pull_request", "parameters": pull_request},
            {"type": "required_status_checks",
             "parameters": {"required_status_checks": required, "strict_required_status_checks_policy": strict}},
            {"type": "file_path_restriction", "parameters": {"restricted_file_paths": list(locked)}}]


def job_path(id):
    return f"repos/{REPOSITORY}/actions/jobs/{id}"


def job_row(id, name, head, conclusion="success", status="completed", runner_id=RUNNER, runner_name="a runner",
            steps=3, run_id=RUN_ID):
    return {"id": id, "name": name, "run_id": run_id, "status": status, "conclusion": conclusion, "head_sha": head,
            "runner_id": runner_id, "runner_name": runner_name, "labels": ["ubuntu-latest"],
            "steps": [{"name": f"step {i}"} for i in range(steps)]}


def run_path(run_id=RUN_ID):
    return f"repos/{REPOSITORY}/actions/runs/{run_id}"


def workflow_run_row(head, path=CI_WORKFLOW, event="pull_request", run_id=RUN_ID, status="completed"):
    return {"id": run_id, "path": path, "event": event, "head_sha": head, "status": status}


def job_answers(runs, head, workflows=WORKFLOWS):
    """A real job and workflow run behind each Actions-bound check run, one workflow run per expected workflow."""
    answers = {}
    paths = list(dict.fromkeys(workflows.values()))
    for r in runs:
        if (r["app"] or {}).get("id") == ACTIONS_ID and r["name"] in workflows:
            run_id = RUN_ID + paths.index(workflows[r["name"]])
            answers[job_path(r["id"])] = json.dumps(job_row(r["id"], r["name"], head, run_id=run_id))
            answers[run_path(run_id)] = json.dumps(workflow_run_row(head, workflows[r["name"]], run_id=run_id))
    return answers


def route_answers(head):
    """The server's answers on a head only the agent pushed, with every required check green and no status."""
    runs = [check_row(i, c, a) for i, (c, a) in enumerate(REQUIRED.items(), 1)]
    return {**job_answers(runs, head), f"repos/{REPOSITORY}/pulls/{NUMBER}": json.dumps({"user": {"login": AGENT}}),
            ACTIVITY_PATH: json.dumps([activity_row(2, "push", EARLIER, head),
                                       activity_row(1, "branch_creation", NO_COMMIT, EARLIER)]),
            RULES_PATH: json.dumps(rule_rows()),
            f"repos/{REPOSITORY}/commits/{head}/check-runs": json.dumps({"check_runs": runs}),
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
        self.answers.update(job_answers(rows, self.head))

    def green_runs_except(self, *names):
        return [check_row(i, c, a) for i, (c, a) in enumerate(REQUIRED.items(), 1) if c not in names]

    def judged(self, who=WHO, gh=None):
        the_pr = Pr(repository=REPOSITORY, head_repo=REPOSITORY, head_ref=PLAN_REF, head=self.head, base_ref=DEFAULT,
                    default_branch=DEFAULT)
        return mergeauthority.check(str(self.repo), the_pr, NUMBER, self.base, who, run=gh or FakeGh(self.answers),
                                    workflows=WORKFLOW_PAIRS)

    def assert_closed(self, says, alone=True):
        """The route closes and says why; `alone` also pins that no other guard is what closed it."""
        result = self.judged()

        self.assertEqual(result.approved_by, "")
        self.assertTrue(any(says in p for p in result.problems), result.problems)
        if alone:
            self.assertEqual(len(result.problems), 1, result.problems)

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

        self.assert_closed("branch_deletion", alone=False)

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

    def test_a_creation_row_with_a_commit_before_it_closes_the_route(self):
        self.set_activity(activity_row(1, "branch_creation", EARLIER, self.head))

        self.assert_closed("creation")

    def test_a_first_push_with_no_commit_before_it_closes_the_route(self):
        self.set_activity(activity_row(1, "push", NO_COMMIT, self.head))

        self.assert_closed("creation")

    def test_a_merge_row_on_the_ref_closes_the_route(self):
        self.set_activity(activity_row(2, "pr_merge", EARLIER, self.head),
                          activity_row(1, "branch_creation", NO_COMMIT, EARLIER))

        self.assert_closed("pr_merge")

    def test_the_agents_login_is_compared_without_case(self):
        self.set_activity(activity_row(2, "push", EARLIER, self.head, actor=AGENT.upper()),
                          activity_row(1, "branch_creation", NO_COMMIT, EARLIER))
        self.answers[PULL_PATH] = json.dumps({"user": {"login": AGENT.upper()}})

        self.assertEqual(self.judged().approved_by, APPROVER)

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

    def test_a_commit_authored_as_the_agent_earns_no_approver_route(self):
        git(self.repo, "commit", "-q", "--allow-empty", "--author", f"{AGENT} <agent@example.invalid>",
            "-m", "as the agent")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.answers.update({**route_answers(self.head),
                             REVIEWS_PATH: json.dumps([review_row(1, APPROVER, commit=self.head)])})
        self.set_activity(activity_row(2, "push", EARLIER, self.head, actor=ADMIN),
                          activity_row(1, "branch_creation", NO_COMMIT, EARLIER))

        self.assert_closed(f"{ADMIN} pushed")

    # R2
    def test_a_pr_the_agent_did_not_author_needs_the_admin(self):
        self.answers[PULL_PATH] = json.dumps({"user": {"login": OTHER_APP}})

        self.assert_closed(f"opened by {OTHER_APP}")

    def test_a_pr_by_a_deleted_account_needs_the_admin(self):
        self.answers[PULL_PATH] = json.dumps({"user": None})

        self.assert_closed("opened by an unknown account")

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

    def test_a_run_with_no_app_does_not_count(self):
        no_app = {**check_row(50, "lint-test", ACTIONS_ID), "app": None}
        self.set_runs(*self.green_runs_except("lint-test"), no_app)

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

    # The job record (ADR 0057): each case is a run the bound App's id alone would pass
    def latest_lint_job(self, **changes):
        self.answers[job_path(1)] = json.dumps(job_row(1, "lint-test", self.head, run_id=RUN_ID, **changes))

    def test_every_actions_bound_check_passes_on_its_real_job(self):
        self.assertEqual(self.judged().approved_by, APPROVER)

    def test_a_forged_run_with_no_runner_closes_the_route(self):
        self.latest_lint_job(runner_id=None, runner_name=None, steps=0)

        self.assert_closed("lint-test")

    def test_a_job_with_no_runner_id_closes_the_route(self):
        self.latest_lint_job(runner_id=None)

        self.assert_closed("lint-test")

    def test_a_job_with_no_runner_name_closes_the_route(self):
        self.latest_lint_job(runner_name=None)

        self.assert_closed("lint-test")

    def test_a_job_with_no_steps_closes_the_route(self):
        self.latest_lint_job(steps=0)

        self.assert_closed("lint-test")

    def test_a_job_from_a_forging_workflow_closes_the_route(self):
        self.answers[run_path()] = json.dumps(workflow_run_row(self.head, path=".github/workflows/e5.yml"))

        self.assert_closed("lint-test")

    def test_a_job_from_another_event_closes_the_route(self):
        self.answers[run_path()] = json.dumps(workflow_run_row(self.head, event="push"))

        self.assert_closed("lint-test")

    def test_a_job_that_did_not_pass_closes_the_route(self):
        self.latest_lint_job(conclusion="failure")

        self.assert_closed("lint-test")

    def test_a_job_not_finished_closes_the_route(self):
        self.latest_lint_job(status="in_progress")

        self.assert_closed("lint-test")

    def test_a_job_under_another_name_closes_the_route(self):
        self.answers[job_path(1)] = json.dumps(job_row(1, "not-lint-test", self.head))

        self.assert_closed("lint-test")

    def test_a_job_on_another_commit_closes_the_route(self):
        self.answers[job_path(1)] = json.dumps(job_row(1, "lint-test", EARLIER))

        self.assert_closed("lint-test")

    def test_a_workflow_run_on_another_commit_closes_the_route(self):
        self.answers[run_path()] = json.dumps(workflow_run_row(EARLIER))

        self.assert_closed("lint-test")

    def test_a_check_with_no_expected_workflow_closes_the_route(self):
        partial = [pair for pair in WORKFLOW_PAIRS if not pair.startswith("lint-test=")]
        the_pr = Pr(repository=REPOSITORY, head_repo=REPOSITORY, head_ref=PLAN_REF, head=self.head, base_ref=DEFAULT,
                    default_branch=DEFAULT)

        result = mergeauthority.check(str(self.repo), the_pr, NUMBER, self.base, WHO, run=FakeGh(self.answers),
                                      workflows=partial)

        self.assertTrue(any("lint-test" in p and "expected workflow" in p for p in result.problems), result.problems)

    def test_a_second_prs_plan_scope_run_voids_the_head_though_a_later_run_passes(self):
        for outcome in ("failure", "skipped"):
            with self.subTest(outcome=outcome):
                self.set_runs(*self.green_runs_except("plan-scope"), check_row(50, "plan-scope", ACTIONS_ID, outcome),
                              check_row(51, "plan-scope", ACTIONS_ID))

                self.assert_closed("voids the head")

    def test_a_carried_over_job_passes_with_its_new_id_and_old_times(self):
        self.set_runs(*self.green_runs_except("lint-test"), check_row(50, "lint-test", ACTIONS_ID))

        self.assertEqual(self.judged().approved_by, APPROVER)

    def test_only_the_latest_run_is_read_for_its_job(self):
        self.set_runs(*self.green_runs_except("lint-test"), check_row(50, "lint-test", ACTIONS_ID),
                      check_row(51, "lint-test", ACTIONS_ID))
        self.answers[job_path(51)] = json.dumps(job_row(51, "lint-test", self.head, runner_id=None, steps=0))

        self.assert_closed("lint-test")

    def test_a_run_that_has_not_passed_has_its_job_left_unread(self):
        self.set_runs(*self.green_runs_except("lint-test"), check_row(50, "lint-test", ACTIONS_ID, "failure"))
        gh = FakeGh(self.answers)

        self.judged(gh=gh)

        self.assertNotIn(job_path(50), [path_of(a) for a in gh.argvs()])

    def test_a_check_bound_to_another_app_needs_no_job(self):
        gh = FakeGh(self.answers)

        self.judged(gh=gh)

        self.assertEqual(sorted(path_of(a) for a in gh.argvs() if "/actions/jobs/" in path_of(a)),
                         sorted(job_path(i) for i, (c, a) in enumerate(REQUIRED.items(), 1)
                                if a == ACTIONS_ID and c != "merge-authority"))

    def test_a_workflow_run_shared_by_two_checks_is_read_once(self):
        self.answers.update({job_path(i): json.dumps(job_row(i, c, self.head)) for i, (c, a)
                             in enumerate(REQUIRED.items(), 1) if a == ACTIONS_ID})
        self.answers[run_path()] = json.dumps(workflow_run_row(self.head, CI_WORKFLOW))
        gh = FakeGh(self.answers)

        self.judged(gh=gh)

        self.assertEqual(sum(1 for a in gh.argvs() if path_of(a) == run_path()), 1)

    def test_too_many_jobs_to_read_is_an_error(self):
        many = {f"check-{i}": ACTIONS_ID for i in range(mergeauthority.JOB_LIMIT)}
        self.answers[RULES_PATH] = json.dumps(rule_rows(checks={**REQUIRED, **many}))
        self.set_runs(*self.green_runs_except(),
                      *[check_row(100 + i, name, ACTIONS_ID) for i, name in enumerate(many)])

        with self.assertRaises(MergeAuthorityError):
            self.judged()

    def test_each_malformed_job_read_is_an_error(self):
        cases = {
            "job not an object": (job_path(1), []),
            "job with no steps key": (job_path(1), {k: v for k, v in job_row(1, "lint-test", self.head).items()
                                                    if k != "steps"}),
            "steps not a list": (job_path(1), {**job_row(1, "lint-test", self.head), "steps": 3}),
            "text run id": (job_path(1), {**job_row(1, "lint-test", self.head), "run_id": "700"}),
            "text runner id": (job_path(1), {**job_row(1, "lint-test", self.head), "runner_id": "1"}),
            "run not an object": (run_path(), []),
            "run with no path": (run_path(), {"id": RUN_ID, "event": "pull_request", "head_sha": self.head}),
            "run with a number for an event": (run_path(), {**workflow_run_row(self.head), "event": 1}),
        }
        for name, (path, body) in cases.items():
            with self.subTest(name):
                with self.assertRaises(MergeAuthorityError):
                    self.judged(gh=FakeGh(self.answers | {path: json.dumps(body)}))

    def test_each_malformed_job_field_is_an_error(self):
        whole = job_row(1, "lint-test", self.head)
        run = workflow_run_row(self.head)
        cases = {
            "null steps": (job_path(1), {**whole, "steps": None}),
            "text steps": (job_path(1), {**whole, "steps": "steps"}),
            "an object for steps": (job_path(1), {**whole, "steps": {"a": 1}}),
            "null name": (job_path(1), {**whole, "name": None}),
            "null status": (job_path(1), {**whole, "status": None}),
            "null commit": (job_path(1), {**whole, "head_sha": None}),
            "a number for a conclusion": (job_path(1), {**whole, "conclusion": 1}),
            "a number for a runner name": (job_path(1), {**whole, "runner_name": 1}),
            "a bool for a runner id": (job_path(1), {**whole, "runner_id": True}),
            "a runner id of 0": (job_path(1), {**whole, "runner_id": 0}),
            "a negative runner id": (job_path(1), {**whole, "runner_id": -1}),
            "a bool for a run id": (job_path(1), {**whole, "run_id": True}),
            "null run path": (run_path(), {**run, "path": None}),
            "null run commit": (run_path(), {**run, "head_sha": None}),
            "run with no commit": (run_path(), {k: v for k, v in run.items() if k != "head_sha"}),
        }
        for name, (path, body) in cases.items():
            with self.subTest(name):
                with self.assertRaises(MergeAuthorityError):
                    self.judged(gh=FakeGh(self.answers | {path: json.dumps(body)}))

    def test_a_finished_job_with_no_conclusion_closes_the_route(self):
        self.latest_lint_job(conclusion=None)

        self.assert_closed("lint-test")

    def test_a_check_run_with_no_job_behind_it_is_an_error(self):
        answering = FakeGh(self.answers)

        def not_found(argv, stdin):
            if path_of(argv) == job_path(1):
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 404: Not Found")
            return answering(argv, stdin)

        with self.assertRaises(MergeAuthorityError):
            self.judged(gh=not_found)

    def test_a_bad_job_workflow_closes_only_the_approver(self):
        the_pr = Pr(repository=REPOSITORY, head_repo=REPOSITORY, head_ref=PLAN_REF, head=self.head, base_ref=DEFAULT,
                    default_branch=DEFAULT)

        with self.assertRaises(MergeAuthorityError):
            mergeauthority.check(str(self.repo), the_pr, NUMBER, self.base, WHO, run=FakeGh(self.answers),
                                 workflows=["lint-test=ci.yml"])

    def test_a_job_read_that_is_refused_is_an_error(self):
        answering = FakeGh(self.answers)

        def forbidden(argv, stdin):
            if "/actions/" in path_of(argv):
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 403")
            return answering(argv, stdin)

        with self.assertRaises(MergeAuthorityError):
            self.judged(gh=forbidden)

    def test_a_failed_job_read_leaves_the_admins_approval_counting(self):
        self.answers[REVIEWS_PATH] = json.dumps([review_row(1, ADMIN, commit=self.head)])

        result = self.judged(gh=FakeGh(self.answers | {job_path(1): "not json"}))

        self.assertEqual(result.approved_by, ADMIN)

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

    def test_a_loose_strict_policy_leaves_a_fresh_head_open(self):
        self.answers[RULES_PATH] = json.dumps(rule_rows(strict=False))

        self.assertEqual(self.judged(), mergeauthority.Result(APPROVER, ()))

    def test_a_stale_head_closes_the_route_through_the_merge_authority_job(self):
        git(self.repo, "switch", "-q", DEFAULT)
        self.write(planscope.ALLOWLIST, "src/**\n")
        self.base = self.commit("narrow the allowlist")
        self.answers[RULES_PATH] = json.dumps(rule_rows(strict=False))

        self.assert_closed(planscope.ALLOWLIST)

    def test_an_unbound_required_check_closes_the_route(self):
        self.answers[RULES_PATH] = json.dumps(rule_rows(checks={**REQUIRED, "lint-test": None}))

        self.assert_closed("lint-test")

    def test_a_ruleset_not_requiring_merge_authority_closes_the_route(self):
        self.answers[RULES_PATH] = json.dumps(
            rule_rows(checks={c: a for c, a in REQUIRED.items() if c != "merge-authority"}))

        self.assert_closed("merge-authority")

    def test_plan_scope_bound_to_another_app_closes_the_route(self):
        self.answers[RULES_PATH] = json.dumps(rule_rows(checks={**REQUIRED, "plan-scope": OTHER_ID}))

        self.assert_closed("plan-scope", alone=False)

    def test_no_rules_at_all_closes_the_route(self):
        self.answers[RULES_PATH] = "[]"

        self.assert_closed("require_code_owner_review", alone=False)

    def test_settings_are_gathered_across_rules(self):
        first, second = SETTINGS[:2], SETTINGS[2:]
        rows = rule_rows(settings=first) + rule_rows(settings=second, approvals=0, checks={"lint-test": ACTIONS_ID},
                                                     strict=False)
        self.answers[RULES_PATH] = json.dumps(rows)

        self.assertEqual(self.judged().approved_by, APPROVER)

    def test_a_rule_with_no_parameters_is_ignored(self):
        self.answers[RULES_PATH] = json.dumps([{"type": "deletion"}, *rule_rows()])

        self.assertEqual(self.judged().approved_by, APPROVER)

    def test_a_check_required_by_a_second_rule_is_judged(self):
        extra = "another-check"
        self.answers[RULES_PATH] = json.dumps(rule_rows() + rule_rows(checks={extra: OTHER_ID}))

        self.assert_closed(extra)

    def test_a_required_check_on_a_later_page_of_rules_is_judged(self):
        extra = "another-check"
        filler = [{"type": "deletion"}] * (PER_PAGE - 3)
        self.answers[RULES_PATH] = [json.dumps(rule_rows() + filler),
                                    json.dumps(rule_rows(checks={extra: OTHER_ID}))]

        self.assert_closed(extra)

    def test_too_many_rules_is_an_error(self):
        full_pages = [json.dumps(rule_rows() + [{"type": "deletion"}] * (PER_PAGE - 3))] * 100
        self.answers[RULES_PATH] = full_pages

        with self.assertRaises(MergeAuthorityError):
            self.judged()

    def test_a_required_check_bound_to_no_real_app_is_an_error(self):
        for bound in (0, True, "15368"):
            with self.subTest(bound=bound):
                rows = rule_rows()
                rows[1]["parameters"]["required_status_checks"][0]["integration_id"] = bound
                self.answers[RULES_PATH] = json.dumps(rows)

                with self.assertRaises(MergeAuthorityError):
                    self.judged()

    def test_no_lock_on_the_workflows_closes_the_route(self):
        self.answers[RULES_PATH] = json.dumps(rule_rows(locked=("CODEOWNERS",)))

        self.assert_closed("does not lock")

    def test_a_lock_in_a_second_rule_counts(self):
        rows = rule_rows(locked=("CODEOWNERS",)) + [{"type": "file_path_restriction",
                                                    "parameters": {"restricted_file_paths": [LOCKED]}}]
        self.answers[RULES_PATH] = json.dumps(rows)

        self.assertEqual(self.judged().approved_by, APPROVER)

    def test_a_later_rule_does_not_undo_a_lock(self):
        later = {"type": "file_path_restriction", "parameters": {"restricted_file_paths": ["CODEOWNERS"]}}
        rows = [*rule_rows(), later]
        self.answers[RULES_PATH] = json.dumps(rows)

        self.assertEqual(self.judged().approved_by, APPROVER)

    def test_a_lock_on_a_narrower_path_does_not_count(self):
        self.answers[RULES_PATH] = json.dumps(rule_rows(locked=(".github/workflows/ci.yml",)))

        self.assert_closed("does not lock")

    def test_a_malformed_lock_is_an_error(self):
        for bad in ("a-path", None, [1]):
            with self.subTest(bad=bad):
                rows = [*rule_rows(), {"type": "file_path_restriction", "parameters": {"restricted_file_paths": bad}}]
                self.answers[RULES_PATH] = json.dumps(rows)

                with self.assertRaises(MergeAuthorityError):
                    self.judged()

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

    def test_the_route_reads_every_check_under_every_name(self):
        gh = FakeGh(self.answers)

        self.judged(gh=gh)

        reads = [query_of(a) for a in gh.argvs() if path_of(a) == self.runs_path()]
        self.assertEqual(([q.get("check_name") for q in reads], reads[-1]["filter"]),
                         (["plan-scope", None], "all"))

    def test_the_route_reads_the_head_branchs_activity(self):
        gh = FakeGh(self.answers)

        self.judged(gh=gh)

        read = next(a for a in gh.argvs() if path_of(a) == ACTIVITY_PATH)
        self.assertEqual(query_of(read)["ref"], f"refs/heads/{PLAN_REF}")

    def test_a_malformed_route_check_run_read_is_an_error(self):
        good = self.answers[self.runs_path()]
        self.answers[self.runs_path()] = [good, json.dumps({"unexpected": 1})]

        with self.assertRaises(MergeAuthorityError):
            self.judged()

    def test_a_route_read_with_a_text_app_id_is_an_error(self):
        bad = {**check_row(50, "something", ACTIONS_ID), "app": {"id": "1", "slug": "x"}}
        rows = json.loads(self.answers[self.runs_path()])["check_runs"]
        self.answers[self.runs_path()] = json.dumps({"check_runs": [*rows, bad]})

        with self.assertRaises(MergeAuthorityError):
            self.judged()

    def test_a_malformed_row_is_an_error(self):
        whole = activity_row(1, "branch_creation", NO_COMMIT, self.head)
        cases = {
            "activity without a key": (ACTIVITY_PATH, [{k: v for k, v in whole.items() if k != "before"}]),
            "activity with a text id": (ACTIVITY_PATH, [{**whole, "id": "1"}]),
            "activity with a number for a commit": (ACTIVITY_PATH, [{**whole, "after": 1}]),
            "activity with a text actor": (ACTIVITY_PATH, [{**whole, "actor": AGENT}]),
            "activity with a number for a login": (ACTIVITY_PATH, [{**whole, "actor": {"login": 1}}]),
            "a row that is no object": (ACTIVITY_PATH, ["row"]),
            "status without a context": (self.status_path(), [{"state": "success"}]),
            "status with a number for a context": (self.status_path(), [{"context": 1}]),
            "author with a number for a login": (PULL_PATH, {"user": {"login": 1}}),
            "rule with no type": (RULES_PATH, [{"parameters": {}}]),
            "rule that is no object": (RULES_PATH, ["rule"]),
            "required check with no context": (RULES_PATH, [{"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"integration_id": ACTIONS_ID}]}}]),
            "required check with a number for a context": (RULES_PATH, [{
                "type": "required_status_checks", "parameters": {"required_status_checks": [{"context": 1}]}}]),
            "approvals as text": (RULES_PATH, [{"type": "pull_request", "parameters": {
                "required_approving_review_count": "1"}}]),
        }
        for name, (path, body) in cases.items():
            with self.subTest(name):
                with self.assertRaises(MergeAuthorityError):
                    self.judged(gh=FakeGh(self.answers | {path: json.dumps(body)}))

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

    def test_a_bad_app_id_leaves_the_admins_approval_counting(self):
        self.set_runs(*self.green_runs_except("plan-scope"),
                      {**check_row(50, "plan-scope", ACTIONS_ID), "app": {"id": "1", "slug": "github-actions"}})
        self.answers[REVIEWS_PATH] = json.dumps([review_row(1, ADMIN, commit=self.head)])

        self.assertEqual(self.judged().approved_by, ADMIN)

    def test_a_closed_route_never_fails_the_admins_own_approval(self):
        self.set_activity(activity_row(1, "force_push", EARLIER, self.head, actor=ADMIN))
        self.answers[REVIEWS_PATH] = json.dumps([review_row(1, ADMIN, commit=self.head)])

        self.assertEqual(self.judged(), mergeauthority.Result(ADMIN, ()))


def tip_row(sha):
    return json.dumps({"commit": {"sha": sha}})


def pull_row(head, head_ref=PLAN_REF, head_repo=REPOSITORY, base_ref=DEFAULT, state="open", draft=False, merged=False,
             labels=(), author=AGENT):
    return {"user": {"login": author}, "state": state, "draft": draft, "merged": merged,
            "labels": [{"name": name} for name in labels],
            "head": {"sha": head, "ref": head_ref, "repo": {"full_name": head_repo} if head_repo else None},
            "base": {"ref": base_ref, "repo": {"default_branch": DEFAULT}}}


INLINE_PATH = f"repos/{REPOSITORY}/pulls/{NUMBER}/comments"
CONVERSATION_PATH = f"repos/{REPOSITORY}/issues/{NUMBER}/comments"
GATES = list(REQUIRED)
TRACKER = "the-reviewer[bot]"
PERSON = "a-person"
OWN_ID = list(REQUIRED).index("merge-authority") + 1


def inline_row(id, login, reply_to=None):
    return {"id": id, "user": {"login": login}, **({"in_reply_to_id": reply_to} if reply_to else {})}


def conversation_row(login, at):
    return {"id": 1, "user": {"login": login}, "created_at": at}


def review_body_row(id, login, at, body="a review's words"):
    return {**review_row(id, login, "COMMENTED", HEAD), "body": body, "submitted_at": at}


EARLY, MIDDLE, LATE = "2026-10-25T10:00:00Z", "2026-10-25T10:30:00Z", "2026-10-25T11:00:00Z"


class ApproverAction(GitRepository):
    """check-approver-route's guards through check_action(): every test starts where the App may write, and breaks
    one fact."""

    def setUp(self):
        super().setUp()
        self.write("src/app.py", "x = 2\n")
        self.head = self.commit("item")
        self.answers = {**route_answers(self.head), PULL_PATH: json.dumps(pull_row(self.head)),
                        REVIEWS_PATH: json.dumps([review_row(1, APPROVER, commit=self.head)]),
                        INLINE_PATH: "[]", CONVERSATION_PATH: "[]", BRANCH_PATH: tip_row(self.base)}

    def set(self, path, *rows):
        self.answers[path] = json.dumps(list(rows))

    def set_pull(self, **fields):
        self.answers[PULL_PATH] = json.dumps(pull_row(self.head, **fields))

    def runs_path(self):
        return f"repos/{REPOSITORY}/commits/{self.head}/check-runs"

    def set_run(self, name, conclusion):
        rows = [check_row(i, c, a, conclusion if c == name else "success")
                for i, (c, a) in enumerate(REQUIRED.items(), 1)]
        self.answers[self.runs_path()] = json.dumps({"check_runs": rows})

    def acted(self, action=mergeauthority.MERGE, gh=None, who=WHO, gates=GATES, reviewer=TRACKER, head=None,
              workflows=WORKFLOW_PAIRS):
        return mergeauthority.check_action(str(self.repo), REPOSITORY, NUMBER, self.base, head or self.head, action,
                                           who, gates, reviewer=reviewer, run=gh or FakeGh(self.answers),
                                           workflows=workflows)

    def assert_refused(self, says, action=mergeauthority.MERGE, alone=True, **kwargs):
        result = self.acted(action, **kwargs)

        self.assertEqual(result.verdict, mergeauthority.REFUSE, result)
        self.assertTrue(any(says in p for p in result.problems), result.problems)
        if alone:
            self.assertEqual(len(result.problems), 1, result.problems)

    def assert_acts(self, action=mergeauthority.MERGE, **kwargs):
        self.assertEqual(self.acted(action, **kwargs), mergeauthority.ActionResult(mergeauthority.ACT, ()))

    def test_every_guard_satisfied_lets_each_action_go(self):
        for action in mergeauthority.ACTIONS:
            with self.subTest(action=action):
                self.assert_acts(action)

    def test_approving_needs_no_earlier_approval(self):
        self.set(REVIEWS_PATH)

        self.assert_acts(mergeauthority.APPROVE)

    def test_the_route_is_held_even_when_the_admin_also_approved(self):
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=self.head), review_row(2, ADMIN, commit=self.head))
        self.set(ACTIVITY_PATH, activity_row(1, "force_push", EARLIER, self.head))

        self.assert_refused("creation")

    def test_a_route_guard_refuses_each_action(self):
        self.set(ACTIVITY_PATH, activity_row(2, "push", EARLIER, self.head, actor=ADMIN),
                 activity_row(1, "branch_creation", NO_COMMIT, EARLIER))

        for action in mergeauthority.ACTIONS:
            with self.subTest(action=action):
                self.assert_refused(f"{ADMIN} pushed", action)

    def test_a_pr_someone_else_opened_refuses(self):
        self.set_pull(author=PERSON)

        self.assert_refused(f"opened by {PERSON}")

    def test_no_agent_login_refuses(self):
        self.assert_refused("no agent login", who=Accounts(ADMIN, APPROVER))

    def test_a_checker_in_the_tree_refuses(self):
        git(self.repo, "switch", "-q", DEFAULT)
        self.write(mergeauthority.CHECKER_MARK, "#!/bin/sh\n")
        self.base = self.commit("checker")
        git(self.repo, "switch", "-q", PLAN_REF)
        git(self.repo, "merge", "-q", "--no-edit", DEFAULT)
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.answers.update(route_answers(self.head))
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=self.head))
        self.set_pull()

        self.assert_refused("runs its checker from its own tree")

    def test_a_voided_plan_scope_refuses_though_its_latest_run_passed(self):
        rows = [check_row(i, c, a) for i, (c, a) in enumerate(REQUIRED.items(), 1)]
        voided = check_row(0, "plan-scope", ACTIONS_ID, "failure")
        self.answers[self.runs_path()] = json.dumps({"check_runs": [voided, *rows]})

        self.assert_refused("voids the head")

    # The head being acted on
    def test_a_head_that_moved_refuses_each_action(self):
        self.set_pull()

        for action in mergeauthority.ACTIONS:
            with self.subTest(action=action):
                self.assert_refused("not the PR's head", action, head=EARLIER)

    # M1: the gates on this head
    def test_each_failed_gate_refuses_the_merge(self):
        for name in REQUIRED:
            with self.subTest(gate=name):
                self.set_run(name, "failure")

                self.assert_refused(f"{name}", alone=False)

    def test_a_required_check_red_refuses_approval_too(self):
        self.set_run("lint-test", "failure")

        self.assert_refused("lint-test", mergeauthority.APPROVE)

    def test_a_gate_off_the_ruleset_refuses_each_action(self):
        scanner = "GitGuardian Security Checks"
        self.set(RULES_PATH, *rule_rows({c: a for c, a in REQUIRED.items() if c != scanner}))

        for action in mergeauthority.ACTIONS:
            with self.subTest(action=action):
                self.assert_refused(f"does not require {scanner}", action)

    def test_no_gate_named_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            self.acted(gates=[])

    # merge-authority's own slot, which its own run cannot vouch for
    def test_merge_authority_red_is_expected_before_the_approval(self):
        self.set(REVIEWS_PATH)
        self.set_run("merge-authority", "failure")

        self.assert_acts(mergeauthority.APPROVE)

    def test_merge_authority_red_refuses_resolving_and_merging(self):
        self.set_run("merge-authority", "failure")

        for action in (mergeauthority.RESOLVE, mergeauthority.MERGE):
            with self.subTest(action=action):
                self.assert_refused("merge-authority", action)

    def test_no_merge_authority_run_refuses_resolving_and_merging(self):
        rows = [check_row(i, c, a) for i, (c, a) in enumerate(REQUIRED.items(), 1) if c != "merge-authority"]
        self.answers[self.runs_path()] = json.dumps({"check_runs": rows})

        for action in (mergeauthority.RESOLVE, mergeauthority.MERGE):
            with self.subTest(action=action):
                self.assert_refused("merge-authority has no run", action)

    def test_a_forged_merge_authority_run_refuses_merging(self):
        self.answers[job_path(OWN_ID)] = json.dumps(job_row(OWN_ID, "merge-authority", self.head, runner_id=None,
                                                            runner_name=None, steps=0))

        self.assert_refused("merge-authority's latest run is not a passing job")

    def test_merge_authority_with_no_expected_workflow_refuses_merging_only(self):
        pairs = [pair for pair in WORKFLOW_PAIRS if not pair.startswith("merge-authority=")]

        self.assert_refused("merge-authority has no expected workflow", workflows=pairs)
        self.assert_acts(mergeauthority.APPROVE, workflows=pairs)

    # M2: the approver's approval on the head being merged
    def test_an_approval_on_an_earlier_head_does_not_merge(self):
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=EARLIER))

        self.assert_refused(f"no approval from {APPROVER}")

    def test_an_approval_on_an_earlier_head_does_not_resolve(self):
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=EARLIER))

        self.assert_refused(f"no approval from {APPROVER}", mergeauthority.RESOLVE)

    def test_the_admins_approval_does_not_stand_in_for_the_approvers(self):
        self.set(REVIEWS_PATH, review_row(1, ADMIN, commit=self.head))

        self.assert_refused(f"no approval from {APPROVER}")

    def test_a_dismissed_approval_does_not_merge(self):
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=self.head),
                 review_row(2, APPROVER, "DISMISSED", self.head))

        self.assert_refused(f"no approval from {APPROVER}")

    def test_a_request_for_changes_refuses_each_action(self):
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=self.head),
                 review_row(2, ADMIN, "CHANGES_REQUESTED", self.head))

        for action in mergeauthority.ACTIONS:
            with self.subTest(action=action):
                self.assert_refused(f"{ADMIN} requested changes", action)

    def test_the_approvers_own_request_for_changes_refuses_each_action(self):
        self.set(REVIEWS_PATH, review_row(1, APPROVER, "CHANGES_REQUESTED", self.head))

        for action in mergeauthority.ACTIONS:
            with self.subTest(action=action):
                self.assert_refused(f"{APPROVER} requested changes", action, alone=False)

    # M3: not an item PR the approver may touch
    def test_a_non_plan_head_is_not_merged(self):
        self.set_pull(head_ref=FEATURE_REF)

        self.assert_refused("not a plan branch")

    def test_a_malformed_plan_head_is_not_merged(self):
        self.set_pull(head_ref="plan/0019-y")

        self.assert_refused("not plan/<plan>/item-NN-partK")

    def test_a_fork_is_not_merged(self):
        self.set_pull(head_repo=FORK)

        self.assert_refused(FORK)

    def test_a_deleted_head_repository_is_not_merged(self):
        self.set_pull(head_repo=None)

        self.assert_refused("not this repository")

    def test_a_pr_to_another_branch_is_not_merged(self):
        self.set_pull(base_ref="release")

        self.assert_refused("not the default branch")

    def test_a_github_path_change_is_not_merged(self):
        self.write(".github/workflows/ci.yml", "on: push\n")
        self.head = self.commit("workflow")
        self.answers.update(route_answers(self.head))
        self.set_pull()

        self.assert_refused(".github/workflows/ci.yml")

    def test_a_github_path_change_is_not_approved(self):
        self.write(".github/workflows/ci.yml", "on: push\n")
        self.head = self.commit("workflow")
        self.answers.update(route_answers(self.head))
        self.set_pull()

        self.assert_refused(".github/workflows/ci.yml", mergeauthority.APPROVE)

    # M4: a comment with no posted response
    def test_an_unanswered_comment_refuses_the_merge(self):
        self.set(INLINE_PATH, inline_row(1, TRACKER))

        self.assert_refused("review comment")

    def test_an_answered_comment_merges(self):
        self.set(INLINE_PATH, inline_row(1, TRACKER), inline_row(2, AGENT, reply_to=1))

        self.assert_acts()

    def test_a_reply_after_the_agents_refuses_the_merge(self):
        self.set(INLINE_PATH, inline_row(1, PERSON), inline_row(2, AGENT, reply_to=1),
                 inline_row(3, PERSON, reply_to=1))

        self.assert_refused("review comment")

    def test_thread_order_is_by_id_not_by_position(self):
        self.set(INLINE_PATH, inline_row(3, PERSON, reply_to=1), inline_row(1, PERSON),
                 inline_row(2, AGENT, reply_to=1))

        self.assert_refused("review comment")

    def test_a_reply_to_another_thread_answers_nothing(self):
        self.set(INLINE_PATH, inline_row(1, TRACKER), inline_row(2, TRACKER), inline_row(3, AGENT, reply_to=2))

        self.assert_refused("review comment")

    def test_the_agents_own_comment_needs_no_answer(self):
        self.set(INLINE_PATH, inline_row(1, AGENT))

        self.assert_acts()

    def test_the_agents_reply_is_recognised_without_case(self):
        self.set(INLINE_PATH, inline_row(1, TRACKER), inline_row(2, AGENT.upper(), reply_to=1))

        self.assert_acts()

    def test_an_unanswered_conversation_comment_refuses_the_merge(self):
        self.set(CONVERSATION_PATH, conversation_row(PERSON, EARLY))

        self.assert_refused(f"{PERSON}'s comment")

    def test_a_later_agent_comment_answers_the_conversation(self):
        self.set(CONVERSATION_PATH, conversation_row(PERSON, EARLY), conversation_row(AGENT, LATE))

        self.assert_acts()

    def test_an_earlier_agent_comment_answers_nothing(self):
        self.set(CONVERSATION_PATH, conversation_row(AGENT, EARLY), conversation_row(PERSON, LATE))

        self.assert_refused(f"{PERSON}'s comment")

    def test_the_agents_latest_comment_is_the_answer(self):
        self.set(CONVERSATION_PATH, conversation_row(AGENT, EARLY), conversation_row(PERSON, MIDDLE),
                 conversation_row(AGENT, LATE))

        self.assert_acts()

    def test_the_agents_comment_is_recognised_without_case(self):
        self.set(CONVERSATION_PATH, conversation_row(PERSON, EARLY), conversation_row(AGENT.upper(), LATE))

        self.assert_acts()

    def test_a_comment_in_the_same_second_is_not_an_answer(self):
        self.set(CONVERSATION_PATH, conversation_row(PERSON, EARLY), conversation_row(AGENT, EARLY))

        self.assert_refused(f"{PERSON}'s comment")

    def test_an_inline_reply_does_not_answer_the_conversation(self):
        self.set(CONVERSATION_PATH, conversation_row(PERSON, EARLY))
        self.set(INLINE_PATH, {**inline_row(1, AGENT), "created_at": LATE})

        self.assert_refused(f"{PERSON}'s comment")

    def test_the_trackers_comment_needs_no_answer(self):
        self.set(CONVERSATION_PATH, conversation_row(TRACKER, EARLY))

        self.assert_acts()

    def test_with_no_reviewer_named_the_trackers_comment_needs_an_answer(self):
        self.set(CONVERSATION_PATH, conversation_row(TRACKER, EARLY))

        self.assert_refused(f"{TRACKER}'s comment", reviewer=None)

    def test_the_tracker_is_recognised_without_case(self):
        self.set(CONVERSATION_PATH, conversation_row(TRACKER.upper(), EARLY))

        self.assert_acts()

    def test_the_trackers_review_body_needs_an_answer(self):
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=self.head), review_body_row(2, TRACKER, EARLY))

        self.assert_refused(f"{TRACKER}'s comment")

    def test_a_deleted_accounts_comment_needs_an_answer_whatever_the_reviewer(self):
        for reviewer in (TRACKER, None, ""):
            with self.subTest(reviewer=reviewer):
                self.set(CONVERSATION_PATH, {"id": 1, "user": None, "created_at": EARLY})

                self.assert_refused("a deleted account's comment", reviewer=reviewer)

    def test_a_deleted_accounts_review_body_needs_an_answer_whatever_the_reviewer(self):
        ghost = {**review_body_row(2, PERSON, EARLY), "user": None}
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=self.head), ghost)

        for reviewer in (TRACKER, None, ""):
            with self.subTest(reviewer=reviewer):
                self.assert_refused("a deleted account's comment", reviewer=reviewer)

    def test_a_later_agent_comment_answers_a_deleted_account(self):
        self.set(CONVERSATION_PATH, {"id": 1, "user": None, "created_at": EARLY}, conversation_row(AGENT, LATE))

        self.assert_acts(reviewer=None)

    def test_the_tracker_is_exempt_only_in_the_conversation(self):
        self.set(INLINE_PATH, inline_row(1, TRACKER))

        self.assert_refused("review comment", reviewer=TRACKER)

    def test_an_unanswered_review_body_refuses_the_merge(self):
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=self.head), review_body_row(2, PERSON, EARLY))

        self.assert_refused(f"{PERSON}'s comment")

    def test_an_answered_review_body_merges(self):
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=self.head), review_body_row(2, PERSON, EARLY))
        self.set(CONVERSATION_PATH, conversation_row(AGENT, LATE))

        self.assert_acts()

    def test_an_empty_review_body_needs_no_answer(self):
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=self.head), review_body_row(2, PERSON, EARLY, body=""))

        self.assert_acts()

    def test_an_unanswered_comment_is_left_for_the_threads_guard_before_the_merge(self):
        self.set(INLINE_PATH, inline_row(1, TRACKER))

        for action in (mergeauthority.APPROVE, mergeauthority.RESOLVE):
            with self.subTest(action=action):
                self.assert_acts(action)

    def test_comments_are_not_read_before_the_merge(self):
        gh = FakeGh(self.answers)

        self.acted(mergeauthority.RESOLVE, gh=gh)

        self.assertFalse({INLINE_PATH, CONVERSATION_PATH} & {path_of(a) for a in gh.argvs()})

    def test_comments_are_read_from_every_page(self):
        first = [inline_row(i, AGENT) for i in range(1, 101)]
        self.answers[INLINE_PATH] = [json.dumps(first), json.dumps([inline_row(101, PERSON)])]

        self.assert_refused("review comment")

    def test_too_many_comments_is_an_error(self):
        full = json.dumps([inline_row(i, AGENT) for i in range(1, 101)])
        self.answers[INLINE_PATH] = [full] * 11 + ["[]"]

        with self.assertRaises(MergeAuthorityError):
            self.acted()

    def test_a_malformed_comment_is_an_error(self):
        for path, row in ((INLINE_PATH, {"id": "1", "user": {"login": PERSON}}),
                          (INLINE_PATH, {"id": 1, "user": {"login": PERSON}, "in_reply_to_id": "1"}),
                          (INLINE_PATH, {"user": {"login": PERSON}}),
                          (CONVERSATION_PATH, {"id": 1, "user": {"login": PERSON}}),
                          (CONVERSATION_PATH, {"id": 1, "user": {"login": PERSON}, "created_at": 5})):
            with self.subTest(row=row):
                saved = self.answers[path]
                self.set(path, row)

                with self.assertRaises(MergeAuthorityError):
                    self.acted()
                self.answers[path] = saved

    def test_a_time_in_another_shape_is_an_error(self):
        for shape in ("2026-10-25T10:00:00+01:00", "2026-10-25T10:00:00.123Z", "2026-10-25", ""):
            with self.subTest(shape=shape):
                self.set(CONVERSATION_PATH, conversation_row(PERSON, shape))

                with self.assertRaises(MergeAuthorityError):
                    self.acted()

    def test_a_review_body_time_in_another_shape_is_an_error(self):
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=self.head),
                 review_body_row(2, PERSON, "2026-10-25T10:00:00+01:00"))

        with self.assertRaises(MergeAuthorityError):
            self.acted()

    def test_comments_up_to_the_limit_are_read(self):
        full = json.dumps([inline_row(i, AGENT) for i in range(1, 101)])
        self.answers[INLINE_PATH] = [full] * 10 + ["[]"]

        self.assert_acts()

    def test_too_many_conversation_comments_is_an_error(self):
        full = json.dumps([conversation_row(AGENT, EARLY) for _ in range(100)])
        self.answers[CONVERSATION_PATH] = [full] * 11 + ["[]"]

        with self.assertRaises(MergeAuthorityError):
            self.acted()

    def test_a_malformed_review_body_is_an_error(self):
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=self.head),
                 {**review_body_row(2, PERSON, EARLY), "body": 5})

        with self.assertRaises(MergeAuthorityError):
            self.acted()

    def test_a_review_body_with_no_time_is_an_error(self):
        self.set(REVIEWS_PATH, review_row(1, APPROVER, commit=self.head), review_body_row(2, PERSON, None))

        with self.assertRaises(MergeAuthorityError):
            self.acted()

    def test_a_failed_comment_read_is_an_error(self):
        self.answers[CONVERSATION_PATH] = "not json"

        with self.assertRaises(MergeAuthorityError):
            self.acted()

    # M5: labels and state
    def test_a_blocked_pr_is_not_merged(self):
        for label in mergeauthority.BLOCKING_LABELS:
            for action in mergeauthority.ACTIONS:
                with self.subTest(label=label, action=action):
                    self.set_pull(labels=[label])

                    self.assert_refused(label, action)

    def test_another_label_is_not_a_block(self):
        self.set_pull(labels=["plan:continue"])

        self.assert_acts()

    def test_a_draft_is_not_merged(self):
        for action in mergeauthority.ACTIONS:
            with self.subTest(action=action):
                self.set_pull(draft=True)

                self.assert_refused("draft", action)

    def test_a_closed_pr_is_not_merged(self):
        for action in mergeauthority.ACTIONS:
            with self.subTest(action=action):
                self.set_pull(state="closed")

                self.assert_refused("closed", action)

    # M6: nothing left to do
    def test_a_merged_pr_is_a_no_op(self):
        self.set_pull(state="closed", merged=True)

        for action in mergeauthority.ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(self.acted(action), mergeauthority.ActionResult(mergeauthority.NOOP, ()))

    def test_a_merged_pr_reads_nothing_more(self):
        self.set_pull(state="closed", merged=True)
        gh = FakeGh(self.answers)

        self.acted(gh=gh)

        self.assertEqual([path_of(a) for a in gh.argvs()], [PULL_PATH])

    def test_a_merged_pr_is_a_no_op_whatever_head_was_asked_for(self):
        self.set_pull(state="closed", merged=True)

        self.assertEqual(self.acted(head=EARLIER).verdict, mergeauthority.NOOP)

    # Reads and inputs
    def test_a_refusal_from_the_pr_itself_reads_nothing_more(self):
        self.set_pull(head_ref=FEATURE_REF)
        gh = FakeGh(self.answers)

        self.acted(gh=gh)

        self.assertEqual([path_of(a) for a in gh.argvs()], [PULL_PATH])

    def test_a_fork_reads_nothing_more(self):
        self.set_pull(head_repo=FORK)
        gh = FakeGh(self.answers)

        self.acted(gh=gh)

        self.assertEqual([path_of(a) for a in gh.argvs()], [PULL_PATH])

    def test_approving_reads_no_merge_authority_job(self):
        gh = FakeGh(self.answers)

        self.acted(mergeauthority.APPROVE, gh=gh)

        self.assertNotIn(job_path(OWN_ID), [path_of(a) for a in gh.argvs()])

    def test_the_pr_is_read_once(self):
        gh = FakeGh(self.answers)

        self.acted(gh=gh)

        self.assertEqual([path_of(a) for a in gh.argvs()].count(PULL_PATH), 1)

    def test_the_reviews_are_read_once(self):
        gh = FakeGh(self.answers)

        self.acted(gh=gh)

        self.assertEqual([path_of(a) for a in gh.argvs()].count(REVIEWS_PATH), 1)

    def test_a_failed_pr_read_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            self.acted(gh=FakeGh(returncode=1, stderr="HTTP 500"))

    def test_a_malformed_pr_is_an_error(self):
        whole = pull_row(self.head)
        for broken in ({k: v for k, v in whole.items() if k != "head"}, {k: v for k, v in whole.items() if k != "user"},
                       {**whole, "merged": "no"}, {**whole, "draft": 1},
                       {**whole, "labels": [{"name": 5}]}, {**whole, "labels": None}, {**whole, "state": None},
                       {**whole, "head": {"sha": self.head, "ref": 5, "repo": None}},
                       {**whole, "base": {"ref": DEFAULT, "repo": None}}, []):
            with self.subTest(broken=broken):
                self.answers[PULL_PATH] = json.dumps(broken)

                with self.assertRaises(MergeAuthorityError):
                    self.acted()

    def test_an_unknown_action_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            self.acted("rebase")

    def test_no_approver_login_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            self.acted(who=Accounts(ADMIN, "", AGENT))

    def test_bad_inputs_are_refused_before_any_call(self):
        gh = FakeGh(self.answers)

        with self.assertRaises(MergeAuthorityError):
            self.acted(gh=gh, head="HEAD")

        self.assertEqual(gh.calls, [])

    def fake_gh(self):
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                      "print(json.loads(os.environ['FAKE_ANSWERS'])[sys.argv[4].split('?')[0]])\n",
                      encoding="utf-8")
        gh.chmod(0o755)
        return {"PATH": f"{bin_dir}{os.pathsep}{self.env['PATH']}", "FAKE_ANSWERS": json.dumps(self.answers)}

    def cli(self, *extra, reviewer=TRACKER):
        named = ["--reviewer", reviewer] if reviewer else []
        return subprocess.run([str(TRIMTAB), "check-approver-route", "--repo", str(self.repo), "--action", "merge",
                               "--repository", REPOSITORY, "--pr", str(NUMBER), "--base", self.base,
                               "--head", self.head,
                               "--admin", ADMIN, "--approver", APPROVER, "--agent", AGENT, *named,
                               *(arg for name in GATES for arg in ("--gate", name)), *JOB_ARGS, *extra],
                              capture_output=True, text=True, env={**self.env, **self.fake_gh()})

    def test_the_cli_prints_act_and_exits_0(self):
        done = self.cli()

        self.assertEqual((done.returncode, done.stdout), (0, "verdict=act\n"), done.stderr)

    def test_the_cli_prints_noop_and_exits_0_on_a_merged_pr(self):
        self.set_pull(state="closed", merged=True)

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout), (0, "verdict=noop\n"), done.stderr)

    def test_the_cli_prints_refuse_and_exits_1_naming_why(self):
        self.set_pull(draft=True)

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout, "draft" in done.stderr), (1, "verdict=refuse\n", True),
                         done.stderr)

    def test_the_cli_exits_2_printing_no_verdict_when_two_logins_are_one(self):
        for extra in (("--admin", APPROVER), ("--agent", ADMIN), ("--admin", "")):
            with self.subTest(extra=extra):
                done = self.cli(*extra)

                self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)

    def test_the_cli_refuses_a_trackers_comment_without_a_reviewer(self):
        self.set(CONVERSATION_PATH, conversation_row(TRACKER, EARLY))

        done = self.cli(reviewer=None)

        self.assertEqual((done.returncode, done.stdout), (1, "verdict=refuse\n"), done.stderr)

    def test_the_cli_needs_the_action_named(self):
        done = subprocess.run([str(TRIMTAB), "check-approver-route", "--repo", str(self.repo)], capture_output=True,
                              text=True, env=self.env)

        self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)

    def test_the_cli_exits_2_printing_no_verdict_on_a_failed_read(self):
        del self.answers[CONVERSATION_PATH]

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)


JOB_ARGS = [arg for pair in WORKFLOW_PAIRS for arg in ("--job-workflow", pair)]


class Repository(GitRepository):
    """check() and the CLI end to end; a fake gh for the server."""

    def check(self, head, reviewer=APPROVER):
        gh = FakeGh({**route_answers(head),
                     f"repos/{REPOSITORY}/pulls/{NUMBER}/reviews": json.dumps([review_row(1, reviewer, commit=head)])})
        the_pr = Pr(repository=REPOSITORY, head_repo=REPOSITORY, head_ref=PLAN_REF, head=head, base_ref=DEFAULT,
                    default_branch=DEFAULT)
        return (mergeauthority.check(str(self.repo), the_pr, NUMBER, self.base, WHO, run=gh, workflows=WORKFLOW_PAIRS),
                gh)

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
        done = self.cli("--admin", ADMIN, "--approver", APPROVER, "--agent", AGENT, *JOB_ARGS, reviewer=APPROVER)

        self.assertEqual((done.returncode, f"approved by {APPROVER}" in done.stdout), (0, True), done.stderr)

    def test_the_cli_exits_2_when_agent_and_admin_are_one_login(self):
        done = self.cli("--admin", ADMIN, "--approver", APPROVER, "--agent", ADMIN)

        self.assertEqual(done.returncode, 2, done.stderr)

    def test_the_cli_exits_2_on_a_bad_job_workflow_when_the_approver_carries_the_pr(self):
        done = self.cli("--admin", ADMIN, "--approver", APPROVER, "--agent", AGENT,
                        "--job-workflow", "lint-test=ci.yml", reviewer=APPROVER)

        self.assertEqual(done.returncode, 2, done.stderr)

    def test_a_bad_job_workflow_leaves_the_admins_approval_counting(self):
        done = self.cli("--admin", ADMIN, "--approver", APPROVER, "--agent", AGENT,
                        "--job-workflow", "lint-test=ci.yml", reviewer=ADMIN)

        self.assertEqual((done.returncode, f"approved by {ADMIN}" in done.stdout), (0, True), done.stderr)

    def test_the_cli_exits_1_naming_a_check_with_no_expected_workflow(self):
        done = self.cli("--admin", ADMIN, "--approver", APPROVER, "--agent", AGENT, reviewer=APPROVER)

        self.assertEqual((done.returncode, "expected workflow" in done.stderr), (1, True), done.stderr)

    def test_the_cli_exits_1_naming_the_missing_agent_login(self):
        done = self.cli("--admin", ADMIN, "--approver", APPROVER, reviewer=APPROVER)

        self.assertEqual((done.returncode, "no agent login" in done.stderr), (1, True), done.stderr)

    def test_the_cli_exits_1_with_a_fail_line_when_unapproved(self):
        done = self.cli("--admin", ADMIN, "--approver", APPROVER, "--agent", AGENT, reviewer=AGENT)

        self.assertEqual((done.returncode, done.stderr.startswith("FAIL  ")), (1, True), done.stderr)

    def test_the_cli_refuses_to_run_without_an_admin_argument(self):
        self.assertEqual(self.cli().returncode, 2)


VARIABLE_PATH = f"repos/{REPOSITORY}/actions/variables/TRIMTAB_ADMIN"


def variable_row(value=ADMIN):
    return json.dumps({"name": "TRIMTAB_ADMIN", "value": value, "created_at": "2026-10-25T10:00:00Z"})


class PlanAdmin(unittest.TestCase):
    """plan_admin_problems(): the instance's admin must exist and agree with the merge gate's."""

    def test_an_instance_naming_no_admin_is_a_problem(self):
        problems = mergeauthority.plan_admin_problems(None, REPOSITORY, FakeGh({VARIABLE_PATH: variable_row()}))

        self.assertEqual(len(problems), 1)
        self.assertIn("names no admin", problems[0])

    def test_an_instance_naming_no_admin_reads_nothing(self):
        gh = FakeGh({VARIABLE_PATH: variable_row()})

        mergeauthority.plan_admin_problems(None, REPOSITORY, gh)

        self.assertEqual(gh.calls, [])

    def test_a_different_admin_is_a_problem_naming_both(self):
        problems = mergeauthority.plan_admin_problems(ADMIN, REPOSITORY, FakeGh({VARIABLE_PATH: variable_row(AGENT)}))

        self.assertEqual(len(problems), 1)
        self.assertIn(ADMIN, problems[0])
        self.assertIn(AGENT, problems[0])

    def test_the_same_admin_passes(self):
        gh = FakeGh({VARIABLE_PATH: variable_row()})

        self.assertEqual(mergeauthority.plan_admin_problems(ADMIN, REPOSITORY, gh), ())
        self.assertEqual([path_of(argv) for argv in gh.argvs()], [VARIABLE_PATH])

    def test_logins_are_compared_without_case(self):
        gh = FakeGh({VARIABLE_PATH: variable_row(ADMIN.upper())})

        self.assertEqual(mergeauthority.plan_admin_problems(ADMIN, REPOSITORY, gh), ())

    def test_space_around_the_variable_is_ignored_as_the_merge_gate_ignores_it(self):
        gh = FakeGh({VARIABLE_PATH: variable_row(f" {ADMIN}\n")})

        self.assertEqual(mergeauthority.plan_admin_problems(ADMIN, REPOSITORY, gh), ())

    def test_a_repository_with_no_variable_is_an_error(self):
        gh = FakeGh(returncode=1, stderr="gh: Not Found (HTTP 404)")

        with self.assertRaises(MergeAuthorityError):
            mergeauthority.plan_admin_problems(ADMIN, REPOSITORY, gh)

    def test_a_token_refused_the_variable_is_an_error(self):
        gh = FakeGh(returncode=1, stderr="gh: Resource not accessible by integration (HTTP 403)")

        with self.assertRaises(MergeAuthorityError):
            mergeauthority.plan_admin_problems(ADMIN, REPOSITORY, gh)

    def test_a_variable_with_no_usable_value_is_an_error(self):
        answers = {"blank": variable_row(""), "spaces": variable_row("  "), "number": variable_row(7),
                   "no value": json.dumps({"name": "TRIMTAB_ADMIN"}), "a list": "[]", "not JSON": "<html>"}
        for what, answer in answers.items():
            with self.subTest(what=what):
                with self.assertRaises(MergeAuthorityError):
                    mergeauthority.plan_admin_problems(ADMIN, REPOSITORY, FakeGh({VARIABLE_PATH: answer}))

    def test_a_malformed_repository_is_an_error_before_any_read(self):
        for repository in ("owner", "owner/repo/actions", "owner/repo?x=1", "owner/..", "own..er/repo", ""):
            with self.subTest(repository=repository):
                gh = FakeGh({VARIABLE_PATH: variable_row()})

                with self.assertRaises(MergeAuthorityError):
                    mergeauthority.plan_admin_problems(ADMIN, repository, gh)
                self.assertEqual(gh.calls, [])


class PlanAdminCli(unittest.TestCase):
    """`trimtab check-plan-admin` against a fixture instance, with only gh faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inst = make_instance(Path(self.tmp.name))
        self.answer = variable_row()

    def name_admin(self, login=ADMIN):
        path = self.inst / "instance.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**data, "accounts": {"admin": login}}), encoding="utf-8")

    def cli(self):
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text("#!/usr/bin/env python3\nimport os, sys\n"
                      f"if sys.argv[4] != {VARIABLE_PATH!r}:\n    sys.exit('gh: Not Found (HTTP 404)')\n"
                      "answer = os.environ['FAKE_ANSWER']\n"
                      "if not answer:\n    sys.exit('gh: Not Found (HTTP 404)')\n"
                      "print(answer)\n", encoding="utf-8")
        gh.chmod(0o755)
        env = {**env_for(self.inst), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_ANSWER": self.answer}
        return subprocess.run([str(TRIMTAB), "check-plan-admin", "--repository", REPOSITORY], capture_output=True,
                              text=True, env=env)

    def test_a_match_prints_the_login_alone_and_exits_0(self):
        self.name_admin()

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout), (0, f"{ADMIN}\n"), done.stderr)

    def test_an_instance_naming_no_admin_exits_1_printing_no_login(self):
        done = self.cli()

        self.assertEqual((done.returncode, done.stdout, "names no admin" in done.stderr), (1, "", True), done.stderr)

    def test_a_different_admin_exits_1_printing_no_login(self):
        self.name_admin(AGENT)

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout, "FAIL  " in done.stderr), (1, "", True), done.stderr)

    def test_a_repository_with_no_variable_exits_2_printing_no_login(self):
        self.name_admin()
        self.answer = ""

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout, "nothing is vouched for" in done.stderr), (2, "", True),
                         done.stderr)

    def test_a_broken_instance_file_exits_2_printing_no_login(self):
        (self.inst / "instance.json").write_text("{", encoding="utf-8")

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout, "nothing is vouched for" in done.stderr), (2, "", True),
                         done.stderr)

    def test_the_cli_needs_the_repository_named(self):
        self.name_admin()

        done = subprocess.run([str(TRIMTAB), "check-plan-admin"], capture_output=True, text=True,
                              env=env_for(self.inst))

        self.assertEqual((done.returncode, done.stdout, "--repository" in done.stderr), (2, "", True), done.stderr)



PLAN_SCOPE_WORKFLOW = ".github/workflows/plan-scope.yml"
OWN_JOB = "merge-authority"
REPOSITORY_PATH = f"repos/{REPOSITORY}"
ACTIONS_RUNS_PATH = f"repos/{REPOSITORY}/actions/runs"
SECOND_RUN = RUN_ID + 1


def jobs_path(run_id=RUN_ID):
    return f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs"


def plan_scope_job(id=1, **changes):
    return job_row(id, mergeauthority.CHECK_NAME, changes.pop("head", HEAD), **changes)


def plan_scope_answers(jobs=None, runs=None):
    """A head whose one workflow run holds one real, passing plan-scope job, on a ruleset binding it to Actions."""
    return {REPOSITORY_PATH: json.dumps({"default_branch": DEFAULT}),
            RULES_PATH: json.dumps(rule_rows()),
            ACTIONS_RUNS_PATH: json.dumps({"workflow_runs": runs or [workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW)]}),
            jobs_path(): json.dumps({"jobs": [plan_scope_job()] if jobs is None else jobs})}


class PlanScopeJob(unittest.TestCase):
    """plan_scope_job_result(): what a session whose token can't read check runs reads instead."""

    def problems(self, answers):
        return mergeauthority.plan_scope_job_result(REPOSITORY, HEAD, FakeGh(answers)).problems

    def assert_one_problem(self, answers, word):
        problems = self.problems(answers)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn(word, problems[0])

    def test_a_real_passing_job_on_a_bound_ruleset_has_no_problem(self):
        self.assertEqual(self.problems(plan_scope_answers()), ())

    def test_a_carried_over_job_beside_its_passing_first_attempt_has_no_problem(self):
        self.assertEqual(self.problems(plan_scope_answers([plan_scope_job(1), plan_scope_job(2)])), ())

    def test_the_jobs_of_every_attempt_are_read(self):
        gh = FakeGh(plan_scope_answers())

        mergeauthority.plan_scope_job_result(REPOSITORY, HEAD, gh)

        queries = [query_of(argv) for argv in gh.argvs() if path_of(argv) == jobs_path()]
        self.assertEqual([q.get("filter") for q in queries], ["all"])

    def test_the_runs_are_read_for_the_head(self):
        gh = FakeGh(plan_scope_answers())

        mergeauthority.plan_scope_job_result(REPOSITORY, HEAD, gh)

        queries = [query_of(argv) for argv in gh.argvs() if path_of(argv) == ACTIONS_RUNS_PATH]
        self.assertEqual([q.get("head_sha") for q in queries], [HEAD])

    def test_no_check_run_is_read_since_the_session_token_gets_a_403(self):
        gh = FakeGh(plan_scope_answers())

        mergeauthority.plan_scope_job_result(REPOSITORY, HEAD, gh)

        self.assertEqual([path_of(argv) for argv in gh.argvs() if "check-runs" in path_of(argv)], [])

    def test_no_plan_scope_job_is_a_problem(self):
        self.assert_one_problem(plan_scope_answers([job_row(1, "another-job", HEAD)]), "no plan-scope job")

    def test_a_head_with_no_workflow_runs_is_a_problem(self):
        answers = plan_scope_answers()
        answers[ACTIONS_RUNS_PATH] = json.dumps({"workflow_runs": []})

        self.assert_one_problem(answers, "no plan-scope job on the head commit")

    def test_a_job_still_running_is_a_problem(self):
        self.assert_one_problem(plan_scope_answers([plan_scope_job(status="in_progress", conclusion=None)]),
                                "has not completed")

    def test_an_unfinished_run_says_to_retry_once_it_finishes(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, status="queued")])

        self.assertIn("retry once it finishes", self.problems(answers)[0])

    def test_a_voided_head_gives_no_advice_a_merged_pr_cannot_take(self):
        problem = self.problems(plan_scope_answers([plan_scope_job(conclusion="failure")]))[0]

        self.assertNotIn("new commit", problem)

    def test_a_job_that_did_not_pass_is_a_problem(self):
        for conclusion in ("failure", "cancelled", "skipped", None):
            with self.subTest(conclusion=conclusion):
                self.assert_one_problem(plan_scope_answers([plan_scope_job(conclusion=conclusion)]), "voids the head")

    def test_an_earlier_failure_outweighs_a_later_pass(self):
        jobs = [plan_scope_job(1, conclusion="failure"), plan_scope_job(2)]

        self.assert_one_problem(plan_scope_answers(jobs), "voids the head")

    def test_a_passing_job_with_no_runner_is_a_problem(self):
        self.assert_one_problem(plan_scope_answers([plan_scope_job(runner_id=None)]), "voids the head")

    def test_a_passing_job_with_no_runner_name_is_a_problem(self):
        self.assert_one_problem(plan_scope_answers([plan_scope_job(runner_name="")]), "voids the head")

    def test_a_passing_job_with_no_steps_is_a_problem(self):
        self.assert_one_problem(plan_scope_answers([plan_scope_job(steps=0)]), "voids the head")

    def test_a_job_on_another_commit_is_a_problem(self):
        self.assert_one_problem(plan_scope_answers([plan_scope_job(head=EARLIER)]), "voids the head")

    def test_a_workflow_run_on_another_commit_is_a_problem(self):
        answers = plan_scope_answers(runs=[workflow_run_row(EARLIER, PLAN_SCOPE_WORKFLOW)])

        self.assert_one_problem(answers, "voids the head")

    def test_a_job_from_a_forging_workflow_is_a_problem(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, ".github/workflows/forged.yml")])

        self.assert_one_problem(answers, "voids the head")

    def test_a_forged_job_beside_the_real_one_is_a_problem(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW),
                                           workflow_run_row(HEAD, ".github/workflows/forged.yml", run_id=SECOND_RUN)])
        answers[jobs_path(SECOND_RUN)] = json.dumps({"jobs": [plan_scope_job(2, run_id=SECOND_RUN)]})

        self.assert_one_problem(answers, "voids the head")

    def test_a_job_from_another_event_is_a_problem(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, event="workflow_dispatch")])

        self.assert_one_problem(answers, "voids the head")

    def test_other_workflows_jobs_are_not_judged(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW),
                                           workflow_run_row(HEAD, CI_WORKFLOW, run_id=SECOND_RUN)])
        answers[jobs_path(SECOND_RUN)] = json.dumps(
            {"jobs": [job_row(2, "lint-test", HEAD, conclusion="failure", run_id=SECOND_RUN)]})

        self.assertEqual(self.problems(answers), ())

    def test_a_job_on_a_later_page_is_judged(self):
        answers = plan_scope_answers()
        full_page = [job_row(i, "another-job", HEAD) for i in range(1, PER_PAGE + 1)]
        answers[jobs_path()] = [json.dumps({"jobs": full_page}),
                                json.dumps({"jobs": [plan_scope_job(PER_PAGE + 1, conclusion="failure")]})]

        self.assert_one_problem(answers, "voids the head")

    def test_a_queued_re_run_listing_no_new_job_yet_is_a_problem(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, status="queued")])

        self.assert_one_problem(answers, "has not completed")

    def test_an_unfinished_run_beside_a_finished_one_is_a_problem(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, status="queued"),
                                           workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, run_id=SECOND_RUN)])
        answers[jobs_path(SECOND_RUN)] = json.dumps({"jobs": [plan_scope_job(2, run_id=SECOND_RUN)]})

        self.assert_one_problem(answers, "has not completed")

    def test_a_finished_run_holding_no_plan_scope_job_voids_the_head(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW),
                                           workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, run_id=SECOND_RUN)])
        answers[jobs_path(SECOND_RUN)] = json.dumps({"jobs": []})

        self.assert_one_problem(answers, "voids the head")

    def test_a_jobless_run_before_a_passing_one_voids_the_head(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, run_id=SECOND_RUN),
                                           workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW)])
        answers[jobs_path(SECOND_RUN)] = json.dumps({"jobs": []})

        self.assert_one_problem(answers, "voids the head")

    def test_a_finished_run_holding_only_other_jobs_voids_the_head(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW),
                                           workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, run_id=SECOND_RUN)])
        answers[jobs_path(SECOND_RUN)] = json.dumps({"jobs": [job_row(2, OWN_JOB, HEAD, run_id=SECOND_RUN)]})

        self.assert_one_problem(answers, "voids the head")

    def test_another_workflows_run_with_no_plan_scope_job_is_not_judged(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW),
                                           workflow_run_row(HEAD, CI_WORKFLOW, run_id=SECOND_RUN)])
        answers[jobs_path(SECOND_RUN)] = json.dumps({"jobs": []})

        self.assertEqual(self.problems(answers), ())

    def test_an_unfinished_run_alone_is_something_to_wait_for(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, status="queued")])

        self.assertTrue(mergeauthority.plan_scope_job_result(REPOSITORY, HEAD, FakeGh(answers)).wait)

    def test_an_unfinished_job_alone_is_something_to_wait_for(self):
        answers = plan_scope_answers([plan_scope_job(status="in_progress", conclusion=None)])

        self.assertTrue(mergeauthority.plan_scope_job_result(REPOSITORY, HEAD, FakeGh(answers)).wait)

    def test_an_unfinished_run_on_a_bad_ruleset_is_not_something_to_wait_for(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, status="queued")])
        answers[RULES_PATH] = "[]"

        self.assertFalse(mergeauthority.plan_scope_job_result(REPOSITORY, HEAD, FakeGh(answers)).wait)

    def test_a_refusal_is_not_something_to_wait_for(self):
        answers = plan_scope_answers([plan_scope_job(conclusion="failure")])

        self.assertFalse(mergeauthority.plan_scope_job_result(REPOSITORY, HEAD, FakeGh(answers)).wait)

    def test_a_pass_is_not_something_to_wait_for(self):
        self.assertFalse(mergeauthority.plan_scope_job_result(REPOSITORY, HEAD, FakeGh(plan_scope_answers())).wait)

    def test_another_workflows_unfinished_run_is_not_judged(self):
        answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW),
                                           workflow_run_row(HEAD, CI_WORKFLOW, run_id=SECOND_RUN, status="in_progress")])
        answers[jobs_path(SECOND_RUN)] = json.dumps({"jobs": []})

        self.assertEqual(self.problems(answers), ())

    def test_a_plan_scope_run_with_no_status_is_an_error(self):
        run = workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, status=None)

        with self.assertRaises(MergeAuthorityError):
            self.problems(plan_scope_answers(runs=[run]))

    def test_a_job_row_with_no_string_name_is_an_error(self):
        for row in ({"id": 2}, {"id": 2, "name": None}, {"id": 2, "name": 5}):
            with self.subTest(row=row):
                with self.assertRaises(MergeAuthorityError):
                    self.problems(plan_scope_answers([plan_scope_job(), row]))

    def test_a_bad_ruleset_and_a_bad_job_are_both_reported(self):
        answers = plan_scope_answers([plan_scope_job(conclusion="failure")])
        answers[RULES_PATH] = "[]"

        problems = self.problems(answers)

        self.assertEqual((len(problems), "ruleset" in problems[0], "voids the head" in problems[1]),
                         (2, True, True), problems)

    def test_exactly_the_bound_of_runs_is_read(self):
        others = [workflow_run_row(HEAD, CI_WORKFLOW, run_id=i)
                  for i in range(RUN_ID + 1, RUN_ID + mergeauthority.ACTIONS_RUN_LIMIT)]
        runs = [workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW), *others]
        pages = [json.dumps({"workflow_runs": runs[i:i + PER_PAGE]}) for i in range(0, len(runs), PER_PAGE)]
        gh = FakeGh({**plan_scope_answers(), ACTIONS_RUNS_PATH: [*pages, json.dumps({"workflow_runs": []})]},
                    default=json.dumps({"jobs": []}))

        self.assertEqual(mergeauthority.plan_scope_job_result(REPOSITORY, HEAD, gh).problems, ())

    def test_exactly_the_bound_of_jobs_is_read(self):
        jobs = [plan_scope_job(1), *[job_row(i, "another-job", HEAD)
                                     for i in range(2, mergeauthority.ACTIONS_JOB_LIMIT + 1)]]
        pages = [json.dumps({"jobs": jobs[i:i + PER_PAGE]}) for i in range(0, len(jobs), PER_PAGE)]

        self.assertEqual(self.problems({**plan_scope_answers(), jobs_path(): [*pages, json.dumps({"jobs": []})]}), ())

    def test_a_full_last_page_ends_on_the_empty_page_after_it(self):
        full_page = [plan_scope_job(i) for i in range(1, PER_PAGE + 1)]
        gh = FakeGh({**plan_scope_answers(), jobs_path(): [json.dumps({"jobs": full_page}), json.dumps({"jobs": []})]})

        self.assertEqual(mergeauthority.plan_scope_job_result(REPOSITORY, HEAD, gh).problems, ())

    def test_a_ruleset_not_requiring_plan_scope_is_a_problem(self):
        answers = plan_scope_answers()
        answers[RULES_PATH] = json.dumps(rule_rows(checks={"lint-test": ACTIONS_ID}))

        self.assert_one_problem(answers, "ruleset")

    def test_plan_scope_with_no_bound_app_is_a_problem(self):
        answers = plan_scope_answers()
        answers[RULES_PATH] = json.dumps(rule_rows(checks={"plan-scope": None}))

        self.assert_one_problem(answers, "ruleset")

    def test_plan_scope_bound_to_another_app_is_a_problem(self):
        answers = plan_scope_answers()
        answers[RULES_PATH] = json.dumps(rule_rows(checks={"plan-scope": OTHER_ID}))

        self.assert_one_problem(answers, "ruleset")

    def test_a_second_rule_leaving_plan_scope_unbound_is_a_problem(self):
        answers = plan_scope_answers()
        answers[RULES_PATH] = json.dumps(rule_rows() + rule_rows(checks={"plan-scope": None}))

        self.assert_one_problem(answers, "ruleset")

    def test_a_failed_read_is_an_error(self):
        for path in (REPOSITORY_PATH, RULES_PATH, ACTIONS_RUNS_PATH, jobs_path()):
            with self.subTest(path=path):
                answers = plan_scope_answers()
                answers[path] = "not json"

                with self.assertRaises(MergeAuthorityError):
                    self.problems(answers)

    def test_a_refused_read_is_an_error(self):
        with self.assertRaises(MergeAuthorityError):
            mergeauthority.plan_scope_job_result(REPOSITORY, HEAD, FakeGh(returncode=1, stderr="HTTP 403"))

    def test_an_unexpected_shape_is_an_error(self):
        cases = {REPOSITORY_PATH: ("{}", '{"default_branch": 5}', '{"default_branch": ""}', "[]"),
                 ACTIONS_RUNS_PATH: ("[]", "{}", '{"workflow_runs": 5}', '{"workflow_runs": [5]}',
                                     '{"workflow_runs": [{}]}', '{"workflow_runs": [{"path": "x"}]}'),
                 jobs_path(): ("[]", "{}", '{"jobs": 5}', '{"jobs": [5]}',
                               json.dumps({"jobs": [{"name": mergeauthority.CHECK_NAME}]}))}
        for path, answers in cases.items():
            for answer in answers:
                with self.subTest(path=path, answer=answer):
                    with self.assertRaises(MergeAuthorityError):
                        self.problems({**plan_scope_answers(), path: answer})

    def test_a_run_with_no_usable_id_is_an_error(self):
        for run_id in (None, "1", True, 0):
            with self.subTest(run_id=run_id):
                answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, run_id=run_id)])
                # Answers any jobs path, so only the check on the id can refuse.
                gh = FakeGh(answers, default=json.dumps({"jobs": [plan_scope_job()]}))

                with self.assertRaises(MergeAuthorityError):
                    mergeauthority.plan_scope_job_result(REPOSITORY, HEAD, gh)

    def test_a_job_with_no_usable_id_is_an_error(self):
        for job_id in (None, "1", True, 0):
            with self.subTest(job_id=job_id):
                with self.assertRaises(MergeAuthorityError):
                    self.problems(plan_scope_answers([plan_scope_job(job_id)]))

    def test_more_runs_than_the_bound_is_an_error(self):
        runs = [workflow_run_row(HEAD, CI_WORKFLOW, run_id=i)
                for i in range(1, mergeauthority.ACTIONS_RUN_LIMIT + 2)]
        pages = [json.dumps({"workflow_runs": runs[i:i + PER_PAGE]}) for i in range(0, len(runs), PER_PAGE)]

        with self.assertRaises(MergeAuthorityError):
            self.problems({**plan_scope_answers(), ACTIONS_RUNS_PATH: pages})

    def test_more_jobs_than_the_bound_is_an_error(self):
        jobs = [job_row(i, "another-job", HEAD) for i in range(1, mergeauthority.ACTIONS_JOB_LIMIT + 2)]
        pages = [json.dumps({"jobs": jobs[i:i + PER_PAGE]}) for i in range(0, len(jobs), PER_PAGE)]

        with self.assertRaises(MergeAuthorityError):
            self.problems({**plan_scope_answers(), jobs_path(): pages})

    def test_a_bad_head_is_an_error_reading_nothing(self):
        gh = FakeGh(plan_scope_answers())

        with self.assertRaises(MergeAuthorityError):
            mergeauthority.plan_scope_job_result(REPOSITORY, "main", gh)

        self.assertEqual(gh.calls, [])

    def test_a_bad_repository_is_an_error_reading_nothing(self):
        for repository in ("not-a-repo", "owner/..", "../repo"):
            with self.subTest(repository=repository):
                gh = FakeGh(plan_scope_answers())

                with self.assertRaises(MergeAuthorityError):
                    mergeauthority.plan_scope_job_result(repository, HEAD, gh)

                self.assertEqual(gh.calls, [])

    def test_the_skills_merger_test_takes_plan_scope_from_the_command(self):
        skill = (ROOT / "skills" / "plan-run" / "references" / "record-authority.md").read_text(encoding="utf-8")
        # The command that judges the whole merger runs plan-scope's job check itself (ADR 0072).
        start = skill.index("## Its PR: the code gate and the merger")
        section = skill[start:]

        self.assertIn("trimtab check-record-authorised --repository", section)
        self.assertIn("as a command of its own, with no pipe", " ".join(section.split()))
        self.assertIn("never a commit status", " ".join(section.split()))
        self.assertNotIn("check-runs", section)

    def test_the_skill_allows_one_bounded_re_run(self):
        skill = (ROOT / "skills" / "plan-run" / "references" / "record-authority.md").read_text(encoding="utf-8")
        start = skill.index("## Its PR: the code gate and the merger")
        section = " ".join(skill[start:].split())

        self.assertIn("`plan-scope`'s run on the PR's final head has not finished | Wait up to ten minutes, then "
                      "run it once more", section)
        self.assertIn("Run `git fetch origin` once, wait a minute, then run it once more", section)
        self.assertIn("One re-run in all", section)


class PlanScopeJobCli(unittest.TestCase):
    """`trimtab check-plan-scope-job`, with only gh faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.answers = plan_scope_answers()

    def cli(self, *args):
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                      "answers = json.loads(os.environ['FAKE_ANSWERS'])\n"
                      "path = sys.argv[4].split('?')[0]\n"
                      "if path not in answers:\n    sys.exit('gh: Not Found (HTTP 404)')\n"
                      "print(answers[path])\n", encoding="utf-8")
        gh.chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               "FAKE_ANSWERS": json.dumps(self.answers)}
        return subprocess.run([str(TRIMTAB), "check-plan-scope-job", *(args or ("--repository", REPOSITORY,
                                                                               "--head", HEAD))],
                              capture_output=True, text=True, env=env)

    def test_a_real_job_exits_0(self):
        done = self.cli()

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_problem_exits_1_naming_it(self):
        self.answers = plan_scope_answers([plan_scope_job(conclusion="failure")])

        done = self.cli()

        self.assertEqual((done.returncode, "FAIL  " in done.stderr), (1, True), done.stderr)

    def test_an_unfinished_run_exits_3_naming_it(self):
        self.answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, status="queued")])

        done = self.cli()

        self.assertEqual((done.returncode, "has not completed" in done.stderr), (3, True), done.stderr)

    def test_an_unfinished_run_on_a_bad_ruleset_exits_1(self):
        self.answers = plan_scope_answers(runs=[workflow_run_row(HEAD, PLAN_SCOPE_WORKFLOW, status="queued")])
        self.answers[RULES_PATH] = "[]"

        self.assertEqual(self.cli().returncode, 1)

    def test_a_refused_read_exits_2(self):
        del self.answers[ACTIONS_RUNS_PATH]

        done = self.cli()

        self.assertEqual((done.returncode, "nothing is vouched for" in done.stderr), (2, True), done.stderr)

    def test_a_bad_head_exits_2(self):
        done = self.cli("--repository", REPOSITORY, "--head", "main")

        self.assertEqual((done.returncode, "nothing is vouched for" in done.stderr), (2, True), done.stderr)

    def test_the_cli_needs_the_head_named(self):
        done = self.cli("--repository", REPOSITORY)

        self.assertEqual((done.returncode, "--head" in done.stderr), (2, True), done.stderr)

    def test_the_cli_needs_the_repository_named(self):
        done = self.cli("--head", HEAD)

        self.assertEqual((done.returncode, "--repository" in done.stderr), (2, True), done.stderr)


class FreshnessJudged(unittest.TestCase):
    """freshness_problems alone, where git can't set the case up through check_action."""

    def test_no_merge_base_refuses(self):
        problems = mergeauthority.freshness_problems(mergeauthority.MainRead(()), "0019-y", [], WORKFLOWS)

        self.assertEqual(len(problems), 1)
        self.assertIn("0 merge-bases", problems[0])

    def test_route_facts_without_freshness_close_the_route(self):
        result = mergeauthority.decide(pr(PLAN_REF), WHO, list(SOURCE), False, [review(APPROVER)], PASSED,
                                       lambda: replace(OPEN_ROUTE, freshness=None))

        self.assertEqual(result.approved_by, "")
        self.assertTrue(any("freshness" in p for p in result.problems), result.problems)


LOCK_TEXT = '{"trimtab_sha": "' + "d" * 40 + '", "id_prefix": "X", "schema_version": 2}\n'


class Freshness(ApproverAction):
    """ADR 0065 through check-approver-route: with strict off, the route holds only a head plan-scope's pass may
    still vouch for against the default branch's tip. Each test moves main from a head the App may act on."""

    PLAN = planscope.plan_of(PLAN_REF).plan

    def setUp(self):
        super().setUp()
        self.start = self.base
        self.server_tip = None  # None: the server reports the base judged as the tip
        self.answers[RULES_PATH] = json.dumps(rule_rows(strict=False))

    def acted(self, *args, **kwargs):
        self.answers[BRANCH_PATH] = tip_row(self.server_tip or self.base)
        return super().acted(*args, **kwargs)

    def rehead(self):
        """The plan branch's tip as the head, with the server's answers for it."""
        self.head = git(self.repo, "rev-parse", PLAN_REF)
        rules = self.answers[RULES_PATH]
        self.answers = {**route_answers(self.head), PULL_PATH: json.dumps(pull_row(self.head)),
                        REVIEWS_PATH: json.dumps([review_row(1, APPROVER, commit=self.head)]),
                        INLINE_PATH: "[]", CONVERSATION_PATH: "[]", RULES_PATH: rules}

    def on_main(self, *files, message="main moves"):
        """Commit on main: each (path, text) written, or removed where text is None; main's tip becomes the base."""
        git(self.repo, "switch", "-q", DEFAULT)
        for rel, text in files:
            if text is None:
                git(self.repo, "rm", "-q", rel)
            else:
                self.write(rel, text)
        self.base = self.commit(message)
        git(self.repo, "switch", "-q", PLAN_REF)
        return self.base

    def take_main(self):
        git(self.repo, "merge", "-q", "--no-edit", DEFAULT)
        self.rehead()

    def restart_main(self):
        git(self.repo, "branch", "-f", DEFAULT, self.start)

    # Passing: what strict asked for still passes, and so does what strict alone stopped.
    def test_an_up_to_date_head_acts_with_strict_on(self):
        self.answers[RULES_PATH] = json.dumps(rule_rows(strict=True))
        self.on_main(("src/other.py", "y = 1\n"))
        self.take_main()

        self.assert_acts()

    def test_main_moving_elsewhere_leaves_the_head_fresh(self):
        self.on_main(("src/other.py", "y = 1\n"), ("docs/notes.md", "n\n"))

        self.assert_acts()

    # Refusals: one per guard.
    def test_a_conflict_with_main_refuses(self):
        self.on_main(("src/app.py", "x = 3\n"))

        self.assert_refused("conflicts with the default branch")

    def test_more_than_one_merge_base_refuses(self):
        side = self.on_main(("src/main.py", "m\n"))
        git(self.repo, "switch", "-q", DEFAULT)
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", self.head)
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", PLAN_REF)
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", side)
        self.rehead()

        self.assert_refused("2 merge-bases")

    def test_main_changing_what_plan_scope_reads_refuses(self):
        plan_md, writes = planscope.plan_files(self.PLAN)
        for path in (planscope.ALLOWLIST, plan_md, writes, *mergeauthority.PIN_FILES,
                     mergeauthority.PLAN_SCOPE_WORKFLOW):
            with self.subTest(path=path):
                self.restart_main()
                self.on_main((path, LOCK_TEXT if path == ".claude/trimtab.json" else "changed\n"))

                self.assert_refused(path)

    def test_main_changing_the_configured_plan_scope_workflow_refuses(self):
        own = mergeauthority.PLAN_SCOPE_WORKFLOW.replace("plan-scope", "scope")
        workflows = {**WORKFLOWS, "plan-scope": own}
        self.on_main((own, "on: pull_request\n"))
        runs = json.loads(self.answers[self.runs_path()])["check_runs"]
        self.answers.update(job_answers(runs, self.head, workflows))

        self.assert_refused(own, workflows=["=".join(pair) for pair in workflows.items()])

    def test_main_adding_a_path_the_head_adds_refuses(self):
        record = f"{planscope.PLANS_DIR}/progress/{self.PLAN}/item-01-part1.md"
        self.write(record, "same\n")
        self.commit("record")
        self.rehead()
        self.on_main((record, "same\n"))

        self.assert_refused(record)

    def test_main_renaming_away_a_path_the_head_changes_refuses(self):
        git(self.repo, "switch", "-q", DEFAULT)
        git(self.repo, "mv", "src/app.py", "src/renamed.py")
        self.base = self.commit("rename")
        git(self.repo, "switch", "-q", PLAN_REF)

        # Some git versions follow the rename and merge cleanly; others report a conflict as well. The rename
        # refusal must stand on its own either way.
        self.assert_refused("renamed away src/app.py", alone=False)

    def test_main_deleting_a_path_the_head_changes_refuses(self):
        self.on_main(("src/app.py", None))

        self.assert_refused("renamed away src/app.py", alone=False)

    def test_a_reverted_change_on_main_still_refuses(self):
        self.on_main((planscope.ALLOWLIST, "src/**\n"))
        self.on_main((planscope.ALLOWLIST, None), message="revert")

        self.assert_refused(planscope.ALLOWLIST)

    def test_a_change_main_merged_in_refuses(self):
        git(self.repo, "switch", "-q", "-c", "side", DEFAULT)
        self.write(planscope.ALLOWLIST, "src/**\n")
        self.commit("side")
        git(self.repo, "switch", "-q", DEFAULT)
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "side")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", PLAN_REF)

        self.assert_refused(planscope.ALLOWLIST)

    def test_main_changing_an_instruction_file_or_rule_refuses(self):
        for path in ("CLAUDE.md", "src/CLAUDE.local.md", ".claude/rules/Testing.md"):
            with self.subTest(path=path):
                self.restart_main()
                self.on_main((path, "changed\n"))

                self.assert_refused(path)

    def test_the_rules_dir_comes_from_the_base_commits_lock(self):
        self.on_main((".claude/trimtab.json", LOCK_TEXT.replace("}", ', "rules_dir": "./docs/rules/"}')))
        self.take_main()
        self.on_main(("docs/rules/Own.md", "changed\n"))

        self.assert_refused("docs/rules/Own.md")

    def test_an_unreadable_lock_at_the_base_is_an_error(self):
        self.on_main((".claude/trimtab.json", "not json"))
        self.take_main()

        with self.assertRaisesRegex(MergeAuthorityError, "rules_dir"):
            self.acted()

    def test_too_many_changes_on_main_are_an_error(self):
        self.on_main(("src/a.py", "1\n"), ("src/b.py", "2\n"))

        with mock.patch.object(planscope, "MAX_MAIN_CHANGES", 1), \
                self.assertRaisesRegex(MergeAuthorityError, "more than 1 paths"):
            self.acted()

    def test_a_merge_tree_answer_with_no_tree_is_an_error(self):
        real = planscope.git_answer

        def no_tree(repo, *args, **kwargs):
            if "merge-tree" in args:
                return 1, b"CONFLICT, but no tree\n"
            return real(repo, *args, **kwargs)

        with mock.patch.object(planscope, "git_answer", no_tree), \
                self.assertRaisesRegex(MergeAuthorityError, "no tree id"):
            self.acted()

    def test_a_base_that_is_not_the_servers_tip_refuses(self):
        self.server_tip = self.on_main((planscope.ALLOWLIST, "src/**\n"))
        self.base = self.start

        self.assert_refused("not the default branch's tip")

    def test_an_unreadable_tip_is_an_error(self):
        self.server_tip = "not-a-sha"

        with self.assertRaisesRegex(MergeAuthorityError, "branches"):
            self.acted()

    def test_a_malformed_tip_answer_is_an_error(self):
        for body in ([], {}, {"commit": None}, {"commit": "a" * 40}, {"commit": {"sha": 7}}, {"commit": {}}):
            with self.subTest(body=body):
                gh = FakeGh({BRANCH_PATH: json.dumps(body)})

                with self.assertRaisesRegex(MergeAuthorityError, "branches"):
                    mergeauthority.read_tip(GhApi(gh), REPOSITORY, DEFAULT)

    def test_an_unparseable_log_of_main_is_an_error(self):
        real = planscope._git
        for out in (b"M\0src/a.py\0D\0", b"R100\0src/a.py\0", b"X\0src/a.py\0"):
            with self.subTest(out=out):
                def log(repo, *args, out=out, **kwargs):
                    return out if args[0] == "log" else real(repo, *args, **kwargs)

                with mock.patch.object(planscope, "_git", log), \
                        self.assertRaisesRegex(planscope.PlanScopeError, "could not be parsed"):
                    planscope.main_changes(str(self.repo), self.start, self.base)

    def test_one_walk_of_main_gives_every_path_and_the_deleted_ones(self):
        self.on_main(("src/new.py", "n\n"), ("src/app.py", None))

        self.assertEqual(planscope.main_changes(str(self.repo), self.start, self.base),
                         (frozenset({"src/new.py", "src/app.py"}), frozenset({"src/app.py"})))

    def test_a_branch_name_with_a_slash_is_read_as_one_name(self):
        gh = FakeGh({f"repos/{REPOSITORY}/branches/release%2F2": tip_row(self.base)})

        self.assertEqual(mergeauthority.read_tip(GhApi(gh), REPOSITORY, "release/2"), self.base)

    def test_a_case_variant_of_a_path_the_head_adds_refuses(self):
        self.write("src/New.py", "n\n")
        self.commit("add")
        self.rehead()
        self.on_main(("src/new.py", "n\n"))

        self.assert_refused("src/new.py")

    def test_main_deleting_a_case_variant_of_a_path_the_head_changes_refuses(self):
        judged = mergeauthority.freshness_problems(
            mergeauthority.MainRead((self.start,), clean=True, touched=frozenset({"src/app.py"}),
                                    deleted=frozenset({"src/app.py"})),
            self.PLAN, [planscope.Change("M", "Src/App.py", "100644")], WORKFLOWS)

        self.assertEqual(len(judged), 1)
        self.assertIn("renamed away src/app.py", judged[0])

    def test_a_rules_dir_that_names_no_directory_in_the_tree_is_an_error(self):
        for rules in (".", "./", "/", "..", "../rules", "/abs/rules", "a/../.."):
            with self.subTest(rules_dir=rules):
                with self.assertRaisesRegex(MergeAuthorityError, "rules_dir"):
                    mergeauthority._rules_dir(LOCK_TEXT.replace("}", f', "rules_dir": "{rules}"}}'))

    def test_a_rules_dir_is_read_as_git_names_it(self):
        lock = LOCK_TEXT.replace("}", ', "rules_dir": " ./docs/./rules/ "}')

        self.assertEqual(mergeauthority._rules_dir(lock), "docs/rules")

    def test_a_sibling_of_the_rules_dir_is_not_held(self):
        self.assertFalse(mergeauthority.judged_by("docs/rules-extra/x.md", "docs/rules"))

    def test_a_lock_that_is_not_a_regular_file_is_an_error(self):
        git(self.repo, "switch", "-q", DEFAULT)
        (self.repo / ".claude").mkdir(exist_ok=True)
        os.symlink("elsewhere.json", self.repo / ".claude" / "trimtab.json")
        self.base = self.commit("link the lock")
        git(self.repo, "switch", "-q", PLAN_REF)
        self.take_main()

        with self.assertRaisesRegex(MergeAuthorityError, "not a regular file"):
            self.acted()

    def test_exactly_the_cap_of_changes_on_main_is_judged(self):
        self.on_main(("src/a.py", "1\n"), ("src/b.py", "2\n"))

        with mock.patch.object(planscope, "MAX_MAIN_CHANGES", 2):
            self.assert_acts()

    def test_unrelated_histories_have_no_merge_base(self):
        git(self.repo, "switch", "-q", "--orphan", "elsewhere")
        self.write("other.txt", "o\n")
        unrelated = self.commit("unrelated")
        git(self.repo, "switch", "-q", PLAN_REF)

        self.assertEqual(planscope.merge_bases(str(self.repo), unrelated, self.head), ())

    def test_an_unreadable_merge_base_answer_is_an_error(self):
        real = planscope.git_answer
        for answer in ((0, b""), (1, b"a" * 40 + b"\n"), (0, b"not-a-sha\n")):
            with self.subTest(answer=answer):
                def stub(repo, *args, answer=answer, **kwargs):
                    return answer if "merge-base" in args else real(repo, *args, **kwargs)

                with mock.patch.object(planscope, "git_answer", stub), \
                        self.assertRaisesRegex(planscope.PlanScopeError, "merge-base"):
                    planscope.merge_bases(str(self.repo), self.base, self.head)

    def test_an_object_directory_git_cannot_name_as_an_alternate_is_an_error(self):
        real = planscope._git

        def colon(repo, *args, **kwargs):
            return b"/srv/ws:1/repo/.git/objects\n" if "--git-path" in args else real(repo, *args, **kwargs)

        with mock.patch.object(planscope, "_git", colon), \
                self.assertRaisesRegex(planscope.PlanScopeError, "alternate"):
            planscope.merges_cleanly(str(self.repo), self.base, self.head)

    def test_a_failed_scratch_directory_is_an_error(self):
        with mock.patch.object(planscope.tempfile, "TemporaryDirectory", side_effect=OSError("full")), \
                self.assertRaisesRegex(planscope.PlanScopeError, "scratch"):
            planscope.merges_cleanly(str(self.repo), self.base, self.head)

    def test_git_returns_an_exit_code_the_caller_allows(self):
        answer = sources.git_answer(self.repo, "merge-base", "--is-ancestor", self.head, self.start, answers=(0, 1))

        self.assertEqual(answer, (1, b""))

    def test_git_raises_on_an_exit_code_the_caller_does_not_allow(self):
        with self.assertRaisesRegex(sources.SourceError, "merge-base exited 1"):
            sources.git_answer(self.repo, "merge-base", "--is-ancestor", self.head, self.start)

    def test_git_scrubs_an_id_from_the_stderr_it_reports(self):
        run_id = "session" + "_" + "A1b2C3d4E5f6G7h8J9k0"

        with self.assertRaises(sources.SourceError) as caught:
            sources.git_answer(self.repo, "show", run_id, answers=(0, 1))

        self.assertIn("exited 128", str(caught.exception))
        self.assertNotIn(run_id, str(caught.exception))

    def test_git_that_cannot_run_or_times_out_raises(self):
        for failure in (FileNotFoundError("git"), subprocess.TimeoutExpired("git", sources.GIT_TIMEOUT_SECONDS)):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(sources.subprocess, "run", side_effect=failure), \
                        self.assertRaisesRegex(sources.SourceError, type(failure).__name__):
                    sources.git_answer(self.repo, "merge-base", "--all", self.base, self.head, answers=(0, 1))

    def test_a_long_list_of_stale_paths_is_cut_short(self):
        paths = [f"src/new{i}.py" for i in range(mergeauthority.SHOWN_PATHS + 2)]
        judged = mergeauthority.freshness_problems(
            mergeauthority.MainRead((self.start,), clean=True, touched=frozenset(paths)),
            self.PLAN, [planscope.Change("A", p, "100644") for p in paths], WORKFLOWS)

        self.assertEqual(len(judged), 1)
        self.assertIn(f"{sorted(paths)[mergeauthority.SHOWN_PATHS - 1]} and 2 more since", judged[0])
        self.assertNotIn(sorted(paths)[mergeauthority.SHOWN_PATHS], judged[0])

    def test_a_git_without_merge_tree_write_tree_is_an_error_naming_the_version(self):
        real = planscope.git_answer

        def old_git(repo, *args, **kwargs):
            return (planscope.USAGE, b"") if "merge-tree" in args else real(repo, *args, **kwargs)

        with mock.patch.object(planscope, "git_answer", old_git), \
                self.assertRaisesRegex(planscope.PlanScopeError, "git 2.38"):
            planscope.merges_cleanly(str(self.repo), self.base, self.head)

    def test_judging_a_conflict_writes_no_object_into_the_repository(self):
        self.on_main(("src/app.py", "x = 3\n"))
        before = git(self.repo, "count-objects", "-v")

        self.assert_refused("conflicts")

        self.assertEqual(git(self.repo, "count-objects", "-v"), before)


if __name__ == "__main__":
    unittest.main()
