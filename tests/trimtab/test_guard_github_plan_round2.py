"""Spellings found by review round 2 of PR 166, each now refused (or, for ordinary commands, still allowed).

Every case fails with its fix reverted. GitHub and git are injected (TST-2)."""

import unittest
from pathlib import Path

from test_guard_github_plan import CONFIG_REF, HEAD_REF, ON_MAIN, PUSH_REF, NoFacts
from test_guard_github_plan_bypasses import REPO, Cases, config, with_config
from test_planfacts import INSTANCE, gh_404
from test_planrows import BRANCH, DRAFT, H1, FakeFacts
from trimtab import planfacts, planguard
from trimtab.capture.ghapi import GhApi
from trimtab.enforcement import tools
from trimtab.planrows import FactsError


class GraphqlLookingRoute(Cases):
    def test_a_rest_route_ending_in_graphql_is_still_rest(self):
        self.refused(f"gh api -X PUT {REPO}/contents/graphql -f message=m -f content=aGk= -f branch={BRANCH}",
                     "E14", run=False)
        self.refused(f"gh api -X DELETE {REPO}/git/refs/heads/{BRANCH}/graphql", "E16", facts=NoFacts())

    def test_a_bare_graphql_path_is_still_graphql(self):
        self.refused("gh api graphql -f query='mutation { closePullRequest(input:{pullRequestId:\"x\"}) { id } }'",
                     "E16", facts=NoFacts())


class DirectoryAndEnvironmentAnywhere(Cases):
    def test_a_directory_change_in_nested_text_hides_the_checkout(self):
        for cmd in ("bash -c 'cd /tmp/e && git push'", "env -C /tmp/e git push origin HEAD",
                    "bash -c 'cd /tmp/e; git push origin HEAD'", "env --chdir=/tmp/e git push origin HEAD"):
            with self.subTest(cmd):
                self.refused(cmd, "cannot read", git=ON_MAIN, facts=NoFacts())

    def test_environment_that_steers_git_is_seen_in_any_segment(self):
        for cmd in ("export GIT_DIR=/x; git push origin feature/x",
                    "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push; git push origin feature/x",
                    "HOME=/tmp/h git push origin feature/x", "export XDG_CONFIG_HOME=/tmp/x; git push origin feature/x",
                    "bash -c 'GIT_DIR=/x git push origin feature/x'"):
            with self.subTest(cmd):
                self.refused(cmd, "configuration", git=ON_MAIN, facts=NoFacts())

    def test_a_push_after_a_cd_that_names_no_plan_branch_passes(self):
        self.allowed("cd /repo && git push origin feature/x", git=ON_MAIN, facts=NoFacts())
        self.allowed("cd /repo && git push origin feature/x:feature/x", git=ON_MAIN, facts=NoFacts())

    def test_a_push_after_a_cd_that_names_a_plan_branch_refuses(self):
        self.refused(f"cd /repo && git push origin {BRANCH}", "cannot tell", git=ON_MAIN)

    def test_a_bare_push_after_a_cd_refuses_because_nothing_resolves(self):
        self.refused("cd /repo && git push", "cannot read", git=ON_MAIN, facts=NoFacts())


class BuiltUpGit(Cases):
    def test_git_and_the_subcommand_both_assembled_in_the_command(self):
        self.refused("G=git P=pus; $G ${P}h origin HEAD", "words it builds", git=ON_MAIN)


class QueryStrings(Cases):
    def test_a_write_to_a_plan_route_with_a_query_string_refuses(self):
        for cmd in (f"gh api -X PATCH '{REPO}/pulls/5?state=closed'",
                    f"gh api -X PATCH '{REPO}/git/refs/heads/{BRANCH}?sha={H1}&force=true'",
                    f"gh api -X POST '{REPO}/git/refs?ref=refs/heads/{BRANCH}&sha={H1}'"):
            with self.subTest(cmd):
                self.refused(cmd, "query string", facts=NoFacts())

    def test_a_read_with_a_query_string_passes(self):
        self.allowed(f"gh api '{REPO}/pulls?state=open'", facts=NoFacts())
        self.allowed(f"gh api '{REPO}/git/refs/heads/{BRANCH}?x=1'", facts=NoFacts())


class TrackedBranchSelector(Cases):
    def test_gh_without_a_selector_follows_the_branch_upstream(self):
        git = {PUSH_REF: "refs/remotes/origin/foo\n", HEAD_REF: "foo\n",
               CONFIG_REF: config(("branch.foo.merge", f"refs/heads/{BRANCH}"))}
        self.refused("gh pr close", "E16", git=git, facts=FakeFacts())

    def test_a_branch_without_an_upstream_is_its_own_pr(self):
        git = {PUSH_REF: "refs/remotes/origin/foo\n", HEAD_REF: "foo\n", CONFIG_REF: config()}
        self.allowed("gh pr close", git=git, facts=FakeFacts())


class RefspecMapping(Cases):
    def test_a_refspec_with_no_destination_is_mapped_by_remote_push(self):
        git = with_config(("remote.origin.push", f"+refs/heads/main:refs/heads/{BRANCH}"), base=ON_MAIN)
        self.refused("git push origin main", "configuration", git=git, facts=NoFacts())

    def test_without_that_configuration_it_passes(self):
        self.allowed("git push origin main", git=with_config(base=ON_MAIN), facts=NoFacts())


class AliasOnTheCommand(Cases):
    def test_an_alias_defined_with_dash_c_may_stand_for_a_push(self):
        for cmd in (f"git -c alias.p=push p origin +HEAD:{BRANCH}", "git -c alias.p=push p origin feature/x",
                    f"git --config-env=alias.p=X p origin {BRANCH}"):
            with self.subTest(cmd):
                self.refused(cmd, "alias", git=ON_MAIN, facts=NoFacts())


class GraphqlMerges(Cases):
    def test_merging_a_pull_request_by_id_refuses(self):
        for mutation in ('mutation { mergePullRequest(input:{pullRequestId:"PR_x"}) { clientMutationId } }',
                         'mutation { enablePullRequestAutoMerge(input:{pullRequestId:"PR_x"}) { clientMutationId } }'):
            with self.subTest(mutation):
                self.refused(f"gh api graphql -f query='{mutation}'", "E16", facts=NoFacts())


class EffectiveConfigIsRead(unittest.TestCase):
    def test_global_configuration_reaches_the_steering_check(self):
        # An injected git answers for itself; the real reader drops GIT_CONFIG_GLOBAL and GIT_CONFIG_NOSYSTEM.
        seen = []

        class Recorder:
            def __init__(self, **kw):
                self.env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin"}

            def check(self, cwd):
                pass

            def argv(self, cwd, *args):
                return ["git", *args]

        real_run = planguard.subprocess.run

        def run(argv, **kw):
            seen.append(kw["env"])
            return planguard.subprocess.CompletedProcess(argv, 0, b"remote.origin.push\n+x:y\0", b"")

        planguard.subprocess.run = run
        original = planguard.tools.Git
        planguard.tools.Git = Recorder
        try:
            got = planguard.PlanSupport(Path("/w"), environ={"HOME": "/home/u"}).config(Path("/w"))
        finally:
            planguard.subprocess.run = real_run
            planguard.tools.Git = original
        self.assertEqual(got, {"remote.origin.push": ["+x:y"]})
        self.assertNotIn("GIT_CONFIG_GLOBAL", seen[0])
        self.assertNotIn("GIT_CONFIG_NOSYSTEM", seen[0])
        self.assertEqual(seen[0]["HOME"], "/home/u")


class LazinessAndConfigParsing(unittest.TestCase):
    def test_facts_are_not_built_until_a_row_reads_one(self):
        built = []
        lazy = planguard.LazyFacts(Path("/w"), build=lambda cwd: built.append(cwd) or FakeFacts(repo="o/r"))
        self.assertEqual(built, [])
        self.assertEqual(lazy.repository(), "o/r")
        lazy.repository()
        self.assertEqual(built, [Path("/w")])

    def test_a_failed_build_surfaces_at_the_read(self):
        def broken(cwd):
            raise FactsError("no gh")
        with self.assertRaises(FactsError):
            planguard.LazyFacts(Path("/w"), build=broken).repository()

    def test_config_parsing(self):
        got = planguard.parse_config("Remote.Origin.Mirror\ntrue\0push.default\nmatching\0core.bare\0remote.o.url\na\nb\0")
        self.assertEqual(got, {"remote.origin.mirror": ["true"], "push.default": ["matching"], "core.bare": [""],
                               "remote.o.url": ["a\nb"]})

    def test_a_bare_key_and_integers_are_true(self):
        self.assertTrue(planguard._true([""]))
        self.assertTrue(planguard._true(["yes"]))
        self.assertFalse(planguard._true(["false"]))
        self.assertFalse(planguard._true([]))


class NotFoundIsATypedError(unittest.TestCase):
    def facts(self, missing):
        return planfacts.GitHubFacts("owner/proj", GhApi(gh_404(missing)), tools.Git(), Path("."), INSTANCE)

    def test_reads_that_cannot_be_absent_turn_a_404_into_a_facts_error(self):
        f = self.facts({"repos/owner/proj/pulls", "repos/owner/proj/pulls/7", "repos/owner/proj/activity",
                        "repos/owner/proj/actions/runs"})
        for read in (lambda: f.open_pr(BRANCH), lambda: f.conflicted(7), lambda: f.failing_heads(BRANCH)):
            with self.subTest(read), self.assertRaises(FactsError):
                read()


if __name__ == "__main__":
    unittest.main()
