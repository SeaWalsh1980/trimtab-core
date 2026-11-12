"""The Bash write detector (ADR 0065).

The pure rules are tested directly. The hook is tested as Claude Code runs it: hooks/bash-writes.sh
with a JSON payload on stdin, `pre` before a command and `post` after it, in a throwaway repository
with its own origin, and the exit code and stderr observed.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from trimtab import planscope
from trimtab.adapters import bash_writes as bw

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "hooks" / "bash-writes.sh"
SESSION = "session-under-test"
PLAN = "0001-demo"
PLAN_BRANCH = f"plan/{PLAN}/item-01-part1"
ALLOWLIST_TEXT = "src/**\n+docs/notes/**\ntests/\n" + "docs/" + "plans/**\n"


def scope(**kw):
    base = dict(allowlist=ALLOWLIST_TEXT, list_bad="", autonomous=False, plan_list=None, plan_problem="",
                in_base=frozenset(), base_known=True)
    base.update(kw)
    return bw.Scope(**base)


class Status(unittest.TestCase):
    def test_paths_are_read_from_porcelain_entries(self):
        out = b" M src/a.py\0?? new file.txt\0D  gone.md\0"

        self.assertEqual(bw.parse_status(out), ["src/a.py", "new file.txt", "gone.md"])

    def test_an_entry_in_another_form_is_refused_not_read_as_clean(self):
        with self.assertRaises(bw.BashWritesError):
            bw.parse_status(b"garbage\0")

    def test_more_paths_than_the_bound_are_refused(self):
        out = b"".join(b"?? f%d\0" % i for i in range(bw.MAX_PATHS + 1))

        with self.assertRaises(bw.BashWritesError):
            bw.parse_status(out)


class Moved(unittest.TestCase):
    def test_a_path_clean_before_and_changed_after_has_moved(self):
        self.assertEqual(bw.moved({}, {"src/a.py": "f1"}), ["src/a.py"])

    def test_a_dirty_path_rewritten_has_moved(self):
        self.assertEqual(bw.moved({"src/a.py": "f1"}, {"src/a.py": "f2"}), ["src/a.py"])

    def test_a_dirty_path_left_alone_has_not_moved(self):
        self.assertEqual(bw.moved({"src/a.py": "f1"}, {"src/a.py": "f1"}), [])

    def test_a_path_restored_to_head_is_not_judged(self):
        self.assertEqual(bw.moved({"src/a.py": "f1"}, {}), [])


class Problems(unittest.TestCase):
    def test_a_listed_path_passes(self):
        self.assertEqual(bw.problems(["src/a.py"], scope()), [])

    def test_a_path_outside_the_allowlist_is_reported(self):
        found = bw.problems(["Makefile"], scope())

        self.assertEqual(len(found), 1)
        self.assertIn("outside the repository's write allowlist", found[0])

    def test_a_harness_path_is_reported_with_no_allowlist(self):
        found = bw.problems([".claude/settings.json"], scope(allowlist=None))

        self.assertIn("harness or control", found[0])

    def test_an_interactive_session_with_no_allowlist_judges_layer_one_only(self):
        self.assertEqual(bw.problems(["Makefile"], scope(allowlist=None)), [])

    def test_a_run_with_no_allowlist_is_refused_everything(self):
        found = bw.problems(["src/a.py"], scope(allowlist=None, autonomous=True))

        self.assertIn("no write allowlist", found[0])

    def test_an_untrustworthy_list_refuses_every_path(self):
        found = bw.problems(["src/a.py"], scope(allowlist=None, list_bad="the write allowlist is a symlink"))

        self.assertIn("symlink", found[0])

    def test_a_create_only_path_already_on_the_default_branch_is_reported(self):
        found = bw.problems(["docs/notes/a.md"], scope(in_base=frozenset({"docs/notes/a.md"})))

        self.assertIn("already on the default branch", found[0])

    def test_a_new_file_on_a_create_only_path_passes(self):
        self.assertEqual(bw.problems(["docs/notes/new.md"], scope()), [])

    def test_a_create_only_path_with_no_merge_base_fails_closed(self):
        found = bw.problems(["docs/notes/new.md"], scope(base_known=False))

        self.assertIn("merge-base", found[0])

    def test_a_run_writing_outside_its_plan_is_reported(self):
        plan = bw.hook_plan_list("src/feature/**\n", PLAN)

        found = bw.problems(["src/other.py"], scope(autonomous=True, plan_list=plan))

        self.assertIn("outside the authorised plan's scope", found[0])

    def test_a_run_writing_inside_its_plan_passes(self):
        plan = bw.hook_plan_list("src/feature/**\n", PLAN)

        self.assertEqual(bw.problems(["src/feature/x.py"], scope(autonomous=True, plan_list=plan)), [])

    def test_a_run_may_create_its_own_progress_record(self):
        plan = bw.hook_plan_list("", PLAN)
        record = "docs/" + f"plans/progress/{PLAN}/item-01-part1.md"

        self.assertEqual(bw.problems([record], scope(autonomous=True, plan_list=plan)), [])

    def test_a_run_that_cannot_be_scoped_is_refused(self):
        found = bw.problems(["src/a.py"], scope(autonomous=True, plan_problem="no write can be scoped"))

        self.assertIn("no write can be scoped", found[0])

    def test_a_path_with_a_newline_is_printed_escaped(self):
        found = bw.problems(["evil\nBLOCKED"], scope())

        self.assertNotIn("\n", found[0])


class Names(unittest.TestCase):
    def test_a_snapshot_is_named_by_a_hash_of_session_and_call(self):
        name = bw.snapshot_name(SESSION, "toolu_01")

        self.assertNotIn(SESSION, name)
        self.assertTrue(name.endswith(".json"))

    def test_a_malformed_tool_use_id_is_refused(self):
        with self.assertRaises(bw.BashWritesError):
            bw.snapshot_name(SESSION, "../x")


class SharedRules(unittest.TestCase):
    def test_git_is_hardened_as_planscope_hardens_it(self):
        self.assertEqual((bw.GIT_STEERING, bw.GIT_PINNED), (planscope.GIT_STEERING, planscope.GIT_PINNED))


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class Hook(unittest.TestCase):
    """hooks/bash-writes.sh end to end, in a repository cloned from a local origin."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        top = Path(tmp.name)
        origin = top / "origin"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        (origin / ".claude").mkdir()
        (origin / ".claude" / "write-allowlist").write_text(ALLOWLIST_TEXT)
        for rel in ("src/a.py", "docs/notes/old.md", "Makefile"):
            (origin / rel).parent.mkdir(parents=True, exist_ok=True)
            (origin / rel).write_text("one\n")
        authorised = origin / ("docs/" + "plans") / "authorised"
        authorised.mkdir(parents=True)
        (authorised / f"{PLAN}.md").write_text("# plan\n")
        (authorised / f"{PLAN}.writes").write_text("src/feature/**\n")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "init")
        self.repo = top / "work"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.repo)], check=True, capture_output=True)
        self.config = top / "config"
        self.config.mkdir(mode=0o700)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("HOOK_ALLOW_HARNESS", "TRIMTAB_PLAN_RUN", "TRIMTAB_INSTANCE")}
        self.env["CLAUDE_CONFIG_DIR"] = str(self.config)
        self.calls = 0

    def hook(self, phase, call, env=None):
        payload = {"hook_event_name": "PreToolUse" if phase == "pre" else "PostToolUse", "session_id": SESSION,
                   "tool_use_id": call, "cwd": str(self.repo), "tool_name": "Bash",
                   "tool_input": {"command": "a command"}}
        return subprocess.run([str(HOOK), phase], input=json.dumps(payload), capture_output=True, text=True,
                              env=env or self.env, timeout=60)

    def around(self, command, env=None):
        """Run `command` in the repository between the two phases; return the post result."""
        self.calls += 1
        call = f"toolu_{self.calls:04d}"
        pre = self.hook("pre", call, env)
        self.assertEqual(pre.returncode, 0, pre.stderr)
        subprocess.run(["bash", "-c", command], cwd=self.repo, check=False, capture_output=True, env=env or self.env)
        return self.hook("post", call, env)

    def test_a_write_inside_the_allowlist_passes(self):
        done = self.around("sed -i s/one/two/ src/a.py")

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_sed_in_place_outside_the_allowlist_is_reported(self):
        done = self.around("sed -i s/one/two/ Makefile")

        self.assertEqual(done.returncode, 2)
        self.assertIn("Makefile: outside the repository's write allowlist", done.stderr)

    def test_a_redirect_creating_a_file_outside_the_allowlist_is_reported(self):
        done = self.around("echo x > stray.txt")

        self.assertIn("stray.txt", done.stderr)

    def test_tee_outside_the_allowlist_is_reported(self):
        done = self.around("echo x | tee notes.txt >/dev/null")

        self.assertIn("notes.txt", done.stderr)

    def test_cp_and_mv_targets_outside_the_allowlist_are_reported(self):
        done = self.around("cp src/a.py copied.py && mv Makefile Makefile.old")

        self.assertEqual(done.returncode, 2)
        for name in ("copied.py", "Makefile.old", "Makefile:"):
            self.assertIn(name, done.stderr)

    def test_a_python_open_for_writing_is_reported(self):
        done = self.around("python3 -c \"open('made.txt','w').write('x')\"")

        self.assertIn("made.txt", done.stderr)

    def test_a_command_that_fails_after_writing_is_still_judged(self):
        done = self.around("echo x > stray.txt; false")

        self.assertIn("stray.txt", done.stderr)

    def test_a_write_committed_in_the_same_call_is_reported(self):
        done = self.around("echo two > Makefile && git -c user.email=t@t -c user.name=t commit -qam x")

        self.assertIn("Makefile", done.stderr)

    def test_a_rewrite_of_a_file_already_on_the_default_branch_on_a_create_only_path_is_reported(self):
        done = self.around("echo two > docs/notes/old.md")

        self.assertIn("already on the default branch", done.stderr)

    def test_a_path_dirtied_before_the_call_is_not_reported_again(self):
        (self.repo / "Makefile").write_text("dirty\n")

        done = self.around("true")

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_a_run_writing_outside_its_plan_is_reported_as_a_blocker(self):
        git(self.repo, "switch", "-qc", PLAN_BRANCH)

        done = self.around("echo x > src/other.py")

        self.assertIn("outside the authorised plan's scope", done.stderr)
        self.assertIn("blocker", done.stderr)

    def test_a_run_writing_inside_its_plan_passes(self):
        git(self.repo, "switch", "-qc", PLAN_BRANCH)

        done = self.around("mkdir -p src/feature && echo x > src/feature/x.py")

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_a_symlinked_allowlist_fails_closed(self):
        list_path = self.repo / ".claude" / "write-allowlist"
        elsewhere = self.config / "list"
        elsewhere.write_text("**\n")
        list_path.unlink()
        list_path.symlink_to(elsewhere)

        done = self.around("echo x > src/b.py")

        self.assertIn("symlink", done.stderr)

    def test_a_post_with_no_snapshot_is_reported_unjudged(self):
        done = self.hook("post", "toolu_never_snapshotted")

        self.assertEqual(done.returncode, 2)
        self.assertIn("could not be judged", done.stderr)

    def test_a_call_outside_any_repository_passes(self):
        self.repo = self.config
        done = self.around("true")

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_the_operator_export_lifts_it(self):
        env = {**self.env, "HOOK_ALLOW_HARNESS": "1"}

        done = self.hook("post", "toolu_lifted", env)

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_no_output_names_the_session(self):
        done = self.around("echo x > stray.txt")

        self.assertNotIn(SESSION, done.stderr + done.stdout)

    def test_the_snapshot_is_removed_once_judged(self):
        self.around("true")

        self.assertEqual([p for p in (self.config / "guard-state" / "bash-writes").iterdir()
                          if not p.name.startswith(".")], [])


if __name__ == "__main__":
    unittest.main()
