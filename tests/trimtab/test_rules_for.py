"""The resolver behind `/trimtab-plan` answers the loader's question, and never quietly.

Ported from a consuming project's own resolver tests, to unittest and to synthetic trees: Trimtab has no path-scoped
rules of its own, so every corpus here is built in a temporary directory.

A planner that trusts the resolver plans without any rule it omits. Three
things could make it omit one silently: a glob translator whose `*` crosses a
directory or whose dot rule differs from the loader's; a path that cannot be
governed answered as "no rule applies"; and a corpus that failed to load
answered the same way. Each is pinned here. The agreement tests are the
load-bearing ones: the translator must agree with `glob.glob(..., recursive=True)`.
"""

import glob
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from instance_fixture import env_for, make_instance
from trimtab.registry.rules_for import (
    NEW_PARENT, NO_RULE, RuleFile, corpus, glob_to_regex, governing, normalise, render, rule_globs,
    validate_glob,
)

REPO = Path(__file__).resolve().parents[2]
TRIMTAB = REPO / "bin" / "trimtab"

SYNTHETIC_FILES = [
    "src/x.py", "src/a/y.py", "src/a/b/z.py", "src/.hidden/h.py", "src/a/.dot.py", "src/.env",
    "src/persistence/p.py", "src/mod/persistence/deep/q.py", "src/clients/c.py", "src/clients/zoho/d.py",
    "pyproject.toml", "pyprojectXtoml", "abc.py", "a/c.py", ".github/workflows/ci.yml",
    "terraform/prod/.terraform.lock.hcl",
]
SYNTHETIC_PATTERNS = [
    "src/**", "src/*.py", "src/**/z.py", "src/**/persistence/**", "src/clients/*.py", "**/c.py", "**",
    "a?c.py", "pyproject.toml", ".github/**", "terraform/**",
]

TERRAFORM_RULE = '---\nid_prefix: SCR-TF\npaths: ["terraform/**"]\n---\n\n## 1. Plans\n\n- **REQUIRE** a plan.\n'
ALWAYS_RULE = "---\nid_prefix: SCR-ALL\ndescription: x\n---\n\n## 1. Always\n\n- **PREFER** small PRs.\n"


def write(root: Path, relative: str, text: str = "x") -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def tree(root: Path) -> set[str]:
    files = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root).as_posix()
        for name in filenames:
            files.add(name if rel == "." else f"{rel}/{name}")
    return files


def rule(name, *globs):
    return RuleFile(name, tuple(globs), 1)


class Translator(unittest.TestCase):
    def test_agrees_with_glob_over_a_tree_that_exercises_every_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in SYNTHETIC_FILES:
                write(root, relative)
            files = tree(root)

            for pattern in SYNTHETIC_PATTERNS:
                validate_glob(pattern)
                expected = {Path(m).as_posix() for m in glob.glob(pattern, root_dir=tmp, recursive=True)
                            if (root / m).is_file()}
                got = {f for f in files if glob_to_regex(pattern).fullmatch(f)}

                self.assertEqual(got, expected, pattern)

    def test_each_arm(self):
        cases = [
            ("src/clients/*.py", "src/clients/x.py", True),
            ("src/clients/*.py", "src/clients/zoho/x.py", False),
            ("src/**", "src/a/b/c/x.py", True),
            ("src/**/persistence/**", "src/persistence/x.py", True),
            ("src/**/x.py", "src/x.py", True),
            ("src/**/x.py", "src/a/y.py", False),
            ("a?c.py", "a/c.py", False),
            ("pyproject.toml", "pyprojectXtoml", False),
            ("src/**", "src", False),
            ("src/**", "src/.hidden/x.py", False),
            ("terraform/**", "terraform/prod/.terraform.lock.hcl", False),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertIs(glob_to_regex(pattern).fullmatch(path) is not None, expected)

    def test_a_glob_it_cannot_model_is_refused_not_literalised(self):
        for pattern in ["", "src/[ab]/**", "src/{a,b}/**", "src\\x", "/src/**", "src/", "src//x", "src/../x",
                        "src/**.py", "src/***"]:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ValueError, "unsupported glob"):
                validate_glob(pattern, "some-rule.md")


class Frontmatter(unittest.TestCase):
    def test_a_missing_paths_key_means_always_on(self):
        self.assertEqual(rule_globs("---\ndescription: x\n---\nbody\n"), ())
        self.assertEqual(rule_globs("no frontmatter at all\n"), ())

    def test_flow_and_block_lists_both_read(self):
        self.assertEqual(rule_globs('---\npaths: ["src/**"]\n---\n'), ("src/**",))
        self.assertEqual(rule_globs('---\npaths:\n  - "src/**"\n  - "a/*.py"\n---\n'), ("src/**", "a/*.py"))

    def test_a_paths_key_it_cannot_read_raises_naming_the_file(self):
        for text, reason in [
            ('---\npaths: "src/**"\n---\n', "list of glob strings"),
            ("---\npaths: []\n---\n", "empty"),
            ('---\npaths: [1, "src/**"]\n---\n', "list of strings"),
            ('---\npaths: ["src/[a]/**"]\n---\n', "unsupported glob"),
            ("---\npaths: [unclosed\n---\n", "does not parse"),
        ]:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, reason) as caught:
                rule_globs(text, "broken.md")
            self.assertIn("broken.md", str(caught.exception))

    def test_a_paths_line_outside_the_frontmatter_is_body_text(self):
        self.assertEqual(rule_globs('---\ndescription: x\n---\n\npaths: ["src/**"]\n'), ())


class Paths(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        write(self.root, "terraform/prod/main.tf")

    def tearDown(self):
        self._tmp.cleanup()

    def test_paths_are_normalised_before_matching(self):
        self.assertEqual(normalise(str(self.root / "terraform/prod/main.tf"), self.root), "terraform/prod/main.tf")
        self.assertEqual(normalise("./terraform//prod/./x.tf", self.root), "terraform/prod/x.tf")
        self.assertEqual(normalise("terraform/a/../prod/x.tf", self.root), "terraform/prod/x.tf")

    def test_a_path_that_cannot_be_governed_raises_rather_than_matching_nothing(self):
        for path, reason in [("/elsewhere/x.py", "not under the repository root"), ("../other/x.py", "escapes"),
                             ("terraform/prod/", "directory"), ("terraform/prod", "directory"), ("  ", "empty")]:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, reason):
                normalise(path, self.root)

    def test_a_file_that_does_not_exist_yet_resolves_like_its_sibling(self):
        rules = [rule("tf.md", "terraform/**")]

        resolved = governing(["terraform/prod/new.tf", "terraform/prod/main.tf"], rules, self.root)

        self.assertEqual([r.source for r in resolved["terraform/prod/new.tf"]], ["tf.md"])
        self.assertEqual(resolved["terraform/prod/new.tf"], resolved["terraform/prod/main.tf"])

    def test_a_rule_file_with_no_paths_key_loads_for_every_path(self):
        resolved = governing(["README.md"], [rule("always.md"), rule("scoped.md", "terraform/**")], self.root)

        self.assertEqual([r.source for r in resolved["README.md"]], ["always.md"])


class Corpus(unittest.TestCase):
    """A project's rules plus the base: the base always loads, so no project rules is not an error."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name).resolve()
        self.project, self.base = tmp / "project", tmp / "base"
        write(self.project, "CLAUDE.md", "# core\n")
        write(self.project, ".claude/rules/terraform.md", TERRAFORM_RULE)
        write(self.project, "terraform/prod/main.tf")
        write(self.base, "rules/Always.md", ALWAYS_RULE)

    def tearDown(self):
        self._tmp.cleanup()

    def test_project_rules_then_base_rules_with_their_items(self):
        rules = corpus(self.project, ".claude/rules", self.base)

        self.assertEqual([r.source for r in rules], [".claude/rules/terraform.md", "base:rules/Always.md"])
        self.assertEqual([i.id for i in rules[0].items], ["SCR-TF-1"])
        self.assertTrue(rules[1].always)

    def test_a_project_without_rules_is_governed_by_the_base_alone(self):
        rules = corpus(self.base, ".claude/rules", self.base)

        self.assertEqual([r.source for r in rules], ["rules/Always.md"])

    def test_a_broken_rule_file_is_named_in_the_error(self):
        write(self.project, ".claude/rules/bad.md", "---\npaths: []\n---\n")

        with self.assertRaisesRegex(ValueError, "bad.md"):
            corpus(self.project, ".claude/rules", self.base)

    def test_the_report_lists_per_path_then_the_union_once_then_the_ids(self):
        rules = corpus(self.project, ".claude/rules", self.base)
        by_path = governing(["terraform/prod/main.tf", "terraform/new/x.tf", "docs/x.md"], rules, self.project)

        out = render(by_path, self.project, ["CLAUDE.md"])

        self.assertIn("always loaded: CLAUDE.md (7 bytes)", out)
        self.assertIn("terraform/prod/main.tf: .claude/rules/terraform.md\n", out)
        self.assertIn(f"terraform/new/x.tf: .claude/rules/terraform.md  {NEW_PARENT}", out)
        self.assertIn(f"docs/x.md: {NO_RULE}", out)
        self.assertEqual(out.count("  .claude/rules/terraform.md  ("), 1)
        self.assertIn("base:rules/Always.md", out)
        self.assertIn("SCR-TF-1  REQUIRE", out)
        self.assertIn("SCR-ALL-1  PREFER", out)


class Cli(unittest.TestCase):
    """What the planner actually calls: `bin/trimtab rules-for`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name).resolve() / "project"
        write(self.project, "CLAUDE.md", "# core\n")
        write(self.project, ".claude/rules/terraform.md", TERRAFORM_RULE)
        write(self.project, "terraform/prod/main.tf")
        self.env = env_for(make_instance(Path(self._tmp.name)))

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([str(TRIMTAB), "rules-for", "--project", str(self.project), *args],
                              capture_output=True, text=True, env=self.env)

    def test_answers_for_another_checkout(self):
        done = self.run_cli("terraform/prod/main.tf")

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("terraform/prod/main.tf: .claude/rules/terraform.md", done.stdout)
        self.assertIn("SCR-TF-1", done.stdout)
        self.assertIn("base:rules/Example.md", done.stdout)  # the instance's base, always loaded

    def test_an_ungovernable_path_exits_non_zero_and_never_prints_no_rule(self):
        done = self.run_cli("/elsewhere/src/x.py")

        self.assertEqual(done.returncode, 1)
        self.assertIn("not under the repository root", done.stderr)
        self.assertNotIn(NO_RULE, done.stdout)

    def test_a_broken_corpus_exits_non_zero(self):
        write(self.project, ".claude/rules/bad.md", '---\npaths: "src/**"\n---\n')

        done = self.run_cli("terraform/prod/main.tf")

        self.assertEqual(done.returncode, 1)
        self.assertIn("bad.md", done.stderr)

    def test_cat_prints_each_matched_file_once(self):
        done = self.run_cli("--cat", "terraform/prod/main.tf")

        self.assertEqual(done.stdout.count("===== .claude/rules/terraform.md ====="), 1)
        self.assertIn("**REQUIRE** a plan.", done.stdout)


if __name__ == "__main__":
    unittest.main()
