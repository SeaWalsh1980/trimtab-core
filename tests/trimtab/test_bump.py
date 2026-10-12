"""`/trimtab-bump`: changed items between two Trimtab SHAs, flagged overrides, a gated lock write."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from instance_fixture import env_for, make_instance
from trimtab import bump, roots
from trimtab import config as project_config
from trimtab import instance as instance_file
from trimtab.capture.prblock import check_body
from trimtab.items import Item, Strength
from trimtab.registry.lookup import registry_for

TRIMTAB = roots.code_root() / "bin" / "trimtab"


RULE_V1 = """---
id_prefix: TST
---

## 1. Strategy

- **REQUIRE** hermetic unit tests.

## 2. Mocking Policy

- **PREFER** in-memory fakes over mocks for your own infrastructure.

## 3. Structure

- **PREFER** Arrange / Act / Assert.
"""
RULE_V2 = RULE_V1.replace("- **PREFER** Arrange / Act / Assert.", "- **PREFER** Arrange / Act / Assert, blank-line separated.")
RULE_V2 += "\n## 4. Data\n\n- **AVOID** magic numbers.\n"

OVERRIDES = """---
name: Overrides
---

```yaml
- id: TST-3
  reason: given/when/then reads better here
  adr: docs/adr/0001-gwt.md
  text: "**ALLOW** Given / When / Then."
- id: TST-2
  reason: unchanged upstream
  adr: docs/adr/0001-gwt.md
  text: "**ALLOW** mocks for our HTTP client."
```
"""


BASE_V1, BASE_V2 = "a" * 40, "b" * 40


def instance_json(base_sha):
    return json.dumps({"schema_version": 1, "repo": "owner/inst", "base": {"repo": "owner/base", "sha": base_sha}})


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def item(item_id, strength=Strength.PREFER, text="x", withdrawn=False):
    return Item(item_id, item_id.split("-")[0], item_id.split("-")[1], "rule", "rules/T.md", "h", strength,
                withdrawn, text)


class Changes(unittest.TestCase):
    def test_each_kind_of_change_is_named(self):
        before = {"A-1": item("A-1"), "A-2": item("A-2"), "A-3": item("A-3"), "A-4": item("A-4"), "A-5": item("A-5")}
        after = {"A-1": item("A-1"), "A-2": item("A-2", text="y"), "A-3": item("A-3", Strength.REQUIRE),
                 "A-4": item("A-4", withdrawn=True), "A-6": item("A-6")}

        found = {c.id: c.what for c in bump.changes(before, after)}

        self.assertEqual(found, {"A-2": "text", "A-3": "strength", "A-4": "withdrawn", "A-5": "removed",
                                 "A-6": "added"})


class Bump(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.base, self.project = tmp / "base", tmp / "project"
        (self.base / "rules").mkdir(parents=True)
        (self.base / "rules" / "Example.md").write_text(RULE_V1)
        (self.base / instance_file.FILE).write_text(instance_json(BASE_V1))
        git(self.base, "init", "-q")
        git(self.base, "add", "-A")
        git(self.base, "commit", "-qm", "v1")
        self.v1 = git(self.base, "rev-parse", "HEAD")
        (self.base / "rules" / "Example.md").write_text(RULE_V2)
        (self.base / instance_file.FILE).write_text(instance_json(BASE_V2))
        git(self.base, "commit", "-qam", "v2")
        self.v2 = git(self.base, "rev-parse", "HEAD")
        (self.project / ".claude" / "rules").mkdir(parents=True)
        self.lock = self.project / ".claude" / "trimtab.json"
        self.lock.write_text('{\n  "trimtab_sha": "%s",\n'
                             '  "id_prefix": "SCR",\n  "item_types": ["rule"],\n  "schema_version": 2\n}\n' % self.v1)
        (self.project / ".claude" / "rules" / "overrides.md").write_text(OVERRIDES)

    def tearDown(self):
        self._tmp.cleanup()

    def test_changed_items_and_the_overrides_citing_them(self):
        todo = bump.plan(self.project, self.base, self.v2)

        self.assertEqual([(c.id, c.what) for c in todo.changes], [("TST-3", "text"), ("TST-4", "added")])
        self.assertEqual(todo.flagged, (("TST-3", 1),))

    def test_the_plan_names_the_base_the_new_instance_pins(self):
        todo = bump.plan(self.project, self.base, self.v2)

        self.assertEqual((todo.base_old, todo.base_new, todo.base_repo), (None, BASE_V2, "owner/base"))

    def test_apply_moves_the_sha_and_adds_base_sha_after_it_keeping_the_formatting(self):
        todo = bump.plan(self.project, self.base, self.v2)
        before = self.lock.read_text()

        bump.apply(self.project, todo, bump.confirmation(todo))

        self.assertEqual(self.lock.read_text(), before.replace(
            f'"trimtab_sha": "{self.v1}",', f'"trimtab_sha": "{self.v2}",\n  "base_sha": "{BASE_V2}",'))

    def test_apply_moves_an_existing_base_sha_in_place(self):
        self.lock.write_text(self.lock.read_text().replace(
            '"id_prefix"', f'"base_sha": "{BASE_V1}",\n  "id_prefix"'))
        todo = bump.plan(self.project, self.base, self.v2)
        before = self.lock.read_text()

        bump.apply(self.project, todo, bump.confirmation(todo))

        self.assertEqual(self.lock.read_text(), before.replace(self.v1, self.v2).replace(BASE_V1, BASE_V2))

    def test_apply_writes_a_lock_the_reader_accepts(self):
        todo = bump.plan(self.project, self.base, self.v2)

        bump.apply(self.project, todo, bump.confirmation(todo))

        config, problems = project_config.load(self.project)
        self.assertEqual((problems, config.trimtab_sha, config.base_sha), ([], self.v2, BASE_V2))

    def test_apply_adds_base_sha_to_a_one_line_lock(self):
        self.lock.write_text(json.dumps({"trimtab_sha": self.v1, "id_prefix": "SCR", "schema_version": 2}))
        todo = bump.plan(self.project, self.base, self.v2)

        bump.apply(self.project, todo, bump.confirmation(todo))

        self.assertEqual(json.loads(self.lock.read_text()),
                         {"trimtab_sha": self.v2, "base_sha": BASE_V2, "id_prefix": "SCR", "schema_version": 2})

    def test_a_lock_without_base_sha_after_a_bad_one_was_removed_bumps(self):
        self.lock.write_text(self.lock.read_text().replace(
            '"id_prefix"', '"base_sha": "MAIN",\n  "id_prefix"'))
        with self.assertRaises(bump.BumpError):
            bump.plan(self.project, self.base, self.v2)
        self.lock.write_text(self.lock.read_text().replace('"base_sha": "MAIN",\n  ', ""))

        todo = bump.plan(self.project, self.base, self.v2)
        bump.apply(self.project, todo, bump.confirmation(todo))

        self.assertEqual(json.loads(self.lock.read_text())["base_sha"], BASE_V2)

    def test_apply_refuses_a_rewrite_that_would_not_read_back_and_writes_nothing(self):
        # A nested object holding the same key and value ahead of the real one:
        # the text rewrite would land on the nested value, not the lock's.
        self.lock.write_text(self.lock.read_text().replace(
            '{\n', '{\n  "notes": {"base_sha": "%s"},\n' % BASE_V1, 1).replace(
            '"id_prefix"', f'"base_sha": "{BASE_V1}",\n  "id_prefix"'))
        todo = bump.plan(self.project, self.base, self.v2)
        before = self.lock.read_text()

        with self.assertRaisesRegex(bump.BumpError, "would not read back"):
            bump.apply(self.project, todo, bump.confirmation(todo))
        self.assertEqual(self.lock.read_text(), before)

    def test_a_current_lock_with_a_stale_base_sha_still_has_something_to_bump(self):
        self.lock.write_text(self.lock.read_text().replace(self.v1, self.v2).replace(
            '"id_prefix"', f'"base_sha": "{BASE_V1}",\n  "id_prefix"'))

        todo = bump.plan(self.project, self.base, self.v2)

        self.assertFalse(todo.is_current)

    def test_a_current_lock_with_the_right_base_sha_is_current(self):
        self.lock.write_text(self.lock.read_text().replace(self.v1, self.v2).replace(
            '"id_prefix"', f'"base_sha": "{BASE_V2}",\n  "id_prefix"'))

        todo = bump.plan(self.project, self.base, self.v2)

        self.assertTrue(todo.is_current)

    def test_a_new_sha_whose_instance_json_cannot_be_read_fails_loudly(self):
        (self.base / instance_file.FILE).unlink()
        git(self.base, "commit", "-qam", "v3: no instance.json")
        v3 = git(self.base, "rev-parse", "HEAD")

        with self.assertRaisesRegex(bump.BumpError, "instance.json"):
            bump.plan(self.project, self.base, v3)

    def test_apply_refuses_when_base_sha_moved_since_the_dry_run(self):
        self.lock.write_text(self.lock.read_text().replace(
            '"id_prefix"', f'"base_sha": "{BASE_V1}",\n  "id_prefix"'))
        todo = bump.plan(self.project, self.base, self.v2)
        self.lock.write_text(self.lock.read_text().replace(BASE_V1, "c" * 40))

        with self.assertRaisesRegex(bump.BumpError, "changed since the dry run"):
            bump.apply(self.project, todo, bump.confirmation(todo))

    def test_apply_refuses_a_wrong_token_and_writes_nothing(self):
        todo = bump.plan(self.project, self.base, self.v2)
        before = self.lock.read_text()

        with self.assertRaises(bump.BumpError):
            bump.apply(self.project, todo, "000000000000")
        self.assertEqual(self.lock.read_text(), before)

    def test_apply_refuses_when_the_lock_moved_since_the_dry_run(self):
        todo = bump.plan(self.project, self.base, self.v2)
        self.lock.write_text(self.lock.read_text().replace(self.v1, "f" * 40))

        with self.assertRaisesRegex(bump.BumpError, "changed since the dry run"):
            bump.apply(self.project, todo, bump.confirmation(todo))

    def test_an_unknown_locked_sha_fails_loudly(self):
        self.lock.write_text(self.lock.read_text().replace(self.v1, "e" * 40))

        with self.assertRaisesRegex(bump.BumpError, "fetch it first"):
            bump.plan(self.project, self.base, self.v2)

    def test_the_pr_body_names_changes_and_flags(self):
        body = bump.pr_body(bump.plan(self.project, self.base, self.v2), "owner/repo")

        self.assertIn("| `TST-3` | text |", body)
        self.assertIn("`TST-3` (overrides.md entry 1): **re-confirm or drop**", body)
        self.assertIn("## Harness items applied", body)

    def test_the_pr_body_names_the_base_move(self):
        body = bump.pr_body(bump.plan(self.project, self.base, self.v2), "owner/repo")

        self.assertIn(f"`base_sha` becomes `{BASE_V2[:12]}`, the base the new instance pins", body)

    def test_the_pr_body_links_the_base_compare_when_the_lock_had_a_base_sha(self):
        self.lock.write_text(self.lock.read_text().replace(
            '"id_prefix"', f'"base_sha": "{BASE_V1}",\n  "id_prefix"'))

        body = bump.pr_body(bump.plan(self.project, self.base, self.v2), "owner/repo")

        self.assertIn(f"https://github.com/owner/base/compare/{BASE_V1}...{BASE_V2}", body)

    def cli_dry_run(self):
        """`trimtab bump` against this instance, with instance.json naming its repository."""
        (self.base / instance_file.FILE).write_text(json.dumps(
            {"schema_version": 1, "repo": "owner/inst", "base": {"repo": "owner/base", "sha": "0" * 40}}))
        body = Path(self._tmp.name) / "body.md"
        done = subprocess.run([str(TRIMTAB), "bump", "--to", self.v2, "--project", str(self.project),
                               "--instance", str(self.base), "--body-file", str(body)],
                              capture_output=True, text=True, env=env_for(None))
        return done, body

    def test_the_dry_runs_compare_link_names_the_instance_repository(self):
        done, body = self.cli_dry_run()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"https://github.com/owner/inst/compare/{self.v1}...{self.v2}", body.read_text())

    def test_the_dry_run_offers_a_bump_for_a_current_lock_that_lacks_base_sha(self):
        self.lock.write_text(self.lock.read_text().replace(self.v1, self.v2))

        done, _ = self.cli_dry_run()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("already", done.stdout)
        self.assertIn(f"base_sha: none -> {BASE_V2[:12]}", done.stdout)

    def test_a_schema_1_lock_still_works_and_warns_that_its_bindings_are_ignored(self):
        self.lock.write_text(json.dumps({"source": "owner/old", "trimtab_sha": self.v1, "id_prefix": "SCR",
                                         "schema_version": 1}))

        done, body = self.cli_dry_run()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("warning:", done.stderr)
        self.assertIn("source ignored", done.stderr)
        self.assertNotIn("owner/old", body.read_text())

    def test_the_pr_body_passes_check_pr(self):
        registry, _ = registry_for(make_instance(Path(self._tmp.name)))

        result = check_body(bump.pr_body(bump.plan(self.project, self.base, self.v2), "o/r"), registry)

        self.assertEqual(result.problems, ())

    def test_the_pr_body_passes_check_pr_against_a_registry_with_no_items(self):
        result = check_body(bump.pr_body(bump.plan(self.project, self.base, self.v2), "o/r"), {})

        self.assertEqual(result.problems, ())

    def test_the_pr_body_cites_no_item_and_says_why(self):
        result = check_body(bump.pr_body(bump.plan(self.project, self.base, self.v2), "o/r"), {})

        self.assertEqual([(a.id, bool(a.why)) for a in result.block.applied], [("none", True)])

    def test_the_pr_body_names_the_lock_as_the_file_applied_to(self):
        result = check_body(bump.pr_body(bump.plan(self.project, self.base, self.v2), "o/r"), {})

        self.assertEqual(result.block.applied[0].files, (".claude/trimtab.json",))


if __name__ == "__main__":
    unittest.main()
