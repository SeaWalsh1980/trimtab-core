"""This base's enforcement manifest: its schema, its entries against the record, and the
contract that every row's tests run in lint-test and fail with their guard removed (ADR 0063 §2)."""

import copy
import json
import unittest
from pathlib import Path

import enforcement_fixture
from enforcement_fixture import record_text, survivors
from trimtab.enforcement import manifest, record
from trimtab.enforcement.manifest import ManifestError

ROOT = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parent
ROWS = {r.id: r for r in record.parse(record_text())}
GITHUB_HOOK = {"file": "guard-github.sh", "event": "PreToolUse", "matcher": "Bash"}


def entry_for(rid, hooks=None, sha=None, tests=None):
    return {"row_sha256": sha or ROWS[rid].sha256,
            "hooks": [GITHUB_HOOK] if hooks is None else hooks,
            "tests": tests or ["test_x.Case.test_y"],
            "decision": {"function": "trimtab.adapters.guard_github:decide", "allow": 0}}


def text(rows):
    return json.dumps({"schema_version": manifest.SCHEMA_VERSION, "rows": rows})


class ReadsTheSchema(unittest.TestCase):
    def test_reads_an_entry(self):
        entries = manifest.parse(text({"E14": entry_for("E14")}))

        self.assertEqual(entries["E14"].hooks[0].file, "guard-github.sh")

    def test_reads_an_empty_manifest(self):
        self.assertEqual(manifest.parse(text({})), {})

    def test_refuses_each_malformed_shape(self):
        good = entry_for("E14")
        bad = {
            "unknown key": {**good, "extra": 1},
            "missing key": {k: v for k, v in good.items() if k != "tests"},
            "short sha": {**good, "row_sha256": "abc"},
            "no tests": {**good, "tests": []},
            "duplicate tests": {**good, "tests": ["test_x.A.test_b", "test_x.A.test_b"]},
            "not a test id": {**good, "tests": ["x.A.b"]},
            "hook path": {**good, "hooks": [{**GITHUB_HOOK, "file": "../guard-github.sh"}]},
            "unknown event": {**good, "hooks": [{**GITHUB_HOOK, "event": "Stop"}]},
            "decision outside the package": {**good, "decision": {"function": "os:system", "allow": 0}},
        }
        for name, entry in bad.items():
            with self.subTest(name), self.assertRaises(ManifestError):
                manifest.parse(text({"E14": entry}))

    def test_refuses_another_schema_version(self):
        with self.assertRaises(ManifestError):
            manifest.parse(json.dumps({"schema_version": 2, "rows": {}}))

    def test_refuses_a_key_that_is_not_a_row_id(self):
        with self.assertRaises(ManifestError):
            manifest.parse(text({"X1": entry_for("E14")}))


class JudgesAnEntryAgainstItsRow(unittest.TestCase):
    def problem(self, rid, entry):
        parsed = manifest.parse(text({rid: entry}))[rid] if entry is not None else None
        return manifest.entry_problem(ROWS[rid], parsed)

    def test_passes_an_entry_for_the_row_as_written(self):
        self.assertIsNone(self.problem("E14", entry_for("E14")))

    def test_refuses_a_base_row_with_no_entry(self):
        self.assertIn("does not map", self.problem("E14", None))

    def test_refuses_an_entry_written_for_other_text(self):
        self.assertIn("other row text", self.problem("E14", entry_for("E14", sha="0" * 64)))

    def test_refuses_a_hook_the_owner_does_not_name(self):
        hook = {**GITHUB_HOOK, "file": "guard-paths.sh"}

        self.assertIn("does not", self.problem("E14", entry_for("E14", hooks=[hook])))

    def test_refuses_a_guard_row_with_no_hook(self):
        self.assertIn("names no hook", self.problem("E14", entry_for("E14", hooks=[])))

    def test_passes_a_command_row_with_no_hook(self):
        self.assertIsNone(self.problem("E20", entry_for("E20", hooks=[])))

    def test_refuses_an_entry_for_a_project_row(self):
        self.assertIn("consuming project", self.problem("E1", entry_for("E1", hooks=[])))


class TheShippedManifest(unittest.TestCase):
    """The contract lint-test holds: E20 counts a row's tests only because this passed on the pinned base."""

    @classmethod
    def setUpClass(cls):
        cls.entries = manifest.parse((ROOT / manifest.PATH).read_text(encoding="utf-8"))

    def test_names_only_hooks_this_base_ships(self):
        missing = [h.file for e in self.entries.values() for h in e.hooks if not (ROOT / "hooks" / h.file).is_file()]

        self.assertEqual(missing, [])

    def test_names_only_tests_lint_test_runs(self):
        loader = unittest.TestLoader()
        missing = []
        for entry in self.entries.values():
            for test_id in entry.tests:
                module = test_id.split(".")[0]
                suite = loader.loadTestsFromName(test_id) if (TESTS / f"{module}.py").is_file() else None
                if suite is None or not suite.countTestCases():
                    missing.append(test_id)

        self.assertEqual(missing, [])

    def test_every_rows_tests_fail_with_its_guard_removed(self):
        survived = {rid: survivors(e.function, e.allow, e.tests) for rid, e in self.entries.items()}

        self.assertEqual({rid: s for rid, s in survived.items() if s}, {})


class TheMutationHarness(unittest.TestCase):
    """The harness itself tells a test of its guard from one that is not."""

    def test_a_refusal_test_fails_once_its_decision_allows(self):
        survived = survivors("enforcement_fixture:fixture_decision", enforcement_fixture.ALLOW,
                             ["enforcement_fixture.FixtureRefusals.test_refuses_the_forbidden_command"])

        self.assertEqual(survived, [])

    def test_a_test_that_ignores_the_decision_survives(self):
        test_id = "enforcement_fixture.FixtureRefusals.test_names_nothing_about_the_guard"

        self.assertEqual(survivors("enforcement_fixture:fixture_decision", enforcement_fixture.ALLOW, [test_id]),
                         [test_id])


if __name__ == "__main__":
    unittest.main()
