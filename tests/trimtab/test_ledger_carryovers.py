"""Carry-overs from PR 172's review (ADR 0063 Amendment 2): a torn append never glues two records, and a
dangling link at the plan directory is not 'absent'."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trimtab import guardstate, planledger as pl


class TornAppend(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        os.chmod(self.dir, 0o700)
        self.path = self.dir / "f.jsonl"

    def private(self, text):
        self.path.write_text(text)
        os.chmod(self.path, 0o600)

    def test_a_file_that_does_not_end_in_a_newline_is_refused_for_append(self):
        self.private('{"v": 1')
        with self.assertRaises(guardstate.GuardStateError):
            guardstate.append(self.path, "next\n")
        self.assertEqual(self.path.read_text(), '{"v": 1')

    def test_an_empty_file_and_a_file_ending_in_a_newline_take_an_append(self):
        self.private("")
        guardstate.append(self.path, "a\n")
        guardstate.append(self.path, "b\n")
        self.assertEqual(self.path.read_text(), "a\nb\n")

    def test_the_line_is_written_in_one_write(self):
        self.private("")
        writes = []
        real = os.write
        with mock.patch.object(guardstate.os, "write", lambda fd, data: writes.append(data) or real(fd, data)):
            guardstate.append(self.path, "one line\n")
        self.assertEqual(writes, [b"one line\n"])

    def test_a_short_write_is_an_error_not_a_torn_record(self):
        self.private("")
        with mock.patch.object(guardstate.os, "write", lambda fd, data: len(data) - 1):
            with self.assertRaises(guardstate.GuardStateError):
                guardstate.append(self.path, "one line\n")


class DanglingPlanDirectory(unittest.TestCase):
    def test_a_dangling_link_at_the_plan_directory_is_not_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            os.chmod(cfg, 0o700)
            led = pl.Ledger(cfg, "o/r", "0008-slug", 2, 1, clock=lambda: "t")
            led.start(remote_branch_exists=False)
            plan_dir = led.path.parent
            plan_dir.rename(plan_dir.with_name("moved"))
            os.symlink(cfg / "gone", plan_dir)  # dangling
            with self.assertRaises(pl.LedgerError):
                led.exists()
            with self.assertRaises(pl.LedgerError):
                led.start(remote_branch_exists=False)


if __name__ == "__main__":
    unittest.main()
