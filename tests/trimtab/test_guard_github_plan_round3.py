"""Fixes from the triage of round 2's MEDIUM and LOW findings (PR 166), each failing without its fix.

GitHub is injected (TST-2); git is real in temporary repositories where the fix is about git itself."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from gh_fake import FakeGh
from test_enforcement_tools import commit, git
from test_guard_github_plan import CONFIG_REF, ON_MAIN, NoFacts
from test_guard_github_plan_bypasses import REPO, Cases, with_config
from test_planfacts import INSTANCE
from test_planrows import BRANCH, DRAFT, H1, FakeFacts
from test_scrub import isolate_git
from trimtab import planfacts, planguard, planrows
from trimtab.capture.ghapi import GhApi
from trimtab.enforcement import tools
from trimtab.planrows import Change, FactsError

PLANS = "docs/" + "plans/"
RECORD = PLANS + "progress/0008-slug/item-02-part1.md"


class NormalisedBranchFields(Cases):
    def test_normalise(self):
        for raw, want in (("plan/x", "plan/x"), ("refs/heads/plan/x", "plan/x"), ("heads/plan/x", "plan/x"),
                          ("  refs/heads/plan/x ", "plan/x"), ("REFS/HEADS/plan/x", "plan/x"), (None, None),
                          (5, None)):
            with self.subTest(raw):
                self.assertEqual(planrows.normalise(raw), want)

    def test_a_prefixed_or_padded_branch_in_a_body_is_still_a_plan_branch(self):
        for field_value in (f"refs/heads/{BRANCH}", f"heads/{BRANCH}", f" {BRANCH}"):
            with self.subTest(field_value):
                self.refused(f"gh api -X PUT {REPO}/contents/a.txt -f branch='{field_value}' -f message=m", "E14",
                             run=False)
                self.refused(f"gh api -X POST {REPO}/merges -f base='{field_value}' -f head=main", "E14", run=False)
                self.refused(f"gh api -X POST {REPO}/branches/feature/rename -f new_name='{field_value}'", "E14",
                             run=False)


class BinaryRecords(unittest.TestCase):
    def test_a_binary_record_is_neither_register_only_nor_progress_only(self):
        changes = [Change(RECORD, "M", (), 0, True)]
        self.assertFalse(planrows.register_shape(changes, BRANCH))
        self.assertFalse(planrows.progress_only(changes, BRANCH))

    def test_git_marks_a_binary_change(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        isolate_git(self, tmp)
        repo = tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / "keep.txt").write_text("one\n")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "a")
        base = git(repo, "rev-parse", "HEAD")
        (repo / "blob.bin").write_bytes(b"\0\1\2binary\0")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "b")
        found = planfacts.GitHubFacts("owner/proj", GhApi(FakeGh()), tools.Git(environ={"HOME": str(tmp)}), repo,
                                      INSTANCE).changes(BRANCH, "HEAD", base)
        self.assertEqual([(c.path, c.binary) for c in found], [("blob.bin", True)])


class ConfigRefusalIsNotNoRepository(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        isolate_git(self, self.tmp)
        self.env = {"HOME": str(self.tmp)}
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        commit(self.repo, "f", "x\n")

    def test_a_directory_with_no_repository_has_no_repository_configuration(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        got = planguard.PlanSupport(plain, environ=self.env).config(plain)
        self.assertEqual([k for k in got if k.startswith("remote.")], [])

    def test_a_shallow_clone_is_read_like_any_other(self):
        # A config read needs none of the object checks, so a shallow, partial or alternates clone pushes as usual.
        commit(self.repo, "g", "y\n")
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.repo}", str(clone)], check=True)
        got = planguard.PlanSupport(clone, environ=self.env).config(clone)
        self.assertIn("remote.origin.url", got)

    def test_a_normal_repository_reads_its_configuration(self):
        git(self.repo, "config", "remote.origin.push", "+HEAD:refs/heads/plan/x")
        got = planguard.PlanSupport(self.repo, environ=self.env).config(self.repo)
        self.assertEqual(got.get("remote.origin.push"), ["+HEAD:refs/heads/plan/x"])


class GitBooleans(Cases):
    def test_integers_are_booleans(self):
        for value, want in (("2", True), ("1", True), ("-1", True), ("1k", True), ("0", False), ("00", False),
                            ("false", False), ("", True), ("yes", True), ("no", False)):
            with self.subTest(value):
                self.assertEqual(planguard._true([value]), want)

    def test_a_mirror_set_to_two_refuses(self):
        git_answers = with_config(("remote.origin.mirror", "2"), base=ON_MAIN)
        self.refused("git push origin feature/x", "E15", git=git_answers, facts=NoFacts())


class GhSelectors(Cases):
    def facts(self):
        return FakeFacts(heads={5: BRANCH, 7: BRANCH}, pr_state=DRAFT, head=H1)

    def test_a_repository_chosen_by_the_environment_refuses(self):
        for cmd in ("GH_REPO=other/repo gh pr close 5", "export GH_HOST=x; gh pr close 5"):
            with self.subTest(cmd):
                self.refused(cmd, "GH_REPO", facts=self.facts())

    def test_an_attached_repository_option_is_read(self):
        for cmd in ("gh --repo=other/repo pr close 5", "gh -Rother/repo pr close 5"):
            with self.subTest(cmd):
                facts = FakeFacts(heads={5: "feature/x"})
                self.allowed(cmd, facts=facts)
                self.assertEqual(facts.looked_up, [(5, "other/repo")])  # the PR is looked up in that repository
        self.refused("gh --repo=$R pr close 5", "shell expansion", facts=facts)

    def test_a_number_after_a_directory_change_refuses(self):
        self.refused("cd /elsewhere && gh pr close 5", "directory", facts=self.facts())

    def test_a_url_carries_its_repository_so_a_directory_change_does_not_matter(self):
        self.refused("cd /elsewhere && gh pr close https://github.com/owner/proj/pull/7", "E16", facts=self.facts())

    def test_a_url_the_guard_cannot_read_refuses(self):
        for cmd in ("gh pr close http://github.com/owner/proj/pull/7", "gh pr close HTTP://github.com/owner/proj/pull/7",
                    "gh issue close https://github.com/owner/proj/issues/7", "gh pr close //github.com/o/r/pull/7"):
            with self.subTest(cmd):
                self.refused(cmd, "URL", facts=self.facts())

    def test_the_host_of_a_url_is_matched_without_case(self):
        self.refused("gh pr close https://GitHub.com/owner/proj/pull/7", "E16", facts=self.facts())


class SecretScanThatNeverRuns(unittest.TestCase):
    def test_a_branch_with_runs_but_none_of_the_scan_workflow_is_unreadable(self):
        # covered at the facts layer in test_planfacts; here the row turns "unreadable" into a refusal
        with self.assertRaisesRegex(planrows.Refusal, "E19"):
            planrows.check_e19("unreadable", lambda: False)


class RefusalsDoNotQuoteTheCommand(Cases):
    MARK = "MARKERxyz"

    def test_an_unknown_option_is_not_quoted(self):
        code, message = guard_decide(f"git push --bogus{self.MARK} origin {BRANCH}")
        self.assertEqual(code, 2)
        self.assertNotIn(self.MARK, message)

    def test_a_short_option_is_not_quoted(self):
        code, message = guard_decide(f"git push -Z origin {BRANCH}")
        self.assertEqual(code, 2)
        self.assertNotIn("-Z ", message)

    def test_another_repository_in_a_path_is_not_quoted(self):
        code, message = guard_decide(f"gh api -X PATCH repos/{self.MARK}/{self.MARK}/git/refs/heads/{BRANCH} -f sha={H1}")
        self.assertEqual(code, 2)
        self.assertNotIn(self.MARK, message)

    def test_a_failed_read_does_not_quote_gh_stderr(self):
        gh = GhApi(FakeGh(returncode=1, stderr=f"HTTP 500 {self.MARK}"))
        f = planfacts.GitHubFacts("owner/proj", gh, tools.Git(), Path("."), INSTANCE)
        with self.assertRaises(FactsError) as caught:
            f.open_pr(BRANCH)
        self.assertNotIn(self.MARK, str(caught.exception))


def guard_decide(command):
    from test_guard_github_plan import decide
    return decide(command, facts=FakeFacts(pr_state=DRAFT, head=H1))


class GraphqlState(Cases):
    def mutation(self, query, *fields):
        extra = " ".join(f"-f {f}" for f in fields)
        return f"gh api graphql -f query='{query}' {extra}"

    def test_the_word_state_in_a_pr_body_is_not_an_argument(self):
        self.allowed(self.mutation('mutation { updatePullRequest(input:{pullRequestId:"x", body:"the state of it"}) '
                                   '{ clientMutationId } }'), facts=NoFacts())

    def test_a_state_argument_refuses(self):
        self.refused(self.mutation('mutation($s: PullRequestUpdateState){ updatePullRequest(input:{pullRequestId:"x", '
                                   'state:$s}) { clientMutationId } }', "s=CLOSED"), "E16", facts=NoFacts())

    def test_a_variable_named_state_refuses(self):
        self.refused(self.mutation('mutation($t: String){ updatePullRequest(input:{pullRequestId:"x", title:$t}) '
                                   '{ clientMutationId } }', "t=hi", "state=CLOSED"), "E16", facts=NoFacts())


class ActivityPaging(unittest.TestCase):
    def test_the_read_follows_the_link_cursor_and_never_a_page_number(self):
        # `page=` is ignored by the endpoint, so a loop on it would re-read page one until its limit; the
        # rest of the list sits behind the Link header's `after=` cursor (ADR 0063 Amendment 1).
        page = ("HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + json.dumps([]))
        fake = FakeGh({"repos/owner/proj/pulls": json.dumps([{"number": 7, "draft": True, "created_at": "2026-11-12T10:00:00Z",
                                                              "head": {"ref": BRANCH}, "base": {"ref": "main"}}]),
                       "repos/owner/proj/activity": page})
        f = planfacts.GitHubFacts("owner/proj", GhApi(fake), tools.Git(), Path("."), INSTANCE)
        f.pushes(BRANCH)
        calls = [a for a in fake.argvs() if "activity" in a[4]]
        self.assertEqual(len(calls), 1)
        self.assertNotIn("page=", calls[0][4].replace("per_page=", ""))


if __name__ == "__main__":
    unittest.main()
