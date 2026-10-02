"""Citations against a diff: IDs and set arithmetic, never text fingerprints (F2, R2, R3)."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from instance_fixture import env_for, make_instance
from trimtab.capture.citations import check
from trimtab.capture.prblock import parse_applied
from trimtab.registry.rules_for import corpus, governing

REPO = Path(__file__).resolve().parents[2]
TRIMTAB = REPO / "bin" / "trimtab"

TF_RULE = """---
id_prefix: SCR-TF
paths: ["terraform/**"]
---

## 1. Plans

- **REQUIRE** a plan before apply.

## 2. Naming

- **PREFER** kebab-case names.
"""
CI_RULE = """---
id_prefix: SCR-CI
paths: [".github/**"]
---

## 1. Pins

- **PROHIBIT** unpinned actions.
"""
LOCK = ('{"source": "owner/repo", "trimtab_sha": "0", "id_prefix": "SCR", '
        '"schema_version": 1}\n')


def plan(*ids):
    entries = "".join(f"- id: {i}\n  why: applied\n" for i in ids)
    return f"## Task\n\nx\n\n## Harness items applied\n```yaml\n{entries}```\n"


class Check(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name).resolve() / "project"
        self.instance = make_instance(Path(self._tmp.name))
        for rel, text in {".claude/rules/terraform.md": TF_RULE, ".claude/rules/ci.md": CI_RULE,
                          ".claude/trimtab.json": LOCK, "terraform/main.tf": "x"}.items():
            (self.project / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.project / rel).write_text(text)
        self.rules = corpus(self.project, ".claude/rules", self.instance)
        self.registry = {i.id: i for r in self.rules for i in r.items}

    def tearDown(self):
        self._tmp.cleanup()

    def report(self, cited, files=("terraform/main.tf",)):
        return check(cited, governing(files, self.rules, self.project), self.rules)

    def test_a_binding_path_scoped_item_the_plan_did_not_cite_is_uncited(self):
        report = self.report(["SCR-TF-2"])

        self.assertEqual(report.uncited_binding, ("SCR-TF-1",))

    def test_citing_every_binding_item_in_scope_leaves_nothing_uncited(self):
        report = self.report(["SCR-TF-1"])

        self.assertEqual(report.uncited_binding, ())

    def test_a_cited_path_scoped_item_that_reaches_no_changed_file_is_out_of_scope(self):
        report = self.report(["SCR-TF-1", "SCR-CI-1"])

        self.assertEqual(report.out_of_scope, ("SCR-CI-1",))

    def test_always_loaded_items_are_counted_never_reported_uncited(self):
        report = self.report(["SCR-TF-1", "TST-2"])

        self.assertEqual(report.cited_always, ("TST-2",))
        self.assertNotIn("TST-2", report.out_of_scope)
        self.assertTrue(all(not i.startswith("TST") for i in report.uncited_binding))

    def test_the_line_is_stable(self):
        self.assertEqual(self.report(["SCR-TF-1"]).line(),
                         "CITATIONS: plan=present cited=1 always_loaded=0 in_scope_binding=1 "
                         "uncited_binding=0 out_of_scope=0 unknown=0")

    def test_a_plan_is_parsed_by_the_pr_block_parser(self):
        applied, problems = parse_applied(plan("SCR-TF-1", "NOPE-9"), self.registry)

        self.assertEqual([a.id for a in applied], ["SCR-TF-1", "NOPE-9"])
        self.assertEqual([p.code for p in problems], ["unknown-id"])

    def test_a_plan_in_a_newer_block_version_is_refused_not_read_as_1(self):
        applied, problems = parse_applied("<!-- trimtab-block: 2 -->\n" + plan("SCR-TF-1"), self.registry)

        self.assertEqual(applied, ())
        self.assertEqual([p.code for p in problems], ["unknown-version"])

    def test_the_cli_reports_uncited_items_for_a_file_list(self):
        (self.project / "plan.md").write_text(plan("SCR-TF-2"))

        done = subprocess.run([str(TRIMTAB), "citations", "--project", str(self.project), "--plan-file",
                               str(self.project / "plan.md"), "--files", "terraform/main.tf"],
                              capture_output=True, text=True, env=env_for(self.instance))

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("uncited binding (path-scoped, in scope): SCR-TF-1", done.stdout)
        self.assertIn("CITATIONS: plan=present cited=1", done.stdout)


if __name__ == "__main__":
    unittest.main()
