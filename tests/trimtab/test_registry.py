"""Registry: rule discovery, IDs, HARNESS.md generation and its checks."""

import tempfile
import unittest
from pathlib import Path

from instance_fixture import real_instance
from trimtab.items import Strength
from trimtab.registry import harness
from trimtab.registry.rules import parse_rule_file

RULE = """---
name: Example
description: "An example rule file."
id_prefix: EXA
---

# Example

Preamble text is not an item.

## 1. First

- **PREFER** small things.

## 2. Second

- **AVOID** big things.

### 2.1 Nested

- **PROHIBIT** huge things.

```bash
# 9. a comment inside a fence is not a heading
```

## Examples

- **REQUIRE** this is in an unnumbered section and is not an item.
"""


def ids(items):
    return [i.id for i in items]


class ParseRuleFile(unittest.TestCase):
    def test_numbered_sections_become_items_under_the_frontmatter_prefix(self):
        items, problems = parse_rule_file("rules/Example.md", RULE)

        self.assertEqual(ids(items), ["EXA-1", "EXA-2", "EXA-2.1"])
        self.assertEqual(problems, [])

    def test_strength_is_the_strongest_keyword_including_subsections(self):
        items, _ = parse_rule_file("rules/Example.md", RULE)
        by_id = {i.id: i for i in items}

        self.assertEqual(by_id["EXA-1"].strength, Strength.PREFER)
        self.assertEqual(by_id["EXA-2"].strength, Strength.PROHIBIT)
        self.assertEqual(by_id["EXA-2.1"].strength, Strength.PROHIBIT)

    def test_a_section_without_keywords_has_no_strength(self):
        text = RULE.replace("- **PREFER** small things.", "Plain prose.")

        items, _ = parse_rule_file("rules/Example.md", text)

        self.assertEqual(items[0].strength, Strength.NONE)

    def test_a_file_without_id_prefix_is_a_problem(self):
        text = RULE.replace("id_prefix: EXA\n", "")

        items, problems = parse_rule_file("rules/Example.md", text)

        self.assertEqual(items, [])
        self.assertEqual([p.code for p in problems], ["missing-prefix"])

    def test_a_withdrawn_heading_keeps_its_id_and_is_marked(self):
        text = RULE.replace("## 1. First", "## 1. First (withdrawn, see EXA-2)")

        items, _ = parse_rule_file("rules/Example.md", text)

        self.assertTrue(items[0].withdrawn)
        self.assertEqual(items[0].id, "EXA-1")

    def test_a_repeated_section_number_is_a_duplicate(self):
        text = RULE.replace("## 2. Second", "## 1. Second")

        _, problems = parse_rule_file("rules/Example.md", text)

        self.assertIn("duplicate-id", [p.code for p in problems])


def write_rules(root: Path, files: dict):
    (root / "rules").mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / "rules" / name).write_text(text, encoding="utf-8")


class HarnessFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_rules(self.root, {"Example.md": RULE})

    def tearDown(self):
        self._tmp.cleanup()

    def generate(self):
        result = harness.discover(self.root)
        return harness.render(result.items)

    def test_regenerates_byte_identically(self):
        self.assertEqual(self.generate(), self.generate())

    def test_check_passes_when_harness_is_current(self):
        (self.root / "HARNESS.md").write_text(self.generate(), encoding="utf-8")

        problems = harness.check(self.root)

        self.assertEqual(problems, [])

    def test_check_fails_when_harness_is_stale(self):
        (self.root / "HARNESS.md").write_text(self.generate(), encoding="utf-8")
        write_rules(self.root, {"Example.md": RULE + "\n## 3. Third\n\nText.\n"})

        problems = harness.check(self.root)

        self.assertEqual([p.code for p in problems], ["stale"])

    def test_check_fails_when_an_id_vanishes_without_a_withdrawal_stub(self):
        (self.root / "HARNESS.md").write_text(self.generate(), encoding="utf-8")
        write_rules(self.root, {"Example.md": RULE.replace("## 1. First", "## First")})

        problems = harness.check(self.root)

        self.assertIn("vanished", [p.code for p in problems])

    def test_a_withdrawal_stub_is_not_vanished(self):
        (self.root / "HARNESS.md").write_text(self.generate(), encoding="utf-8")
        write_rules(self.root, {"Example.md": RULE.replace("## 1. First", "## 1. First (withdrawn)")})

        problems = harness.check(self.root)

        self.assertNotIn("vanished", [p.code for p in problems])

    def test_check_fails_when_two_files_share_a_prefix_and_section(self):
        write_rules(self.root, {"Copy.md": RULE})
        (self.root / "HARNESS.md").write_text(self.generate(), encoding="utf-8")

        problems = harness.check(self.root)

        self.assertIn("duplicate-id", [p.code for p in problems])

    def test_a_renamed_file_with_the_same_prefix_passes_after_regenerating(self):
        (self.root / "HARNESS.md").write_text(self.generate(), encoding="utf-8")
        previous = (self.root / "HARNESS.md").read_text(encoding="utf-8")
        (self.root / "rules" / "Example.md").rename(self.root / "rules" / "Renamed.md")

        (self.root / "HARNESS.md").write_text(self.generate(), encoding="utf-8")
        problems = harness.check(self.root, previous=previous)

        self.assertEqual(problems, [])

    def test_vanished_is_caught_against_a_previous_registry(self):
        previous = self.generate()
        write_rules(self.root, {"Example.md": RULE.replace("## 1. First", "## First")})
        (self.root / "HARNESS.md").write_text(self.generate(), encoding="utf-8")

        problems = harness.check(self.root, previous=previous)

        self.assertEqual([p.code for p in problems], ["vanished"])

    def test_write_refuses_when_an_id_would_vanish(self):
        (self.root / "HARNESS.md").write_text(self.generate(), encoding="utf-8")
        write_rules(self.root, {"Example.md": RULE.replace("## 1. First", "## First")})

        problems = harness.write(self.root)

        self.assertEqual([p.code for p in problems], ["vanished"])
        self.assertIn("EXA-1", (self.root / "HARNESS.md").read_text(encoding="utf-8"))


class InstanceDoctrine(unittest.TestCase):
    """Instance check: the rules and HARNESS.md of the instance in TRIMTAB_INSTANCE, skipped without one."""

    def setUp(self):
        self.root = real_instance()

    def test_every_base_rule_file_has_a_prefix_and_the_registry_is_current(self):
        problems = harness.check(self.root)

        self.assertEqual([(p.code, p.where) for p in problems], [])

    def test_the_eight_base_prefixes_are_the_ones_the_plan_names(self):
        result = harness.discover(self.root)
        prefixes = {i.prefix for i in result.items}

        self.assertEqual(prefixes, {"NRM", "ENG", "ARC", "TST", "REL", "SEC", "AGW", "CFG"})


if __name__ == "__main__":
    unittest.main()
