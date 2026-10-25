"""The plan-scope check: a plan run's PR judged from its diff against lists read from the base commit."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from trimtab import planscope
from trimtab.planscope import Change, Listed, Ref

TRIMTAB = Path(__file__).resolve().parents[2] / "bin" / "trimtab"
PLAN = "0019-y"
OTHER_PLAN = "0018-x"
PLAN_REF = f"plan/{PLAN}/item-01-part1"
PROGRESS = f"{planscope.PLANS_DIR}/progress/{PLAN}/item-01-part1.md"
REGISTER = f"{planscope.PLANS_DIR}/unauthorised/{PLAN}/item-01-part1-01.md"
EXISTING_ADR = "docs/adr/existing.md"
NEW_ADR = "docs/adr/new.md"
ALLOWLIST = "\n".join(["README.md", "src/**", "+docs/adr/**",
                       f"+{planscope.PLANS_DIR}/progress/**", f"{planscope.PLANS_DIR}/unauthorised/**", ""])
WRITES = "# what this plan may write\nREADME.md\n+docs/adr/**\n"
REGULAR = "100644"


def change(path, status="A", mode=REGULAR):
    return Change(status, path, mode if status != "D" else "000000")


def judged(*changes, in_base=frozenset(), writes=WRITES):
    return planscope.judge(list(changes), planscope.parse_list(ALLOWLIST), planscope.scope(writes, PLAN),
                           frozenset(in_base))


class Lists(unittest.TestCase):
    def test_a_double_star_spans_directories(self):
        entries = planscope.parse_list("src/**\n")

        self.assertIs(planscope.match(entries, "src/a/b/c.py"), Listed.YES)

    def test_a_plus_entry_is_create_only(self):
        entries = planscope.parse_list("+docs/adr/**\n")

        self.assertIs(planscope.match(entries, NEW_ADR), Listed.CREATE_ONLY)

    def test_a_plain_entry_wins_over_a_create_only_one(self):
        entries = planscope.parse_list("+docs/**\ndocs/adr/**\n")

        self.assertIs(planscope.match(entries, NEW_ADR), Listed.YES)

    def test_comments_blank_lines_and_a_bare_plus_name_nothing(self):
        entries = planscope.parse_list("# note\n\n+ # only a comment\n   \n")

        self.assertEqual(entries, ())

    def test_a_trailing_slash_means_everything_under_it(self):
        entries = planscope.parse_list("./docs/\n")

        self.assertIs(planscope.match(entries, "docs/x/y.md"), Listed.YES)

    def test_matching_is_case_sensitive(self):
        entries = planscope.parse_list("README.md\n")

        self.assertIs(planscope.match(entries, "readme.md"), Listed.NO)

    def test_a_bash_negated_class_is_read_as_negated(self):
        entries = planscope.parse_list("src/[^x]*.py\n")

        self.assertIs(planscope.match(entries, "src/xa.py"), Listed.NO)


class Refs(unittest.TestCase):
    def test_a_plan_branch_names_its_plan(self):
        self.assertEqual(planscope.plan_of(PLAN_REF), (Ref.PLAN, PLAN))

    def test_a_full_ref_names_the_same_plan(self):
        self.assertEqual(planscope.plan_of("refs/heads/" + PLAN_REF), (Ref.PLAN, PLAN))

    def test_a_non_plan_branch_is_skipped(self):
        self.assertIs(planscope.plan_of("feat/something")[0], Ref.NOT_PLAN)

    def test_a_plan_branch_without_an_item_is_malformed(self):
        self.assertIs(planscope.plan_of(f"plan/{PLAN}")[0], Ref.MALFORMED)

    def test_a_plan_name_with_dot_dot_is_malformed(self):
        self.assertIs(planscope.plan_of("plan/a..b/item-01-part1")[0], Ref.MALFORMED)


class Judge(unittest.TestCase):
    def test_an_in_scope_change_passes(self):
        problems = judged(change("README.md", "M"), change(NEW_ADR), change(PROGRESS), change(REGISTER))

        self.assertEqual(problems, [])

    def test_an_allowlisted_path_the_plan_does_not_declare_fails(self):
        problems = judged(change("src/app.py", "M"))

        self.assertEqual(len(problems), 1)
        self.assertIn("plan's scope", problems[0])

    def test_a_path_outside_the_allowlist_fails_even_when_declared(self):
        problems = judged(change("Makefile", "M"), writes="Makefile\n")

        self.assertIn("write allowlist", problems[0])

    def test_another_plans_progress_folder_fails(self):
        problems = judged(change(f"{planscope.PLANS_DIR}/progress/{OTHER_PLAN}/item-01-part1.md"))

        self.assertEqual(len(problems), 1)

    def test_another_plans_register_folder_fails(self):
        problems = judged(change(f"{planscope.PLANS_DIR}/unauthorised/{OTHER_PLAN}/item-01-part1-01.md"))

        self.assertEqual(len(problems), 1)

    def test_modifying_a_create_only_file_in_the_base_fails(self):
        problems = judged(change(EXISTING_ADR, "M"))

        self.assertIn("modifies a file already in the base", problems[0])

    def test_deleting_a_create_only_file_in_the_base_fails(self):
        problems = judged(change(EXISTING_ADR, "D"))

        self.assertIn("deletes", problems[0])

    def test_adding_a_create_only_path_the_base_already_has_fails(self):
        problems = judged(change(NEW_ADR), in_base={NEW_ADR})

        self.assertIn("base commit already has it", problems[0])

    def test_the_register_is_create_only_though_the_allowlist_entry_is_plain(self):
        problems = judged(change(REGISTER, "M"))

        self.assertIn("create-only", problems[0])

    def test_a_harness_path_fails_whatever_the_lists_say(self):
        problems = judged(change(".github/workflows/ci.yml", "M"), writes="**\n")

        self.assertIn("harness or control", problems[0])

    def test_an_authorised_plan_fails_whatever_the_lists_say(self):
        problems = judged(change(f"{planscope.AUTHORISED}/{PLAN}.writes", "M"), writes="**\n")

        self.assertIn("harness or control", problems[0])

    def test_a_link_fails(self):
        problems = judged(change(NEW_ADR, mode="120000"))

        self.assertIn("link or submodule", problems[0])

    def test_an_empty_declaration_allows_bookkeeping_only(self):
        problems = judged(change(PROGRESS), change("README.md", "M"), writes="")

        self.assertEqual(len(problems), 1)
        self.assertIn("README.md", problems[0])


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class Repository(unittest.TestCase):
    """Real git: the lists come from the base commit, the changes from base...head."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "TRIMTAB_INSTANCE"}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_CEILING_DIRECTORIES=str(root.resolve()),
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self._saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)
        self.addCleanup(self._restore)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        self.write(planscope.ALLOWLIST, ALLOWLIST)
        self.write(f"{planscope.AUTHORISED}/{PLAN}.md", "# plan\n")
        self.write(f"{planscope.AUTHORISED}/{PLAN}.writes", WRITES)
        self.write(EXISTING_ADR, "# existing\n")
        self.write("README.md", "readme\n")
        self.write("src/app.py", "x = 1\n")
        self.base = self.commit("base")
        git(self.repo, "switch", "-q", "-c", PLAN_REF)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def check(self, head_ref=PLAN_REF):
        return planscope.check(str(self.repo), self.base, "HEAD", head_ref)

    def test_an_in_scope_pr_passes(self):
        self.write("README.md", "changed\n")
        self.write(NEW_ADR, "# new\n")
        self.write(PROGRESS, "## Done\n")
        self.commit("item")

        self.assertEqual(self.check().problems, ())

    def test_an_undeclared_path_fails(self):
        self.write("src/app.py", "x = 2\n")
        self.commit("item")

        self.assertEqual(len(self.check().problems), 1)

    def test_a_rename_of_a_create_only_file_fails(self):
        git(self.repo, "mv", EXISTING_ADR, NEW_ADR)
        self.commit("item")

        self.assertTrue(any("deletes" in p for p in self.check().problems))

    def test_a_declaration_widened_on_the_branch_does_not_count(self):
        self.write(f"{planscope.AUTHORISED}/{PLAN}.writes", WRITES + "src/**\n")
        self.write("src/app.py", "x = 2\n")
        self.commit("item")

        problems = self.check().problems

        self.assertTrue(any("src/app.py" in p and "plan's scope" in p for p in problems))

    def test_a_create_only_file_added_on_main_since_the_branch_fails(self):
        self.write(NEW_ADR, "# mine\n")
        self.commit("item")
        git(self.repo, "switch", "-q", "main")
        self.write(NEW_ADR, "# theirs\n")
        self.base = self.commit("main moves")

        problems = planscope.check(str(self.repo), self.base, PLAN_REF, PLAN_REF).problems

        self.assertTrue(any("base commit already has it" in p for p in problems))

    def test_a_plan_with_no_declaration_in_the_base_fails(self):
        self.write(PROGRESS, "## Done\n")
        self.commit("item")

        problems = planscope.check(str(self.repo), self.base, "HEAD", "plan/0020-z/item-01-part1").problems

        self.assertEqual(len(problems), 2)

    def test_a_non_plan_head_ref_is_skipped(self):
        self.write("src/app.py", "x = 2\n")
        self.commit("item")

        result = self.check(head_ref="feat/other")

        self.assertEqual((result.ref, result.problems), (Ref.NOT_PLAN, ()))

    def test_the_cli_exits_1_on_a_problem(self):
        self.write("src/app.py", "x = 2\n")
        self.commit("item")

        done = self.cli(PLAN_REF)

        self.assertEqual(done.returncode, 1, done.stderr)

    def test_the_cli_exits_0_when_in_scope(self):
        self.write(NEW_ADR, "# new\n")
        self.commit("item")

        done = self.cli(PLAN_REF)

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_cli_exits_0_on_a_non_plan_branch(self):
        self.assertEqual(self.cli("main").returncode, 0)

    def test_the_cli_exits_1_on_a_malformed_plan_branch(self):
        self.assertEqual(self.cli(f"plan/{PLAN}").returncode, 1)

    def test_the_cli_exits_2_on_an_unknown_base(self):
        self.assertEqual(self.cli(PLAN_REF, base="0" * 40).returncode, 2)

    def cli(self, head_ref, base=None):
        return subprocess.run([str(TRIMTAB), "check-plan-scope", "--repo", str(self.repo),
                               "--base", base or self.base, "--head-ref", head_ref],
                              capture_output=True, text=True, env=self.env)


if __name__ == "__main__":
    unittest.main()
