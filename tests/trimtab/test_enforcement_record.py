"""The doctrine record's §Enforcement table parses into exactly E1-E23 (ADR 0063 §2)."""

import unittest

from enforcement_fixture import OWNERS, record_text, row_line
from trimtab.enforcement import record
from trimtab.enforcement.record import BASE, BOTH, PROJECT, RecordError


class ParsesTheTable(unittest.TestCase):
    def test_reads_every_row_in_order(self):
        rows = record.parse(record_text())

        self.assertEqual(tuple(r.id for r in rows), record.ROW_IDS)

    def test_gives_each_owner_its_side(self):
        rows = {r.id: r for r in record.parse(record_text())}

        self.assertEqual((rows["E1"].side, rows["E14"].side, rows["E22"].side), (PROJECT, BASE, BOTH))

    def test_limits_a_plan_run_layer_row_to_guard_paths(self):
        rows = {r.id: r for r in record.parse(record_text())}

        self.assertEqual(rows["E13"].hooks, frozenset({"guard-paths.sh"}))

    def test_keeps_an_escaped_pipe_inside_its_cell(self):
        lines = "".join(row_line(r, condition="a \\| b" if r == "E5" else None) for r in OWNERS)

        rows = {r.id: r for r in record.parse(record_text(lines=lines))}

        self.assertEqual(rows["E5"].condition, "a | b")

    def test_hash_changes_when_a_row_changes(self):
        before = {r.id: r.sha256 for r in record.parse(record_text())}
        lines = "".join(row_line(r, condition="tightened" if r == "E7" else None) for r in OWNERS)

        after = {r.id: r.sha256 for r in record.parse(record_text(lines=lines))}

        self.assertEqual([r for r in before if before[r] != after[r]], ["E7"])


class RefusesAnythingElse(unittest.TestCase):
    def assertRefused(self, text):
        with self.assertRaises(RecordError):
            record.parse(text)

    def test_refuses_a_missing_row(self):
        self.assertRefused(record_text(ids=[r for r in OWNERS if r != "E9"]))

    def test_refuses_an_extra_row(self):
        self.assertRefused(record_text(lines="".join(row_line(r) for r in OWNERS) + row_line("E1").replace("E1", "E24")))

    def test_refuses_a_duplicate_row(self):
        self.assertRefused(record_text(lines="".join(row_line(r) for r in OWNERS) + row_line("E23")))

    def test_refuses_rows_out_of_order(self):
        ids = list(OWNERS)
        ids[4], ids[5] = ids[5], ids[4]

        self.assertRefused(record_text(ids=ids))

    def test_refuses_an_owner_it_does_not_know(self):
        lines = "".join(row_line(r, owner="Someone" if r == "E10" else None) for r in OWNERS)

        self.assertRefused(record_text(lines=lines))

    def test_refuses_an_empty_cell(self):
        lines = "".join(row_line(r, condition=" " if r == "E3" else None) for r in OWNERS)

        self.assertRefused(record_text(lines=lines))

    def test_refuses_two_enforcement_headings(self):
        self.assertRefused(record_text() + "\n## Enforcement\n")

    def test_refuses_no_enforcement_heading(self):
        self.assertRefused(record_text(before="# 0001\n\n## Other\n\n"))

    def test_refuses_a_heading_with_no_table(self):
        self.assertRefused("## Enforcement\n\nText.\n\n## Next\n\n" + record_text(before=""))

    def test_refuses_another_header(self):
        self.assertRefused(record_text().replace("| Owner |", "| Who |", 1))


if __name__ == "__main__":
    unittest.main()
