"""guardstate.append: a private regular file, never a followed link, bounded, locked (ADR 0063 §1)."""

import os
import tempfile
import unittest
from pathlib import Path

from trimtab import guardstate


class Append(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        os.chmod(self.dir, 0o700)
        self.path = self.dir / "f.jsonl"

    def test_create_makes_a_private_file_and_appends_in_order(self):
        guardstate.append(self.path, "a\n", create=True)
        guardstate.append(self.path, "b\n")
        self.assertEqual(self.path.read_text(), "a\nb\n")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_create_refuses_a_file_planted_before_it(self):
        self.path.write_text("planted\n")
        os.chmod(self.path, 0o600)
        with self.assertRaises(guardstate.GuardStateError):
            guardstate.append(self.path, "a\n", create=True)
        self.assertEqual(self.path.read_text(), "planted\n")

    def test_a_lock_held_by_another_process_is_a_typed_error_not_a_hang(self):
        import fcntl
        from unittest import mock
        self.path.write_text("")
        os.chmod(self.path, 0o600)
        with open(self.path, "ab") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            with mock.patch.object(guardstate, "LOCK_TRIES", 3), self.assertRaises(guardstate.GuardStateError):
                guardstate.append(self.path, "a\n")

    def test_a_missing_file_is_refused_without_create(self):
        with self.assertRaises(guardstate.GuardStateError):
            guardstate.append(self.path, "a\n")

    def test_a_planted_link_is_refused_and_its_target_untouched(self):
        target = self.dir / "elsewhere"
        target.write_text("")
        os.symlink(target, self.path)
        with self.assertRaises(guardstate.GuardStateError):
            guardstate.append(self.path, "a\n")
        self.assertEqual(target.read_text(), "")

    def test_a_file_open_to_others_is_refused(self):
        self.path.write_text("")
        os.chmod(self.path, 0o644)
        with self.assertRaises(guardstate.GuardStateError):
            guardstate.append(self.path, "a\n")

    def test_an_append_that_would_pass_the_size_bound_is_refused(self):
        self.path.write_bytes(b"x" * (guardstate.MAX_BYTES - 1))
        os.chmod(self.path, 0o600)
        with self.assertRaises(guardstate.GuardStateError):
            guardstate.append(self.path, "ab")
        guardstate.append(self.path, "a")


if __name__ == "__main__":
    unittest.main()
