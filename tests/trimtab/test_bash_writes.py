"""The Bash write detector (ADR 0066).

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
THEIR_COMMIT_DATE = "2026-01-01T00:00:00+00:00"
ALLOWLIST_TEXT = "src/**\n+docs/notes/**\ntests/\n" + "docs/" + "plans/**\n"


def scope(**kw):
    base = dict(allowlist=ALLOWLIST_TEXT, list_bad="", autonomous=False, plan_list=None, plan_problem="",
                in_base=frozenset(), base_known=True)
    base.update(kw)
    return bw.Scope(**base)


class Status(unittest.TestCase):
    def test_paths_are_read_from_porcelain_entries_with_their_status(self):
        out = b" M src/a.py\0?? new file.txt\0D  gone.md\0"

        self.assertEqual(bw.parse_status(out), {"src/a.py": " M", "new file.txt": "??", "gone.md": "D "})

    def test_a_path_listed_twice_keeps_both_statuses(self):
        self.assertEqual(bw.parse_status(b"D  f\0?? f\0"), {"f": "D ??"})

    def test_an_entry_in_another_form_is_refused_not_read_as_clean(self):
        with self.assertRaises(bw.BashWritesError):
            bw.parse_status(b"garbage\0")

    def test_more_paths_than_the_bound_are_refused(self):
        out = b"".join(b"?? f%d\0" % i for i in range(bw.MAX_PATHS + 1))

        with self.assertRaises(bw.BashWritesError):
            bw.parse_status(out)

    def test_flagged_paths_that_are_not_on_the_disk_are_skipped(self):
        out = b"".join(b"S sparse%d\0" % i for i in range(bw.MAX_PATHS + 1)) + b"S here.json\0"

        self.assertEqual(bw.parse_flagged(out, lambda path: path == "here.json"), {"here.json": "S"})

    def test_more_flagged_paths_on_the_disk_than_the_bound_are_refused(self):
        out = b"".join(b"S f%d\0" % i for i in range(bw.MAX_PATHS + 1))

        with self.assertRaises(bw.BashWritesError):
            bw.parse_flagged(out)

    def test_skip_worktree_and_assume_unchanged_paths_are_flagged(self):
        out = b"H src/a.py\0S hidden.json\0h quiet.md\0"

        self.assertEqual(bw.parse_flagged(out), {"hidden.json": "S", "quiet.md": "h"})


def GONE(path):
    return False


def STILL_THERE(path):
    return True


class Moved(unittest.TestCase):
    def test_a_path_clean_before_and_changed_after_has_moved(self):
        self.assertEqual(bw.moved({}, {"src/a.py": "f1"}, GONE), ["src/a.py"])

    def test_a_dirty_path_rewritten_has_moved(self):
        self.assertEqual(bw.moved({"src/a.py": "f1"}, {"src/a.py": "f2"}, GONE), ["src/a.py"])

    def test_a_dirty_path_left_alone_has_not_moved(self):
        self.assertEqual(bw.moved({"src/a.py": "f1"}, {"src/a.py": "f1"}, GONE), [])

    def test_a_path_restored_to_head_is_not_judged(self):
        self.assertEqual(bw.moved({"src/a.py": "f1"}, {}, GONE), [])

    def test_staging_a_path_that_was_already_modified_has_not_moved(self):
        self.assertEqual(bw.moved({"Makefile": " M|f1"}, {"Makefile": "M |f1"}, STILL_THERE), [])

    def test_staging_an_untracked_path_that_was_already_there_has_not_moved(self):
        self.assertEqual(bw.moved({"notes.txt": "??|f1"}, {"notes.txt": "A |f1"}, STILL_THERE), [])

    def test_staging_with_a_changed_file_has_moved(self):
        self.assertEqual(bw.moved({"Makefile": " M|f1"}, {"Makefile": "M |f2"}, STILL_THERE), ["Makefile"])

    def test_removing_a_modified_path_from_the_index_has_moved(self):
        self.assertEqual(bw.moved({"Makefile": " M|f1"}, {"Makefile": "D ??|f1"}, STILL_THERE), ["Makefile"])

    def test_a_harness_entry_that_is_gone_has_moved(self):
        self.assertEqual(bw.moved({"CLAUDE.local.md": "[fs]|f1"}, {}, GONE), ["CLAUDE.local.md"])

    def test_a_tracked_harness_file_restored_outside_the_walk_has_not_moved(self):
        self.assertEqual(bw.moved({".github/ci.yml": " M|f1"}, {}, STILL_THERE), [])

    def test_a_tracked_harness_file_restored_inside_the_walk_has_not_moved(self):
        self.assertEqual(bw.moved({"CLAUDE.md": " M|f1"}, {"CLAUDE.md": "[fs]|f2"}, STILL_THERE), [])

    def test_an_untracked_harness_file_that_went_quiet_has_moved(self):
        self.assertEqual(bw.moved({"CLAUDE.md": "??|f1"}, {"CLAUDE.md": "[fs]|f1"}, STILL_THERE), ["CLAUDE.md"])

    def test_a_change_to_the_index_alone_changes_the_fingerprint(self):
        st = os.stat(__file__)

        self.assertNotEqual(bw.fingerprint(" M", st), bw.fingerprint("D ??", st))


class Mode(unittest.TestCase):
    def test_either_phase_saying_autonomous_makes_the_call_autonomous(self):
        self.assertTrue(bw.judged_mode("autonomous", "interactive", "", ""))

    def test_a_plan_branch_in_the_judged_repository_makes_the_call_autonomous(self):
        self.assertTrue(bw.judged_mode("interactive", "interactive", "", PLAN_BRANCH))

    def test_interactive_everywhere_is_interactive(self):
        self.assertFalse(bw.judged_mode("interactive", "interactive", "main", "main"))

    def test_a_mode_the_classifier_never_gives_is_refused(self):
        with self.assertRaises(bw.BashWritesError):
            bw.judged_mode("", "interactive", "", "")


class Deadlines(unittest.TestCase):
    def test_a_spent_deadline_refuses_the_next_git_call(self):
        ticks = iter([0.0, 10.0])
        deadline = bw.Deadline(7.0, clock=lambda: next(ticks))

        with self.assertRaises(bw.BashWritesError):
            deadline.remaining()


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

    def hook(self, phase, call, env=None, event=None, **fields):
        payload = {"hook_event_name": event or ("PreToolUse" if phase == "pre" else "PostToolUse"),
                   "session_id": SESSION, "tool_use_id": call, "cwd": str(self.repo), "tool_name": "Bash",
                   "tool_input": {"command": "a command"}, **fields}
        return subprocess.run([str(HOOK), phase], input=json.dumps(payload), capture_output=True, text=True,
                              env=env or self.env, timeout=60)

    def around(self, command, env=None, event=None, fails=False, cwd=None):
        """Run `command` between the two phases, from `cwd` (the repository by default); return the post
        result. The command must succeed unless `fails`, so a test cannot pass on a write that never ran."""
        self.calls += 1
        call = f"toolu_{self.calls:04d}"
        where = cwd or self.repo
        pre = self.hook("pre", call, env, cwd=str(where))
        self.assertEqual(pre.returncode, 0, pre.stderr)
        ran = subprocess.run(["bash", "-c", command], cwd=where, check=False, capture_output=True, text=True,
                             env=env or self.env)
        if not fails:
            self.assertEqual(ran.returncode, 0, ran.stderr)
        return self.hook("post", call, env, event=event, cwd=str(where))

    def commit_in_origin(self):
        """Someone else's commit, made before the call: what a fetch or pull brings in."""
        origin = self.config.parent / "origin"
        (origin / "Makefile").write_text("theirs\n")
        subprocess.run(["git", "-C", str(origin), "-c", "user.email=o@o", "-c", "user.name=o", "commit", "-qam",
                        "theirs"], check=True, capture_output=True,
                       env={**self.env, "GIT_COMMITTER_DATE": THEIR_COMMIT_DATE, "GIT_AUTHOR_DATE": THEIR_COMMIT_DATE})

    def snapshot_file(self, call):
        return self.config / "guard-state" / "bash-writes" / bw.snapshot_name(SESSION, call)

    def test_a_write_inside_the_allowlist_passes(self):
        done = self.around("sed -i s/one/two/ src/a.py && grep -q two src/a.py")

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

    def test_a_cp_target_outside_the_allowlist_is_reported(self):
        done = self.around("cp src/a.py copied.py")

        self.assertIn("copied.py", done.stderr)

    def test_an_mv_out_of_place_reports_the_removed_source(self):
        done = self.around("mv Makefile Makefile.old")

        self.assertIn("Makefile:", done.stderr)

    def test_a_python_open_for_writing_is_reported(self):
        done = self.around("python3 -c \"open('made.txt','w').write('x')\"")

        self.assertIn("made.txt", done.stderr)

    def test_a_command_that_fails_after_writing_is_still_judged(self):
        done = self.around("echo x > stray.txt; false", fails=True)

        self.assertIn("stray.txt", done.stderr)

    def test_a_failed_call_judged_on_post_tool_use_failure_is_reported(self):
        done = self.around("echo x > stray.txt; exit 1", event="PostToolUseFailure", fails=True)

        self.assertIn("stray.txt", done.stderr)

    def test_a_commit_moved_off_head_onto_another_branch_is_reported(self):
        done = self.around("echo two > Makefile && git -c user.email=t@t -c user.name=t commit -qam x "
                           "&& git branch kept && git reset -q --hard HEAD~1")

        self.assertIn("Makefile", done.stderr)

    def test_switching_to_an_existing_branch_does_not_report_its_history(self):
        git(self.repo, "switch", "-qc", "other")
        (self.repo / "Makefile").write_text("other\n")
        git(self.repo, "commit", "-qam", "other")
        git(self.repo, "switch", "-q", "main")

        done = self.around("git switch -q other")

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_merging_an_existing_branch_does_not_report_its_changes(self):
        git(self.repo, "switch", "-qc", "feature")
        (self.repo / "Makefile").write_text("feature\n")
        git(self.repo, "commit", "-qam", "feature")
        git(self.repo, "switch", "-q", "main")
        (self.repo / "src" / "a.py").write_text("main\n")
        git(self.repo, "commit", "-qam", "main side")

        done = self.around("git -c user.email=t@t -c user.name=t merge -q --no-edit feature")

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_an_edit_to_a_skip_worktree_file_is_reported(self):
        done = self.around("git update-index --skip-worktree Makefile && echo two > Makefile")

        self.assertIn("Makefile", done.stderr)

    def test_core_worktree_pointing_elsewhere_does_not_hide_a_write(self):
        done = self.around(f"git config core.worktree {self.config} && echo x > stray.txt")

        self.assertIn("stray.txt", done.stderr)

    def test_turning_off_ctime_checks_does_not_hide_a_same_size_rewrite(self):
        done = self.around("git config core.trustctime false && git config core.checkStat minimal "
                           "&& t=$(stat -c %Y Makefile) && printf 'two\\n' > Makefile && touch -d @$t Makefile")

        self.assertIn("Makefile", done.stderr)

    def test_turning_off_file_modes_does_not_hide_a_mode_change(self):
        done = self.around("git config core.fileMode false && chmod +x Makefile")

        self.assertIn("Makefile", done.stderr)

    def test_turning_on_the_untracked_cache_does_not_hide_a_new_file(self):
        # The cache hides a file only with ctime checks off and a directory's mtime reset, so this does
        # not isolate the core.untrackedCache pin; it pins that a new file is still reported.
        done = self.around("git config core.untrackedCache true && git status -s >/dev/null && echo x > stray.txt")

        self.assertIn("stray.txt", done.stderr)

    def test_turning_on_ignore_case_does_not_hide_a_new_file(self):
        done = self.around("git config core.ignoreCase true && echo x > makefile")

        self.assertIn("makefile", done.stderr)

    def test_residual_a_commit_dated_before_the_call_is_not_judged(self):
        # Accepted residual, pinned so a change to it is seen (ADR 0066, routes a deliberate command can
        # take): the commit check tells the call's commits from others' work by committer time.
        done = self.around("echo two > Makefile && GIT_COMMITTER_DATE=2020-01-01T00:00:00Z "
                           "git -c user.email=t@t -c user.name=t commit -qam x")

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_a_write_to_a_gitignored_instruction_file_is_reported(self):
        (self.repo / ".git" / "info" / "exclude").write_text("CLAUDE.local.md\n")

        done = self.around("echo x > CLAUDE.local.md")

        self.assertIn("CLAUDE.local.md: a repository harness or control path", done.stderr)

    def test_deleting_a_gitignored_instruction_file_is_reported(self):
        (self.repo / ".git" / "info" / "exclude").write_text("CLAUDE.local.md\n")
        (self.repo / "CLAUDE.local.md").write_text("local\n")

        done = self.around("rm CLAUDE.local.md")

        self.assertIn("CLAUDE.local.md", done.stderr)

    def test_a_sparse_checkout_with_many_excluded_files_is_judged(self):
        for i in range(30):
            (self.repo / "src" / f"s{i}.py").write_text("x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "many")
        git(self.repo, "sparse-checkout", "set", "--no-cone", "/Makefile", "/.claude/")

        done = self.around("echo x > stray.txt")

        self.assertIn("stray.txt", done.stderr)

    def test_staging_a_file_that_was_already_modified_is_not_reported(self):
        (self.repo / "Makefile").write_text("operator edit\n")

        done = self.around("git add Makefile")

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_taking_a_modified_file_out_of_the_index_is_reported(self):
        (self.repo / "Makefile").write_text("operator edit\n")

        done = self.around("git rm -q --cached Makefile")

        self.assertIn("Makefile", done.stderr)

    def test_a_post_with_a_different_tool_use_id_says_what_to_do(self):
        self.assertEqual(self.hook("pre", "toolu_pre").returncode, 0)

        done = self.hook("post", "toolu_other")

        self.assertIn("share a tool_use_id", done.stderr)

    def test_restoring_a_tracked_harness_file_is_not_reported(self):
        (self.repo / "CLAUDE.md").write_text("tracked\n")
        git(self.repo, "add", "CLAUDE.md")
        git(self.repo, "commit", "-qm", "instructions")
        (self.repo / "CLAUDE.md").write_text("edited\n")

        done = self.around("git restore CLAUDE.md")

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_writes_under_claude_worktrees_are_not_judged_as_harness(self):
        (self.repo / ".claude" / "worktrees").mkdir()
        (self.repo / ".git" / "info" / "exclude").write_text(".claude/worktrees/\n")

        done = self.around("mkdir .claude/worktrees/w && echo x > .claude/worktrees/w/f")

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_core_bare_does_not_hide_a_write(self):
        done = self.around("echo x > stray.txt && git config core.bare true")

        self.assertIn("stray.txt", done.stderr)

    def test_a_harness_write_in_a_repository_with_no_commit_yet_is_reported(self):
        self.repo = self.config / "fresh"
        self.repo.mkdir()
        git(self.repo, "init", "-q")

        done = self.around("mkdir .claude && echo x > .claude/notes.txt")

        self.assertIn("harness or control", done.stderr)

    def test_a_write_from_a_subdirectory_is_judged_in_its_repository(self):
        done = self.around("echo two > ../Makefile", cwd=self.repo / "src")

        self.assertIn("Makefile", done.stderr)

    def test_a_write_in_a_worktree_is_judged_in_that_worktree(self):
        tree = self.config / "tree"
        git(self.repo, "worktree", "add", "-q", "-b", "side", str(tree))
        self.repo = tree

        done = self.around("echo x > stray.txt")

        self.assertIn("stray.txt", done.stderr)

    def test_a_same_size_rewrite_of_an_already_dirty_file_is_reported(self):
        (self.repo / "Makefile").write_text("aaa\n")

        done = self.around("sleep 0.01 && printf 'bbb\\n' > Makefile")

        self.assertIn("Makefile", done.stderr)

    def test_a_commit_pushed_to_the_default_branch_in_the_same_call_is_reported(self):
        git(self.config.parent / "origin", "config", "receive.denyCurrentBranch", "ignore")

        done = self.around("echo two > Makefile && git -c user.email=t@t -c user.name=t commit -qam x "
                           "&& git push -q origin HEAD:main")

        self.assertIn("Makefile", done.stderr)

    def test_fetching_others_work_is_not_reported(self):
        self.commit_in_origin()

        done = self.around("git fetch -q origin")

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_pulling_others_work_is_not_reported(self):
        self.commit_in_origin()

        done = self.around("git pull -q --ff-only origin main")

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_a_write_to_a_gitignored_harness_file_is_reported(self):
        (self.repo / ".git" / "info" / "exclude").write_text(".claude/local.json\n")

        done = self.around("echo x > .claude/local.json")

        self.assertIn(".claude/local.json: a repository harness or control path", done.stderr)

    def test_the_detector_alone_can_be_lifted(self):
        env = {**self.env, "HOOK_ALLOW_BASH_WRITES": "1"}

        done = self.around("echo x > stray.txt", env)

        self.assertEqual((done.returncode, done.stderr), (0, ""))

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

    def test_a_payload_without_a_working_directory_blocks_the_call(self):
        done = self.hook("pre", "toolu_nocwd", cwd=None)

        self.assertEqual(done.returncode, 2)
        self.assertIn("blocked", done.stderr)

    def test_git_missing_inside_a_repository_blocks_the_call(self):
        tools = self.config / "bin"
        tools.mkdir()
        for name in ("python3", "cat", "dirname", "readlink", "bash"):
            found = subprocess.run(["bash", "-c", f"command -v {name}"], capture_output=True, text=True).stdout.strip()
            (tools / name).symlink_to(found)
        env = {**self.env, "PATH": str(tools)}

        done = self.hook("pre", "toolu_nogit", env)

        self.assertEqual(done.returncode, 2)
        self.assertIn("blocked", done.stderr)

    def test_a_snapshot_that_cannot_be_written_blocks_the_call(self):
        env = {**self.env, "CLAUDE_CONFIG_DIR": str(self.config / "missing")}

        done = self.hook("pre", "toolu_nostate", env)

        self.assertEqual(done.returncode, 2)

    def test_a_snapshot_that_is_not_private_is_refused(self):
        self.assertEqual(self.hook("pre", "toolu_open").returncode, 0)
        self.snapshot_file("toolu_open").chmod(0o644)

        done = self.hook("post", "toolu_open")

        self.assertIn("not a private regular file", done.stderr)

    def test_a_snapshot_another_version_wrote_is_refused(self):
        self.assertEqual(self.hook("pre", "toolu_old").returncode, 0)
        self.snapshot_file("toolu_old").write_text(json.dumps({"version": 1, "root": None}))

        done = self.hook("post", "toolu_old")

        self.assertIn("not written by this version", done.stderr)

    def test_a_malformed_snapshot_is_refused(self):
        self.assertEqual(self.hook("pre", "toolu_bad").returncode, 0)
        self.snapshot_file("toolu_bad").write_text(json.dumps({"version": bw.SNAPSHOT_VERSION, "root": "/x"}))

        done = self.hook("post", "toolu_bad")

        self.assertIn("malformed", done.stderr)

    def test_a_run_with_no_merge_base_cannot_be_scoped(self):
        git(self.repo, "remote", "set-head", "origin", "-d")
        git(self.repo, "switch", "-qc", PLAN_BRANCH)

        done = self.around("mkdir -p src/feature && echo x > src/feature/x.py")

        self.assertIn("merge-base with origin's default branch cannot be read", done.stderr)

    def test_a_run_whose_plan_is_not_at_the_merge_base_cannot_be_scoped(self):
        git(self.repo, "switch", "-qc", "plan/0002-absent/item-01-part1")

        done = self.around("mkdir -p src/feature && echo x > src/feature/x.py")

        self.assertIn("cannot be read at the merge-base", done.stderr)

    def test_an_unknown_phase_is_refused(self):
        done = subprocess.run([str(HOOK), "sideways"], input="{}", capture_output=True, text=True, env=self.env,
                              timeout=60)

        self.assertEqual(done.returncode, 2)

    def test_a_payload_that_is_not_json_blocks_the_call(self):
        done = subprocess.run([str(HOOK), "pre"], input="not json", capture_output=True, text=True, env=self.env,
                              timeout=60)

        self.assertEqual(done.returncode, 2)

    def test_a_snapshot_whose_time_is_not_a_number_is_refused(self):
        self.assertEqual(self.hook("pre", "toolu_time").returncode, 0)
        data = json.loads(self.snapshot_file("toolu_time").read_text())
        data["taken"] = True
        self.snapshot_file("toolu_time").write_text(json.dumps(data))

        done = self.hook("post", "toolu_time")

        self.assertIn("malformed", done.stderr)

    def test_a_run_on_a_branch_that_names_no_plan_is_refused(self):
        env = {**self.env, "TRIMTAB_PLAN_RUN": "1"}

        done = self.around("echo x > src/feature.py", env)

        self.assertIn("no write can be scoped", done.stderr)

    def test_a_post_with_no_snapshot_is_reported_unjudged(self):
        done = self.hook("post", "toolu_never_snapshotted")

        self.assertEqual(done.returncode, 2)
        self.assertIn("could not be judged", done.stderr)

    def test_a_call_outside_any_repository_passes(self):
        self.repo = self.config
        done = self.around("true")

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_the_operator_export_lifts_both_phases(self):
        env = {**self.env, "HOOK_ALLOW_HARNESS": "1"}

        done = self.around("echo x > stray.txt", env)

        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_no_output_names_the_session(self):
        done = self.around("echo x > stray.txt")

        self.assertNotIn(SESSION, done.stderr + done.stdout)

    def test_the_snapshot_is_removed_once_judged(self):
        self.around("true")

        self.assertEqual(list((self.config / "guard-state" / "bash-writes").iterdir()), [])


class Pruning(unittest.TestCase):
    NOW = 1_800_000_000

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def make(self, name, age):
        path = self.dir / name
        path.write_text("{}")
        os.utime(path, (self.NOW - age, self.NOW - age))
        return path

    def test_a_snapshot_older_than_the_ttl_is_removed(self):
        old = self.make("old.json", bw.TTL_SECONDS + 1)

        bw._prune(self.dir, "own.json", self.NOW)

        self.assertFalse(old.exists())

    def test_a_fresh_snapshot_of_another_call_is_kept(self):
        fresh = self.make("fresh.json", 60)

        bw._prune(self.dir, "own.json", self.NOW)

        self.assertTrue(fresh.exists())

    def test_the_calls_own_snapshot_is_kept_however_old(self):
        own = self.make("own.json", bw.TTL_SECONDS + 1)

        bw._prune(self.dir, "own.json", self.NOW)

        self.assertTrue(own.exists())


class Allowlists(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / ".claude").mkdir()

    def test_a_list_larger_than_any_written_by_hand_is_not_trusted(self):
        (self.root / ".claude" / "write-allowlist").write_text("x\n" * bw.MAX_LIST_BYTES)

        self.assertEqual(bw.read_allowlist(str(self.root))[0], None)

    def test_a_linked_claude_directory_is_not_trusted(self):
        target = self.root / "elsewhere"
        target.mkdir()
        (self.root / ".claude").rmdir()
        (self.root / ".claude").symlink_to(target)

        self.assertIn("symlink", bw.read_allowlist(str(self.root))[1])

    def test_a_repository_without_a_list_has_none(self):
        self.assertEqual(bw.read_allowlist(str(self.root)), (None, ""))

    def test_only_create_only_paths_need_the_default_branch(self):
        self.assertEqual(bw.create_only_paths(["src/a.py", "docs/notes/a.md"], ALLOWLIST_TEXT, None),
                         ["docs/notes/a.md"])


if __name__ == "__main__":
    unittest.main()
