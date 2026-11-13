"""The instance file's `secret_scan` and `review` keys (ADR 0063 §4, §5 E19) read and refuse every bad shape."""

import copy
import unittest

from trimtab import instance
from trimtab.instance import InstanceFileError

BASE = {"schema_version": 1, "repo": "owner/instance", "base": {"repo": "owner/core", "sha": "a" * 40}}
FLOWS = ".github/" + "workflows/"  # in two pieces: this file edits no control
SCAN = {"workflow": FLOWS + "scan.yml", "job": "secret-scan"}
REVIEW = {
    "run": {"workflow": FLOWS + "review.yml", "job": "review", "event": "pull_request"},
    "facts": {"workflow": FLOWS + "review.yml", "job": "review-facts"},
    "login": "review-bot[bot]",
    "rerequest_label": "review:run",
    "tags": {"fixed": "[fixed]", "wrong": "[wrong]", "intentional": "[intentional]", "moot": "[moot]",
             "deferred": "[deferred]"},
}


def parsed(**keys):
    return instance.parse({**BASE, **keys})


class SecretScan(unittest.TestCase):
    def test_reads_the_job(self):
        found = parsed(secret_scan=SCAN)
        self.assertEqual((found.secret_scan.workflow, found.secret_scan.job), (SCAN["workflow"], "secret-scan"))

    def test_absent_means_none(self):
        self.assertIsNone(parsed().secret_scan)

    def test_refuses_each_bad_shape(self):
        bad = {
            "not an object": [],
            "no job": {"workflow": SCAN["workflow"]},
            "workflow outside the workflows folder": {**SCAN, "workflow": "scan.yml"},
            "unknown key": {**SCAN, "extra": 1},
            "control character in the job": {**SCAN, "job": "a\nb"},
        }
        for name, value in bad.items():
            with self.subTest(name), self.assertRaises(InstanceFileError):
                parsed(secret_scan=value)


class Review(unittest.TestCase):
    def test_reads_every_field_with_defaults(self):
        found = parsed(review=REVIEW).review
        self.assertEqual((found.run.job, found.run_event, found.facts.job, found.login, found.rerequest_label,
                          found.tags["fixed"], found.review_minutes, found.watch_hours),
                         ("review", "pull_request", "review-facts", "review-bot[bot]", "review:run", "[fixed]",
                          60, 24))

    def test_absent_means_none(self):
        self.assertIsNone(parsed().review)

    def test_the_bounds_are_read(self):
        found = parsed(review={**REVIEW, "review_minutes": 30, "watch_hours": 48}).review
        self.assertEqual((found.review_minutes, found.watch_hours), (30, 48))

    def test_refuses_each_bad_shape(self):
        def with_(**changes):
            out = copy.deepcopy(REVIEW)
            out.update(changes)
            return out

        bad = {
            "not an object": [],
            "unknown key": with_(extra=1),
            "no run": {k: v for k, v in REVIEW.items() if k != "run"},
            "unknown event": with_(run={**REVIEW["run"], "event": "schedule"}),
            "no facts": {k: v for k, v in REVIEW.items() if k != "facts"},
            "facts is the review job": with_(facts={k: REVIEW["run"][k] for k in ("workflow", "job")}),
            "login not a login": with_(login="not a login"),
            "no label": with_(rerequest_label=""),
            "a tag missing": with_(tags={k: v for k, v in REVIEW["tags"].items() if k != "moot"}),
            "a tag with no brackets": with_(tags={**REVIEW["tags"], "fixed": "fixed"}),
            "two tags the same": with_(tags={**REVIEW["tags"], "wrong": "[fixed]"}),
            "a bound of zero": with_(review_minutes=0),
            "a bound that is a string": with_(watch_hours="24"),
            "a bound that is a bool": with_(watch_hours=True),
        }
        for name, value in bad.items():
            with self.subTest(name), self.assertRaises(InstanceFileError):
                parsed(review=value)


if __name__ == "__main__":
    unittest.main()
