"""The instance file's `enforcement` key and the guard-state files it leads to refuse every bad shape (ADR 0063)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import enforcement_fixture
from trimtab import guardstate, instance
from trimtab.instance import InstanceFileError

BASE = {"schema_version": 1, "repo": "owner/instance", "base": {"repo": "owner/core", "sha": "a" * 40}}
GOOD = {"record": "docs/" + "adr/0001-the-record.md",  # in two pieces, for the citation test
        "projects": {"owner/project": {"E1": {"workflow": ".github/workflows/check.yml", "job": "check-4"}}}}


def parsed(enforcement):
    return instance.parse({**BASE, "enforcement": enforcement})


class ReadsTheEnforcementKey(unittest.TestCase):
    def test_reads_the_record_and_each_projects_jobs(self):
        found = parsed(GOOD)

        self.assertEqual((found.enforcement_record, found.enforcement_jobs["owner/project"]["E1"].job),
                         (GOOD["record"], "check-4"))

    def test_an_instance_without_it_names_nothing(self):
        found = instance.parse(BASE)

        self.assertEqual((found.enforcement_record, dict(found.enforcement_jobs)), (None, {}))

    def test_refuses_each_bad_shape(self):
        job = GOOD["projects"]["owner/project"]["E1"]
        bad = {
            "not an object": [],
            "unknown key": {**GOOD, "extra": 1},
            "record outside the ADRs": {**GOOD, "record": "README.md"},
            "record path climbing out": {**GOOD, "record": "docs/" + "adr/../0001-x.md"},
            "projects not an object": {**GOOD, "projects": []},
            "repository not owner/name": {**GOOD, "projects": {"owner": {}}},
            "same repository twice in two cases": {**GOOD, "projects": {"owner/project": {}, "Owner/Project": {}}},
            "rows not an object": {**GOOD, "projects": {"owner/project": []}},
            "row past E23": {**GOOD, "projects": {"owner/project": {"E24": job}}},
            "not a row id": {**GOOD, "projects": {"owner/project": {"X1": job}}},
            "job not an object": {**GOOD, "projects": {"owner/project": {"E1": "check"}}},
            "workflow outside workflows": {**GOOD, "projects": {"owner/project": {"E1": {**job, "workflow": "ci.yml"}}}},
            "job name with a line break": {**GOOD, "projects": {"owner/project": {"E1": {**job, "job": "a\nb"}}}},
            "unknown job key": {**GOOD, "projects": {"owner/project": {"E1": {**job, "event": "push"}}}},
        }
        for name, value in bad.items():
            with self.subTest(name), self.assertRaises(InstanceFileError):
                parsed(value)


class GuardStateFiles(unittest.TestCase):
    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.config)])
        self.dir = guardstate.directory(self.config, "x", create=True)

    def test_writes_private_files_and_reads_them_back(self):
        guardstate.write(self.dir / "f", "text")

        self.assertEqual((guardstate.read(self.dir / "f"), os.stat(self.dir / "f").st_mode & 0o777), ("text", 0o600))

    def test_reads_no_file_as_none(self):
        self.assertIsNone(guardstate.read(self.dir / "absent"))

    def test_refuses_a_file_others_can_read(self):
        (self.dir / "f").write_text("x")
        os.chmod(self.dir / "f", 0o644)

        with self.assertRaises(guardstate.GuardStateError):
            guardstate.read(self.dir / "f")

    def test_refuses_a_file_past_its_bound(self):
        guardstate.write(self.dir / "f", "x" * (guardstate.MAX_BYTES + 1))

        with self.assertRaises(guardstate.GuardStateError):
            guardstate.read(self.dir / "f")

    def test_refuses_a_file_that_is_not_utf8(self):
        fd = os.open(self.dir / "f", os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b"\xff")
        os.close(fd)

        with self.assertRaises(guardstate.GuardStateError):
            guardstate.read(self.dir / "f")

    def test_refuses_a_directory_planted_as_a_symlink(self):
        (self.config / guardstate.ROOT / "y").symlink_to(self.dir)

        with self.assertRaises(guardstate.GuardStateError):
            guardstate.directory(self.config, "y", create=False)

    def test_refuses_a_missing_directory_without_create(self):
        with self.assertRaises(guardstate.GuardStateError):
            guardstate.directory(self.config, "z", create=False)

    def test_refuses_a_path_part_that_climbs(self):
        with self.assertRaises(guardstate.GuardStateError):
            guardstate.directory(self.config, "..", create=True)


class TheHarnessCountsAnErrorAsASurvivor(unittest.TestCase):
    def test_a_test_that_errors_does_not_count_as_testing_its_guard(self):
        test_id = "enforcement_fixture.FixtureRefusals.test_errors_whatever_the_decision"

        self.assertEqual(enforcement_fixture.survivors("enforcement_fixture:fixture_decision", 0, [test_id]), [test_id])


if __name__ == "__main__":
    unittest.main()
