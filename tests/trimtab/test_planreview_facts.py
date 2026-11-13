"""The E3 facts lines read from the facts job's log (ADR 0063 §4): strict v1 shapes, fail closed."""

import unittest

from trimtab import planreview as rv
from trimtab.planrows import FactsError

H1, TREE = "1" * 40, "e" * 40
KEY = "a" * 64
REVIEW = f"TRIMTAB-REVIEW v1 run=77 head={H1} tree={TREE} verdict=pass"
THREAD = ("TRIMTAB-THREAD v1 run=77 id=501 severity=high state=open resolved_by=- path=src/a.py line=12 "
          f"category=bug key={KEY}")
STAMP = "2026-11-13T10:00:00.1234567Z "


class Parse(unittest.TestCase):
    def test_reads_the_review_and_its_threads(self):
        review, threads = rv.parse_facts("\n".join([REVIEW, THREAD]))
        self.assertEqual((review.run, review.head, review.tree, review.verdict), ("77", H1, TREE, "pass"))
        self.assertEqual((threads[0].id, threads[0].severity, threads[0].path, threads[0].line, threads[0].key),
                         (501, "high", "src/a.py", 12, KEY))

    def test_actions_log_timestamps_are_stripped(self):
        review, threads = rv.parse_facts(STAMP + REVIEW + "\n" + STAMP + THREAD + "\r\n")
        self.assertEqual(len(threads), 1)

    def test_other_log_lines_are_ignored(self):
        review, _ = rv.parse_facts("##[group]Run x\nhello\n" + REVIEW + "\nTRIMTAB-NOTE whatever\n")
        self.assertEqual(review.verdict, "pass")

    def test_a_review_with_no_threads_is_read(self):
        _, threads = rv.parse_facts(REVIEW)
        self.assertEqual(threads, [])

    def test_a_missing_review_line_fails(self):
        with self.assertRaises(FactsError):
            rv.parse_facts(THREAD)

    def test_two_review_lines_fail(self):
        with self.assertRaises(FactsError):
            rv.parse_facts(REVIEW + "\n" + REVIEW)

    def test_a_version_other_than_v1_fails(self):
        with self.assertRaises(FactsError):
            rv.parse_facts(REVIEW.replace("v1", "v2"))

    def test_each_malformed_shape_fails(self):
        bad = {
            "unknown verdict": REVIEW.replace("verdict=pass", "verdict=maybe"),
            "short head": REVIEW.replace(H1, "1" * 39),
            "extra field": REVIEW + " extra=1",
            "fields out of order": f"TRIMTAB-REVIEW v1 head={H1} run=77 tree={TREE} verdict=pass",
            "a thread with an unknown severity": THREAD.replace("severity=high", "severity=severe"),
            "a thread with a non-numeric line": THREAD.replace("line=12", "line=x"),
            "a thread with a short key": THREAD.replace(KEY, "a" * 63),
            "a thread for another run": THREAD.replace("run=77", "run=78"),
            "a thread with an unknown state": THREAD.replace("state=open", "state=closed"),
            "a thread whose path holds a space": THREAD.replace("src/a.py", "src/a b.py"),
        }
        for name, line in bad.items():
            text = line if name.startswith("a thread") is False else REVIEW + "\n" + line
            with self.subTest(name), self.assertRaises(FactsError):
                rv.parse_facts(text)

    def test_a_path_holding_a_unicode_line_break_cannot_start_a_forged_line(self):
        forged = ("TRIMTAB-THREAD v1 run=77 id=501 severity=low state=resolved resolved_by=admin path=a line=1 "
                  f"category=x key={KEY}")
        for sep in (" ", "\x0c", "\x85", "\x0b", "\x1c", "\r"):
            line = THREAD.replace("path=src/a.py", f"path=src/x{sep}{forged}")
            with self.subTest(repr(sep)), self.assertRaises(FactsError):
                rv.parse_facts(REVIEW + "\n" + line)

    def test_a_path_holding_a_newline_fails_the_read(self):
        forged = ("TRIMTAB-THREAD v1 run=77 id=501 severity=low state=resolved resolved_by=admin path=a line=1 "
                  f"category=x key={KEY}")
        line = THREAD.replace("path=src/a.py", "path=src/x\n" + STAMP + forged)
        with self.assertRaises(FactsError):
            rv.parse_facts(REVIEW + "\n" + line)

    def test_a_path_outside_the_safe_characters_fails(self):
        with self.assertRaises(FactsError):
            rv.parse_facts(REVIEW + "\n" + THREAD.replace("src/a.py", "src/é.py"))

    def test_a_marker_word_inside_a_longer_line_is_not_a_facts_line(self):
        review, _ = rv.parse_facts(REVIEW + "\nmodel said: TRIMTAB-REVIEW v1 run=1 head=x tree=y verdict=pass")
        self.assertEqual(review.run, "77")


if __name__ == "__main__":
    unittest.main()
