"""The recorder is the one writer of the pass, and leaves none behind unless every row is shown (ADR 0063 §2)."""

import os
import subprocess
from unittest import mock

import unittest

from test_enforcement_gate import PROJECT, World, git
from trimtab import guardstate
from trimtab.adapters import enforcement_record
from trimtab.enforcement import gate, tools


class Records(World):
    def setUp(self):
        super().setUp()
        bare = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        url = f"https://github.com/{PROJECT}.git"
        git(self.proj, "remote", "add", "origin", url)
        git(self.proj, "config", f"url.{bare}.insteadOf", url)
        git(self.proj, "push", "-q", "origin", "main")
        git(self.proj, "update-ref", "-d", "refs/remotes/origin/main")  # the recorder fetches it itself

    def record(self, fake):
        with mock.patch.object(tools, "gh_runner", lambda: fake), \
                mock.patch.object(tools, "Git", lambda: self.git), \
                mock.patch.object(enforcement_record.roots, "instance_root", lambda: self.inst), \
                mock.patch.object(enforcement_record.roots, "code_root", lambda: self.code), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}):
            return enforcement_record.record(self.proj)

    def key(self):
        return self.gather().pinned

    def status(self):
        directory = guardstate.directory(self.config, *gate.CACHE_PARTS, create=False)
        return guardstate.read(directory / (gate.cache_name(PROJECT) + enforcement_record.STATUS_SUFFIX))

    def test_caches_a_pass_when_every_row_is_shown(self):
        self.record(self.gh())

        self.assertTrue(gate.cached_pass(self.config, PROJECT, self.key()))

    def test_says_passed_beside_the_cache(self):
        self.record(self.gh())

        self.assertEqual(self.status(), "passed\n")

    def test_removes_an_earlier_pass_when_a_row_is_not_shown(self):
        self.record(self.gh())
        key = self.key()

        self.record(self.gh(failing="E3"))

        self.assertFalse(gate.cached_pass(self.config, PROJECT, key))

    def test_names_the_rows_not_shown(self):
        self.record(self.gh(failing="E3"))

        self.assertIn("E3:", self.status())

    def test_removes_an_earlier_pass_when_a_read_fails(self):
        self.record(self.gh())
        key = self.key()

        self.record(self.gh(server_head="e" * 40))

        self.assertFalse(gate.cached_pass(self.config, PROJECT, key))

    def test_removes_an_earlier_pass_on_an_unexpected_error(self):
        self.record(self.gh())
        key = self.key()

        with mock.patch.object(gate, "decide", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            self.record(self.gh())

        self.assertFalse(gate.cached_pass(self.config, PROJECT, key))

    def test_main_never_raises(self):
        with mock.patch.object(enforcement_record, "record", side_effect=RuntimeError("boom")):
            self.assertEqual(enforcement_record.main([str(self.proj)]), 0)

    def test_refuses_an_origin_off_github(self):
        git(self.proj, "remote", "set-url", "origin", "https://example.invalid/owner/project.git")

        with self.assertRaises(gate.GateError):
            self.record(self.gh())


class ReadsTheRepository(unittest.TestCase):
    def test_reads_each_github_url_form(self):
        urls = ["https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@github.com/o/r", "https://github.com/o/r"]

        self.assertEqual({enforcement_record.REMOTE.match(u).group(1) for u in urls}, {"o/r"})

    def test_reads_no_other_host(self):
        self.assertIsNone(enforcement_record.REMOTE.match("https://github.com.evil.example/o/r.git"))


if __name__ == "__main__":
    unittest.main()
