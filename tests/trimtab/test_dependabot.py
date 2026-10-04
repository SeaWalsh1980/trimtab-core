"""dependabot-block: the harness block for a Dependabot PR, planned without touching GitHub."""

import contextlib
import io
import json
import unittest
from dataclasses import replace

from trimtab.capture.prblock import check_body
from trimtab.capture.sources import SourceError, _pr_view, _runs
from trimtab.cli import main
from trimtab.dependabot import (
    BLOCK, DEPENDABOT_LOGIN, MAX_BODY, BlockRefused, CiRun, HostError, NotDependabot, PullRequestView,
    RerunFailed, StaleBlockPlan, apply, confirmation, plan, read_plan,
)

REGISTRY = {}  # the block cites `id: none`, so no item has to resolve
REPO = "owner/repo"
OTHER_REPO = "owner/other"
WORKFLOW = "ci.yml"
PR_NUMBER = 76
OTHER_PR_NUMBER = 77
HEAD_SHA = "a" * 40
FAILED_RUN = 1001
NEWER_RUN = 1002
CANARY = "CANARY-7f3e9b"
DEPENDABOT_TEXT = (
    "Bumps [actions/checkout](https://github.com/actions/checkout) from 4.4.0 to 7.0.1.\n"
    "<details>\n<summary>Release notes</summary>\n\n# heading inside release notes\n</details>\n"
)
VALID_BLOCK_BODY = DEPENDABOT_TEXT + "\n\n" + BLOCK


def dependabot_pr(body=DEPENDABOT_TEXT, login=DEPENDABOT_LOGIN, is_bot=True, state="OPEN", number=PR_NUMBER):
    return PullRequestView(number=number, author_login=login, author_is_bot=is_bot, body=body,
                           head_sha=HEAD_SHA, state=state)


def failed_run(run_id=FAILED_RUN):
    return CiRun(id=run_id, status="completed", conclusion="failure")


def plan_for(pr, runs=(), repo=REPO):
    return plan(pr, runs, REGISTRY, repo, WORKFLOW)


class WhoItAccepts(unittest.TestCase):
    def test_refuses_a_pr_opened_by_a_person(self):
        pr = dependabot_pr(login="someone", is_bot=False)

        with self.assertRaises(NotDependabot):
            plan_for(pr)

    def test_refuses_a_bot_that_is_not_dependabot(self):
        pr = dependabot_pr(login="app/renovate")

        with self.assertRaises(NotDependabot):
            plan_for(pr)

    def test_refuses_the_dependabot_login_on_an_account_that_is_not_a_bot(self):
        pr = dependabot_pr(is_bot=False)

        with self.assertRaises(NotDependabot):
            plan_for(pr)

    def test_refuses_a_closed_pr(self):
        pr = dependabot_pr(state="CLOSED")

        with self.assertRaises(NotDependabot):
            plan_for(pr)


class TheBody(unittest.TestCase):
    def test_the_new_body_passes_check_pr(self):
        todo = plan_for(dependabot_pr())

        self.assertTrue(check_body(todo.new_body, REGISTRY).ok)

    def test_keeps_dependabots_text_as_an_exact_prefix(self):
        todo = plan_for(dependabot_pr())

        self.assertTrue(todo.new_body.startswith(DEPENDABOT_TEXT.rstrip()))

    def test_plans_no_body_change_when_a_valid_block_is_already_there(self):
        todo = plan_for(dependabot_pr(body=VALID_BLOCK_BODY))

        self.assertIsNone(todo.new_body)

    def test_refuses_a_body_whose_existing_block_is_invalid(self):
        body = DEPENDABOT_TEXT + "\n## Harness items applied\nprose, not yaml\n"

        with self.assertRaises(BlockRefused):
            plan_for(dependabot_pr(body=body))

    def test_refuses_when_an_unclosed_fence_would_hide_the_block(self):
        body = DEPENDABOT_TEXT + "\n```\nunclosed fence from release notes\n"

        with self.assertRaises(BlockRefused):
            plan_for(dependabot_pr(body=body))

    def test_refuses_a_body_that_would_exceed_githubs_limit(self):
        body = "x" * MAX_BODY

        with self.assertRaises(BlockRefused):
            plan_for(dependabot_pr(body=body))

    def test_accepts_a_body_that_reaches_githubs_limit_exactly(self):
        body = "x" * (MAX_BODY - len("\n\n") - len(BLOCK))

        todo = plan_for(dependabot_pr(body=body))

        self.assertEqual(len(todo.new_body), MAX_BODY)

    def test_refuses_a_body_one_character_over_githubs_limit(self):
        body = "x" * (MAX_BODY - len("\n\n") - len(BLOCK) + 1)

        with self.assertRaises(BlockRefused):
            plan_for(dependabot_pr(body=body))

    def test_records_the_old_body_by_length_and_digest(self):
        todo = plan_for(dependabot_pr())

        self.assertEqual((todo.old_length, len(todo.old_digest)), (len(DEPENDABOT_TEXT), 64))


class TheRerun(unittest.TestCase):
    def test_plans_a_rerun_of_the_newest_run_when_it_failed(self):
        runs = [CiRun(id=FAILED_RUN, status="completed", conclusion="success"), failed_run(NEWER_RUN)]

        todo = plan_for(dependabot_pr(), runs)

        self.assertEqual(todo.rerun_run_id, NEWER_RUN)

    def test_plans_no_rerun_when_the_newest_run_passed(self):
        runs = [failed_run(FAILED_RUN), CiRun(id=NEWER_RUN, status="completed", conclusion="success")]

        todo = plan_for(dependabot_pr(), runs)

        self.assertIsNone(todo.rerun_run_id)

    def test_plans_no_rerun_while_the_newest_run_is_in_progress(self):
        runs = [failed_run(FAILED_RUN), CiRun(id=NEWER_RUN, status="in_progress", conclusion="")]

        todo = plan_for(dependabot_pr(), runs)

        self.assertEqual((todo.rerun_run_id, todo.run_in_progress), (None, True))

    def test_still_offers_the_rerun_when_the_block_is_already_there(self):
        todo = plan_for(dependabot_pr(body=VALID_BLOCK_BODY), [failed_run()])

        self.assertEqual((todo.new_body, todo.rerun_run_id), (None, FAILED_RUN))

    def test_nothing_to_do_when_the_block_is_there_and_ci_passed(self):
        runs = [CiRun(id=FAILED_RUN, status="completed", conclusion="success")]

        todo = plan_for(dependabot_pr(body=VALID_BLOCK_BODY), runs)

        self.assertTrue(todo.nothing_to_do)


class TheConfirmation(unittest.TestCase):
    def test_the_same_pr_and_runs_give_the_same_token(self):
        first = confirmation(plan_for(dependabot_pr(), [failed_run()]))

        second = confirmation(plan_for(dependabot_pr(), [failed_run()]))

        self.assertEqual(first, second)

    def test_another_repository_gives_another_token(self):
        here = confirmation(plan_for(dependabot_pr()))

        there = confirmation(plan_for(dependabot_pr(), repo=OTHER_REPO))

        self.assertNotEqual(here, there)

    def test_another_pr_gives_another_token(self):
        this = confirmation(plan_for(dependabot_pr()))

        that = confirmation(plan_for(dependabot_pr(number=OTHER_PR_NUMBER)))

        self.assertNotEqual(this, that)

    def test_a_changed_body_gives_another_token(self):
        before = confirmation(plan_for(dependabot_pr()))

        after = confirmation(plan_for(dependabot_pr(body=DEPENDABOT_TEXT + "edited\n")))

        self.assertNotEqual(before, after)


class NoBodyTextInMessages(unittest.TestCase):
    def test_a_refusal_never_quotes_the_body(self):
        body = DEPENDABOT_TEXT + "\n## Harness items applied\n" + CANARY + "\n"

        with self.assertRaises(BlockRefused) as caught:
            plan_for(dependabot_pr(body=body))

        shown = str(caught.exception) + " ".join(str(p) for p in caught.exception.problems)
        self.assertNotIn(CANARY, shown)

    def test_a_refused_author_is_not_quoted(self):
        with self.assertRaises(NotDependabot) as caught:
            plan_for(dependabot_pr(login=CANARY, is_bot=False))

        self.assertNotIn(CANARY, str(caught.exception))


def gh_pr_json(**overrides):
    row = {"number": PR_NUMBER, "author": {"login": DEPENDABOT_LOGIN, "is_bot": True, "name": ""},
           "body": DEPENDABOT_TEXT, "headRefOid": HEAD_SHA, "state": "OPEN"}
    row.update(overrides)
    return json.dumps(row)


class GhPullRequestParsing(unittest.TestCase):
    def test_a_parsed_view_keeps_the_login_and_the_bot_flag(self):
        view = _pr_view(gh_pr_json())

        self.assertEqual((view.author_login, view.author_is_bot), (DEPENDABOT_LOGIN, True))

    def test_a_null_body_reads_as_empty(self):
        view = _pr_view(gh_pr_json(body=None))

        self.assertEqual(view.body, "")

    def test_json_without_an_author_is_refused(self):
        row = json.loads(gh_pr_json())
        del row["author"]

        with self.assertRaises(SourceError):
            _pr_view(json.dumps(row))

    def test_a_bot_flag_that_is_not_a_boolean_is_refused(self):
        with self.assertRaises(SourceError):
            _pr_view(gh_pr_json(author={"login": DEPENDABOT_LOGIN, "is_bot": "true"}))

    def test_a_head_sha_that_is_not_40_hex_characters_is_refused(self):
        with self.assertRaises(SourceError):
            _pr_view(gh_pr_json(headRefOid="not-a-sha"))

    def test_output_that_is_not_json_is_refused(self):
        with self.assertRaises(SourceError):
            _pr_view("<html>")


class GhRunParsing(unittest.TestCase):
    def test_runs_parse_into_ci_runs(self):
        out = json.dumps([{"databaseId": FAILED_RUN, "status": "completed", "conclusion": "failure"}])

        self.assertEqual(_runs(out), [failed_run()])

    def test_a_pending_run_has_an_empty_conclusion(self):
        out = json.dumps([{"databaseId": NEWER_RUN, "status": "in_progress", "conclusion": ""}])

        self.assertEqual(_runs(out)[0].conclusion, "")

    def test_a_run_id_that_is_not_an_integer_is_refused(self):
        out = json.dumps([{"databaseId": "1001", "status": "completed", "conclusion": "failure"}])

        with self.assertRaises(SourceError):
            _runs(out)

    def test_a_run_without_a_status_is_refused(self):
        out = json.dumps([{"databaseId": FAILED_RUN, "conclusion": "failure"}])

        with self.assertRaises(SourceError):
            _runs(out)


class FakeGitHub:
    """An in-memory PullRequestHost: one PR, its runs, and the re-runs requested."""

    def __init__(self, pr, runs=(), rerun_fails=False):
        self.pr = pr
        self.ci = list(runs)
        self.reruns = []
        self.body_writes = 0
        self.rerun_fails = rerun_fails

    def view(self, number):
        return self.pr

    def set_body(self, number, body):
        self.pr = replace(self.pr, body=body)
        self.body_writes += 1

    def runs(self, head_sha, workflow, limit):
        return [r for r in self.ci][:limit]

    def rerun_failed(self, run_id):
        if self.rerun_fails:
            raise HostError("rerun refused")
        self.reruns.append(run_id)


def dry_run_token(host):
    return confirmation(read_plan(host, REGISTRY, REPO, WORKFLOW, PR_NUMBER))


def apply_to(host, token):
    return apply(host, REGISTRY, REPO, WORKFLOW, PR_NUMBER, token)


class Applying(unittest.TestCase):
    def test_a_dry_run_changes_nothing_on_github(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])

        dry_run_token(host)

        self.assertEqual((host.pr.body, host.reruns), (DEPENDABOT_TEXT, []))

    def test_apply_with_the_dry_runs_token_writes_a_body_that_passes_check_pr(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])

        apply_to(host, dry_run_token(host))

        self.assertTrue(check_body(host.pr.body, REGISTRY).ok)

    def test_apply_with_the_dry_runs_token_reruns_the_failed_run(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])

        apply_to(host, dry_run_token(host))

        self.assertEqual(host.reruns, [FAILED_RUN])

    def test_apply_aborts_with_nothing_written_when_the_body_changed_after_the_dry_run(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])
        token = dry_run_token(host)
        host.pr = replace(host.pr, body=DEPENDABOT_TEXT + "edited\n")

        with self.assertRaises(StaleBlockPlan):
            apply_to(host, token)
        self.assertEqual((host.body_writes, host.reruns), (0, []))

    def test_apply_aborts_when_the_head_moved_after_the_dry_run(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])
        token = dry_run_token(host)
        host.pr = replace(host.pr, head_sha="b" * 40)

        with self.assertRaises(StaleBlockPlan):
            apply_to(host, token)

    def test_apply_aborts_with_nothing_written_when_a_new_failed_run_appeared_after_the_dry_run(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])
        token = dry_run_token(host)
        host.ci.append(failed_run(NEWER_RUN))

        with self.assertRaises(StaleBlockPlan):
            apply_to(host, token)
        self.assertEqual((host.body_writes, host.reruns), (0, []))

    def test_apply_aborts_with_nothing_written_when_a_newer_run_passed_after_the_dry_run(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])
        token = dry_run_token(host)
        host.ci.append(CiRun(id=NEWER_RUN, status="completed", conclusion="success"))

        with self.assertRaises(StaleBlockPlan):
            apply_to(host, token)
        self.assertEqual((host.body_writes, host.reruns), (0, []))

    def test_a_second_apply_on_a_pr_that_has_the_block_writes_nothing(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])
        apply_to(host, dry_run_token(host))
        host.ci = [CiRun(id=NEWER_RUN, status="completed", conclusion="success")]

        result = apply_to(host, dry_run_token(host))

        self.assertEqual((result.wrote_body, host.body_writes), (False, 1))

    def test_a_failed_rerun_reports_that_the_body_was_written(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()], rerun_fails=True)

        with self.assertRaises(RerunFailed) as caught:
            apply_to(host, dry_run_token(host))

        self.assertTrue(caught.exception.wrote_body)

    def test_after_a_failed_rerun_running_again_reruns_without_rewriting_the_body(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()], rerun_fails=True)
        with self.assertRaises(RerunFailed):
            apply_to(host, dry_run_token(host))
        host.rerun_fails = False

        apply_to(host, dry_run_token(host))

        self.assertEqual((host.body_writes, host.reruns), (1, [FAILED_RUN]))


class TheCommand(unittest.TestCase):
    def test_apply_without_a_confirmation_token_is_refused(self):
        argv = ["dependabot-block", str(PR_NUMBER), "--repo", REPO, "--workflow", WORKFLOW, "--apply"]

        with contextlib.redirect_stderr(io.StringIO()):
            code = main(argv)

        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
