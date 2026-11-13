"""guard-github reads the spellings of a `plan/*` write and hands them to the plan rows (ADR 0063 §5).

The rows themselves are tested in test_planrows; here every spelling of a write (git push, the REST
API, gh, GraphQL) must reach them, and commands that write no `plan/*` ref must read nothing."""

import json
import unittest
from pathlib import Path

from test_planrows import BRANCH, DRAFT, H1, READY, FakeFacts
import plan_fixture
from trimtab import planguard
from trimtab.adapters import guard_github
from trimtab.enforcement.tools import ToolError


def setUpModule():
    plan_fixture.set_up()


def tearDownModule():
    plan_fixture.tear_down()

CWD = "/work"
OTHER = "plan/0009-other/item-01-part1"


class NoFacts:
    """Facts that must never be asked: a command that writes no plan ref reads nothing."""

    def __getattr__(self, name):
        raise AssertionError(f"read {name} for a command that writes no plan/* ref")


def fake_git(answers):
    def run(cwd, *args):
        if args not in answers:
            raise ToolError(f"git {args[0]} is not answered")
        return answers[args]
    return run


PUSH_REF = ("rev-parse", "--symbolic-full-name", "@{push}")
HEAD_REF = ("symbolic-ref", "--short", "-q", "HEAD")
CONFIG_REF = ("config", "--list", "-z")
ORIGIN = "remote.origin.url\nhttps://github.com/owner/proj.git\0"
ON_PLAN = {PUSH_REF: f"refs/remotes/origin/{BRANCH}\n", HEAD_REF: f"{BRANCH}\n", CONFIG_REF: ORIGIN}
ON_MAIN = {PUSH_REF: "refs/remotes/origin/main\n", HEAD_REF: "main\n", CONFIG_REF: ORIGIN}


def decide(command, run=True, facts=None, git=None, cwd=CWD):
    plan = planguard.PlanSupport(Path(cwd), environ={"TRIMTAB_PLAN_RUN": "1"} if run else {},
                                 facts=facts if facts is not None else FakeFacts(pr_state=DRAFT, head=H1),
                                 git=fake_git(ON_PLAN if git is None else git))
    return guard_github.decide({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}, plan)


class Cases(unittest.TestCase):
    def refused(self, command, row, **kw):
        code, message = decide(command, **kw)
        self.assertEqual(code, guard_github.BLOCK, f"allowed: {command}")
        self.assertIn(row, message, command)

    def allowed(self, command, **kw):
        code, message = decide(command, **kw)
        self.assertEqual((code, message), (guard_github.ALLOW, ""), command)


class GitPush(Cases):
    def test_a_push_to_another_branch_reads_nothing(self):
        for cmd in ("git push origin feature/x", "git push -u origin feature/x", "git push origin HEAD:feature/x",
                    "git status", "git commit -m 'git push origin plan/x'", "git push --tags origin"):
            with self.subTest(cmd):
                self.allowed(cmd, facts=NoFacts(), git=ON_MAIN)

    def test_e14_a_plan_push_needs_a_plan_run(self):
        for cmd in (f"git push origin {BRANCH}", f"git push origin HEAD:{BRANCH}",
                    f"git push origin HEAD:refs/heads/{BRANCH}", "git push", "git push origin", "git push origin HEAD",
                    f"git -C /work push origin {BRANCH}", f"sudo git push origin {BRANCH}",
                    f"bash -c 'git push origin {BRANCH}'", f"echo hi && git push origin {BRANCH}",
                    f"git push origin PLAN/0008-x/item-01-part1", f"git push origin feature/x {BRANCH}"):
            with self.subTest(cmd):
                self.refused(cmd, "E14", run=False)

    def test_a_plan_run_may_push_a_draft_branch(self):
        for cmd in (f"git push origin {BRANCH}", "git push", "git push origin HEAD", f"git push -u origin {BRANCH}"):
            with self.subTest(cmd):
                self.allowed(cmd)

    def test_e15_force_refuses_in_a_run(self):
        for cmd in (f"git push --force origin {BRANCH}", f"git push -f origin {BRANCH}",
                    f"git push --force-with-lease origin {BRANCH}", f"git push --force-with-lease={BRANCH}:abc origin {BRANCH}",
                    f"git push --force-if-includes origin {BRANCH}", f"git push origin +{BRANCH}",
                    f"git push origin +HEAD:{BRANCH}", "git push --mirror origin", "git push -f"):
            with self.subTest(cmd):
                self.refused(cmd, "E15")

    def test_e16_delete_refuses_in_any_mode(self):
        for run in (True, False):
            for cmd in (f"git push origin :{BRANCH}", f"git push --delete origin {BRANCH}",
                        f"git push -d origin {BRANCH}", f"git push origin :refs/heads/{BRANCH}"):
                with self.subTest(cmd=cmd, run=run):
                    self.refused(cmd, "E16", run=run)

    def test_prune_refuses(self):
        self.refused("git push --prune origin", "E16")

    def test_e17_a_ready_pr_refuses_a_push(self):
        self.refused(f"git push origin {BRANCH}", "E17", facts=FakeFacts(pr_state=READY, head=H1))

    def test_e19_a_failed_scan_refuses_a_push(self):
        self.refused(f"git push origin {BRANCH}", "E19", facts=FakeFacts(pr_state=DRAFT, head=H1, scan="fail",
                                                                         changes=()))

    def test_e12_the_fourth_round_refuses_a_push(self):
        from trimtab.planrows import Push
        pushes = [Push(H1, "2" * 40), Push("2" * 40, "3" * 40), Push("3" * 40, "4" * 40)]
        facts = FakeFacts(pr_state=DRAFT, head="4" * 40, pushes=pushes, failing={H1, "2" * 40, "3" * 40, "4" * 40})
        self.refused(f"git push origin {BRANCH}", "E12", facts=facts)

    def test_an_unreadable_fact_refuses(self):
        for name in ("open_pr", "branch_head", "secret_scan", "pushes"):
            with self.subTest(name):
                self.refused(f"git push origin {BRANCH}", "unreadable",
                             facts=FakeFacts(pr_state=DRAFT, head=H1, broken=(name,)))

    def test_a_bare_push_that_cannot_resolve_refuses(self):
        self.refused("git push", "cannot read", git={})

    def test_a_bare_push_resolves_through_the_upstream(self):
        self.refused("git push", "E14", run=False, git=ON_PLAN)
        self.allowed("git push", facts=NoFacts(), git=ON_MAIN)

    def test_a_push_in_a_directory_the_guard_cannot_tell_refuses(self):
        self.refused(f"cd sub && git push origin {BRANCH}", "cannot tell")
        self.refused(f"git -C $D push origin {BRANCH}", "expansion")
        self.refused(f"git --git-dir=/x push origin {BRANCH}", "chosen by an option")

    def test_dynamic_spellings_refuse(self):
        for cmd in (f"$G push origin {BRANCH}", f"git push origin $B", f"git $CMD origin {BRANCH}",
                    "git push origin refs/heads/*:refs/heads/*", "git send-pack origin", "git http-push x"):
            with self.subTest(cmd):
                code, _ = decide(cmd, run=True, git=ON_MAIN)
                self.assertEqual(code, guard_github.BLOCK, cmd)

    def test_all_judges_each_local_plan_branch(self):
        self.allowed("git push --all origin", facts=FakeFacts(local=(), pr_state=DRAFT, head=H1))
        self.refused("git push --all origin", "E14", run=False, facts=FakeFacts(local=(BRANCH,)))

    def test_a_hidden_command_is_read_by_the_naive_pass(self):
        self.refused(f"echo 'git push --force origin {BRANCH}' | bash", "E15")

    def test_xargs_appending_to_a_push_refuses(self):
        self.refused("echo plan/x | xargs git push origin", "xargs")


class Rest(Cases):
    def api(self, method, path, *fields, **kw):
        flags = " ".join(f"-f '{f}'" for f in fields)
        return decide(f"gh api -X {method} {path} {flags}", **kw)

    def test_a_ref_create_naming_a_plan_branch(self):
        code, msg = self.api("POST", "repos/owner/proj/git/refs", f"ref=refs/heads/{BRANCH}", f"sha={H1}", run=False)
        self.assertEqual(code, guard_github.BLOCK)
        self.assertIn("E14", msg)

    def test_a_ref_create_of_another_branch_reads_nothing(self):
        self.assertEqual(self.api("POST", "repos/owner/proj/git/refs", "ref=refs/heads/feature/x", f"sha={H1}",
                                  facts=NoFacts())[0], guard_github.ALLOW)

    def test_a_ref_update_forces(self):
        code, msg = self.api("PATCH", f"repos/owner/proj/git/refs/heads/{BRANCH}", f"sha={H1}", "force=true")
        self.assertEqual(code, guard_github.BLOCK)
        self.assertIn("E15", msg)

    def test_a_ref_update_without_force_is_judged_like_a_push(self):
        self.assertEqual(self.api("PATCH", f"repos/owner/proj/git/refs/heads/{BRANCH}", f"sha={H1}")[0], guard_github.ALLOW)
        code, msg = self.api("PATCH", f"repos/owner/proj/git/refs/heads/{BRANCH}", f"sha={H1}", run=False)
        self.assertIn("E14", msg)

    def test_a_ref_delete(self):
        for run in (True, False):
            code, msg = decide(f"gh api -X DELETE repos/owner/proj/git/refs/heads/{BRANCH}", run=run)
            self.assertEqual(code, guard_github.BLOCK)
            self.assertIn("E16", msg)

    def test_a_ref_delete_in_the_case_the_branch_has(self):
        # The normalised path is lower case; the branch is looked up as spelled.
        facts = FakeFacts(pr_state=DRAFT, head=H1)
        decide("gh api -X PATCH repos/owner/proj/git/refs/heads/plan/0008-Slug/item-02-part1 -f sha=" + H1, facts=facts)
        self.assertIn("open_pr", facts.reads)

    def test_a_contents_write_on_a_plan_branch(self):
        code, msg = self.api("PUT", "repos/owner/proj/contents/a.txt", f"branch={BRANCH}", "message=m", run=False)
        self.assertIn("E14", msg)
        self.assertEqual(self.api("PUT", "repos/owner/proj/contents/a.txt", "branch=feature/x", "message=m",
                                  facts=NoFacts())[0], guard_github.ALLOW)

    def test_a_contents_write_to_a_ready_pr_refuses(self):
        code, msg = self.api("PUT", "repos/owner/proj/contents/a.txt", f"branch={BRANCH}", "message=m",
                             facts=FakeFacts(pr_state=READY, head=H1))
        self.assertIn("E17", msg)

    def test_a_merge_into_a_plan_branch(self):
        code, msg = self.api("POST", "repos/owner/proj/merges", f"base={BRANCH}", "head=main", run=False)
        self.assertIn("E14", msg)

    def test_a_rename_of_a_plan_branch(self):
        for run in (True, False):
            code, msg = decide(f"gh api -X POST repos/owner/proj/branches/{BRANCH}/rename -f new_name=x", run=run)
            self.assertIn("E16", msg)

    def test_a_rename_onto_a_plan_branch_is_a_plan_write(self):
        code, msg = self.api("POST", "repos/owner/proj/branches/feature/rename", f"new_name={BRANCH}", run=False)
        self.assertIn("E14", msg)

    def test_update_branch_of_a_plan_pr(self):
        facts = FakeFacts(pr_state=DRAFT, head=H1, heads={7: BRANCH})
        # A sourceless write carries no commit, so the ledger could not record it and E13 could never
        # resume the branch: 2b refuses it in a run as well (ADR 0063 §1).
        code, msg = self.api("PUT", "repos/owner/proj/pulls/7/update-branch", facts=facts)
        self.assertEqual((code, "ledger" in msg), (guard_github.BLOCK, True))
        code, msg = self.api("PUT", "repos/owner/proj/pulls/7/update-branch", run=False, facts=facts)
        self.assertIn("E14", msg)

    def test_closing_a_plan_pr(self):
        facts = FakeFacts(heads={7: BRANCH, 8: "feature/x"})
        for path in ("pulls", "issues"):
            code, msg = self.api("PATCH", f"repos/owner/proj/{path}/7", "state=closed", facts=facts)
            self.assertEqual(code, guard_github.BLOCK, path)
            self.assertIn("E16", msg)
            self.assertEqual(self.api("PATCH", f"repos/owner/proj/{path}/8", "state=closed", facts=facts)[0],
                             guard_github.ALLOW)
            self.assertEqual(self.api("PATCH", f"repos/owner/proj/{path}/7", "title=t", facts=facts)[0], guard_github.ALLOW)

    def test_closing_a_pr_whose_lookup_fails_refuses(self):
        code, msg = self.api("PATCH", "repos/owner/proj/pulls/7", "state=closed",
                             facts=FakeFacts(heads={7: BRANCH}, broken=("pr_head",)))
        self.assertEqual(code, guard_github.BLOCK)
        self.assertIn("unreadable", msg)

    def test_reads_are_not_writes(self):
        self.assertEqual(decide(f"gh api repos/owner/proj/git/refs/heads/{BRANCH}", run=False, facts=NoFacts())[0],
                         guard_github.ALLOW)


class GhCommands(Cases):
    def facts(self):
        return FakeFacts(heads={7: BRANCH, 8: "feature/x"}, pr_state=DRAFT, head=H1)

    def test_pr_close(self):
        for cmd in ("gh pr close 7", "gh pr close #7", "gh pr close https://github.com/o/r/pull/7",
                    f"gh pr close {BRANCH}", f"gh pr close o:{BRANCH}", "gh pr close -R o/r 7 --comment x",
                    "gh pr -R o/r close 7", "gh pr close", "gh pr close --delete-branch 7"):
            with self.subTest(cmd):
                self.refused(cmd, "E16", facts=self.facts(), git=ON_PLAN)

    def test_pr_close_of_another_pr_passes(self):
        for cmd in ("gh pr close 8", "gh pr close feature/x", "gh pr close"):
            with self.subTest(cmd):
                self.allowed(cmd, facts=self.facts(), git=ON_MAIN)

    def test_pr_merge_delete_branch(self):
        for cmd in ("gh pr merge 7 --delete-branch", "gh pr merge 7 -d", "gh pr merge 7 --squash -d"):
            with self.subTest(cmd):
                self.refused(cmd, "E16", facts=self.facts())
        self.allowed("gh pr merge 7 --squash", facts=self.facts())
        self.allowed("gh pr merge 8 -d", facts=self.facts())

    def test_pr_update_branch_is_a_plan_write(self):
        self.refused("gh pr update-branch 7", "E14", run=False, facts=self.facts())
        self.refused("gh pr update-branch 7", "ledger", facts=self.facts())  # sourceless: see Rest above
        self.allowed("gh pr update-branch 8", run=False, facts=self.facts())

    def test_issue_close_of_a_plan_pr(self):
        self.refused("gh issue close 7", "E16", facts=self.facts())
        self.allowed("gh issue close 9", facts=self.facts())

    def test_a_selector_the_guard_cannot_read_refuses(self):
        self.refused("gh pr close $N", "expansion", facts=self.facts())
        self.refused("gh pr close -R $X 7", "repository", facts=self.facts())

    def test_a_close_on_the_current_branch_in_an_unknown_directory_refuses(self):
        self.refused("cd sub && gh pr close", "current branch", facts=self.facts(), git=ON_PLAN)


class Graphql(Cases):
    def mutation(self, query):
        return f"gh api graphql -f query='{query}'"

    def test_ref_and_commit_mutations_naming_a_plan_branch(self):
        for query in ('mutation { createRef(input:{name:"refs/heads/' + BRANCH + '"}) { ref { id } } }',
                      'mutation { createCommitOnBranch(input:{branch:{branchName:"' + BRANCH + '"}}) { commit { oid } } }'):
            with self.subTest(query):
                self.refused(self.mutation(query), "E14", facts=NoFacts())

    def test_a_ref_mutation_naming_another_branch_passes(self):
        self.allowed(self.mutation('mutation { createRef(input:{name:"refs/heads/feature/x"}) { ref { id } } }'),
                     facts=NoFacts())

    def test_closing_a_pull_request_refuses(self):
        self.refused(self.mutation('mutation { closePullRequest(input:{pullRequestId:"PR_x"}) { clientMutationId } }'),
                     "E16", facts=NoFacts())


class TheExistingRulesStandAlone(Cases):
    def test_a_draft_pr_still_opens(self):
        # From a branch that is not a plan part: E23 then reads nothing (ADR 0063 §5).
        self.allowed("gh pr create --draft --title t", facts=NoFacts(), git=ON_MAIN)

    def test_an_approving_review_still_refuses(self):
        code, msg = decide("gh pr review 7 --approve", facts=NoFacts())
        self.assertEqual(code, guard_github.BLOCK)

    def test_main_still_cannot_be_written_through_the_api(self):
        code, msg = decide("gh api -X PATCH repos/owner/proj/git/refs/heads/main -f sha=" + H1, facts=NoFacts())
        self.assertEqual(code, guard_github.BLOCK)


if __name__ == "__main__":
    unittest.main()
