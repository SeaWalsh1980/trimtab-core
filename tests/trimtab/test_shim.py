"""The shim (stage S3 spec, section 3). Integration tests at the process boundary.

GitHub is a true external system, so a fake `git` first on PATH serves a local
fixture repository for the one URL the shim may build. The shim itself takes no
URL from anywhere. No test reaches the network.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from trimtab import instance as instance_file

CHECKOUT = Path(__file__).resolve().parents[2]
SHIM = CHECKOUT / "shim" / "bootstrap.sh"
REAL_GIT = shutil.which("git")
BASE_REPO = "owner/base"
URL = f"https://github.com/{BASE_REPO}.git"


def git(root, *args):
    return subprocess.run([REAL_GIT, "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class Shim(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # A fixture base whose bootstrap records how it was called.
        self.base = self.tmp / "base-origin"
        self.base.mkdir()
        (self.base / "bootstrap.sh").write_text(
            '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$SHIM_CALLED"\n')
        (self.base / "bootstrap.sh").chmod(0o755)
        git(self.base, "init", "-q")
        git(self.base, "add", "-A")
        git(self.base, "commit", "-qm", "base")
        self.sha = git(self.base, "rev-parse", "HEAD")
        # A fake git: rewrites the one allowed URL to the fixture, records every URL it saw.
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "git").write_text(
            "#!/usr/bin/env bash\n"
            'args=(); for a in "$@"; do printf "%s\\n" "$a" >> "$FAKE_GIT_LOG"; '
            f'[[ "$a" == "{URL}" ]] && a="{self.base}"; args+=("$a"); done\n'
            f'exec "{REAL_GIT}" "${{args[@]}}"\n')
        (self.bin / "git").chmod(0o755)
        # The instance: a git checkout holding the shim and instance.json.
        self.inst = self.tmp / "instance"
        self.inst.mkdir()
        shutil.copy(SHIM, self.inst / "bootstrap.sh")
        self.write_instance({"schema_version": 1, "repo": "owner/inst",
                             "base": {"repo": BASE_REPO, "sha": self.sha}})
        git(self.inst, "init", "-q")
        git(self.inst, "add", "-A")
        git(self.inst, "commit", "-qm", "instance")
        self.data = self.tmp / "share"
        self.store = self.data / "trimtab" / "core"
        self.called = self.tmp / "called"
        self.log = self.tmp / "git.log"

    def write_instance(self, data):
        (self.inst / "instance.json").write_text(json.dumps(data))

    def run_shim(self, *args, cwd=None):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "XDG_DATA_HOME": str(self.data),
               "SHIM_CALLED": str(self.called), "FAKE_GIT_LOG": str(self.log)}
        return subprocess.run([str((cwd or self.inst) / "bootstrap.sh"), *args], capture_output=True,
                              text=True, env=env, timeout=60)

    def test_a_missing_snapshot_is_fetched_verified_and_handed_over(self):
        out = self.run_shim("--check")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(git(self.store / self.sha, "rev-parse", "HEAD"), self.sha)
        self.assertEqual(self.called.read_text().split("\n")[:3], ["--instance", str(self.inst), "--check"])

    def test_the_only_url_fetched_is_the_pinned_base_on_github(self):
        self.run_shim()
        urls = [line for line in self.log.read_text().splitlines() if "://" in line or line.startswith("git@")]
        self.assertEqual(urls, [URL])

    def test_an_existing_clean_snapshot_is_reused_without_fetching(self):
        self.run_shim()
        self.log.unlink()
        out = self.run_shim()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn(URL, self.log.read_text() if self.log.exists() else "")

    def test_a_dirty_snapshot_stops_the_shim_and_is_not_repaired(self):
        self.run_shim()
        (self.store / self.sha / "planted.sh").write_text("echo hi\n")
        self.called.unlink()
        out = self.run_shim()
        self.assertEqual(out.returncode, 1)
        self.assertIn(self.sha, out.stderr)
        self.assertTrue((self.store / self.sha / "planted.sh").exists())
        self.assertFalse(self.called.exists())

    def test_a_snapshot_at_another_sha_stops_the_shim(self):
        self.run_shim()
        git(self.store / self.sha, "commit", "-q", "--allow-empty", "-m", "moved")
        self.called.unlink()
        out = self.run_shim()
        self.assertEqual(out.returncode, 1)
        self.assertFalse(self.called.exists())

    def test_a_pin_the_base_does_not_have_stops_and_leaves_no_snapshot(self):
        self.write_instance({"schema_version": 1, "repo": "owner/inst", "base": {"repo": BASE_REPO, "sha": "f" * 40}})
        out = self.run_shim()
        self.assertEqual(out.returncode, 1)
        self.assertFalse((self.store / ("f" * 40)).exists())
        self.assertEqual([p for p in self.store.glob("*") if p.is_dir()] if self.store.exists() else [], [])

    def test_a_linked_worktree_is_refused_unless_allowed(self):
        wt = self.tmp / "wt"
        git(self.inst, "worktree", "add", "-q", "--detach", str(wt))
        refused = self.run_shim(cwd=wt)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("worktree", refused.stderr)
        allowed = self.run_shim("--allow-worktree", cwd=wt)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIn("--allow-worktree", self.called.read_text().splitlines())

    def test_the_shim_and_the_package_accept_and_refuse_the_same_files(self):
        good = {"schema_version": 1, "repo": "owner/inst", "base": {"repo": BASE_REPO, "sha": self.sha}}
        cases = [
            good,
            {**good, "base": {"repo": BASE_REPO, "sha": self.sha[:12]}},
            {**good, "base": {"repo": BASE_REPO, "sha": self.sha.upper()}},
            {**good, "base": {"repo": "https://github.com/owner/base", "sha": self.sha}},
            {**good, "base": {"repo": "owner/base;rm", "sha": self.sha}},
            {**good, "repo": "not a repo"},
            {**good, "routines": {}},
            {**good, "cloud": {}},
            {**good, "schema_version": 2},
            {**good, "extra": 1},
        ]
        for data in cases:
            with self.subTest(data=data):
                self.write_instance(data)
                try:
                    instance_file.parse(data)
                    package_ok = True
                except instance_file.InstanceFileError:
                    package_ok = False
                shim_ok = self.run_shim("--check").returncode == 0
                self.assertEqual(shim_ok, package_ok)
