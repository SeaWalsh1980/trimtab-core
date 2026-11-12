"""The recorder is the one writer of the pass, and leaves none behind unless every row is shown (ADR 0063 §2).

GitHub, a true external system, is faked: gh by FakeGh, and the clean fetch by a local depth-1 clone."""

import json
import os
import py_compile
import re
import signal
import subprocess
import sys
import tempfile
import threading
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

        def fetch_clean(repository, branch, into, auth):
            self.fetched.append((repository, branch))
            subprocess.run(["git", "clone", "-q", "--bare", "--depth", "1", "--branch", branch,
                            f"file://{self.bare}", str(into)], check=True)
            return git(into, "rev-parse", f"refs/heads/{branch}")

        self.git.fetch_clean = fetch_clean

    def record(self, fake, epoch="current"):
        """Run the recorder as the hook does: under the epoch the hook just wrote, unless told otherwise."""
        if epoch == "current":
            epoch = self.new_epoch()
        with mock.patch.object(tools, "gh_runner", lambda auth=None: fake), mock.patch.object(tools, "GhAuth", lambda: None), \
                mock.patch.object(tools, "Git", lambda **kw: self.git), \
                mock.patch.object(enforcement_record.roots, "instance_root", lambda: self.inst), \
                mock.patch.object(enforcement_record.roots, "code_root", lambda: self.code), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}):
            return enforcement_record.record(self.proj, epoch)

    def new_epoch(self):
        epoch = os.urandom(16).hex()
        directory = guardstate.directory(self.config, *gate.CACHE_PARTS, create=True)
        guardstate.write(directory / enforcement_record.EPOCH, epoch)
        return epoch

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

    def test_writes_no_pass_when_a_later_session_started_a_new_epoch(self):
        fetch = self.git.fetch_clean

        def fetch_then_new_session(*args):
            tip = fetch(*args)
            self.new_epoch()  # a later session's hook, while this recorder runs
            return tip

        self.git.fetch_clean = fetch_then_new_session

        self.assertIn("new epoch", self.record(self.gh()))

    def test_writes_no_pass_under_an_epoch_that_is_not_the_current_one(self):
        stale = self.new_epoch()
        self.new_epoch()  # a later session started after the hook handed this recorder `stale`

        self.assertFalse(self.record(self.gh(), epoch=stale) == enforcement_record.PASSED)

    def test_removes_its_pass_when_the_epoch_moves_during_the_write(self):
        real = gate.record_pass

        def write_then_new_session(*args):
            real(*args)
            self.new_epoch()

        with mock.patch.object(gate, "record_pass", write_then_new_session):
            line = self.record(self.gh())

        self.assertEqual((line.startswith("not passed"), gate.cached_pass(self.config, PROJECT, self.key())),
                         (True, False))

    def test_writes_no_pass_with_no_epoch(self):
        self.assertIn("no epoch", self.record(self.gh(), epoch=None))

    def test_writes_no_pass_when_the_epoch_is_a_planted_link(self):
        directory = guardstate.directory(self.config, *gate.CACHE_PARTS, create=True)
        target = self.tmp / "planted"
        target.write_text("0" * 32)
        os.symlink(target, directory / enforcement_record.EPOCH)

        self.assertIn("no epoch", self.record(self.gh(), epoch=None))

    def test_has_no_pass_when_it_starts_reading(self):
        gate.record_pass(self.config, PROJECT, PINNED)
        seen = []
        real = gate.gather

        def gather(repository, *args, **kwargs):
            seen.append(any(directory.name.endswith(".json") for directory in
                            (self.config / guardstate.ROOT / gate.CACHE_PARTS[0]).iterdir()))
            return real(repository, *args, **kwargs)

        with mock.patch.object(gate, "gather", gather):
            self.record(self.gh())

        self.assertEqual(seen, [False])  # the pass a SIGKILL would otherwise leave was gone before any read

    def test_waits_for_a_recorder_already_running(self):
        held = threading.Event()
        release = threading.Event()

        def hold():
            with enforcement_record._locked(self.config):
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        held.wait(5)
        done = []
        runner = threading.Thread(target=lambda: done.append(self.record(self.gh())))
        runner.start()
        runner.join(0.5)
        fetched_while_held = list(self.fetched)  # it must not have begun reading under another's lock
        release.set()
        holder.join(5)
        runner.join(10)

        self.assertEqual((fetched_while_held, done), ([], [enforcement_record.PASSED]))


class Clears(World):
    def setUp(self):
        super().setUp()
        git(self.proj, "remote", "add", "origin", f"https://github.com/{PROJECT}.git")

    def clear(self):
        with mock.patch.object(tools, "Git", lambda **kw: self.git), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}):
            enforcement_record.clear(self.proj)

    def test_clears_every_pass_whatever_origin_names(self):
        gate.record_pass(self.config, PROJECT, PINNED)
        git(self.proj, "remote", "set-url", "origin", "https://github.com/other/repo.git")  # pointed elsewhere

        self.clear()

        self.assertFalse(gate.cached_pass(self.config, PROJECT, PINNED))

    def test_refuses_to_leave_passes_in_a_directory_others_can_write(self):
        gate.record_pass(self.config, PROJECT, PINNED)
        os.chmod(self.config / guardstate.ROOT / gate.CACHE_PARTS[0], 0o777)

        with self.assertRaises(guardstate.GuardStateError):
            self.clear()


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

    def run_hook(self, path="/usr/bin:/bin", **env):
        payload = json.dumps({"cwd": str(self.tmp / "project"), "session_id": "s1"})
        base = {"PATH": path, "HOME": str(self.tmp), "CLAUDE_CONFIG_DIR": str(self.config),
                "TRIMTAB_ENFORCEMENT_RECORDER": "0"}
        subprocess.run([str(self.HOOK)], input=payload, text=True, capture_output=True,
                       env={**base, **env}, check=True, timeout=30)

    def enforcement(self):
        return self.config / guardstate.ROOT / gate.CACHE_PARTS[0]

    def test_a_run_session_starts_with_no_pass(self):
        self.run_hook(TRIMTAB_PLAN_RUN="1")

        self.assertFalse(gate.cached_pass(self.config, "other/repo", PINNED))

    def test_an_interactive_session_touches_no_pass(self):
        self.run_hook()

        self.assertTrue(gate.cached_pass(self.config, "other/repo", PINNED))

    def test_a_run_session_starts_a_new_epoch_the_recorder_reads(self):
        self.run_hook(TRIMTAB_PLAN_RUN="1")

        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}):
            self.assertIsNotNone(enforcement_record._epoch(self.config))

    def test_the_epoch_replaces_a_planted_link_rather_than_following_it(self):
        target = self.tmp / "planted"
        target.write_text("x")
        os.symlink(target, self.enforcement() / enforcement_record.EPOCH)

        self.run_hook(TRIMTAB_PLAN_RUN="1")

        self.assertEqual((target.read_text(), (self.enforcement() / enforcement_record.EPOCH).is_symlink()),
                         ("x", False))

    def test_the_bash_fallback_clears_when_the_python_clear_fails(self):
        # The python clear refuses a cache directory others can write; only the bash fallback removes the pass then.
        os.chmod(self.enforcement(), 0o777)
        self.addCleanup(os.chmod, self.enforcement(), 0o700)

        self.run_hook(TRIMTAB_PLAN_RUN="1")

        self.assertEqual(list(self.enforcement().glob("*.json")), [])

    def test_runs_no_tool_from_a_directory_on_the_inherited_path(self):
        shims = self.tmp / "shims"
        shims.mkdir()
        marker = self.tmp / "shim-ran"
        for name in ("rm", "readlink", "dirname", "find", "od", "tr", "mktemp", "mv", "git", "jq", "python3", "cat"):
            (shims / name).write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
            (shims / name).chmod(0o755)

        self.run_hook(path=f"{shims}:/usr/bin:/bin", TRIMTAB_PLAN_RUN="1")

        self.assertFalse(marker.exists())

    def test_the_hook_names_its_shell_by_absolute_path(self):
        self.assertEqual(self.HOOK.read_text().splitlines()[0], "#!/bin/bash")

    def test_every_interpreter_the_hook_starts_skips_bytecode_beside_the_code(self):
        text = self.HOOK.read_text()
        starts = [line for line in text.splitlines() if re.search(r'(python3|"\$py") -I', line)]

        self.assertEqual([line for line in starts if "pycache_prefix" not in line], [])

    def test_the_pycache_prefix_stops_a_planted_pyc_loading(self):
        pkg = self.tmp / "pkg"
        (pkg / "__pycache__").mkdir(parents=True)
        (pkg / "m.py").write_text("WHO = 'source'\n")
        planted = self.tmp / "planted.py"
        planted.write_text("WHO = 'planted'\n")
        py_compile.compile(str(planted), cfile=str(pkg / "__pycache__" / f"m.{sys.implementation.cache_tag}.pyc"),
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        probe = f"import sys; sys.path.insert(0, {str(pkg)!r}); import m; print(m.WHO)"
        run = lambda *flags: subprocess.run(["/usr/bin/python3", "-I", "-B", *flags, "-c", probe],
                                            capture_output=True, text=True).stdout.strip()

        self.assertEqual((run(), run("-X", "pycache_prefix=/dev/null/no-pycache")), ("planted", "source"))

    def test_trusts_the_systems_python_through_its_symlink(self):
        out = subprocess.run(["bash", "-c", f'source "{self.HOOK}"; sm_trusted python3'], capture_output=True,
                             text=True, env={"PATH": "/usr/bin:/bin"})

        self.assertTrue(out.stdout.strip().startswith(("/usr/bin/", "/bin/")))

    def test_trusts_no_binary_this_user_owns(self):
        own = self.tmp / "bin"
        own.mkdir()
        (own / "tool").write_text("#!/bin/sh\n")
        (own / "tool").chmod(0o755)
        out = subprocess.run(["bash", "-c", f'source "{self.HOOK}"; SM_TRUSTED_DIRS={own}; sm_trusted tool'],
                             capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})

        self.assertEqual((out.returncode, out.stdout), (1, ""))

    def test_trusts_no_binary_that_is_not_there(self):
        out = subprocess.run(["bash", "-c", f'source "{self.HOOK}"; sm_trusted no-such-tool-xyz'],
                             capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})

        self.assertEqual((out.returncode, out.stdout), (1, ""))


class Main(unittest.TestCase):
    def test_raises_sigterm_as_an_exit_so_the_cleanup_runs(self):
        seen = []
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)

        with mock.patch.object(enforcement_record, "record",
                               lambda p, e=None: seen.append(signal.getsignal(signal.SIGTERM))):
            enforcement_record.main(["record", "/nonexistent"])

        self.assertEqual(seen, [enforcement_record._terminate])

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
