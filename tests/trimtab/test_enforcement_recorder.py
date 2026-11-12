"""The recorder is the one writer of the pass, and leaves none behind unless every row is shown (ADR 0063 §2).

GitHub, a true external system, is faked: gh by FakeGh, and the clean fetch by a local depth-1 clone."""

import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_enforcement_gate import PINNED, PROJECT, World, git
from trimtab import guardstate
from trimtab.adapters import enforcement_record
from trimtab.enforcement import gate, tools


class Records(World):
    def setUp(self):
        super().setUp()
        self.bare = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        git(self.proj, "remote", "add", "origin", f"https://github.com/{PROJECT}.git")
        git(self.proj, "push", "-q", str(self.bare), "main")
        self.fetched = []

        def fetch_clean(repository, branch, into):
            self.fetched.append((repository, branch))
            subprocess.run(["git", "clone", "-q", "--bare", "--depth", "1", "--branch", branch,
                            f"file://{self.bare}", str(into)], check=True)
            return git(into, "rev-parse", f"refs/heads/{branch}")

        self.git.fetch_clean = fetch_clean

    def record(self, fake):
        with mock.patch.object(tools, "gh_runner", lambda: fake), \
                mock.patch.object(tools, "Git", lambda: self.git), \
                mock.patch.object(enforcement_record.roots, "instance_root", lambda: self.inst), \
                mock.patch.object(enforcement_record.roots, "code_root", lambda: self.code), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}):
            return enforcement_record.record(self.proj)

    def key(self):
        return self.gather().pinned

    def status(self, name=None):
        directory = guardstate.directory(self.config, *gate.CACHE_PARTS, create=False)
        return guardstate.read(directory / ((name or gate.cache_name(PROJECT)) + enforcement_record.STATUS_SUFFIX))

    def test_caches_a_pass_when_every_row_is_shown(self):
        self.record(self.gh())

        self.assertTrue(gate.cached_pass(self.config, PROJECT, self.key()))

    def test_fetches_the_configured_repository_from_github(self):
        self.record(self.gh())

        self.assertEqual(self.fetched, [(PROJECT, "main")])

    def test_ignores_the_projects_own_remote_tracking_ref(self):
        git(self.proj, "update-ref", "-d", "refs/remotes/origin/main")

        self.record(self.gh())

        self.assertTrue(gate.cached_pass(self.config, PROJECT, self.key_from_fetch()))

    def key_from_fetch(self):
        git(self.proj, "update-ref", "refs/remotes/origin/main", self.head)
        return self.key()

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

    def test_removes_an_earlier_pass_when_terminated(self):
        self.record(self.gh())
        key = self.key()

        def killed(*args, **kwargs):
            enforcement_record._terminate(signal.SIGTERM, None)

        with mock.patch.object(gate, "gather", killed), self.assertRaises(SystemExit):
            self.record(self.gh())

        self.assertFalse(gate.cached_pass(self.config, PROJECT, key))

    def test_removes_every_pass_when_the_repository_cannot_be_named(self):
        gate.record_pass(self.config, "other/repo", PINNED)
        git(self.proj, "remote", "set-url", "origin", "https://example.invalid/owner/project.git")

        self.record(self.gh())

        self.assertFalse(gate.cached_pass(self.config, "other/repo", PINNED))

    def test_writes_a_status_when_the_repository_cannot_be_named(self):
        git(self.proj, "remote", "set-url", "origin", "https://example.invalid/owner/project.git")

        self.record(self.gh())

        self.assertIn("not passed", self.status(enforcement_record.UNKNOWN))

    def test_removes_an_earlier_pass_when_git_cannot_be_trusted(self):
        self.record(self.gh())
        key = self.key()

        with mock.patch.object(tools, "Git", side_effect=tools.ToolError("no trusted git")), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}):
            enforcement_record.record(self.proj)

        self.assertFalse(gate.cached_pass(self.config, PROJECT, key))

    def test_leaves_another_projects_pass_alone(self):
        gate.record_pass(self.config, "other/repo", PINNED)

        self.record(self.gh(failing="E3"))

        self.assertTrue(gate.cached_pass(self.config, "other/repo", PINNED))


class Clears(World):
    def setUp(self):
        super().setUp()
        git(self.proj, "remote", "add", "origin", f"https://github.com/{PROJECT}.git")

    def clear(self):
        with mock.patch.object(tools, "Git", lambda: self.git), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}):
            enforcement_record.clear(self.proj)

    def test_clears_this_projects_pass(self):
        gate.record_pass(self.config, PROJECT, PINNED)

        self.clear()

        self.assertFalse(gate.cached_pass(self.config, PROJECT, PINNED))

    def test_clears_every_pass_when_the_project_cannot_be_named(self):
        gate.record_pass(self.config, "other/repo", PINNED)
        git(self.proj, "remote", "remove", "origin")

        self.clear()

        self.assertFalse(gate.cached_pass(self.config, "other/repo", PINNED))


class TheHookClearsFirst(unittest.TestCase):
    """hooks/session-mode.sh removes a cached pass synchronously in a run session, before any recorder runs."""

    HOOK = Path(__file__).resolve().parents[2] / "hooks" / "session-mode.sh"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.config = self.tmp / "config"
        self.config.mkdir()
        gate.record_pass(self.config, "other/repo", PINNED)
        (self.tmp / "project").mkdir()

    def run_hook(self, **env):
        payload = json.dumps({"cwd": str(self.tmp / "project"), "session_id": "s1"})
        base = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "CLAUDE_CONFIG_DIR": str(self.config),
                "TRIMTAB_ENFORCEMENT_RECORDER": "0"}
        subprocess.run(["bash", str(self.HOOK)], input=payload, text=True, capture_output=True,
                       env={**base, **env}, check=True, timeout=30)

    def test_a_run_session_starts_with_no_pass(self):
        self.run_hook(TRIMTAB_PLAN_RUN="1")

        self.assertFalse(gate.cached_pass(self.config, "other/repo", PINNED))

    def test_an_interactive_session_touches_no_pass(self):
        self.run_hook()

        self.assertTrue(gate.cached_pass(self.config, "other/repo", PINNED))


class Main(unittest.TestCase):
    def test_a_failed_record_exits_0(self):
        with mock.patch.object(enforcement_record, "record", side_effect=RuntimeError("boom")):
            self.assertEqual(enforcement_record.main(["record", "/nonexistent"]), 0)

    def test_a_failed_clear_exits_1_so_the_hook_removes_every_pass(self):
        with mock.patch.object(enforcement_record, "clear", side_effect=RuntimeError("boom")):
            self.assertEqual(enforcement_record.main(["clear", "/nonexistent"]), 1)

    def test_an_unknown_subcommand_does_nothing(self):
        with mock.patch.object(enforcement_record, "record", side_effect=AssertionError("ran")):
            self.assertEqual(enforcement_record.main(["write", "/x"]), 0)


class ReadsTheRepository(unittest.TestCase):
    def test_reads_each_github_url_form(self):
        urls = ["https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@github.com/o/r", "https://github.com/o/r"]

        self.assertEqual({enforcement_record.REMOTE.match(u).group(1) for u in urls}, {"o/r"})

    def test_reads_no_other_host(self):
        self.assertIsNone(enforcement_record.REMOTE.match("https://github.com.evil.example/o/r.git"))


if __name__ == "__main__":
    unittest.main()
