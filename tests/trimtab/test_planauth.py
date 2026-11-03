"""The plan authorisation check: wrap-merged's checks 1-3, from real git and a faked gh (ADR 0059)."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gh_fake import FakeGh, path_of, query_of
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


def pull_row(number, merge_sha, merged_by=ADMIN, merged_at=MERGED_AT, base=DEFAULT):
    return json.dumps({"number": number, "merged_by": {"login": merged_by}, "merge_commit_sha": merge_sha,
                       "merged_at": merged_at, "base": {"ref": base}})


class GitRepository(unittest.TestCase):
    """A default branch where PR 7, merged with a merge commit, added the plan and its declaration."""

    default = DEFAULT

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
        subprocess.run(["git", "init", "-q", "-b", self.default, str(self.repo)], check=True)
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
        git(self.repo, "switch", "-q", self.default)
        git(self.repo, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)
        return git(self.repo, "rev-parse", "HEAD")

    def publish(self):
        """What a fetch leaves: origin's default branch at the local tip, and the server reporting the same tip."""
        self.tip = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", f"refs/remotes/origin/{self.default}", self.tip)
        self.answers.update({
            REPOSITORY_PATH: json.dumps({"default_branch": self.default}),
            f"repos/{REPOSITORY}/branches/{self.default}": json.dumps({"name": self.default,
                                                                       "commit": {"sha": self.tip}}),
            pulls_path(self.merge): json.dumps([listed_pull(PLAN_PR, base=self.default)]),
            pull_path(PLAN_PR): pull_row(PLAN_PR, self.merge, base=self.default),
        })

    def check(self, admin=ADMIN, gh=None, repo=None):
        return planauth.check(str(repo or self.repo), REPOSITORY, PLAN, admin, gh or FakeGh(self.answers))


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

    def test_files_one_commit_added_read_its_pull_requests_once(self):
        gh = FakeGh(self.answers)

        self.check(gh=gh)

        paths = [path_of(argv) for argv in gh.argvs()]
        self.assertEqual((paths.count(pulls_path(self.merge)), paths.count(pull_path(PLAN_PR))), (1, 1))

    def test_files_two_commits_of_one_pr_added_pass(self):
        """A rebase merge lands each of the PR's commits on the default branch; both resolve to the PR."""
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        self.write(PLAN_FILE, "# plan\n")
        first = self.commit("add plan")
        self.write(WRITES_FILE, WRITES)
        self.merge = self.commit("add declaration")
        self.publish()
        for sha in (first, self.merge):
            self.answers[pulls_path(sha)] = json.dumps([listed_pull(PLAN_PR)])

        self.assertEqual(self.check().problems, ())

    def test_a_plan_edited_and_restored_after_its_merge_passes_as_its_content_is_unchanged(self):
        self.merge_branch("edit-plan", {PLAN_FILE: "# widened\n"})
        self.merge_branch("restore-plan", {PLAN_FILE: "# plan\n"})
        self.publish()

        self.assertEqual(self.check().problems, ())

    def test_the_pull_request_list_is_asked_for_a_page_as_long_as_its_bound(self):
        gh = FakeGh(self.answers)

        self.check(gh=gh)

        pages = [query_of(argv) for argv in gh.argvs() if path_of(argv) == pulls_path(self.merge)]
        self.assertEqual([q.get("per_page") for q in pages], [str(planauth.PULLS_LIMIT)])

    def test_a_pass_carries_the_plan_from_the_default_branch_not_the_checkout(self):
        self.write(PLAN_FILE, "# a local edit the admin never merged\n")

        result = self.check()

        self.assertEqual((result.problems, result.plan_text), ((), "# plan\n"))

    def test_a_page_just_under_its_bound_passes(self):
        unmerged = range(100, 100 + planauth.PULLS_LIMIT - 2)
        self.answers[pulls_path(self.merge)] = json.dumps(
            [listed_pull(PLAN_PR)] + [listed_pull(n, merged_at=None) for n in unmerged])

        self.assertEqual(self.check().problems, ())

    def test_environment_config_does_not_steer_the_reads(self):
        """A grafts file named by the environment would cut history as info/grafts does (ADR 0059)."""
        self.merge_branch("edit-writes", {WRITES_FILE: "src/*\n"})
        later = self.merge_branch("unrelated", {"README.md": "later\n"})
        self.publish()
        self.answers[pulls_path(later)] = json.dumps([listed_pull(LATER_PR)])
        self.answers[pull_path(LATER_PR)] = pull_row(LATER_PR, later)
        grafts = self.root / "grafts"
        grafts.write_text(f"{later}\n", encoding="utf-8")
        steering = {"GIT_GRAFT_FILE": str(grafts), "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "log.showSignature", "GIT_CONFIG_VALUE_0": "true"}

        with mock.patch.dict(os.environ, steering):
            result = self.check()

        self.assertEqual([p[:8] for p in result.problems], ["check 3:"])

    def test_a_plan_the_pr_renamed_into_place_passes(self):
        """Rename detection is on by default; the path-limited log must still read the PR's commit as the addition."""
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        self.write("draft.md", "# plan\n" * 20)
        self.commit("draft on the default branch")
        self.merge = self.merge_branch("rename-plan", {WRITES_FILE: WRITES})
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        git(self.repo, "switch", "-q", "-c", "renamed")
        (self.repo / PLAN_FILE).parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "mv", "draft.md", PLAN_FILE)
        self.write(WRITES_FILE, WRITES)
        self.commit("rename the draft into place")
        git(self.repo, "switch", "-q", DEFAULT)
        git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge renamed", "renamed")
        self.merge = git(self.repo, "rev-parse", "HEAD")
        self.publish()

        self.assertEqual(self.check().problems, ())

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

    def test_a_plan_or_declaration_that_is_not_a_regular_file_fails_check_1(self):
        for linked in (PLAN_FILE, WRITES_FILE):
            with self.subTest(linked=linked):
                git(self.repo, "rm", "-q", linked)
                os.symlink("../../../README.md", self.repo / linked)
                self.commit(f"link {linked}")
                self.publish()

                result = self.check()

                self.assertEqual([p.startswith("check 1:") and "not a regular file" in p for p in result.problems],
                                 [True])
                git(self.repo, "reset", "-q", "--hard", "HEAD~1")

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
        self.answers[pull_path(PLAN_PR)] = json.dumps({**json.loads(pull_row(PLAN_PR, self.merge)), "merged_by": None})

        self.assertIn("check 2:", self.check().problems[0])
        self.assertIn("a deleted account", self.check().problems[0])

    def test_a_commit_with_no_merged_pr_fails_check_2(self):
        self.answers[pulls_path(self.merge)] = json.dumps([listed_pull(PLAN_PR, merged_at=None)])

        self.assertIn("belongs to 0 PRs merged into", self.check().problems[0])

    def test_a_pr_merged_into_another_branch_fails_check_2(self):
        self.answers[pulls_path(self.merge)] = json.dumps([listed_pull(PLAN_PR, base="release")])

        self.assertIn("belongs to 0 PRs merged into", self.check().problems[0])

    def test_a_commit_two_merged_prs_claim_fails_check_2(self):
        self.answers[pulls_path(self.merge)] = json.dumps([listed_pull(PLAN_PR), listed_pull(LATER_PR)])

        self.assertIn("belongs to 2 PRs merged into", self.check().problems[0])

    def test_a_pr_whose_own_row_is_unmerged_or_elsewhere_fails_check_2(self):
        for row in (pull_row(PLAN_PR, self.merge, merged_at=None), pull_row(PLAN_PR, self.merge, base="release")):
            with self.subTest(row=row):
                self.answers[pull_path(PLAN_PR)] = row

                self.assertIn(f"PR #{PLAN_PR} is not merged into", self.check().problems[0])

    def test_a_merge_commit_with_unrelated_history_is_an_error(self):
        git(self.repo, "switch", "-q", "--orphan", "unrelated")
        self.write("other.txt", "other\n")
        orphan = self.commit("unrelated root")
        git(self.repo, "switch", "-q", "-f", DEFAULT)
        self.answers[pull_path(PLAN_PR)] = pull_row(PLAN_PR, orphan)

        with self.assertRaises(PlanAuthError):
            self.check()

    def test_a_merge_commit_off_the_default_branchs_history_fails_check_2(self):
        git(self.repo, "switch", "-q", "-c", "elsewhere")
        self.write("README.md", "elsewhere\n")
        elsewhere = self.commit("never merged")
        git(self.repo, "switch", "-q", DEFAULT)
        self.answers[pull_path(PLAN_PR)] = pull_row(PLAN_PR, elsewhere)

        result = self.check()

        self.assertEqual([p.startswith("check 2:") and "history" in p for p in result.problems], [True])

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

    def test_no_fetched_default_branch_is_an_error_naming_git_s_subcommand(self):
        git(self.repo, "update-ref", "-d", f"refs/remotes/origin/{DEFAULT}")

        with self.assertRaisesRegex(PlanAuthError, "git rev-parse"):
            self.check()

    def test_a_shallow_clone_is_an_error_not_a_verdict(self):
        """Its oldest commit reads as adding every file: here, the edit after the merge would hide behind it."""
        self.merge_branch("edit-writes", {WRITES_FILE: "src/*\n"})
        later = self.merge_branch("unrelated", {"README.md": "later\n"})
        self.publish()
        shallow = self.root / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.repo}", str(shallow)], check=True)
        self.answers[pulls_path(later)] = json.dumps([listed_pull(LATER_PR)])
        self.answers[pull_path(LATER_PR)] = pull_row(LATER_PR, later)

        with self.assertRaises(PlanAuthError):
            self.check(repo=shallow)

    def test_a_grafts_file_is_an_error_not_a_verdict(self):
        grafts = self.repo / git(self.repo, "rev-parse", "--git-path", "info/grafts")
        grafts.parent.mkdir(parents=True, exist_ok=True)
        grafts.write_text(f"{self.tip}\n", encoding="utf-8")

        with self.assertRaises(PlanAuthError):
            self.check()

    def test_a_repository_answer_of_the_wrong_shape_is_an_error(self):
        for answer in ([], {}, {"default_branch": ""}, {"default_branch": 7}):
            with self.subTest(answer=answer):
                self.answers[REPOSITORY_PATH] = json.dumps(answer)

                with self.assertRaises(PlanAuthError):
                    self.check()

    def test_a_pull_answer_of_the_wrong_shape_is_an_error(self):
        no_base = {k: v for k, v in json.loads(pull_row(PLAN_PR, self.merge)).items() if k != "base"}
        for answer in ([], {**no_base, "merged_at": None}, {"number": PLAN_PR, "merged_by": {"login": ADMIN}},
                       {"number": PLAN_PR, "merged_by": {"login": ADMIN}, "merge_commit_sha": None}):
            with self.subTest(answer=answer):
                self.answers[pull_path(PLAN_PR)] = json.dumps(answer)

                with self.assertRaises(PlanAuthError):
                    self.check()

    def test_a_pull_request_number_that_is_not_a_positive_integer_is_an_error(self):
        for number in (0, -1, True):
            with self.subTest(number=number):
                self.answers[pulls_path(self.merge)] = json.dumps([{**listed_pull(PLAN_PR), "number": number}])

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
                self.answers[pull_path(PLAN_PR)] = json.dumps({**json.loads(pull_row(PLAN_PR, self.merge)),
                                                               "merged_by": merged_by})

                with self.assertRaises(PlanAuthError):
                    self.check()

    def test_an_abbreviated_merge_commit_is_an_error(self):
        self.answers[pull_path(PLAN_PR)] = pull_row(PLAN_PR, self.merge[:12])

        with self.assertRaises(PlanAuthError):
            self.check()

    def test_a_branch_answer_with_no_commit_is_an_error(self):
        for answer in ({"name": DEFAULT}, {"commit": {}}, {"commit": "x"}, {"commit": {"sha": self.tip[:12]}}, []):
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
                      f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
                      "from gh_fake import path_of\n"
                      "answers = json.load(open(os.environ['FAKE_ANSWERS']))\n"
                      "path = path_of(sys.argv)\n"
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

    def test_a_pass_with_no_print_flag_prints_the_verdict_line_alone(self):
        done = self.cli()

        self.assertEqual((done.returncode, done.stdout.count("\n")), (0, 1), done.stderr)

    def test_print_plan_puts_the_default_branch_plan_after_the_verdict(self):
        self.write(PLAN_FILE, "# a local edit\n")

        done = self.cli("--print-plan")

        self.assertEqual((done.returncode, done.stdout.split("\n", 1)[1]), (0, "# plan\n"), done.stderr)

    def test_print_plan_and_print_writes_cannot_be_combined(self):
        done = self.cli("--print-plan", "--print-writes")

        self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)

    def test_a_failed_check_exits_1_naming_it(self):
        self.answers[pull_path(PLAN_PR)] = pull_row(PLAN_PR, self.merge, merged_by=AGENT)

        done = self.cli("--print-writes")

        self.assertEqual((done.returncode, done.stdout, "check 2:" in done.stderr), (1, "", True), done.stderr)

    def test_an_admin_the_repository_variable_disagrees_with_exits_1(self):
        self.answers[VARIABLE_PATH] = json.dumps({"name": "TRIMTAB_ADMIN", "value": AGENT})

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout, AGENT in done.stderr), (1, "", True), done.stderr)

    def test_an_instance_naming_no_admin_exits_1(self):
        path = self.inst / "instance.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({k: v for k, v in data.items() if k != "accounts"}), encoding="utf-8")

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout, "names no admin" in done.stderr), (1, "", True), done.stderr)

    def test_a_broken_instance_file_exits_2(self):
        (self.inst / "instance.json").write_text("{", encoding="utf-8")

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout, "nothing is vouched for" in done.stderr), (2, "", True),
                         done.stderr)

    def test_an_unreadable_admin_variable_exits_2(self):
        del self.answers[VARIABLE_PATH]

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout, "nothing is vouched for" in done.stderr), (2, "", True),
                         done.stderr)

    def test_a_failed_read_exits_2(self):
        del self.answers[BRANCH_PATH]

        done = self.cli()

        self.assertEqual((done.returncode, done.stdout, "nothing is vouched for" in done.stderr), (2, "", True),
                         done.stderr)


class OtherDefaultBranch(GitRepository):
    """Nothing assumes the default branch is called main."""

    default = "trunk"

    def test_a_plan_on_a_trunk_default_branch_passes(self):
        self.assertEqual(self.check().problems, ())


class AddedBy(unittest.TestCase):
    def test_a_path_no_commit_adds_is_an_error_not_a_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**isolated_git_env({k: v for k, v in os.environ.items() if k != "TRIMTAB_INSTANCE"}, root),
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
            with mock.patch.dict(os.environ, env, clear=True):
                subprocess.run(["git", "init", "-q", str(root)], check=True)
                (root / "a").write_text("a\n", encoding="utf-8")
                git(root, "add", "-A")
                git(root, "commit", "-q", "-m", "a")

                with self.assertRaises(planscope.PlanScopeError):
                    planscope.added_by(str(root), "HEAD", "never-added")


if __name__ == "__main__":
    unittest.main()
