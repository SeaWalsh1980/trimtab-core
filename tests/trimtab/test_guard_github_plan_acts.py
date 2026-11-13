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
    def decide(self, command, facts=None, run=True):
        def git(cwd, *args):
            if args not in GIT:
                raise guard_github.planguard.tools.ToolError(f"git {args[0]} is not answered")
            return GIT[args]

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
        self.allowed("gh api graphql -f query='mutation { addLabelsToLabelable(input: {x: 1})"
                     " { clientMutationId } }'", run=False, facts=answered_facts())


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
