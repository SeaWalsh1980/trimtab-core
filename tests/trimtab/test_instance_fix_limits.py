"""The instance file's `fix_limits` key (ADR 0063 §5 E18) reads, defaults and refuses every bad shape."""

import unittest

from trimtab import instance
from trimtab.instance import InstanceFileError

BASE = {"schema_version": 1, "repo": "owner/instance", "base": {"repo": "owner/core", "sha": "a" * 40}}


def parsed(**keys):
    return instance.parse({**BASE, **keys})


class FixLimits(unittest.TestCase):
    def test_absent_means_none(self):
        self.assertIsNone(parsed().fix_limits)

    def test_reads_markers_and_thresholds(self):
        found = parsed(fix_limits={"markers": ["# skip-me"], "thresholds": [r"fail_under\s*=\s*\d+"]}).fix_limits
        self.assertEqual((found.markers, found.thresholds), (("# skip-me",), (r"fail_under\s*=\s*\d+",)))

    def test_an_empty_object_takes_the_default_markers_and_no_thresholds(self):
        found = parsed(fix_limits={}).fix_limits
        self.assertEqual(found.markers, instance.DEFAULT_FIX_MARKERS)
        self.assertEqual(found.thresholds, ())
        self.assertIn("# noqa", found.markers)
        self.assertIn("continue-on-error: true", found.markers)

    def test_named_markers_replace_the_defaults(self):
        found = parsed(fix_limits={"markers": ["# only"]}).fix_limits
        self.assertEqual(found.markers, ("# only",))

    def test_refuses_each_bad_shape(self):
        bad = {
            "not an object": [],
            "unknown key": {"extra": 1},
            "markers not a list": {"markers": "# noqa"},
            "empty marker": {"markers": [""]},
            "marker not a string": {"markers": [3]},
            "marker with a newline": {"markers": ["a\nb"]},
            "duplicate marker": {"markers": ["# a", "# a"]},
            "too many markers": {"markers": [f"# m{i}" for i in range(201)]},
            "thresholds not a list": {"thresholds": r"\d"},
            "threshold that does not compile": {"thresholds": ["("]},
            "threshold too long": {"thresholds": ["a" * 301]},
            "empty threshold": {"thresholds": [""]},
        }
        for name, value in bad.items():
            with self.subTest(name), self.assertRaises(InstanceFileError):
                parsed(fix_limits=value)


if __name__ == "__main__":
    unittest.main()
