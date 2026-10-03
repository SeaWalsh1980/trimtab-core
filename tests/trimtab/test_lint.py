"""trimtab lint: overrides (section 2a), references (F1), structure (section 6d)."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from trimtab.lint import overrides, references, structure

BASE_RULE = """---
name: Testing
description: "t"
id_prefix: TST
---

## 1. Strategy

- **REQUIRE** hermetic unit tests.

## 2. Mocking Policy

- **PREFER** in-memory fakes over mocks for your own infrastructure.
- **PROHIBIT** mocking to verify internal interactions.

## 3. Structure

- **PREFER** Arrange / Act / Assert.
"""


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def overrides_md(entries):
    return "---\nname: Overrides\ndescription: \"o\"\n---\n\n# Overrides\n\n```yaml\n" + entries + "```\n"


GOOD = """- id: TST-3
  reason: our fixtures read better as given/when/then
  adr: docs/adr/0001-gwt.md
  text: |
    **ALLOW** Given / When / Then instead of Arrange / Act / Assert.
"""


class Overrides(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.base, self.project = tmp / "base", tmp / "project"
        (self.base / "rules").mkdir(parents=True)
        (self.base / "rules" / "Example.md").write_text(BASE_RULE)
        git(self.base, "init", "-q")
        git(self.base, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "base", "--allow-empty")
        git(self.base, "add", "-A")
        git(self.base, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "rules")
        self.sha = git(self.base, "rev-parse", "HEAD")
        (self.project / ".claude" / "rules").mkdir(parents=True)
        (self.project / "docs" / "adr").mkdir(parents=True)
        (self.project / "docs" / "adr" / "0001-gwt.md").write_text("# ADR\n")
        (self.project / ".claude" / "trimtab.json").write_text(json.dumps(
            {"trimtab_sha": self.sha, "id_prefix": "PRJ", "schema_version": 2}))

    def tearDown(self):
        self._tmp.cleanup()

    def lint(self, entries):
        (self.project / ".claude" / "rules" / "overrides.md").write_text(overrides_md(entries))
        return [p.code for p in overrides.lint(self.project, self.base)]

    def test_a_prefer_override_with_an_adr_passes(self):
        self.assertEqual(self.lint(GOOD), [])

    def test_no_overrides_file_passes(self):
        self.assertEqual([p.code for p in overrides.lint(self.project, self.base)], [])

    def test_overriding_a_require_fails(self):
        self.assertIn("binding-override", self.lint(GOOD.replace("TST-3", "TST-1")))

    def test_overriding_the_prefer_clause_of_a_prohibit_section_passes(self):
        entry = GOOD.replace("id: TST-3", "id: TST-2\n  clause: PREFER in-memory fakes")

        self.assertEqual(self.lint(entry), [])

    def test_overriding_the_prohibit_clause_fails(self):
        entry = GOOD.replace("id: TST-3", "id: TST-2\n  clause: PROHIBIT mocking")

        self.assertIn("binding-override", self.lint(entry))

    def test_an_unknown_id_fails(self):
        self.assertIn("unknown-id", self.lint(GOOD.replace("TST-3", "TST-9")))

    def test_a_missing_adr_link_fails(self):
        self.assertIn("missing-field", self.lint(GOOD.replace("  adr: docs/adr/0001-gwt.md\n", "")))

    def test_an_adr_link_to_a_missing_file_fails(self):
        self.assertIn("missing-adr", self.lint(GOOD.replace("0001-gwt.md", "0009-nope.md")))

    def test_base_text_changed_since_the_lock_fails(self):
        changed = BASE_RULE.replace("Arrange / Act / Assert.", "Arrange / Act / Assert, one per test.")
        (self.base / "rules" / "Example.md").write_text(changed)
        git(self.base, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "tweak")

        self.assertIn("changed-text", self.lint(GOOD))

    def test_an_unresolvable_lock_fails_closed(self):
        cfg = self.project / ".claude" / "trimtab.json"
        cfg.write_text(cfg.read_text().replace(self.sha, "f" * 40))

        self.assertIn("unverifiable", self.lint(GOOD))


CT_REVIEW = """# ct-review

| Agent | Pass |
|---|---|
| `ct-reviewer` | The project's own rule corpus |
| `pr-review-toolkit:pr-test-analyzer` | Test coverage |
| `pr-review-toolkit:silent-failure-hunter` | Swallowed exceptions |
"""


class References(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".claude" / "commands").mkdir(parents=True)
        (self.root / ".claude" / "agents").mkdir(parents=True)
        (self.root / ".claude" / "agents" / "ct-reviewer.md").write_text("---\nname: ct-reviewer\n---\n")
        self.enabled = {"superpowers"}

    def tearDown(self):
        self._tmp.cleanup()

    def lint(self, name, text):
        (self.root / ".claude" / "commands" / name).write_text(text)
        return references.lint(self.root, base_root=None, enabled_plugins=self.enabled)

    def test_the_two_review_passes_that_never_ran_fail(self):
        problems = self.lint("ct-review.md", CT_REVIEW)

        self.assertEqual(sorted(p.where.split(" ")[-1] for p in problems),
                         ["pr-review-toolkit:pr-test-analyzer", "pr-review-toolkit:silent-failure-hunter"])

    def test_an_agent_that_exists_resolves(self):
        problems = self.lint("ok.md", "| Agent | Pass |\n|---|---|\n| `ct-reviewer` | x |\n")

        self.assertEqual(problems, [])

    def test_an_enabled_plugins_skill_resolves(self):
        problems = self.lint("ok.md", "| Skill | Why |\n|---|---|\n| `superpowers:brainstorming` | x |\n")

        self.assertEqual(problems, [])

    def test_a_subagent_type_naming_a_missing_agent_fails(self):
        problems = self.lint("x.md", 'Spawn it with subagent_type: "trimtab-planner".\n')

        self.assertEqual([p.code for p in problems], ["unresolved"])

    def test_a_built_in_agent_resolves(self):
        self.assertEqual(self.lint("x.md", "subagent_type: general-purpose\n"), [])

    def test_a_missing_trimtab_command_fails(self):
        problems = self.lint("x.md", "Then run `/trimtab-bump` to move the lock.\n")

        self.assertEqual([p.code for p in problems], ["unresolved"])

    def test_references_inside_code_fences_are_ignored(self):
        self.assertEqual(self.lint("x.md", "```\nsubagent_type: nope\n```\n"), [])


class Structure(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".claude" / "rules").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def measure(self, budget_kb=50, previous=None):
        return structure.measure(self.root, rules_dirs=[".claude/rules"], budget_kb=budget_kb, previous=previous)

    def test_counts_instruction_files_imports_and_unscoped_rules(self):
        (self.root / "CLAUDE.md").write_text("a" * 100 + "\n@docs/extra.md\n")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "extra.md").write_text("b" * 50)
        (self.root / ".claude" / "rules" / "global.md").write_text("---\nname: g\n---\n" + "c" * 10)
        (self.root / ".claude" / "rules" / "scoped.md").write_text("---\npaths: ['src/**']\n---\n" + "d" * 999)

        report = self.measure()

        self.assertEqual(sorted(f.path for f in report.files), [".claude/rules/global.md", "CLAUDE.md", "docs/extra.md"])

    def test_agents_md_counts_as_an_instruction_file(self):
        (self.root / "AGENTS.md").write_text("x\n")

        self.assertEqual([f.path for f in self.measure().files], ["AGENTS.md"])

    def test_over_budget_is_reported(self):
        (self.root / "CLAUDE.md").write_text("x" * 3000)

        self.assertIn("over-budget", [p.code for p in self.measure(budget_kb=2).findings])

    def test_an_instruction_file_over_200_lines_is_reported(self):
        (self.root / "CLAUDE.md").write_text("line\n" * 201)

        self.assertIn("too-long", [p.code for p in self.measure().findings])

    def test_growth_over_ten_percent_is_reported(self):
        (self.root / "CLAUDE.md").write_text("x" * 1200)

        report = self.measure(previous={"total_bytes": 1000})

        self.assertIn("growth", [p.code for p in report.findings])

    def test_a_file_within_every_limit_has_no_findings(self):
        (self.root / "CLAUDE.md").write_text("short\n")

        self.assertEqual(self.measure(previous={"total_bytes": 6}).findings, ())


if __name__ == "__main__":
    unittest.main()
