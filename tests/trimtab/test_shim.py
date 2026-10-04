"""The shim (ADR 0003). Integration tests at the process boundary.

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
        # The shim hands git a scrubbed environment, so the fake takes its settings from files, not variables.
        self.env_log = self.tmp / "git-env.log"
        self.log = self.tmp / "git.log"
        (self.bin / "git").write_text(
            "#!/usr/bin/env bash\n"
            # fail-probe: git cannot answer the worktree question (old git, dubious ownership).
            f'[[ -e "{self.tmp}/fail-probe" && " $* " == *" --absolute-git-dir "* ]] && exit 1\n'
            # what the shim let through to git
            f'printf "proxy=%s ca=%s askpass=%s\\n" "${{HTTPS_PROXY:-unset}}" "${{SSL_CERT_FILE:-unset}}" "${{GIT_ASKPASS:-unset}}" >> "{self.env_log}"\n'
            f'args=(); for a in "$@"; do printf "%s\\n" "$a" >> "{self.log}"; '
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

    def run_shim(self, *args, cwd=None, env_extra=None, proc_cwd=None):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "XDG_DATA_HOME": str(self.data),
               "SHIM_CALLED": str(self.called), "FAKE_GIT_LOG": str(self.log), **(env_extra or {})}
        return subprocess.run([str((cwd or self.inst) / "bootstrap.sh"), *args], capture_output=True,
                              text=True, env=env, timeout=60, cwd=proc_cwd)

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
        # glob("*") skips dot-directories, so the temporary clone is checked by name.
        self.assertEqual(list(self.store.glob(".fetch.*")), [])

    def test_a_linked_worktree_is_refused_unless_allowed(self):
        wt = self.tmp / "wt"
        git(self.inst, "worktree", "add", "-q", "--detach", str(wt))
        refused = self.run_shim(cwd=wt)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("worktree", refused.stderr)
        self.assertFalse(self.store.exists())
        self.assertFalse(self.called.exists())
        allowed = self.run_shim("--allow-worktree", cwd=wt)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIn("--allow-worktree", self.called.read_text().splitlines())

    def test_a_checkout_git_cannot_answer_for_is_refused_not_waved_through(self):
        (self.tmp / "fail-probe").write_text("")
        out = self.run_shim()
        self.assertEqual(out.returncode, 1)
        self.assertIn("worktree", out.stderr)
        self.assertFalse(self.store.exists())
        self.assertFalse(self.called.exists())

    def test_a_copy_that_is_not_a_git_checkout_is_installed(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        shutil.copy(self.inst / "bootstrap.sh", plain / "bootstrap.sh")
        shutil.copy(self.inst / "instance.json", plain / "instance.json")
        out = self.run_shim(cwd=plain)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.called.read_text().splitlines()[:2], ["--instance", str(plain)])

    def test_an_instance_in_a_subdirectory_of_a_checkout_is_not_mistaken_for_a_worktree(self):
        sub = self.inst / "sub"
        sub.mkdir()
        shutil.copy(self.inst / "bootstrap.sh", sub / "bootstrap.sh")
        shutil.copy(self.inst / "instance.json", sub / "instance.json")
        out = self.run_shim(cwd=sub)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_an_instance_flag_from_the_caller_is_refused(self):
        other = self.tmp / "other"
        other.mkdir()
        out = self.run_shim("--instance", str(other))
        self.assertEqual(out.returncode, 1)
        self.assertIn("--instance", out.stderr)
        self.assertFalse(self.called.exists())
        self.assertFalse(self.store.exists())

    def test_a_hostile_git_config_cannot_redirect_the_clone(self):
        home = self.tmp / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(f'[url "/nonexistent/evil"]\n\tinsteadOf = {self.base}\n')
        out = self.run_shim(env_extra={"HOME": str(home)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(git(self.store / self.sha, "rev-parse", "HEAD"), self.sha)

    def test_git_environment_variables_cannot_redirect_git(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        git(elsewhere, "init", "-q")
        out = self.run_shim(env_extra={
            "GIT_DIR": str(elsewhere / ".git"), "GIT_WORK_TREE": str(elsewhere),
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "url./nonexistent/evil.insteadOf",
            "GIT_CONFIG_VALUE_0": str(self.base)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(git(self.store / self.sha, "rev-parse", "HEAD"), self.sha)

    def test_a_tracked_file_changed_in_the_snapshot_stops_the_shim(self):
        self.run_shim()
        (self.store / self.sha / "bootstrap.sh").write_text("#!/usr/bin/env bash\necho owned\n")
        self.called.unlink()
        out = self.run_shim()
        self.assertEqual(out.returncode, 1)
        self.assertFalse(self.called.exists())

    def test_a_change_hidden_with_assume_unchanged_stops_the_shim(self):
        self.run_shim()
        snap = self.store / self.sha
        git(snap, "update-index", "--assume-unchanged", "bootstrap.sh")
        (snap / "bootstrap.sh").write_text("#!/usr/bin/env bash\necho owned\n")
        self.called.unlink()
        out = self.run_shim()
        self.assertEqual(out.returncode, 1)
        self.assertFalse(self.called.exists())

    def test_a_planted_file_hidden_by_the_snapshots_own_exclude_stops_the_shim(self):
        self.run_shim()
        snap = self.store / self.sha
        (snap / ".git" / "info").mkdir(exist_ok=True)  # the empty template leaves no info/ directory
        with open(snap / ".git" / "info" / "exclude", "a") as f:
            f.write("planted.sh\n")
        (snap / "planted.sh").write_text("echo hi\n")
        self.called.unlink()
        out = self.run_shim()
        self.assertEqual(out.returncode, 1)
        self.assertIn("planted.sh", out.stderr)
        self.assertFalse(self.called.exists())

    def test_a_planted_fsmonitor_in_the_snapshot_config_is_never_run(self):
        self.run_shim()
        snap = self.store / self.sha
        marker = self.tmp / "fsmonitor-ran"
        hook = self.tmp / "fsm.sh"
        hook.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n")
        hook.chmod(0o755)
        with open(snap / ".git" / "config", "a") as f:
            f.write(f"[core]\n\tfsmonitor = {hook}\n")
        self.run_shim()
        self.assertFalse(marker.exists())

    def test_bytecode_written_by_running_hooks_is_removed_and_reported_not_trusted(self):
        self.run_shim()
        cache = self.store / self.sha / "__pycache__"
        cache.mkdir()
        (cache / "x.cpython-313.pyc").write_bytes(b"\0")
        out = self.run_shim()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(cache.exists())
        self.assertIn(str(cache), out.stdout)

    def test_a_symlinked_bytecode_directory_is_not_followed_and_stops_the_shim(self):
        self.run_shim()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.store / self.sha / "__pycache__").symlink_to(elsewhere)
        self.called.unlink()
        out = self.run_shim()
        self.assertEqual(out.returncode, 1)
        self.assertTrue(elsewhere.exists())
        self.assertFalse(self.called.exists())

    def test_a_replace_ref_cannot_make_a_tampered_tree_match(self):
        self.run_shim()
        snap = self.store / self.sha
        (snap / "bootstrap.sh").write_text("#!/usr/bin/env bash\necho owned\n")
        git(snap, "add", "bootstrap.sh")
        forged = git(snap, "commit-tree", git(snap, "write-tree"), "-m", "forged")
        git(snap, "replace", self.sha, forged)
        self.called.unlink()
        out = self.run_shim()
        self.assertEqual(out.returncode, 1)
        self.assertFalse(self.called.exists())

    def test_a_hook_in_the_snapshot_git_directory_stops_the_shim(self):
        self.run_shim()
        hooks = self.store / self.sha / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "post-checkout").write_text("#!/usr/bin/env bash\ntrue\n")
        self.called.unlink()
        out = self.run_shim()
        self.assertEqual(out.returncode, 1)
        self.assertIn("hooks", out.stderr)
        self.assertFalse(self.called.exists())

    def test_a_template_directory_from_the_environment_installs_no_hook(self):
        marker = self.tmp / "post-checkout-ran"
        templates = self.tmp / "templates"
        (templates / "hooks").mkdir(parents=True)
        hook = templates / "hooks" / "post-checkout"
        hook.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n")
        hook.chmod(0o755)
        out = self.run_shim(env_extra={"GIT_TEMPLATE_DIR": str(templates)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(list((self.store / self.sha / ".git" / "hooks").glob("*"))
                         if (self.store / self.sha / ".git" / "hooks").exists() else [], [])

    def refused_after_planting(self, plant):
        self.run_shim()
        plant(self.store / self.sha / ".git")
        self.called.unlink()
        out = self.run_shim()
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertFalse(self.called.exists())
        return out

    def test_a_hooks_path_in_the_snapshot_config_stops_the_shim(self):
        def plant(dot_git):
            with open(dot_git / "config", "a") as f:
                f.write("[core]\n\thooksPath = /attacker/hooks\n")
        self.assertIn("config", self.refused_after_planting(plant).stderr)

    def test_a_filter_or_include_in_the_snapshot_config_stops_the_shim(self):
        for section in ('[filter "x"]\n\tclean = true\n', "[include]\n\tpath = /attacker/config\n"):
            with self.subTest(section=section):
                self.reset_store()
                def plant(dot_git, section=section):
                    with open(dot_git / "config", "a") as f:
                        f.write(section)
                self.refused_after_planting(plant)

    def reset_store(self):
        shutil.rmtree(self.data, ignore_errors=True)

    def test_an_unreadable_hooks_directory_stops_the_shim_rather_than_reading_as_empty(self):
        if os.geteuid() == 0:
            self.skipTest("root can read a mode-000 directory")
        def plant(dot_git):
            (dot_git / "hooks").mkdir(exist_ok=True)
            (dot_git / "hooks").chmod(0)
            self.addCleanup((dot_git / "hooks").chmod, 0o755)
        self.refused_after_planting(plant)

    def test_alternate_object_stores_in_the_snapshot_stop_the_shim(self):
        def plant(dot_git):
            (dot_git / "objects" / "info").mkdir(exist_ok=True)
            (dot_git / "objects" / "info" / "alternates").write_text("/attacker/objects\n")
        self.refused_after_planting(plant)

    def test_attributes_in_the_snapshot_git_directory_stop_the_shim(self):
        def plant(dot_git):
            (dot_git / "info").mkdir(exist_ok=True)
            (dot_git / "info" / "attributes").write_text("* filter=x\n")
        self.refused_after_planting(plant)

    def test_a_file_in_the_snapshot_git_directory_that_a_clone_never_writes_stops_the_shim(self):
        # commondir redirects git to another directory's config, hooks and objects; the rest are other
        # files git reads. None is on the allowlist.
        for name in ("commondir", "gitdir", "config.worktree", "shallow", "info/grafts", "info/sparse-checkout",
                     "description", "worktrees", "unexpected"):
            with self.subTest(name=name):
                self.reset_store()
                def plant(dot_git, name=name):
                    # commondir names a real repository (git would take its objects, config and hooks
                    # from it); shallow and grafts name a real commit, so git accepts both.
                    content = {"commondir": f"{self.base / '.git'}\n", "shallow": f"{self.sha}\n",
                               "info/grafts": f"{self.sha}\n"}.get(name, "/attacker/dir\n")
                    target = dot_git / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content)
                self.refused_after_planting(plant)

    def test_a_symlinked_entry_in_the_snapshot_git_directory_stops_the_shim(self):
        elsewhere = self.tmp / "elsewhere-objects"
        shutil.copytree(self.base / ".git" / "objects", elsewhere)
        def plant(dot_git):
            shutil.rmtree(dot_git / "objects")
            (dot_git / "objects").symlink_to(elsewhere)
        self.refused_after_planting(plant)

    def test_what_normal_use_leaves_in_the_snapshot_git_directory_does_not_stop_the_shim(self):
        self.run_shim()
        dot_git = self.store / self.sha / ".git"
        (dot_git / "FETCH_HEAD").write_text("")
        (dot_git / "ORIG_HEAD").write_text(self.sha + "\n")
        (dot_git / "info").mkdir(exist_ok=True)
        (dot_git / "info" / "exclude").write_text("# nothing\n")
        (dot_git / "hooks").mkdir(exist_ok=True)
        out = self.run_shim()
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_corrupted_tree_object_stops_the_shim(self):
        # A loose tree object rewritten to list a tampered blob, with the tampered file in place: the HEAD
        # still equals the pin and the working tree matches what ls-tree now says. Only the objects' own
        # hashes give it away.
        import zlib
        def plant(dot_git):
            snap = dot_git.parent
            evil = b"#!/usr/bin/env bash\necho owned\n"
            (snap / "bootstrap.sh").write_bytes(evil)
            blob = git(snap, "hash-object", "-w", "bootstrap.sh")
            tree = git(snap, "rev-parse", "HEAD^{tree}")
            body = b"100755 bootstrap.sh\0" + bytes.fromhex(blob)
            path = dot_git / "objects" / tree[:2] / tree[2:]
            path.chmod(0o644)
            path.write_bytes(zlib.compress(b"tree %d\0" % len(body) + body))
        self.refused_after_planting(plant)

    def test_a_forged_pack_entry_stops_the_shim(self):
        # git does not re-hash an object it reads from a pack, so a tree stored under the pinned tree's name
        # but holding other content passes ls-tree. The snapshot's repository is rewritten as a hand-built
        # pack: the real commit, an honest tampered blob, and a forged tree under the original tree's name.
        import hashlib
        import struct
        import zlib

        def entry(kind, body):
            size, first, out = len(body), (kind << 4) | (len(body) & 15), bytearray()
            size >>= 4
            while size:
                out.append(first | 0x80)
                first, size = size & 0x7F, size >> 7
            out.append(first)
            return bytes(out) + zlib.compress(body)

        def plant(dot_git):
            snap = dot_git.parent
            evil = b"#!/usr/bin/env bash\necho owned\n"
            (snap / "bootstrap.sh").write_bytes(evil)
            blob = bytes.fromhex(git(snap, "hash-object", "bootstrap.sh"))
            tree = bytes.fromhex(git(snap, "rev-parse", "HEAD^{tree}"))
            commit = subprocess.run([REAL_GIT, "-C", str(snap), "cat-file", "commit", "HEAD"],
                                    check=True, capture_output=True).stdout
            objects = [(1, bytes.fromhex(self.sha), commit),
                       (2, tree, b"100755 bootstrap.sh\0" + blob),
                       (3, blob, evil)]
            data, offsets, crcs = b"PACK" + struct.pack(">II", 2, len(objects)), {}, {}
            for kind, name, body in objects:
                packed = entry(kind, body)
                offsets[name], crcs[name] = len(data), zlib.crc32(packed)
                data += packed
            checksum = hashlib.sha1(data).digest()
            names = sorted(offsets)
            fanout = [sum(1 for n in names if n[0] <= i) for i in range(256)]
            idx = (b"\xfftOc" + struct.pack(">I", 2) + struct.pack(">256I", *fanout) + b"".join(names)
                   + struct.pack(f">{len(names)}I", *(crcs[n] for n in names))
                   + struct.pack(f">{len(names)}I", *(offsets[n] for n in names)) + checksum)
            idx += hashlib.sha1(idx).digest()
            objects_dir = dot_git / "objects"
            for child in objects_dir.iterdir():
                if child.name != "info":
                    shutil.rmtree(child)
            (objects_dir / "pack").mkdir()
            (objects_dir / "pack" / f"pack-{checksum.hex()}.pack").write_bytes(data + checksum)
            (objects_dir / "pack" / f"pack-{checksum.hex()}.idx").write_bytes(idx)
        self.refused_after_planting(plant)

    def test_proxy_and_ca_settings_reach_git_but_credential_helpers_and_other_variables_do_not(self):
        self.run_shim(env_extra={"HTTPS_PROXY": "http://proxy.invalid:3128", "SSL_CERT_FILE": "/ca/bundle.pem",
                                 "GIT_ASKPASS": "/attacker/askpass", "GIT_PROXY_COMMAND": "/attacker/cmd"})
        lines = set(self.env_log.read_text().splitlines())
        self.assertEqual(lines, {"proxy=http://proxy.invalid:3128 ca=/ca/bundle.pem askpass=unset"})

    def test_a_ceiling_directory_from_the_environment_cannot_hide_a_worktree(self):
        wt = self.tmp / "wt"
        git(self.inst, "worktree", "add", "-q", "--detach", str(wt))
        sub = wt / "sub"
        sub.mkdir()
        shutil.copy(self.inst / "bootstrap.sh", sub / "bootstrap.sh")
        shutil.copy(self.inst / "instance.json", sub / "instance.json")
        out = self.run_shim(cwd=sub, env_extra={"GIT_CEILING_DIRECTORIES": str(wt)})
        self.assertEqual(out.returncode, 1)
        self.assertIn("worktree", out.stderr)

    def test_a_module_in_the_working_directory_cannot_stand_in_for_the_validator(self):
        evil = self.tmp / "evil"
        evil.mkdir()
        (evil / "json.py").write_text("import sys\nsys.stderr.write('HIJACKED\\n')\nsys.exit(1)\n")
        for label, kwargs in (("cwd", {"proc_cwd": evil}), ("PYTHONPATH", {"env_extra": {"PYTHONPATH": str(evil)}})):
            with self.subTest(via=label):
                out = self.run_shim("--check", **kwargs)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertNotIn("HIJACKED", out.stderr)

    def test_a_module_in_the_working_directory_cannot_stand_in_for_the_verifier(self):
        self.run_shim()
        evil = self.tmp / "evil"
        evil.mkdir()
        (evil / "hashlib.py").write_text("import sys\nsys.stderr.write('HIJACKED\\n')\nsys.exit(1)\n")
        out = self.run_shim(proc_cwd=evil)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("HIJACKED", out.stderr)

    def test_the_shim_and_the_package_accept_and_refuse_the_same_files(self):
        sha = self.sha
        good = {"schema_version": 1, "repo": "owner/inst", "base": {"repo": BASE_REPO, "sha": sha}}

        def with_base(**changes):
            return {**good, "base": {"repo": BASE_REPO, "sha": sha, **changes}}

        def with_guards(guards):
            return {**good, "guards": guards}

        # (file, the key both refusals must name; None when the file is accepted)
        cases = [
            (good, None),
            (with_base(sha=sha[:12]), "base.sha"),
            (with_base(sha=sha.upper()), "base.sha"),
            (with_base(repo="https://github.com/owner/base"), "base.repo"),
            (with_base(repo="owner/base;rm"), "base.repo"),
            (with_base(repo="../x"), "base.repo"),
            (with_base(repo="owner/.."), "base.repo"),
            ({**good, "base": "x"}, "base"),
            ({**good, "base": {"repo": BASE_REPO}}, "base.sha"),
            ({**good, "repo": "not a repo"}, "repo"),
            ({**good, "repo": "../x"}, "repo"),
            ({**good, "routines": {}}, "routines"),
            ({**good, "cloud": {}}, "cloud"),
            ({**good, "schema_version": 2}, "schema_version"),
            ({**good, "schema_version": True}, "schema_version"),
            ({**good, "schema_version": 1.0}, "schema_version"),
            ({**good, "extra": 1}, "extra"),
            (with_guards("x"), "guards"),
            (with_guards({"x": 1}), "guards.x"),
            (with_guards({"secrets_packs": "none"}), None),
            (with_guards({"secrets_packs": ["aws", "gh-1"]}), None),
            (with_guards({"secrets_packs": []}), "guards.secrets_packs"),
            (with_guards({"secrets_packs": ["BAD"]}), "guards.secrets_packs"),
            (with_guards({"secrets_packs": ["none"]}), "guards.secrets_packs"),
            (with_guards({"secrets_packs": "all"}), "guards.secrets_packs"),
            (with_guards({"secrets_patterns": "private/patterns.tsv"}), None),
            (with_guards({"secrets_patterns": "/abs/p"}), "guards.secrets_patterns"),
            (with_guards({"secrets_patterns": "~/p"}), "guards.secrets_patterns"),
            (with_guards({"secrets_patterns": "../p"}), "guards.secrets_patterns"),
            (with_guards({"secrets_patterns": "a/../b"}), "guards.secrets_patterns"),
            (with_guards({"secrets_patterns": ""}), "guards.secrets_patterns"),
            (with_guards({"secrets_patterns": 5}), "guards.secrets_patterns"),
            (with_guards({"secrets_patterns": "a\\b"}), "guards.secrets_patterns"),
        ]
        for data, key in cases:
            with self.subTest(data=data):
                self.write_instance(data)
                try:
                    instance_file.parse(json.loads(json.dumps(data)))
                    package_error = None
                except instance_file.InstanceFileError as err:
                    package_error = str(err)
                out = self.run_shim("--check")
                self.assertEqual(out.returncode == 0, package_error is None, out.stderr)
                if key is not None:
                    self.assertIn(key, package_error)
                    self.assertIn(key, out.stderr)
