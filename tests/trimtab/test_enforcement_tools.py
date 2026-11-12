"""The gate's git and gh resist what a session can write (ADR 0063 §2). Real git in temporary repos."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_scrub import isolate_git
from trimtab.enforcement import tools
from trimtab.enforcement.tools import ToolError


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        isolate_git(self, self.tmp)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.genuine = commit(self.repo, "f", "genuine\n")
        self.git = tools.Git(environ={"HOME": str(self.tmp)})


class ResistsRedirection(Repo):
    def test_ignores_a_replace_ref(self):
        forged = commit(self.repo, "f", "forged\n")
        git(self.repo, "replace", self.genuine, forged)

        self.assertEqual(self.git(self.repo, "show", f"{self.genuine}:f"), "genuine\n")

    def test_refuses_a_repository_with_a_grafts_file(self):
        grafts = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-path", "info/grafts"))
        grafts.parent.mkdir(parents=True, exist_ok=True)
        grafts.write_text(self.genuine + "\n")

        with self.assertRaises(ToolError):
            self.git(self.repo, "log", "--format=%H")

    def test_refuses_a_shallow_clone(self):
        commit(self.repo, "g", "x\n")
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.repo}", str(clone)], check=True)

        with self.assertRaises(ToolError):
            self.git(clone, "log", "--format=%H")

    def test_does_not_run_a_configured_fsmonitor(self):
        marker = self.tmp / "ran"
        script = self.tmp / "monitor.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\n")
        script.chmod(0o755)
        git(self.repo, "config", "core.fsmonitor", str(script))

        self.git(self.repo, "status", "--porcelain")

        self.assertFalse(marker.exists())

    def test_ignores_git_steering_variables_in_the_environment(self):
        other = self.tmp / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        commit(other, "f", "other\n")
        steered = tools.Git(environ={"HOME": str(self.tmp), "GIT_DIR": str(other / ".git")})

        self.assertEqual(steered(self.repo, "show", "HEAD:f"), "genuine\n")


class BuildsItsEnvironment(unittest.TestCase):
    def test_git_keeps_no_steering_or_config_variable(self):
        env = tools.git_env({"HOME": "/h", "GIT_DIR": "/x", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a",
                             "GIT_REPLACE_REF_BASE": "r", "PATH": "/evil"})

        self.assertEqual(sorted(k for k in env if k.startswith("GIT_") and k not in
                                ("GIT_NO_REPLACE_OBJECTS", "GIT_LITERAL_PATHSPECS", "GIT_CONFIG_NOSYSTEM",
                                 "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT", "GIT_NO_LAZY_FETCH")), [])

    def test_git_reads_no_global_config(self):
        self.assertEqual(tools.git_env({})["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_paths_are_the_system_directories(self):
        self.assertEqual(tools.git_env({"PATH": "/evil"})["PATH"], "/usr/bin:/bin")

    def test_git_keeps_the_proxy_settings(self):
        self.assertEqual(tools.git_env({"HTTPS_PROXY": "http://proxy:3128"})["HTTPS_PROXY"], "http://proxy:3128")


class FindsTrustedBinaries(unittest.TestCase):
    def test_finds_the_systems_git(self):
        self.assertTrue(tools.trusted("git").startswith(("/usr/bin/", "/bin/")))

    def test_refuses_a_binary_this_user_owns(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(directory)])
        (directory / "git").write_text("#!/bin/sh\n")
        (directory / "git").chmod(0o755)

        with self.assertRaises(ToolError):
            tools.trusted("git", dirs=(str(directory),))

    def test_refuses_a_name_in_no_system_directory(self):
        with self.assertRaises(ToolError):
            tools.trusted("no-such-tool-xyz")


FIXTURE_TOKEN = "fixture" + "_" + "0" * 20  # built at run time: not a credential shape a scanner reads


class IsolatesGhsAuth(unittest.TestCase):
    def auth(self, environ):
        return tools.GhAuth(environ=environ, binary="/bin/true")

    def test_uses_the_token_from_the_environment(self):
        self.assertEqual(self.auth({"GH_TOKEN": FIXTURE_TOKEN}).token, FIXTURE_TOKEN)

    def test_runs_gh_with_a_fresh_empty_config_dir(self):
        auth = self.auth({"GH_TOKEN": FIXTURE_TOKEN, "GH_CONFIG_DIR": "/session/written"})

        self.assertEqual((auth.env["GH_CONFIG_DIR"] != "/session/written", list(auth.config_dir.iterdir())), (True, []))

    def test_pins_the_host_to_github(self):
        self.assertEqual(self.auth({"GH_TOKEN": FIXTURE_TOKEN, "GH_HOST": "evil.example"}).env["GH_HOST"], "github.com")

    def test_refuses_a_token_of_an_unexpected_shape(self):
        with self.assertRaises(ToolError):
            self.auth({"GH_TOKEN": "x y"})

    def test_refuses_when_gh_has_no_token(self):
        with self.assertRaises(ToolError):
            tools.GhAuth(environ={"HOME": "/nonexistent"}, binary="/bin/false")


class RunsOnlyGh(unittest.TestCase):
    def test_refuses_another_program(self):
        run = tools.gh_runner(tools.GhAuth(environ={"GH_TOKEN": FIXTURE_TOKEN}, binary="/bin/true"))

        with self.assertRaises(ToolError):
            run(["sh", "-c", "true"], None)


class VerifiesObjects(Repo):
    def objects_dir(self, repo):
        return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects"))

    def test_reads_a_file_at_a_commit(self):
        self.assertEqual(self.git.objects(self.repo).file(self.genuine, "f"), b"genuine\n")

    def test_refuses_an_object_whose_file_was_substituted(self):
        blob = git(self.repo, "rev-parse", f"{self.genuine}:f")
        other = self.tmp / "other"
        other.mkdir()
        git(other, "init", "-q")
        (other / "x").write_text("forged\n")
        forged = git(other, "hash-object", "-w", "x")
        source = self.objects_dir(other) / forged[:2] / forged[2:]
        target = self.objects_dir(self.repo) / blob[:2] / blob[2:]
        target.chmod(0o644)
        target.write_bytes(source.read_bytes())  # the forged content under the genuine name

        with self.assertRaises(ToolError):
            self.git.objects(self.repo).file(self.genuine, "f")

    def test_refuses_a_repository_with_alternates(self):
        alternates = self.objects_dir(self.repo) / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(str(self.tmp) + "\n")

        with self.assertRaises(ToolError):
            self.git.objects(self.repo)

    def test_finds_the_commit_that_last_added_a_file(self):
        git(self.repo, "rm", "-q", "f")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "rm")
        again = commit(self.repo, "f", "again\n")
        commit(self.repo, "g", "later\n")

        self.assertEqual(self.git.objects(self.repo).last_added(git(self.repo, "rev-parse", "HEAD"), "f"), again)

    def test_lists_every_file_under_a_directory(self):
        (self.repo / "d" / "e").mkdir(parents=True)
        commit(self.repo, "d/e/h", "h\n")
        top = commit(self.repo, "d/top", "t\n")

        self.assertEqual(sorted(self.git.objects(self.repo).files(top, "d")), ["d/e/h", "d/top"])

    def crafted(self, body: bytes) -> str:
        """A commit object written as given, whatever git would make of it."""
        return subprocess.run(["git", "-C", str(self.repo), "hash-object", "-t", "commit", "-w", "--stdin",
                               "--literally"], input=body, capture_output=True, check=True).stdout.decode().strip()

    def test_reads_the_tree_from_the_commits_first_line_as_git_does(self):
        tree = git(self.repo, "rev-parse", f"{self.genuine}^{{tree}}")
        other = "0" * 40
        sha = self.crafted(f"tree {tree}\nauthor a <a@x> 0 +0000\ncommitter a <a@x> 0 +0000\ntree {other}\n\nm\n"
                           .encode())

        self.assertEqual(self.git.objects(self.repo).commit(sha)[0], tree)

    def test_reads_only_consecutive_parent_lines(self):
        tree = git(self.repo, "rev-parse", f"{self.genuine}^{{tree}}")
        sha = self.crafted(f"tree {tree}\nparent {self.genuine}\nauthor a <a@x> 0 +0000\nparent {'1' * 40}\n"
                           f"committer a <a@x> 0 +0000\n\nm\n".encode())

        self.assertEqual(self.git.objects(self.repo).commit(sha)[1], (self.genuine,))

    def test_refuses_a_commit_that_does_not_open_with_its_tree(self):
        tree = git(self.repo, "rev-parse", f"{self.genuine}^{{tree}}")
        sha = self.crafted(f"parent {self.genuine}\ntree {tree}\n\nm\n".encode())

        with self.assertRaises(ToolError):
            self.git.objects(self.repo).commit(sha)


class ReadsWorktrees(Repo):
    def test_reads_a_linked_worktree_through_its_common_dir(self):
        linked = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", "--detach", str(linked))

        self.assertEqual(self.git.objects(linked).file(self.genuine, "f"), b"genuine\n")

    def test_reads_config_without_the_object_checks(self):
        git(self.repo, "config", "remote.origin.url", "https://github.com/o/r.git")
        clone = self.tmp / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.repo}", str(clone)], check=True)
        git(clone, "config", "remote.origin.url", "https://github.com/o/r.git")

        self.assertEqual(self.git.config_get(clone, "remote.origin.url"), "https://github.com/o/r.git")


class RunsNoFetchOnARead(Repo):
    """A session-written partial-clone config would make a read of a missing object run a fetch command."""

    def plant(self):
        marker = self.tmp / "fetched"
        script = self.tmp / "ssh.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        script.chmod(0o755)
        for key, value in (("core.repositoryformatversion", "1"), ("extensions.partialClone", "evil"),
                           ("remote.evil.promisor", "true"), ("remote.evil.url", "ssh://x/y"),
                           ("core.sshCommand", str(script))):
            git(self.repo, "config", key, value)
        return marker

    def test_refuses_a_repository_configured_as_a_partial_clone(self):
        self.plant()

        with self.assertRaises(ToolError):
            self.git.objects(self.repo)

    def test_a_missing_object_runs_no_command_even_unchecked(self):
        marker = self.plant()
        objects = tools.Objects(self.git, self.repo)  # past check(): the environment alone must stop it
        self.addCleanup(objects.close)

        with self.assertRaises(ToolError):
            objects.get("1" * 40, "blob")

        self.assertFalse(marker.exists())


class BoundsEachRead(Repo):
    def objects_dir(self):
        return Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-path", "objects"))

    def test_refuses_an_object_past_its_size_limit(self):
        blob = git(self.repo, "rev-parse", f"{self.genuine}:f")
        objects = self.git.objects(self.repo)
        self.addCleanup(objects.close)

        with mock.patch.object(tools, "OBJECT_LIMIT", 3), self.assertRaises(ToolError):
            objects.get(blob, "blob")

    def test_gives_up_on_an_object_that_never_comes(self):
        blob = git(self.repo, "rev-parse", f"{self.genuine}:f")
        loose = self.objects_dir() / blob[:2] / blob[2:]
        loose.unlink()
        os.mkfifo(loose)  # git's open of it blocks forever
        objects = tools.Objects(self.git, self.repo, deadline=1)
        self.addCleanup(objects._proc.kill)

        started = time.monotonic()
        with self.assertRaises(ToolError):
            objects.get(blob, "blob")

        self.assertLess(time.monotonic() - started, 5)


class KeepsOnlyTrustedCaSettings(unittest.TestCase):
    @unittest.skipUnless(os.path.isfile("/etc/ssl/certs/ca-certificates.crt"), "no system CA bundle here")
    def test_keeps_a_ca_file_only_root_can_change(self):
        self.assertEqual(tools.git_env({"SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt"}).get("SSL_CERT_FILE"),
                         "/etc/ssl/certs/ca-certificates.crt")

    def test_drops_a_ca_file_this_user_can_write(self):
        handle, path = tempfile.mkstemp()
        os.close(handle)
        self.addCleanup(os.unlink, path)

        self.assertNotIn("SSL_CERT_FILE", tools.git_env({"SSL_CERT_FILE": path}))

    def test_keeps_the_proxy(self):
        self.assertEqual(tools.git_env({"https_proxy": "http://proxy:3128"})["https_proxy"], "http://proxy:3128")


class Fetches(Repo):
    def auth(self):
        return tools.GhAuth(environ={"GH_TOKEN": FIXTURE_TOKEN}, binary="/bin/true")

    def test_refuses_a_branch_name_that_could_be_an_option(self):
        with self.assertRaises(ToolError):
            self.git.fetch_clean("owner/repo", "--upload-pack=x", self.tmp / "into.git", self.auth())

    def test_refuses_a_repository_that_is_not_owner_name(self):
        with self.assertRaises(ToolError):
            self.git.fetch_clean("ext::sh -c x", "main", self.tmp / "into.git", self.auth())


if __name__ == "__main__":
    unittest.main()
