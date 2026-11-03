"""The plan authorisation check: wrap-merged's checks 1-3, from real git and a faked gh (ADR 0059)."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gh_fake import FakeGh, path_of
from instance_fixture import env_for, make_instance
from test_scrub import isolated_git_env
from trimtab import planauth, planscope
from trimtab.planauth import PlanAuthError

ROOT = Path(__file__).resolve().parents[2]
TRIMTAB = ROOT / "bin" / "trimtab"
REPOSITORY = "owner/repo"
DEFAULT = "main"
PLAN = "0001-trial"
PLAN_FILE = f"{planscope.AUTHORISED}/{PLAN}.md"
WRITES_FILE = f"{planscope.AUTHORISED}/{PLAN}.writes"
WRITES = "docs/adr/*.md\n"
ADMIN = "the-admin"
AGENT = "the-agent"
PLAN_PR = 7
LATER_PR = 8
MERGED_AT = "2026-11-03T17:33:15Z"
REPOSITORY_PATH = f"repos/{REPOSITORY}"
BRANCH_PATH = f"repos/{REPOSITORY}/branches/{DEFAULT}"
VARIABLE_PATH = f"repos/{REPOSITORY}/actions/variables/TRIMTAB_ADMIN"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def pulls_path(sha):
    return f"repos/{REPOSITORY}/commits/{sha}/pulls"


def pull_path(number):
    return f"repos/{REPOSITORY}/pulls/{number}"


def listed_pull(number, merged_at=MERGED_AT, base=DEFAULT):
    return {"number": number, "merged_at": merged_at, "base": {"ref": base}}


def pull_row(number, merge_sha, merged_by=ADMIN):
    return json.dumps({"number": number, "merged_by": {"login": merged_by}, "merge_commit_sha": merge_sha})


class GitRepository(unittest.TestCase):
    """A default branch where PR 7, merged with a merge commit, added the plan and its declaration."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.env = isolated_git_env({k: v for k, v in os.environ.items() if k != "TRIMTAB_INSTANCE"}, self.root)
        self.env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        subprocess.run(["git", "init", "-q", "-b", DEFAULT, str(self.repo)], check=True)
        self.write("README.md", "readme\n")
        self.commit("base")
        self.merge = self.merge_branch("plan-pr", {PLAN_FILE: "# plan\n", WRITES_FILE: WRITES})
        self.answers = {}
        self.publish()

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def merge_branch(self, branch, files, removed=()):
        """A PR's branch, merged into the default branch with a merge commit, as GitHub's merge button does."""
        git(self.repo, "switch", "-q", "-c", branch)
        for rel, text in files.items():
            self.write(rel, text)
        for rel in removed:
            git(self.repo, "rm", "-q", rel)
        self.commit(branch)
        git(self.repo, "switch", "-q", DEFAULT)
        git(self.repo, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)
        return git(self.repo, "rev-parse", "HEAD")

    def publish(self):
        """What a fetch leaves: origin's default branch at the local tip, and the server reporting the same tip."""
        self.tip = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", f"refs/remotes/origin/{DEFAULT}", self.tip)
        self.answers.update({
            REPOSITORY_PATH: json.dumps({"default_branch": DEFAULT}),
            BRANCH_PATH: json.dumps({"name": DEFAULT, "commit": {"sha": self.tip}}),
            pulls_path(self.merge): json.dumps([listed_pull(PLAN_PR)]),
            pull_path(PLAN_PR): pull_row(PLAN_PR, self.merge),
        })

    def check(self, admin=ADMIN, gh=None):
        return planauth.check(str(self.repo), REPOSITORY, PLAN, admin, gh or FakeGh(self.answers))


class Authorised(GitRepository):
    def test_a_plan_the_admin_merged_and_nobody_changed_passes(self):
        result = self.check()

        self.assertEqual(result.problems, ())

    def test_a_pass_names_the_pr_and_its_merge_commit(self):
        result = self.check()

        self.assertEqual((result.pr, result.merge_commit), (PLAN_PR, self.merge))

    def test_a_pass_carries_the_declaration_from_the_default_branch(self):
        result = self.check()

        self.assertEqual(result.writes, WRITES)

    def test_an_empty_declaration_passes_as_bookkeeping_only(self):
        self.merge = self.merge_branch("empty", {f"{planscope.AUTHORISED}/0002-empty.md": "# p\n",
                                                 f"{planscope.AUTHORISED}/0002-empty.writes": ""})
        self.publish()
        self.answers[pulls_path(self.merge)] = json.dumps([listed_pull(LATER_PR)])
        self.answers[pull_path(LATER_PR)] = pull_row(LATER_PR, self.merge)

        result = planauth.check(str(self.repo), REPOSITORY, "0002-empty", ADMIN, FakeGh(self.answers))

        self.assertEqual((result.problems, result.writes), ((), ""))

    def test_the_admin_is_compared_without_case(self):
        result = self.check(admin=ADMIN.upper())

        self.assertEqual(result.problems, ())

    def test_edits_inside_the_plan_pr_before_its_merge_pass(self):
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        git(self.repo, "switch", "-q", "-c", "two-commits")
        self.write(PLAN_FILE, "# draft\n")
        self.write(WRITES_FILE, WRITES)
        self.commit("draft")
        self.write(PLAN_FILE, "# plan\n")
        self.commit("final")
        git(self.repo, "switch", "-q", DEFAULT)
        git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge two-commits", "two-commits")
        self.merge = git(self.repo, "rev-parse", "HEAD")
        self.publish()

        self.assertEqual(self.check().problems, ())


class Check1Present(GitRepository):
    def test_a_plan_with_no_declaration_fails_check_1(self):
        self.merge = self.merge_branch("no-writes", {f"{planscope.AUTHORISED}/0003-bare.md": "# p\n"})
        self.publish()

        result = planauth.check(str(self.repo), REPOSITORY, "0003-bare", ADMIN, FakeGh(self.answers))

        self.assertEqual([p[:8] for p in result.problems], ["check 1:"])

    def test_a_plan_that_is_not_there_fails_check_1_without_reading_pull_requests(self):
        gh = FakeGh(self.answers)

        result = planauth.check(str(self.repo), REPOSITORY, "0009-absent", ADMIN, gh)

        self.assertTrue(result.problems and result.problems[0].startswith("check 1:"))
        self.assertNotIn(pull_path(PLAN_PR), [path_of(argv) for argv in gh.argvs()])


class Check2MergedByTheAdmin(GitRepository):
    def test_a_plan_merged_by_another_account_fails_check_2_naming_it(self):
        self.answers[pull_path(PLAN_PR)] = pull_row(PLAN_PR, self.merge, merged_by=AGENT)

        result = self.check()

        self.assertEqual([(p[:8], AGENT in p) for p in result.problems], [("check 2:", True)] * 2)

    def test_a_merge_by_a_deleted_account_fails_check_2(self):
        self.answers[pull_path(PLAN_PR)] = json.dumps({"number": PLAN_PR, "merged_by": None,
                                                        "merge_commit_sha": self.merge})

        self.assertTrue(self.check().problems[0].startswith("check 2:"))

    def test_a_commit_with_no_merged_pr_fails_check_2(self):
        self.answers[pulls_path(self.merge)] = json.dumps([listed_pull(PLAN_PR, merged_at=None)])

        self.assertTrue(self.check().problems[0].startswith("check 2:"))

    def test_a_pr_merged_into_another_branch_fails_check_2(self):
        self.answers[pulls_path(self.merge)] = json.dumps([listed_pull(PLAN_PR, base="release")])

        self.assertTrue(self.check().problems[0].startswith("check 2:"))

    def test_a_commit_two_merged_prs_claim_fails_check_2(self):
        self.answers[pulls_path(self.merge)] = json.dumps([listed_pull(PLAN_PR), listed_pull(LATER_PR)])

        self.assertTrue(self.check().problems[0].startswith("check 2:"))

    def test_a_declaration_added_by_a_later_pr_fails_check_2_even_from_the_admin(self):
        self.merge_branch("drop-writes", {}, removed=(WRITES_FILE,))
        later = self.merge_branch("re-add-writes", {WRITES_FILE: WRITES})
        self.publish()
        self.answers[pulls_path(later)] = json.dumps([listed_pull(LATER_PR)])
        self.answers[pull_path(LATER_PR)] = pull_row(LATER_PR, later)

        result = self.check()

        self.assertEqual([p[:8] for p in result.problems], ["check 2:"])

    def test_an_identical_addition_merged_later_from_a_side_branch_is_not_the_addition(self):
        """Merge order decides, not commit dates: a side branch's own copy, committed later, adds nothing to main."""
        git(self.repo, "switch", "-q", "-c", "side", f"{self.merge}^1")
        self.write(PLAN_FILE, "# plan\n")
        self.write(WRITES_FILE, WRITES)
        git(self.repo, "add", "-A")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "side copy"], check=True,
                       env={**os.environ, "GIT_COMMITTER_DATE": "2099-01-01T00:00:00Z",
                            "GIT_AUTHOR_DATE": "2099-01-01T00:00:00Z"})
        git(self.repo, "switch", "-q", DEFAULT)
        git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
        self.publish()

        self.assertEqual(self.check().problems, ())

    def test_a_plan_deleted_and_re_added_is_judged_by_its_latest_addition(self):
        self.merge_branch("drop-plan", {}, removed=(PLAN_FILE, WRITES_FILE))
        later = self.merge_branch("re-add-plan", {PLAN_FILE: "# plan\n", WRITES_FILE: WRITES})
        self.publish()
        self.answers[pulls_path(later)] = json.dumps([listed_pull(LATER_PR)])
        self.answers[pull_path(LATER_PR)] = pull_row(LATER_PR, later, merged_by=AGENT)

        result = self.check()

        self.assertTrue(result.problems and all(p.startswith("check 2:") for p in result.problems))


class Check3Unchanged(GitRepository):
    def test_a_plan_edited_after_its_merge_fails_check_3(self):
        self.merge_branch("edit-plan", {PLAN_FILE: "# widened\n"})
        self.publish()

        result = self.check()

        self.assertEqual([p[:8] for p in result.problems], ["check 3:"])

    def test_a_declaration_edited_after_its_merge_fails_check_3(self):
        self.merge_branch("edit-writes", {WRITES_FILE: "src/*\n"})
        self.publish()

        result = self.check()

        self.assertEqual([p[:8] for p in result.problems], ["check 3:"])


class ReadFailures(GitRepository):
    def test_a_stale_fetch_is_an_error_not_a_verdict(self):
        self.answers[BRANCH_PATH] = json.dumps({"name": DEFAULT, "commit": {"sha": "f" * 40}})

        with self.assertRaises(PlanAuthError):
            self.check()

    def test_no_fetched_default_branch_is_an_error(self):
        git(self.repo, "update-ref", "-d", f"refs/remotes/origin/{DEFAULT}")

        with self.assertRaises(PlanAuthError):
            self.check()

    def test_a_failed_gh_read_is_an_error(self):
        with self.assertRaises(PlanAuthError):
            self.check(gh=FakeGh(returncode=1, stderr="gh: Not Found (HTTP 404)"))

    def test_a_pull_request_answer_of_the_wrong_shape_is_an_error(self):
        for answer in ("{}", "[1]", "<html>", json.dumps([{"number": "7", "merged_at": MERGED_AT}])):
            with self.subTest(answer=answer):
                self.answers[pulls_path(self.merge)] = answer

                with self.assertRaises(PlanAuthError):
                    self.check()

    def test_a_full_page_of_pull_requests_is_an_error_not_a_partial_list(self):
        unmerged = range(100, 100 + planauth.PULLS_LIMIT - 1)
        rows = [listed_pull(PLAN_PR)] + [listed_pull(n, merged_at=None) for n in unmerged]
        self.answers[pulls_path(self.merge)] = json.dumps(rows)

        with self.assertRaises(PlanAuthError):
            self.check()

    def test_a_pull_request_numbered_as_text_is_an_error(self):
        self.answers[pulls_path(self.merge)] = json.dumps([{**listed_pull(PLAN_PR), "number": str(PLAN_PR)}])

        with self.assertRaises(PlanAuthError):
            self.check()

    def test_a_merger_of_the_wrong_shape_is_an_error(self):
        for merged_by in ({"login": 5}, {}, "the-admin"):
            with self.subTest(merged_by=merged_by):
                self.answers[pull_path(PLAN_PR)] = json.dumps({"number": PLAN_PR, "merged_by": merged_by,
                                                                "merge_commit_sha": self.merge})

                with self.assertRaises(PlanAuthError):
                    self.check()

    def test_an_abbreviated_merge_commit_is_an_error(self):
        self.answers[pull_path(PLAN_PR)] = pull_row(PLAN_PR, self.merge[:12])

        with self.assertRaises(PlanAuthError):
            self.check()

    def test_a_branch_answer_with_no_commit_is_an_error(self):
        for answer in ({"name": DEFAULT}, {"commit": {}}, {"commit": {"sha": self.tip[:12]}}, []):
            with self.subTest(answer=answer):
                self.answers[BRANCH_PATH] = json.dumps(answer)

                with self.assertRaises(PlanAuthError):
                    self.check()

    def test_a_merge_commit_git_does_not_hold_is_an_error(self):
        self.answers[pull_path(PLAN_PR)] = pull_row(PLAN_PR, "e" * 40)

        with self.assertRaises(PlanAuthError):
            self.check()

    def test_a_bad_argument_is_an_error_before_any_read(self):
        cases = {"repository": ("owner", PLAN), "traversal": ("owner/..", PLAN), "plan path": (REPOSITORY, "../x"),
                 "plan dots": (REPOSITORY, "a..b"), "plan empty": (REPOSITORY, ""),
                 "plan option": (REPOSITORY, "-x")}
        for what, (repository, plan) in cases.items():
            with self.subTest(what=what):
                gh = FakeGh(self.answers)

                with self.assertRaises(PlanAuthError):
                    planauth.check(str(self.repo), repository, plan, ADMIN, gh)
                self.assertEqual(gh.calls, [])

    def test_no_admin_is_an_error_before_any_read(self):
        gh = FakeGh(self.answers)

        with self.assertRaises(PlanAuthError):
            planauth.check(str(self.repo), REPOSITORY, PLAN, "", gh)
        self.assertEqual(gh.calls, [])


class Cli(GitRepository):
    """`trimtab check-plan-authorised` against a fixture instance, real git and a scripted gh."""

    def setUp(self):
        super().setUp()
        self.inst = make_instance(self.root)
        path = self.inst / "instance.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**data, "accounts": {"admin": ADMIN}}), encoding="utf-8")
        self.answers[VARIABLE_PATH] = json.dumps({"name": "TRIMTAB_ADMIN", "value": ADMIN})

    def cli(self, *extra):
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        answers = self.root / "answers.json"
        answers.write_text(json.dumps(self.answers), encoding="utf-8")
        gh = bin_dir / "gh"
        gh.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                      "answers = json.load(open(os.environ['FAKE_ANSWERS']))\n"
                      "path = sys.argv[4].split('?', 1)[0]\n"
                      "if path not in answers:\n    sys.exit('gh: Not Found (HTTP 404)')\n"
                      "print(answers[path])\n", encoding="utf-8")
        gh.chmod(0o755)
        env = {**self.env, **env_for(self.inst), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               "FAKE_ANSWERS": str(answers)}
        return subprocess.run([str(TRIMTAB), "check-plan-authorised", "--repository", REPOSITORY, "--plan", PLAN,
                               "--repo", str(self.repo), *extra], capture_output=True, text=True, env=env)

    def test_a_pass_exits_0_naming_the_pr_and_merge_commit(self):
        done = self.cli()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"#{PLAN_PR}", done.stdout)
        self.assertIn(self.merge[:12], done.stdout)

    def test_print_writes_puts_the_declaration_after_the_verdict(self):
        done = self.cli("--print-writes")

        self.assertEqual((done.returncode, done.stdout.split("\n", 1)[1]), (0, WRITES), done.stderr)

    def test_a_failed_check_exits_1_naming_it(self):
        self.answers[pull_path(PLAN_PR)] = pull_row(PLAN_PR, self.merge, merged_by=AGENT)

        done = self.cli("--print-writes")

        self.assertEqual((done.returncode, done.stdout, "check 2:" in done.stderr), (1, "", True), done.stderr)

    def test_an_admin_the_repository_variable_disagrees_with_exits_1(self):
        self.answers[VARIABLE_PATH] = json.dumps({"name": "TRIMTAB_ADMIN", "value": AGENT})

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout), (1, ""), done.stderr)

    def test_a_failed_read_exits_2(self):
        del self.answers[BRANCH_PATH]

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout, "nothing is vouched for" in done.stderr), (2, "", True),
                         done.stderr)


if __name__ == "__main__":
    unittest.main()
