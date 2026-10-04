"""dependabot-block: the harness block for a Dependabot PR, planned without touching GitHub."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from instance_fixture import make_instance
from trimtab import config as project_config
from trimtab.capture.prblock import check_body
from trimtab.capture.sources import GhPullRequests, SourceError, _pr_view, _runs
from trimtab.cli import EXIT_CI_ATTENTION, build_parser, cmd_dependabot_block, main
from trimtab.dependabot import (
    BLOCK, DEPENDABOT_LOGIN, MAX_BODY, BlockRefused, CiRun, HostError, NotDependabot, PullRequestView,
    ReadBackFailed, RerunFailed, StaleBlockPlan, apply, confirmation, plan, read_plan,
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

    def test_refuses_a_merged_pr(self):
        pr = dependabot_pr(state="MERGED")

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

        self.assertTrue(todo.new_body.startswith(DEPENDABOT_TEXT))

    def test_keeps_trailing_whitespace_so_the_prefix_matches_the_previewed_digest(self):
        body = DEPENDABOT_TEXT + "\n\n  "

        todo = plan_for(dependabot_pr(body=body))

        prefix = todo.new_body[:len(body)]
        self.assertEqual((prefix, hashlib.sha256(prefix.encode()).hexdigest()), (body, todo.old_digest))

    def test_the_block_starts_on_its_own_line_after_text_with_no_final_newline(self):
        body = DEPENDABOT_TEXT.rstrip("\n")

        todo = plan_for(dependabot_pr(body=body))

        self.assertEqual(todo.new_body, body + "\n\n" + BLOCK)

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

    def test_picks_the_highest_run_id_when_runs_arrive_newest_first(self):
        runs = [failed_run(NEWER_RUN), CiRun(id=FAILED_RUN, status="completed", conclusion="success")]

        todo = plan_for(dependabot_pr(), runs)

        self.assertEqual(todo.rerun_run_id, NEWER_RUN)

    def test_plans_no_rerun_when_the_newest_run_was_cancelled(self):
        runs = [CiRun(id=FAILED_RUN, status="completed", conclusion="cancelled")]

        todo = plan_for(dependabot_pr(), runs)

        self.assertEqual((todo.rerun_run_id, todo.ci_needs_attention), (None, True))

    def test_plans_no_rerun_and_needs_attention_when_there_is_no_run(self):
        todo = plan_for(dependabot_pr(), [])

        self.assertEqual((todo.rerun_run_id, todo.ci_needs_attention), (None, True))

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

    def __init__(self, pr, runs=(), rerun_fails=False, view_fails=False, keeps_on_write=None,
                 view_fails_after_write=False, set_body_fails=False):
        self.pr = pr
        self.ci = list(runs)
        self.reruns = []
        self.body_writes = 0
        self.rerun_fails = rerun_fails
        self.view_fails = view_fails
        # What the host stores when a body is written, if not the body itself: a host that mangles it.
        self.keeps_on_write = keeps_on_write
        self.view_fails_after_write = view_fails_after_write
        self.set_body_fails = set_body_fails

    def view(self, number):
        if self.view_fails or (self.view_fails_after_write and self.body_writes):
            raise HostError("view refused")
        return self.pr

    def set_body(self, number, body):
        if self.set_body_fails:
            raise HostError("write refused")
        self.pr = replace(self.pr, body=body if self.keeps_on_write is None else self.keeps_on_write)
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
        self.assertEqual((host.body_writes, host.reruns), (0, []))

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


class WritingFails(unittest.TestCase):
    def test_a_failed_write_raises_and_requests_no_rerun(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()], set_body_fails=True)

        with self.assertRaises(HostError):
            apply_to(host, dry_run_token(host))
        self.assertEqual(host.reruns, [])


class ReadingBack(unittest.TestCase):
    def test_a_host_that_alters_the_body_on_write_fails_the_read_back(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()], keeps_on_write=DEPENDABOT_TEXT)

        with self.assertRaises(ReadBackFailed):
            apply_to(host, dry_run_token(host))

    def test_a_read_back_that_cannot_reach_the_host_says_the_body_was_written(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()], view_fails_after_write=True)

        with self.assertRaises(ReadBackFailed) as caught:
            apply_to(host, dry_run_token(host))

        self.assertIn("was written", str(caught.exception))


def run_command(host, *extra):
    """The dependabot-block command against an in-memory host. Returns (exit code, stdout, stderr)."""
    args = build_parser().parse_args(["dependabot-block", str(PR_NUMBER), "--repo", REPO,
                                      "--workflow", WORKFLOW, *extra])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cmd_dependabot_block(args, host_for=lambda repo: host, registry=REGISTRY)
    return code, out.getvalue(), err.getvalue()


def passed_run(run_id=FAILED_RUN):
    return CiRun(id=run_id, status="completed", conclusion="success")


def running_run(run_id=FAILED_RUN):
    return CiRun(id=run_id, status="in_progress", conclusion="")


def cancelled_run(run_id=FAILED_RUN):
    return CiRun(id=run_id, status="completed", conclusion="cancelled")


class TheDryRun(unittest.TestCase):
    def test_a_dry_run_with_work_to_do_prints_a_token_and_exits_0(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])

        code, out, _ = run_command(host)

        self.assertEqual((code, f"confirm: {dry_run_token(host)}" in out), (0, True))

    def test_a_dry_run_names_the_project_in_the_apply_command(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])

        _, out, _ = run_command(host, "--project", "elsewhere")

        self.assertIn("--project elsewhere", out)

    def test_a_dry_run_with_nothing_to_do_prints_no_token_and_exits_0(self):
        host = FakeGitHub(dependabot_pr(body=VALID_BLOCK_BODY), [passed_run()])

        code, out, _ = run_command(host)

        self.assertEqual((code, "confirm:" in out), (0, False))

    def test_a_dry_run_with_no_run_found_names_the_workflow_and_exits_4(self):
        host = FakeGitHub(dependabot_pr(), [])

        code, out, _ = run_command(host)

        self.assertEqual((code, f"no {WORKFLOW} run found" in out), (EXIT_CI_ATTENTION, True))

    def test_a_dry_run_whose_newest_run_was_cancelled_names_it_and_exits_4(self):
        host = FakeGitHub(dependabot_pr(body=VALID_BLOCK_BODY),
                          [CiRun(id=FAILED_RUN, status="completed", conclusion="cancelled")])

        code, out, _ = run_command(host)

        self.assertEqual((code, "ended cancelled" in out), (EXIT_CI_ATTENTION, True))

    def test_a_dry_run_with_a_body_to_write_and_a_cancelled_run_prints_a_token_and_exits_4(self):
        host = FakeGitHub(dependabot_pr(), [cancelled_run()])

        code, out, _ = run_command(host)

        self.assertEqual((code, f"confirm: {dry_run_token(host)}" in out), (EXIT_CI_ATTENTION, True))

    def test_an_unexpected_conclusion_is_not_printed(self):
        host = FakeGitHub(dependabot_pr(), [CiRun(id=FAILED_RUN, status="completed", conclusion=CANARY)])

        _, out, err = run_command(host)

        self.assertNotIn(CANARY, out + err)

    def test_a_dry_run_never_prints_dependabots_text(self):
        host = FakeGitHub(dependabot_pr(body=DEPENDABOT_TEXT + CANARY + "\n"), [failed_run()])

        _, out, err = run_command(host)

        self.assertNotIn(CANARY, out + err)

    def test_a_planned_rerun_names_the_workflow_and_the_run(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])

        _, out, _ = run_command(host)

        self.assertIn(f"the failed jobs of {WORKFLOW} run {FAILED_RUN}", out)

    def test_a_run_still_going_is_named_and_exits_0_without_attention(self):
        host = FakeGitHub(dependabot_pr(), [CiRun(id=FAILED_RUN, status="in_progress", conclusion="")])

        code, out, err = run_command(host)

        self.assertEqual((code, "still going" in out, "attention" in err), (0, True, False))

    def test_a_run_still_going_when_the_block_is_to_be_written_is_noted_as_older_than_it(self):
        host = FakeGitHub(dependabot_pr(), [running_run()])

        _, out, _ = run_command(host)

        self.assertIn("started before the block was added", out)

    def test_a_run_still_going_when_the_block_is_already_there_carries_no_note(self):
        host = FakeGitHub(dependabot_pr(body=VALID_BLOCK_BODY), [running_run()])

        _, out, _ = run_command(host)

        self.assertNotIn("started before the block was added", out)

    def test_a_newest_run_that_passed_is_named(self):
        host = FakeGitHub(dependabot_pr(body=VALID_BLOCK_BODY), [passed_run()])

        _, out, _ = run_command(host)

        self.assertIn(f"the newest {WORKFLOW} run at this head passed", out)

    def test_a_body_with_an_invalid_block_exits_1_and_lists_problems_without_quoting_it(self):
        body = DEPENDABOT_TEXT + "\n## Harness items applied\n" + CANARY + "\n"
        host = FakeGitHub(dependabot_pr(body=body), [failed_run()])

        code, out, err = run_command(host)

        self.assertEqual((code, "FAIL" in err, CANARY in out + err), (1, True, False))

    def test_a_pr_not_opened_by_dependabot_exits_1(self):
        host = FakeGitHub(dependabot_pr(login="someone", is_bot=False), [failed_run()])

        code, _, _ = run_command(host)

        self.assertEqual(code, 1)

    def test_a_host_failure_exits_2_and_says_running_again_is_safe(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()], view_fails=True)

        code, _, err = run_command(host)

        self.assertEqual((code, "safe" in err), (2, True))


class TheApplyCommand(unittest.TestCase):
    def test_apply_with_the_dry_runs_token_exits_0(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])

        code, out, _ = run_command(host, "--apply", "--confirm", dry_run_token(host))

        self.assertEqual((code, "block added" in out), (0, True))

    def test_apply_that_adds_the_block_while_ci_needs_a_look_warns_and_exits_4(self):
        host = FakeGitHub(dependabot_pr(), [cancelled_run()])

        code, _, err = run_command(host, "--apply", "--confirm", dry_run_token(host))

        self.assertEqual((code, "attention" in err, host.body_writes), (EXIT_CI_ATTENTION, True, 1))

    def test_apply_that_adds_the_block_while_a_run_is_going_notes_it_is_older(self):
        host = FakeGitHub(dependabot_pr(), [running_run()])

        code, out, _ = run_command(host, "--apply", "--confirm", dry_run_token(host))

        self.assertEqual((code, "started before the block was added" in out), (0, True))

    def test_apply_with_a_stale_token_exits_3(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])
        token = dry_run_token(host)
        host.ci.append(failed_run(NEWER_RUN))

        code, _, _ = run_command(host, "--apply", "--confirm", token)

        self.assertEqual(code, 3)

    def test_a_failed_rerun_exits_2_and_says_the_body_was_written(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()], rerun_fails=True)

        code, _, err = run_command(host, "--apply", "--confirm", dry_run_token(host))

        self.assertEqual((code, "the body was written" in err), (2, True))

    def test_a_failed_write_exits_2_and_says_running_again_is_safe(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()], set_body_fails=True)

        code, _, err = run_command(host, "--apply", "--confirm", dry_run_token(host))

        self.assertEqual((code, "running the dry run again is safe" in err, host.reruns), (2, True, []))

    def test_a_failed_rerun_when_the_block_was_already_there_does_not_claim_a_write(self):
        host = FakeGitHub(dependabot_pr(body=VALID_BLOCK_BODY), [failed_run()], rerun_fails=True)

        code, _, err = run_command(host, "--apply", "--confirm", dry_run_token(host))

        self.assertEqual((code, "the body was written" in err), (2, False))

    def test_apply_after_the_head_moved_exits_3(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])
        token = dry_run_token(host)
        host.pr = replace(host.pr, head_sha="b" * 40)

        code, _, _ = run_command(host, "--apply", "--confirm", token)

        self.assertEqual((code, host.body_writes), (3, 0))

    def test_apply_after_the_body_changed_exits_3(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()])
        token = dry_run_token(host)
        host.pr = replace(host.pr, body=DEPENDABOT_TEXT + "edited\n")

        code, _, _ = run_command(host, "--apply", "--confirm", token)

        self.assertEqual((code, host.body_writes), (3, 0))

    def test_a_failed_read_back_exits_1_and_says_the_body_was_written(self):
        host = FakeGitHub(dependabot_pr(), [failed_run()], view_fails_after_write=True)

        code, _, err = run_command(host, "--apply", "--confirm", dry_run_token(host))

        self.assertEqual((code, "was written" in err), (1, True))


class TheCommand(unittest.TestCase):
    def test_apply_without_a_confirmation_token_is_refused(self):
        argv = ["dependabot-block", str(PR_NUMBER), "--repo", REPO, "--workflow", WORKFLOW, "--apply"]

        with contextlib.redirect_stderr(io.StringIO()):
            code = main(argv)

        self.assertEqual(code, 2)


class GhRecorder:
    """Stands in for the gh CLI, a true external system: records each command and its stdin, answers canned output."""

    def __init__(self, answer=""):
        self.calls = []
        self.answer = answer

    def gh(self, *args):
        self.calls.append((args, None))
        return self.answer

    def gh_input(self, stdin, *args):
        self.calls.append((args, stdin))
        return self.answer

    def __enter__(self):
        self._patches = [patch("trimtab.capture.sources._gh", self.gh),
                         patch("trimtab.capture.sources._gh_input", self.gh_input)]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


class GhPullRequestCommands(unittest.TestCase):
    def test_set_body_patches_the_pr_with_the_body_as_json_on_stdin(self):
        with GhRecorder() as gh:
            GhPullRequests(REPO).set_body(PR_NUMBER, CANARY)

        (args, stdin), = gh.calls
        self.assertEqual((args[:4], json.loads(stdin)), (("api", "-X", "PATCH", f"repos/{REPO}/pulls/{PR_NUMBER}"),
                                                         {"body": CANARY}))

    def test_set_body_never_puts_the_body_in_the_arguments(self):
        with GhRecorder() as gh:
            GhPullRequests(REPO).set_body(PR_NUMBER, CANARY)

        (args, _), = gh.calls
        self.assertFalse(any(CANARY in a for a in args))

    def test_runs_filters_by_workflow_head_and_event_and_is_bounded(self):
        with GhRecorder(answer="[]") as gh:
            GhPullRequests(REPO).runs(HEAD_SHA, WORKFLOW, 10)

        (args, _), = gh.calls
        pairs = dict(zip(args, args[1:]))
        self.assertEqual((pairs["--workflow"], pairs["--commit"], pairs["--event"], pairs["--limit"]),
                         (WORKFLOW, HEAD_SHA, "pull_request", "10"))

    def test_rerun_failed_reruns_only_the_failed_jobs(self):
        with GhRecorder() as gh:
            GhPullRequests(REPO).rerun_failed(FAILED_RUN)

        (args, _), = gh.calls
        self.assertEqual(args[:3] + ("--failed" in args,), ("run", "rerun", str(FAILED_RUN), True))

    def test_view_asks_for_the_fields_the_plan_needs(self):
        with GhRecorder(answer=gh_pr_json()) as gh:
            GhPullRequests(REPO).view(PR_NUMBER)

        (args, _), = gh.calls
        self.assertEqual(dict(zip(args, args[1:]))["--json"], "number,author,body,headRefOid,state")


class TheRegistry(unittest.TestCase):
    """Integration, not a unit test: builds an instance in a temporary directory, as test_bump does."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_a_registry_that_cannot_be_built_exits_1_before_github_is_touched(self):
        instance = make_instance(Path(self._tmp.name))
        project = Path(self._tmp.name) / "project"
        lock = project / project_config.PATH
        lock.parent.mkdir(parents=True)
        lock.write_text("{ not json", encoding="utf-8")
        args = build_parser().parse_args(["dependabot-block", str(PR_NUMBER), "--repo", REPO, "--workflow", WORKFLOW,
                                          "--instance", str(instance), "--project", str(project)])
        touched = []

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = cmd_dependabot_block(args, host_for=lambda repo: touched.append(repo))

        self.assertEqual((code, "cannot build the registry" in err.getvalue(), touched), (1, True, []))


if __name__ == "__main__":
    unittest.main()
