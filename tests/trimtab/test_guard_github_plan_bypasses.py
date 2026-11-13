"""Spellings that once got a `plan/*` write past guard-github (review round 1 of PR 166), each now refused.

Every case here fails with its fix reverted. Facts and git are injected (TST-2): GitHub is a true external
system, and the git answers stand for a checkout's config."""

import unittest
from pathlib import Path

from test_guard_github_plan import CONFIG_REF, HEAD_REF, ON_MAIN, ON_PLAN, ORIGIN, PUSH_REF, NoFacts, decide
from test_planrows import BRANCH, DRAFT, H1, FakeFacts
import plan_fixture
from trimtab import planguard, planrows
from trimtab.adapters import guard_github


def setUpModule():
    plan_fixture.set_up()


def tearDownModule():
    plan_fixture.tear_down()

PLAN = {**ON_PLAN}
REPO = "repos/owner/proj"


def config(*lines):
    return "".join(f"{k}\n{v}\0" for k, v in (("remote.origin.url", "https://github.com/owner/proj.git"), *lines))


def with_config(*lines, base=None):
    return {**(base or ON_PLAN), CONFIG_REF: config(*lines)}


class Cases(unittest.TestCase):
    def refused(self, command, text, **kw):
        code, message = decide(command, **kw)
        self.assertEqual(code, guard_github.BLOCK, f"allowed: {command}")
        self.assertIn(text, message, command)

    def allowed(self, command, **kw):
        code, message = decide(command, **kw)
        self.assertEqual((code, message), (guard_github.ALLOW, ""), command)


class HiddenCommandWords(Cases):
    def test_git_and_push_both_held_in_variables(self):
        for cmd in (f"G=git; S=push; $G $S origin HEAD:{BRANCH}", f'G=git; S=push; $G "$S" origin {BRANCH}',
                    "G=git; S=push; $G $S origin $B"):
            with self.subTest(cmd):
                self.refused(cmd, "words it builds", git=ON_MAIN)

    def test_an_unrelated_command_with_variables_is_not_refused(self):
        self.allowed("$PYTHON $SCRIPT --flag", facts=NoFacts())

    def test_a_brace_list_that_spells_the_command_refuses(self):
        self.refused(f"{{git,push}} origin {BRANCH}", "words it builds", git=ON_MAIN)

    def test_the_binary_behind_the_push_subcommand_is_judged_as_a_push(self):
        self.refused(f"/usr/lib/git-core/git-push origin {BRANCH}", "E14", run=False)
        self.allowed(f"/usr/lib/git-core/git-push origin {BRANCH}")
        self.refused("git-send-pack origin x", "does not read", git=ON_MAIN)

    def test_routes_that_name_no_plan_write_keep_their_variables(self):
        self.allowed(f"gh api repos/$R/pulls/$N/reviews -f event=COMMENT -f body=x", facts=NoFacts())
        self.allowed(f"gh api -X POST repos/$O/$R/issues/$N/comments -f body=x", facts=NoFacts())


class DynamicMethod(Cases):
    def test_a_method_the_shell_builds_refuses(self):
        for cmd in (f'M=DELETE; gh api -X "$M" {REPO}/git/refs/heads/{BRANCH}',
                    f'gh api -X $M {REPO}/git/refs/heads/{BRANCH} -f sha={H1}',
                    f'gh api --method "$M" {REPO}/contents/a.txt -f branch={BRANCH}'):
            with self.subTest(cmd):
                self.refused(cmd, "method", facts=NoFacts())


class GitConfigSteering(Cases):
    def test_inline_configuration_that_steers_a_push_refuses(self):
        for cmd in ("git -c remote.origin.push=+refs/heads/*:refs/heads/* push",
                    "git -c remote.origin.mirror=true push origin feature/x",
                    "git -c push.default=matching push", "git --config-env=remote.origin.push=V push",
                    "git -c url.https://x/.insteadOf=https://github.com/ push origin feature/x",
                    "git -c $K=v push origin feature/x"):
            with self.subTest(cmd):
                self.refused(cmd, "configuration", git=ON_MAIN, facts=NoFacts())

    def test_unrelated_inline_configuration_passes(self):
        self.allowed("git -c user.name=x push origin feature/x", git=ON_MAIN, facts=NoFacts())

    def test_environment_that_steers_git_refuses(self):
        for cmd in ("GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0=x git push",
                    "GIT_DIR=/elsewhere git push origin feature/x", "GIT_SSH_COMMAND=x git push origin feature/x"):
            with self.subTest(cmd):
                self.refused(cmd, "configuration", git=ON_MAIN, facts=NoFacts())

    def test_a_mirror_remote_refuses_any_push(self):
        git = with_config(("remote.origin.mirror", "true"), base=ON_MAIN)
        self.refused("git push origin feature/x", "E15", git=git, facts=NoFacts())
        self.refused("git push", "E15", git=git, facts=NoFacts())

    def test_a_remote_that_is_not_a_mirror_passes(self):
        git = with_config(("remote.origin.mirror", "false"), base=ON_MAIN)
        self.allowed("git push origin feature/x", git=git, facts=NoFacts())

    def test_push_refspecs_in_config_refuse_a_push_with_no_destination(self):
        git = with_config(("remote.origin.push", "+HEAD:refs/heads/plan/x"), base=ON_MAIN)
        self.refused("git push", "configuration", git=git, facts=NoFacts())
        self.refused("git push origin feature/x", "configuration", git=git, facts=NoFacts())

    def test_push_refspecs_in_config_leave_a_push_with_a_destination_alone(self):
        git = with_config(("remote.origin.push", "+HEAD:refs/heads/plan/x"), base=ON_MAIN)
        self.allowed("git push origin feature/x:feature/x", git=git, facts=NoFacts())

    def test_matching_refuses_a_bare_push_when_plan_branches_exist(self):
        git = with_config(("push.default", "matching"), base=ON_MAIN)
        self.refused("git push", "matching", git=git, facts=FakeFacts(local=(BRANCH,)))
        self.allowed("git push", git=git, facts=FakeFacts(local=()))

    def test_a_pushurl_to_another_repository_refuses_a_plan_push(self):
        git = with_config(("remote.origin.pushurl", "https://github.com/me/fork.git"))
        git[CONFIG_REF] = ORIGIN.replace("\0", "") and (
            "remote.origin.url\nhttps://github.com/owner/proj.git\0remote.origin.pushurl\nhttps://github.com/me/fork.git\0")
        self.refused(f"git push origin {BRANCH}", "other than", git=git)

    def test_a_push_to_another_remote_or_url_refuses_when_it_is_a_plan_push(self):
        self.refused(f"git push https://github.com/me/fork.git {BRANCH}", "other than")
        git = {**ON_PLAN, CONFIG_REF: config(("remote.fork.url", "https://github.com/me/fork.git"))}
        self.refused(f"git push fork {BRANCH}", "other than", git=git)

    def test_url_rewrites_refuse_a_plan_push(self):
        git = with_config(("url.https://x/.insteadof", "https://github.com/"))
        self.refused(f"git push origin {BRANCH}", "rewrites", git=git)

    def test_a_pushurl_does_not_touch_a_non_plan_push(self):
        git = with_config(("remote.origin.pushurl", "git@example.com:me/x.git"), base=ON_MAIN)
        self.allowed("git push origin feature/x", git=git, facts=NoFacts())

    def test_unreadable_configuration_in_a_repository_refuses(self):
        def broken(cwd, *args):
            if args == CONFIG_REF:
                raise planguard.tools.ToolError("config unreadable")
            return "x" if args[0] == "rev-parse" else ""
        plan = planguard.PlanSupport(Path("/work"), environ={}, facts=NoFacts(), git=broken)
        code, message = guard_github.decide({"tool_name": "Bash", "cwd": "/work",
                                             "tool_input": {"command": "git push origin feature/x"}}, plan)
        self.assertEqual(code, guard_github.BLOCK)
        self.assertIn("configuration", message)


class AllWithForce(Cases):
    def test_force_and_delete_carry_into_each_branch(self):
        # `-n` is no longer a flag to pass over: a dry run writes nothing the ledger could record, so it
        # is unreadable before any row is reached (ADR 0063 §1, Amendment 3).
        for opt in ("-f", "--force", "--force-with-lease", "-qf", "--delete"):
            with self.subTest(opt):
                self.refused(f"git push --all {opt} origin", "E1", facts=FakeFacts(local=(BRANCH,), pr_state=DRAFT, head=H1))

    def test_a_dry_run_is_judged_like_any_push_and_never_recorded(self):
        # It writes nothing, so no ledger entry may vouch for it; every row still reads it, so a dry
        # run of a push the rows would refuse is refused too (E14 here, with no plan run).
        for opt in ("-n", "--dry-run"):
            with self.subTest(opt):
                self.refused(f"git push {opt} origin {BRANCH}", "E14", run=False,
                             facts=FakeFacts(pr_state=DRAFT, head=H1))
                self.allowed(f"git push {opt} origin {BRANCH}", facts=FakeFacts(pr_state=DRAFT, head=H1))

    def test_a_dry_run_reaches_the_record_callback_never_and_a_real_push_always(self):
        from pathlib import Path
        from test_planrows import FakeFacts as Facts
        from trimtab import planguard

        def judged(args):
            seen = []
            support = planguard.PlanSupport(Path("/work"), environ={"TRIMTAB_PLAN_RUN": "1"},
                                            facts=Facts(pr_state=DRAFT, head=H1),
                                            git=lambda cwd, *a: ON_PLAN[a])
            planguard.judge_push([(w, False) for w in args], support, Path("/work"),
                                 record=lambda sup, branch, source: seen.append(branch))
            return seen

        self.assertEqual(judged(["origin", BRANCH]), [BRANCH])
        self.assertEqual(judged(["--dry-run", "origin", BRANCH]), [])
        self.assertEqual(judged(["-n", "origin", BRANCH]), [])

    def test_a_dry_run_of_another_branch_still_passes(self):
        for opt in ("-n", "--dry-run"):
            with self.subTest(opt):
                self.allowed(f"git push {opt} origin feature/x", facts=NoFacts(), git=ON_MAIN)

    def test_all_is_refused_once_a_local_plan_branch_exists(self):
        # ADR 0063 §1: a broad write names refs this reading cannot list one by one, so no ledger entry
        # could cover them and E13 would read the branch as holding work no run sent. 2b tightened this.
        self.refused("git push --all origin", "ledger",
                     facts=FakeFacts(local=(BRANCH,), pr_state=DRAFT, head=H1))

    def test_all_still_passes_where_no_local_plan_branch_exists(self):
        self.allowed("git push --all origin", facts=FakeFacts(local=(), pr_state=DRAFT, head=H1))


class DynamicFirstWord(Cases):
    def test_a_variable_in_the_remote_slot_may_be_an_option(self):
        for cmd in (f"F=--force; git push $F origin {BRANCH}", f"git push $F origin HEAD:{BRANCH}", "git push $F"):
            with self.subTest(cmd):
                self.refused(cmd, "built by the shell", git=ON_PLAN)

    def test_a_variable_remote_may_split_into_refspecs_and_refuses(self):
        self.refused("git push $REMOTE feature/x", "built by the shell", git=ON_MAIN, facts=NoFacts())


class RouteOrder(Cases):
    def test_a_contents_path_that_looks_like_a_ref_route_is_still_contents(self):
        self.refused(f"gh api -X PUT {REPO}/contents/git/refs/heads/a -f branch={BRANCH} -f message=m -f content=Zg==",
                     "E14", run=False)

    def test_another_repository_in_the_path_is_refused_for_a_plan_ref(self):
        self.refused(f"gh api -X PATCH repos/other/repo/git/refs/heads/{BRANCH} -f sha={H1}", "cannot be judged")

    def test_cheap_rows_refuse_before_any_repository_lookup(self):
        self.refused(f"gh api -X DELETE repos/other/repo/git/refs/heads/{BRANCH}", "E16", facts=NoFacts())


class DynamicNumbers(Cases):
    def test_a_pr_number_the_shell_builds_refuses(self):
        for cmd in (f"N=5; gh api -X PATCH {REPO}/pulls/$N -f state=closed",
                    f"gh api -X PATCH {REPO}/issues/$N -f state=closed",
                    f"gh api -X PUT {REPO}/pulls/$N/update-branch", f"gh api -X PUT {REPO}/pulls/$N/merge"):
            with self.subTest(cmd):
                self.refused(cmd, "shell expansion", facts=NoFacts())


class DynamicNumberEdits(Cases):
    def test_an_edit_that_does_not_close_keeps_its_variable(self):
        self.allowed(f"gh api -X PATCH {REPO}/pulls/$N -f body=x", facts=NoFacts())
        self.allowed(f"gh api -X PATCH {REPO}/issues/$N -f state=open", facts=NoFacts())


class Graphql(Cases):
    def mutation(self, query):
        return f"gh api graphql -f query='{query}'"

    def test_update_pull_request_with_a_state_closes_a_pr(self):
        self.refused(self.mutation('mutation { updatePullRequest(input:{pullRequestId:"PR_x", state: CLOSED}) '
                                   '{ clientMutationId } }'), "E16", facts=NoFacts())

    def test_update_pull_request_without_a_state_passes(self):
        self.allowed(self.mutation('mutation { updatePullRequest(input:{pullRequestId:"PR_x", title:"t"}) '
                                   '{ clientMutationId } }'), facts=NoFacts())

    def test_update_pull_request_branch_writes_the_head(self):
        self.refused(self.mutation('mutation { updatePullRequestBranch(input:{pullRequestId:"PR_x"}) '
                                   '{ clientMutationId } }'), "E16", facts=NoFacts())

    def test_a_string_escape_cannot_hide_a_branch_name(self):
        self.refused(self.mutation('mutation { createRef(input:{name:"refs/heads/\\u0070lan/x"}) { ref { id } } }'),
                     "escapes", facts=NoFacts())


class MergeIntoAPlanBase(Cases):
    def facts(self, **kw):
        return FakeFacts(pr_state=planrows.PrState(7, True, "", BRANCH), head=H1,
                         heads={5: "feature/x", 6: "feature/y"}, bases={5: BRANCH, 6: "main"}, **kw)

    def test_gh_pr_merge_by_number(self):
        self.refused("gh pr merge 5 --squash", "E14", run=False, facts=self.facts())
        self.allowed("gh pr merge 6 --squash", facts=self.facts())

    def test_gh_pr_merge_by_branch(self):
        facts = FakeFacts(pr_state=planrows.PrState(7, True, "", BRANCH), head=H1)
        self.refused("gh pr merge feature/x", "E14", run=False, facts=facts)

    def test_rest_merge(self):
        self.refused(f"gh api -X PUT {REPO}/pulls/5/merge", "E14", run=False, facts=self.facts())
        self.allowed(f"gh api -X PUT {REPO}/pulls/6/merge", facts=self.facts())

    def test_a_failed_lookup_refuses_the_merge(self):
        self.refused("gh pr merge 5", "unreadable", facts=self.facts(broken=("pr_base",)))


class ChecksoutFollowsC(unittest.TestCase):
    def test_facts_are_built_for_the_directory_the_push_runs_in(self):
        support = planguard.PlanSupport(Path("/a"), environ={})
        self.assertEqual(support.facts_for(Path("/b"))._cwd, Path("/b"))
        self.assertIs(support.facts_for(Path("/b")), support.facts_for(Path("/b")))
        self.assertIs(support.facts_for(Path("/a")), support.facts)

    def test_injected_facts_stand_for_every_directory(self):
        facts = FakeFacts()
        support = planguard.PlanSupport(Path("/a"), environ={}, facts=facts)
        self.assertIs(support.facts_for(Path("/b")), facts)


if __name__ == "__main__":
    unittest.main()
