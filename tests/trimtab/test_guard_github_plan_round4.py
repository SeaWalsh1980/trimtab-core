"""Round 3's HIGH findings on PR 166, each fixed and each failing without its fix.

GitHub and git are injected (TST-2)."""

import unittest

from test_guard_github_plan import CONFIG_REF, HEAD_REF, ON_MAIN, PUSH_REF, NoFacts
from test_guard_github_plan_bypasses import REPO, Cases, config, with_config
from test_planrows import BRANCH, DRAFT, H1, FakeFacts
import plan_fixture
from trimtab import planguard


def setUpModule():
    plan_fixture.set_up()


def tearDownModule():
    plan_fixture.tear_down()

PLAN_UPSTREAM = f"refs/heads/{BRANCH}"


class OrdinaryArgumentsAreNotRefused(Cases):
    def test_dynamic_words_in_argument_position_pass(self):
        for cmd in ("git add src/*.py tests/*.py", 'git diff "$A" "$B"', 'git commit -m "$M" -m "$B"',
                    'git mv "$a" "$b"', "ls *.py *.md; git status", "git log $A..$B"):
            with self.subTest(cmd):
                self.allowed(cmd, git=ON_MAIN, facts=NoFacts())


class CommandWordsAssembledFromVariables(Cases):
    def test_a_command_built_from_fragments_defined_in_the_command(self):
        self.refused("A=gi B=t C=pu D=sh E=pla F=n/x; $A$B $C$D origin $E$F", "words it builds", git=ON_MAIN)

    def test_a_dynamic_command_word_alone_with_an_assignment_passes(self):
        self.allowed("FOO=1 $TOOL", facts=NoFacts())
        self.allowed("$PYTHON $SCRIPT --flag", facts=NoFacts())


class EarlierPartsOfTheSameCommand(Cases):
    def test_a_switch_before_a_push_of_head(self):
        self.refused(f"git switch -c {BRANCH} && git push origin HEAD", "earlier part", git=ON_MAIN)
        self.refused(f"git checkout -b {BRANCH}; git push", "earlier part", git=ON_MAIN)

    def test_a_head_change_that_touches_no_configuration(self):
        self.refused(f"git symbolic-ref HEAD refs/heads/{BRANCH} && git push origin HEAD", "earlier part", git=ON_MAIN)
        self.refused(f"git symbolic-ref HEAD refs/heads/{BRANCH} && git push", "earlier part", git=ON_MAIN)

    def test_a_config_write_before_a_push(self):
        self.refused("git config remote.origin.push HEAD:refs/heads/plan/x && git push origin", "earlier part",
                     git=ON_MAIN)
        self.refused("git config remote.origin.push +main:refs/heads/plan/x && git push origin main", "earlier part",
                     git=ON_MAIN)
        self.refused("git remote set-url origin https://example.invalid/x.git && git push", "earlier part", git=ON_MAIN)

    def test_a_commit_before_a_push_that_names_a_plan_branch(self):
        self.refused(f"git commit -am x && git push origin HEAD:{BRANCH}", "earlier part", git=ON_MAIN)

    def test_a_commit_before_an_ordinary_push_passes(self):
        for cmd in ("git commit -m x && git push origin feature/x", "git add -A && git commit -m x && git push origin HEAD",
                    "git add -A && git commit -m x && git push origin feature/x:feature/x"):
            with self.subTest(cmd):
                self.allowed(cmd, git=ON_MAIN, facts=NoFacts())

    def test_stability_is_read_from_the_command(self):
        self.assertEqual(planguard.stability_of("git status && git push"), planguard.STABLE)
        self.assertFalse(planguard.stability_of("git switch x && git push").name)
        self.assertFalse(planguard.stability_of("git commit -m x && git push").content)
        self.assertFalse(planguard.stability_of("git config a b && git push").config)


class GhPlaceholders(Cases):
    def test_a_placeholder_gh_fills_in_on_a_plan_route_refuses(self):
        for cmd in (f"git checkout {BRANCH} && gh api -X PATCH repos/{{owner}}/{{repo}}/git/refs/heads/{{branch}} -F sha={H1} -F force=true",
                    f"gh api -X PUT repos/{{owner}}/{{repo}}/contents/f -F branch={{branch}} -f message=m"):
            with self.subTest(cmd):
                self.refused(cmd, "gh fills", facts=NoFacts())

    def test_a_placeholder_on_a_route_that_writes_no_branch_passes(self):
        self.allowed("gh api -X POST repos/{owner}/{repo}/issues -f title=t", facts=NoFacts())
        self.allowed("gh api repos/{owner}/{repo}/issues/5/comments -f body=x", facts=NoFacts())


class OtherGitWriters(Cases):
    def test_subtree_and_find_exec(self):
        for cmd in (f"git subtree push --prefix=d origin {BRANCH}", f"git-subtree push --prefix=d origin {BRANCH}"):
            with self.subTest(cmd):
                self.refused(cmd, "does not read", git=ON_MAIN, facts=NoFacts())
        self.refused("find . -name x -exec git push origin {} \\;", "cannot read", git=ON_MAIN, facts=NoFacts())

    def test_a_tilde_directory_is_not_expanded_by_the_guard(self):
        self.refused("git -C ~/repo push origin feature/x", "chosen by an option", git=ON_MAIN, facts=NoFacts())


class CreateLinkedBranch(Cases):
    def test_creating_a_linked_branch_named_plan(self):
        q = 'mutation { createLinkedBranch(input:{issueId:"I", oid:"abc", name:"plan/x"}) { linkedBranch { id } } }'
        self.refused(f"gh api graphql -f query='{q}'", "E14", facts=NoFacts())


class UpstreamMapping(Cases):
    def test_push_default_upstream_maps_a_colonless_refspec_onto_a_plan_branch(self):
        git = with_config(("push.default", "upstream"), ("branch.feature.merge", PLAN_UPSTREAM), base=ON_MAIN)
        self.refused("git push origin feature", "upstream", git=git, facts=NoFacts())
        self.refused("git push origin HEAD", "upstream", git=git, facts=NoFacts())

    def test_without_a_plan_upstream_or_with_a_destination_it_passes(self):
        other = with_config(("push.default", "upstream"), ("branch.feature.merge", "refs/heads/main"), base=ON_MAIN)
        self.allowed("git push origin feature", git=other, facts=NoFacts())
        plan = with_config(("push.default", "upstream"), ("branch.feature.merge", PLAN_UPSTREAM), base=ON_MAIN)
        self.allowed("git push origin feature:feature", git=plan, facts=NoFacts())
        simple = with_config(("push.default", "simple"), ("branch.feature.merge", PLAN_UPSTREAM), base=ON_MAIN)
        self.allowed("git push origin feature", git=simple, facts=NoFacts())


class GhRepositoryReachesTheWrite(Cases):
    def facts(self):
        return FakeFacts(heads={7: BRANCH}, bases={5: BRANCH}, pr_state=DRAFT, head=H1)

    def test_update_branch_in_another_repository_is_not_judged_against_origin(self):
        self.refused("gh pr update-branch 7 -R other/repo", "another repository", facts=self.facts())
        self.refused("gh pr update-branch https://github.com/other/repo/pull/7", "another repository", facts=self.facts())

    def test_a_merge_in_another_repository_into_a_plan_base(self):
        self.refused("gh pr merge 5 -R other/repo", "another repository", facts=self.facts())

    def test_a_branch_selected_in_another_repository_cannot_be_read(self):
        self.refused("gh pr merge feature/x -R other/repo", "another repository", facts=self.facts())

    def test_the_same_repository_named_explicitly_is_judged(self):
        self.refused("gh pr update-branch 7 -R owner/proj", "E14", run=False, facts=self.facts())

    def test_a_default_repository_set_for_gh_refuses(self):
        git = with_config(("remote.origin.gh-resolved", "base"), base=ON_MAIN)
        self.refused("gh pr close 5", "set-default", git=git, facts=self.facts())

    def test_without_one_it_passes(self):
        self.allowed("gh pr close 5", git=ON_MAIN, facts=FakeFacts(heads={5: "feature/x"}))


if __name__ == "__main__":
    unittest.main()
