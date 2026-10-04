"""check-pr: the three-section PR block (ADR 0005)."""

import unittest
from pathlib import Path

from trimtab.capture.prblock import block_version, check_body
from trimtab.items import Item, Strength


def rule(item_id, strength, text=""):
    prefix, local = item_id.rsplit("-", 1)
    return Item(id=item_id, prefix=prefix, local_id=local, type="rule", source="rules/X.md",
                heading="h", strength=strength, text=text)


REGISTRY = {
    "ARC-3": rule("ARC-3", Strength.REQUIRE),
    "TST-2": rule("TST-2", Strength.PROHIBIT,
                  "## 2. Mocking Policy\n- **PREFER** in-memory fakes over mocks\n- **PROHIBIT** mocking internals\n"),
    "REL-2": rule("REL-2", Strength.PREFER),
    "OLD-1": Item(id="OLD-1", prefix="OLD", local_id="1", type="rule", source="rules/O.md",
                  heading="h", strength=Strength.PREFER, withdrawn=True),
}

VALID = """Summary of the change.

## Harness items applied
```yaml
- id: ARC-3
  why: ports own the types crossing the module line
  files: [src/app/ports.py]
- id: none
  why: docs-only change
```

## Harness feedback
```yaml
- id: TST-2
  kind: ambiguous
  scope: upstream
  evidence: "in-memory fake vs mock is undecided for our own HTTP client"
```

## Process cost
```yaml
plan:   { tokens: 61000, seconds: 240 }
review:
  - { pass: reviewer, status: ran, tokens: 213000, seconds: 947, findings: 3 }
  - { pass: test-analyzer, status: timed_out, tokens: 151000, seconds: 301, findings: 1 }
```
"""


def codes(result):
    return [p.code for p in result.problems]


class ValidBlocks(unittest.TestCase):
    def test_a_complete_block_passes(self):
        result = check_body(VALID, REGISTRY)

        self.assertEqual(result.problems, ())

    def test_a_passing_block_exposes_its_citations_and_feedback(self):
        result = check_body(VALID, REGISTRY)

        self.assertEqual([a.id for a in result.block.applied], ["ARC-3", "none"])
        self.assertEqual([(f.id, f.kind, f.scope) for f in result.block.feedback],
                         [("TST-2", "ambiguous", "upstream")])

    def test_feedback_and_cost_may_be_empty(self):
        body = VALID.split("## Harness feedback")[0] + "## Harness feedback\n\n## Process cost\n```yaml\n```\n"

        result = check_body(body, REGISTRY)

        self.assertEqual(result.problems, ())

    def test_a_footer_after_the_last_block_is_allowed(self):
        body = VALID + "\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"

        self.assertEqual(check_body(body, REGISTRY).problems, ())

    def test_prose_before_the_yaml_block_fails(self):
        body = VALID.replace("## Process cost\n", "## Process cost\nAbout 60k tokens.\n")

        self.assertIn("not-yaml", codes(check_body(body, REGISTRY)))

    def test_an_html_comment_from_the_template_is_allowed_in_a_section(self):
        body = VALID.replace("## Harness feedback\n", "## Harness feedback\n<!-- kinds: missed | gap -->\n")

        self.assertEqual(check_body(body, REGISTRY).problems, ())


class InvalidBlocks(unittest.TestCase):
    def test_a_missing_section_fails(self):
        body = VALID.split("## Process cost")[0]

        self.assertIn("missing-section", codes(check_body(body, REGISTRY)))

    def test_no_block_at_all_fails(self):
        self.assertIn("missing-section", codes(check_body("Just a description.", REGISTRY)))

    def test_prose_instead_of_yaml_fails(self):
        body = VALID.replace(
            "```yaml\n- id: ARC-3\n  why: ports own the types crossing the module line\n"
            "  files: [src/app/ports.py]\n- id: none\n  why: docs-only change\n```",
            "Applied ARC-3 because ports own the types.")

        self.assertIn("not-yaml", codes(check_body(body, REGISTRY)))

    def test_an_unknown_id_fails(self):
        body = VALID.replace("- id: ARC-3", "- id: ARC-99")

        self.assertIn("unknown-id", codes(check_body(body, REGISTRY)))

    def test_citing_a_withdrawn_id_fails(self):
        body = VALID.replace("- id: ARC-3", "- id: OLD-1")

        self.assertIn("withdrawn-id", codes(check_body(body, REGISTRY)))

    def test_none_without_why_fails(self):
        body = VALID.replace("- id: none\n  why: docs-only change\n", "- id: none\n")

        self.assertIn("missing-field", codes(check_body(body, REGISTRY)))

    def test_an_empty_items_applied_section_fails(self):
        body = VALID.replace(
            "- id: ARC-3\n  why: ports own the types crossing the module line\n"
            "  files: [src/app/ports.py]\n- id: none\n  why: docs-only change\n", "")

        self.assertIn("empty-section", codes(check_body(body, REGISTRY)))

    def test_a_review_pass_without_status_fails(self):
        body = VALID.replace("status: timed_out, ", "")

        self.assertIn("missing-field", codes(check_body(body, REGISTRY)))

    def test_a_status_outside_the_vocabulary_fails(self):
        body = VALID.replace("status: timed_out", "status: slow")

        self.assertIn("bad-value", codes(check_body(body, REGISTRY)))

    def test_a_feedback_kind_outside_the_vocabulary_fails(self):
        body = VALID.replace("kind: ambiguous", "kind: confusing")

        self.assertIn("bad-value", codes(check_body(body, REGISTRY)))

    def test_a_require_finding_graded_medium_fails(self):
        body = VALID.replace("  scope: upstream\n", "  scope: upstream\n  severity: MEDIUM\n") \
                    .replace("- id: TST-2\n  kind: ambiguous", "- id: ARC-3\n  kind: missed")

        self.assertIn("severity-below-binding", codes(check_body(body, REGISTRY)))

    def test_a_prefer_clause_of_a_binding_section_may_be_graded_medium(self):
        body = VALID.replace("  scope: upstream\n",
                             "  scope: upstream\n  severity: MEDIUM\n  clause: PREFER in-memory fakes\n")

        self.assertEqual(check_body(body, REGISTRY).problems, ())

    def test_a_prefer_item_may_be_graded_medium(self):
        body = VALID.replace("  scope: upstream\n", "  scope: upstream\n  severity: MEDIUM\n") \
                    .replace("- id: TST-2", "- id: REL-2")

        self.assertEqual(check_body(body, REGISTRY).problems, ())

    def test_a_problem_message_never_quotes_the_body(self):
        body = VALID.replace("- id: ARC-3", "- id: SECRET-TOKEN-abc123")

        result = check_body(body, REGISTRY)

        self.assertTrue(result.problems)
        self.assertFalse(any("abc123" in str(p) for p in result.problems))


class LegacyHeading(unittest.TestCase):
    LEGACY = "## Rules applied\n- rules/Example.md §6.2 — batching\n"

    def test_the_legacy_heading_fails_without_backfill(self):
        self.assertIn("legacy-heading", codes(check_body(self.LEGACY, REGISTRY)))

    def test_the_legacy_heading_is_accepted_under_backfill(self):
        result = check_body(self.LEGACY, REGISTRY, backfill=True)

        self.assertEqual(result.problems, ())
        self.assertTrue(result.block.legacy)


class BlockVersion(unittest.TestCase):
    """ADR 0007: no marker is version 1; a reader refuses a version it does not know."""

    def marked(self, value):
        return f"<!-- trimtab-block: {value} -->\n" + VALID

    def test_a_block_without_a_marker_is_version_1(self):
        result = check_body(VALID, REGISTRY)

        self.assertEqual(result.problems, ())
        self.assertEqual(result.block.version, 1)

    def test_a_block_marked_1_passes(self):
        result = check_body(self.marked("1"), REGISTRY)

        self.assertEqual(result.problems, ())
        self.assertEqual(result.block.version, 1)

    def test_a_block_marked_2_fails_rather_than_being_read_as_1(self):
        result = check_body(self.marked("2"), REGISTRY)

        self.assertEqual(codes(result), ["unknown-version"])
        self.assertIsNone(result.block)
        self.assertIn("update Trimtab", str(result.problems[0]))

    def test_a_malformed_marker_fails_without_quoting_it(self):
        result = check_body(self.marked("v2; rm -rf"), REGISTRY)

        self.assertEqual(codes(result), ["bad-version"])
        self.assertNotIn("rm -rf", str(result.problems[0]))

    def test_two_markers_that_disagree_fail(self):
        result = check_body("<!-- trimtab-block: 1 -->\n" + self.marked("2"), REGISTRY)

        self.assertEqual(codes(result), ["bad-version"])

    def test_a_marker_inside_a_code_fence_is_not_the_blocks_version(self):
        quoted = "Docs example:\n```text\n<!-- trimtab-block: 2 -->\n```\n" + VALID

        self.assertEqual(check_body(quoted, REGISTRY).problems, ())

    def test_a_marker_mentioned_in_prose_is_not_the_blocks_version(self):
        prose = "This adds `<!-- trimtab-block: N -->`; see `<!-- trimtab-block: 2 -->` too.\n" + VALID

        self.assertEqual(check_body(prose, REGISTRY).problems, ())

    def test_the_pr_template_is_marked_version_1(self):
        template = (Path(__file__).resolve().parents[2] / "templates" / "pull_request_template.md").read_text()

        self.assertEqual(block_version(template), (1, None))
        self.assertIn("<!-- trimtab-block: 1 -->", template)


if __name__ == "__main__":
    unittest.main()
