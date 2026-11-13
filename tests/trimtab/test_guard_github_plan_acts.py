"""guard-github reads the plan-run acts of ADR 0063 §1a and §5: the ready mark, the re-request label,
a reply to a review comment, returning the PR to draft, and the GraphQL spellings of each.

The rows and the ledger are tested in test_planchecks and test_planacts; here every spelling must
reach them. The facts are a fake of this codebase's own reader, and the instance, config directory
and ledger come from `plan_fixture`, so nothing reads the machine's own `~/.claude`.
"""

import unittest
from pathlib import Path

import plan_fixture
from test_planchecks import AGENT, BRANCH, FakeFacts, H1, H2, PLAN, REVIEWER, review, thread
from trimtab import planguard, planreview
from trimtab.adapters import guard_github

CWD = "/work"
PUSH_REF = ("rev-parse", "--symbolic-full-name", "@{push}")
HEAD_REF = ("symbolic-ref", "--short", "-q", "HEAD")
CONFIG_REF = ("config", "--list", "-z")
GIT = {PUSH_REF: f"refs/remotes/origin/{BRANCH}\n", HEAD_REF: f"{BRANCH}\n",
       CONFIG_REF: "remote.origin.url\nhttps://github.com/owner/proj.git\0"}


def setUpModule():
    plan_fixture.set_up()


def tearDownModule():
    plan_fixture.tear_down()


def answered_facts(**kw):
    """Facts under which the ready mark's rows all allow it: a passing run on the head, every thread
    answered with the fixed tag and closed by a later run."""
    base = {
        "runs": [(review(head=H1), [thread()]),
                 (review(head=H1, run="43"), [thread(state="resolved", by=REVIEWER, run="43")])],
        "comments": [planreview.Comment(11, REVIEWER, "a finding"),
                     planreview.Comment(12, AGENT, f"[fixed] {H2}", 11)],
    }
    return FakeFacts(**{**base, **kw})


class Acts(unittest.TestCase):
    def decide(self, command, facts=None, run=True, branch=BRANCH):
        answers = {**GIT, HEAD_REF: f"{branch}\n", PUSH_REF: f"refs/remotes/origin/{branch}\n"}

        def git(cwd, *args):
            if args not in answers:
                raise guard_github.planguard.tools.ToolError(f"git {args[0]} is not answered")
            return answers[args]

        plan = planguard.PlanSupport(Path(CWD), environ={"TRIMTAB_PLAN_RUN": "1"} if run else {},
                                     facts=facts if facts is not None else answered_facts(), git=git)
        return guard_github.decide({"tool_name": "Bash", "tool_input": {"command": command},
                                    "cwd": CWD}, plan)

    def refused(self, command, text, **kw):
        code, message = self.decide(command, **kw)
        self.assertEqual(code, guard_github.BLOCK, f"allowed: {command}")
        self.assertIn(text, message, command)

    def allowed(self, command, **kw):
        code, message = self.decide(command, **kw)
        self.assertEqual((code, message), (guard_github.ALLOW, ""), command)


class ReadyMark(Acts):
    def test_every_spelling_of_the_ready_mark_reaches_the_rows(self):
        facts = answered_facts(runs=[(review(head=H2), [thread()])])  # no passing run on this head: E7
        for command in (f"gh pr ready {BRANCH}", "gh pr ready 7", "gh pr ready",
                        "gh api -X PATCH repos/owner/proj/pulls/7 -F draft=false"):
            with self.subTest(command):
                self.refused(command, "E7", facts=facts)

    def test_a_ready_mark_every_row_allows_passes(self):
        for command in ("gh pr ready 7", "gh api -X PATCH repos/owner/proj/pulls/7 -F draft=false"):
            with self.subTest(command):
                self.allowed(command)

    def test_the_ready_mark_of_another_branchs_pr_reads_no_plan_row(self):
        self.allowed("gh pr ready 8", facts=answered_facts(pr_heads={8: "feature/x"}))

    def test_an_unreadable_draft_flag_refuses(self):
        self.refused("gh api -X PATCH repos/owner/proj/pulls/7 -F draft=$WHAT", "draft flag")

    def test_returning_the_pr_to_draft_needs_the_ledger(self):
        self.allowed("gh pr ready 7 --undo")  # the fixture holds the ledger, so it is the run's own act
        self.allowed("gh api -X PATCH repos/owner/proj/pulls/7 -F draft=true")


class Labels(Acts):
    def test_every_spelling_of_the_rerequest_label_reaches_e6_and_e10(self):
        facts = answered_facts(runs=[], branch_commits=())  # no complete round in the ledger: E10
        for command in ("gh pr edit 7 --add-label review:run",
                        "gh issue edit 7 --add-label review:run",
                        "gh pr edit 7 --add-label other,review:run",
                        "gh api -X POST repos/owner/proj/issues/7/labels -f labels[]=review:run"):
            with self.subTest(command):
                self.refused(command, "E10", facts=facts)

    def test_a_label_the_guard_cannot_read_refuses_on_a_plan_pr(self):
        for command in ("gh pr edit 7 --add-label $L",
                        "gh api -X POST repos/owner/proj/issues/7/labels -f labels[]=$L"):
            with self.subTest(command):
                self.refused(command, "re-request")

    def test_an_ordinary_label_passes_with_the_ledger(self):
        for command in ("gh pr edit 7 --add-label needs-work",
                        "gh api -X DELETE repos/owner/proj/issues/7/labels/needs-work"):
            with self.subTest(command):
                self.allowed(command)

    def test_a_label_on_another_branchs_pr_reads_no_plan_row(self):
        self.allowed("gh pr edit 8 --add-label review:run", facts=answered_facts(pr_heads={8: "feature/x"}))


class Replies(Acts):
    def test_a_reply_whose_body_the_guard_cannot_read_refuses(self):
        self.refused("gh api -X POST repos/owner/proj/pulls/7/comments -f body=$B", "body is unreadable")

    def test_a_reply_passes_with_the_ledger(self):
        self.allowed("gh api -X POST repos/owner/proj/pulls/7/comments -f body=[wrong] no")

    def test_a_comment_on_the_pr_itself_is_not_a_reply(self):
        self.allowed("gh api -X POST repos/owner/proj/issues/7/comments -f body=hello")


class Graphql(Acts):
    def test_a_run_refuses_the_mutations_that_name_a_pr_or_thread_by_id(self):
        for mutation in ("markPullRequestReadyForReview", "convertPullRequestToDraft",
                         "addLabelsToLabelable", "removeLabelsFromLabelable", "addPullRequestReviewThreadReply"):
            with self.subTest(mutation):
                self.refused(f"gh api graphql -f query='mutation {{ {mutation}(input: {{x: 1}})"
                             " { clientMutationId } }'", "ID this guard cannot read")

    def test_outside_a_run_they_are_no_plan_rows_business(self):
        # Not a run: no flag and no plan/* branch checked out. A plan/* branch alone makes it one
        # (ADR 0041's signals), which is why this case needs a branch of its own.
        self.allowed("gh api graphql -f query='mutation { addLabelsToLabelable(input: {x: 1})"
                     " { clientMutationId } }'", run=False, facts=answered_facts(), branch="feature/x")

    def test_a_plan_branch_without_the_flag_is_still_a_run(self):
        self.refused("gh api graphql -f query='mutation { addLabelsToLabelable(input: {x: 1})"
                     " { clientMutationId } }'", "ID this guard cannot read", run=False)


class MoreSpellings(Acts):
    """The spellings round 1 found unread: review round 1's security and code passes (ADR 0063 §1a, §5)."""

    def test_a_pr_opened_through_the_api_reaches_e23(self):
        self.refused("gh api -X POST repos/owner/proj/pulls -f head=" + BRANCH
                     + " -f base=main -f title=t -f body=b -F draft=true", "E23")

    def test_a_pr_opened_from_another_branch_through_the_api_reads_no_plan_row(self):
        self.allowed("gh api -X POST repos/owner/proj/pulls -f head=feature/x -f base=main "
                     "-f title=t -f body=b -F draft=true")

    def test_a_pr_whose_head_the_api_call_hides_refuses(self):
        self.refused("gh api -X POST repos/owner/proj/pulls -f head=$B -f base=main", "unreadable")

    def test_a_graphql_create_naming_a_plan_branch_refuses(self):
        self.refused("gh api graphql -f query='mutation { createPullRequest(input: {headRefName: "
                     f'"{BRANCH}", baseRefName: "main", draft: true}})' + " { clientMutationId } }'", "E23")

    def test_a_label_replacement_removes_every_other_label(self):
        # §1a allows removing plan:continue only, so a replacement needs the ledger.
        self.allowed("gh api -X PUT repos/owner/proj/issues/7/labels -f labels[]=plan:blocked")

    def test_a_label_clear_is_a_removal_too(self):
        self.allowed("gh api -X DELETE repos/owner/proj/issues/7/labels")

    def test_a_reply_through_the_replies_route_is_read(self):
        self.allowed("gh api -X POST repos/owner/proj/pulls/7/comments/987/replies -f body=[wrong] no")

    def test_editing_a_review_comment_refuses(self):
        self.refused("gh api -X PATCH repos/owner/proj/pulls/comments/987 -f body=[fixed] abc", "outcome tag")

    def test_a_review_posted_on_a_plan_pr_refuses(self):
        self.refused("gh api -X POST repos/owner/proj/pulls/7/reviews -f body=[fixed] abc -f event=COMMENT",
                     "comment bodies")

    def test_an_alias_that_stops_at_a_plan_act_refuses(self):
        for expansion in ("pr ready", "pr edit", "issue edit", "pr close"):
            with self.subTest(expansion):
                self.refused(f"gh alias set x '{expansion}'", "alias")

    def test_the_rerequest_label_in_a_patch_of_the_issue_or_pull_reaches_e6_and_e10(self):
        facts = answered_facts(runs=[], branch_commits=())
        for route in ("issues", "pulls"):
            with self.subTest(route):
                self.refused(f"gh api -X PATCH repos/owner/proj/{route}/7 -f labels[]=review:run", "E10",
                             facts=facts)

    def test_a_patch_that_replaces_the_labels_is_a_removal_too(self):
        self.allowed("gh api -X PATCH repos/owner/proj/issues/7 -f labels[]=needs-work")

    def test_a_patch_whose_labels_the_guard_cannot_read_refuses(self):
        self.refused("gh api -X PATCH repos/owner/proj/issues/7 -f labels[]=$L", "re-request")

    def test_graphql_mutations_that_clear_labels_or_edit_review_comments_refuse_in_a_run(self):
        for mutation in ("clearLabelsFromLabelable", "updatePullRequestReviewComment",
                         "submitPullRequestReview", "addPullRequestReviewComment"):
            with self.subTest(mutation):
                self.refused(f"gh api graphql -f query='mutation {{ {mutation}(input: {{x: 1}})"
                             " { clientMutationId } }'", "ID this guard cannot read")

    def test_an_update_pull_request_mutation_carrying_label_ids_refuses_in_a_run(self):
        self.refused("gh api graphql -f query='mutation { updatePullRequest(input: {pullRequestId: \"x\", "
                     "labelIds: [\"y\"]}) { clientMutationId } }'", "label")

    def test_the_rerequest_label_given_when_a_pr_is_created_refuses(self):
        # A label attached at creation emits `labeled`, which starts a CI review, so it is a re-request;
        # no PR is open yet, so E6 and E10 have nothing to judge and the label refuses (review round 3).
        for command in ("gh pr create --draft --title t --body b --label review:run",
                        "gh pr create --draft --title t --body b -l other,review:run",
                        "gh pr create --draft --title t --body b --label=review:run"):
            with self.subTest(command):
                self.refused(command, "open PR", facts=answered_facts(open_pr=None))

    def test_another_label_given_at_creation_is_no_row_of_ours(self):
        from trimtab import planledger
        ledger = plan_fixture.ledger()
        for name in planledger.PASSES:
            ledger.round(H2, name, before_draft=True)

        self.allowed("gh pr create --draft --title t --body b --label needs-work",
                     facts=answered_facts(branch_head=H2))

    def test_a_label_the_guard_cannot_read_at_creation_refuses(self):
        self.refused("gh pr create --draft --title t --body b --label $L", "re-request")

    def test_a_quoted_configuration_assignment_refuses_a_push(self):
        self.refused('export G"IT_CONFIG_COUNT"=1; git push origin feature/x', "configuration")


class TheDraftPr(Acts):
    def test_opening_the_pr_needs_a_complete_round_on_the_head(self):
        self.refused("gh pr create --draft --title x --body y", "E23")

    def test_a_pr_from_another_branch_is_judged_only_as_a_draft(self):
        def git(cwd, *args):
            answers = {**GIT, HEAD_REF: "feature/x\n", PUSH_REF: "refs/remotes/origin/feature/x\n"}
            return answers[args]

        plan = planguard.PlanSupport(Path(CWD), environ={"TRIMTAB_PLAN_RUN": "1"}, facts=answered_facts(),
                                     git=git)
        code, _ = guard_github.decide({"tool_name": "Bash", "cwd": CWD, "tool_input": {
            "command": "gh pr create --draft --title x --body y"}}, plan)

        self.assertEqual(code, guard_github.ALLOW)

    def test_a_pr_opened_after_a_complete_round_passes(self):
        # On its own head, so this test and the one above do not read each other's rounds.
        from trimtab import planledger
        ledger = plan_fixture.ledger()
        for name in planledger.PASSES:
            ledger.round(H2, name, before_draft=True)

        self.allowed("gh pr create --draft --title x --body y", facts=answered_facts(branch_head=H2))


if __name__ == "__main__":
    unittest.main()
