"""`PlanSupport.in_run`: guard-github reads a run by the same three signals the session-mode classifier uses
(ADR 0041, ADR 0063 §3, review round 2), so a session cannot be a run to one guard and interactive to the
other. The signals are the run flag, a checked-out `plan/*` branch, and a record that the session was
autonomous earlier, which makes the mode only tighten.
"""

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_scrub import isolate_git
from trimtab import planguard
from trimtab.adapters import guard_github
from trimtab.enforcement.tools import ToolError

SESSION_MODE = Path(__file__).resolve().parents[2] / "hooks" / "session-mode.sh"
BRANCH = "plan/0008-slug/item-02-part1"
SID = "session-abc_123"
HEAD_REF = ("symbolic-ref", "--short", "-q", "HEAD")
GIT_DIR = ("rev-parse", "--git-dir")


def git_answering(answers):
    def run(cwd, *args):
        if args not in answers:
            raise ToolError(f"git {args[0]} is not answered")
        return answers[args]
    return run


class InRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.config = self.tmp / "config"
        self.records = self.config / "guard-state" / "session-mode"

    def support(self, answers, *, flag=False, sid=SID):
        environ = {"CLAUDE_CONFIG_DIR": str(self.config), **({"TRIMTAB_PLAN_RUN": "1"} if flag else {})}
        return planguard.PlanSupport(self.tmp, environ=environ, facts=object(), git=git_answering(answers),
                                     session_id=sid)

    def record(self, sid=SID):
        self.records.mkdir(parents=True, mode=0o700)
        (self.records / hashlib.sha256(sid.encode()).hexdigest()).write_text("")

    def test_the_run_flag_alone_makes_a_run(self):
        self.assertTrue(self.support({HEAD_REF: "main\n", GIT_DIR: ".git\n"}, flag=True).in_run())

    def test_a_checked_out_plan_branch_alone_makes_a_run(self):
        self.assertTrue(self.support({HEAD_REF: f"{BRANCH}\n", GIT_DIR: ".git\n"}).in_run())

    def test_a_recorded_session_is_a_run_after_it_leaves_the_plan_branch(self):
        self.record()

        self.assertTrue(self.support({HEAD_REF: "main\n", GIT_DIR: ".git\n"}).in_run())

    def test_a_session_with_no_signal_is_interactive(self):
        self.assertFalse(self.support({HEAD_REF: "main\n", GIT_DIR: ".git\n"}).in_run())

    def test_another_sessions_record_is_no_signal(self):
        self.record("someone-else")

        self.assertFalse(self.support({HEAD_REF: "main\n", GIT_DIR: ".git\n"}).in_run())

    def test_a_link_on_the_records_path_counts_as_recorded(self):
        # sm_recorded's rule: a link anywhere there could point the record where a session can delete it.
        self.config.mkdir()
        (self.config / "elsewhere").mkdir()
        (self.config / "guard-state").symlink_to(self.config / "elsewhere")

        self.assertTrue(self.support({HEAD_REF: "main\n", GIT_DIR: ".git\n"}).in_run())

    def test_a_session_id_that_is_not_a_plain_token_is_no_record(self):
        self.record()

        self.assertFalse(self.support({HEAD_REF: "main\n", GIT_DIR: ".git\n"}, sid="a/b").in_run())
        self.assertFalse(self.support({HEAD_REF: "main\n", GIT_DIR: ".git\n"}, sid=None).in_run())

    def test_a_git_that_fails_inside_a_repository_is_a_run_not_interactive(self):
        # tools.Git refuses a shallow clone or one with alternates, so every read raises and the branch
        # reads as None; that must not be taken for "not a run" (fail closed).
        sup = self.support({})
        sup._is_repository = lambda cwd: True

        self.assertTrue(sup.in_run())

    def test_a_detached_head_in_a_readable_repository_is_not_a_run_by_itself(self):
        # symbolic-ref fails on a detached HEAD while `rev-parse --git-dir` still answers.
        self.assertFalse(self.support({GIT_DIR: ".git\n"}).in_run())

    def test_outside_any_repository_with_no_signal_is_interactive(self):
        sup = self.support({})
        sup._is_repository = lambda cwd: False

        self.assertFalse(sup.in_run())

    def test_a_tag_with_the_branchs_name_does_not_hide_the_plan_branch(self):
        # With a tag `plan/x` beside the branch, `symbolic-ref --short` prints `heads/plan/x`, which the
        # classifier avoids by reading the full ref. A session could otherwise make itself interactive to
        # guard-github alone with `git tag plan/x; git switch plan/x` (review round 3).
        self.assertTrue(self.support({HEAD_REF: "heads/plan/0008-slug/item-02-part1\n", GIT_DIR: ".git\n"}).in_run())

    def test_head_branch_names_the_branch_whatever_a_tag_does_to_the_short_form(self):
        sup = self.support({HEAD_REF: "heads/plan/0008-slug/item-02-part1\n"})

        self.assertEqual(sup.head_branch(self.tmp), BRANCH)

    def test_a_session_id_of_a_plain_token_reads_its_own_record_at_the_length_limit(self):
        long_id = "a" * 128
        self.record(long_id)

        self.assertTrue(self.support({HEAD_REF: "main\n", GIT_DIR: ".git\n"}, sid=long_id).in_run())
        self.assertFalse(self.support({HEAD_REF: "main\n", GIT_DIR: ".git\n"}, sid="a" * 129).in_run())

    def test_an_id_that_is_not_a_token_is_no_record_even_when_a_file_is_named_for_it(self):
        bad = "has space"
        self.records.mkdir(parents=True, mode=0o700)
        (self.records / hashlib.sha256(bad.encode()).hexdigest()).write_text("")

        self.assertFalse(self.support({HEAD_REF: "main\n", GIT_DIR: ".git\n"}, sid=bad).in_run())

    def test_a_dangling_link_at_the_record_counts_as_recorded(self):
        self.records.mkdir(parents=True, mode=0o700)
        (self.records / hashlib.sha256(SID.encode()).hexdigest()).symlink_to(self.tmp / "nowhere")

        self.assertTrue(self.support({HEAD_REF: "main\n", GIT_DIR: ".git\n"}).in_run())

    def test_without_a_config_dir_the_record_is_read_under_home(self):
        home = self.tmp / "home"
        folder = home / ".claude" / "guard-state" / "session-mode"
        folder.mkdir(parents=True, mode=0o700)
        (folder / hashlib.sha256(SID.encode()).hexdigest()).write_text("")
        sup = planguard.PlanSupport(self.tmp, environ={"HOME": str(home)}, facts=object(),
                                    git=git_answering({HEAD_REF: "main\n", GIT_DIR: ".git\n"}), session_id=SID)

        self.assertTrue(sup.in_run())

    def test_a_different_directory_is_asked_afresh(self):
        # `git -C other` runs in another checkout, so the first answer must not stand for it.
        branches = {"here": "main\n", "there": f"{BRANCH}\n"}

        def git(cwd, *args):
            if args == HEAD_REF:
                return branches[Path(cwd).name]
            return ".git\n"

        sup = planguard.PlanSupport(self.tmp / "here", environ={"CLAUDE_CONFIG_DIR": str(self.config)},
                                    facts=object(), git=git, session_id=SID)

        self.assertFalse(sup.in_run(self.tmp / "here"))
        self.assertTrue(sup.in_run(self.tmp / "there"))

    def test_guard_github_hands_the_payloads_session_id_to_the_support_it_builds(self):
        # Without the id `_recorded` is always False in production, and the sticky signal is lost.
        from trimtab.adapters import guard_github

        seen = []
        real = planguard.PlanSupport

        class Spy(real):
            def __init__(self, *a, **kw):
                seen.append(kw.get("session_id"))
                super().__init__(*a, **kw)

        original = guard_github.planguard.PlanSupport
        guard_github.planguard.PlanSupport = Spy
        try:
            guard_github.decide({"tool_name": "Bash", "tool_input": {"command": "echo hi"},
                                 "cwd": str(self.tmp), "session_id": SID})
            guard_github.decide({"tool_name": "Bash", "tool_input": {"command": "echo hi"},
                                 "cwd": str(self.tmp), "session_id": 7})
        finally:
            guard_github.planguard.PlanSupport = original

        self.assertEqual(seen, [SID, None])

    def test_branch_unreadable_tells_a_failed_git_from_a_detached_head_or_no_repository(self):
        branch = self.support({HEAD_REF: "main\n", GIT_DIR: ".git\n"})
        detached = self.support({GIT_DIR: ".git\n"})  # symbolic-ref fails, rev-parse still answers
        failing = self.support({})
        failing._is_repository = lambda cwd: True
        outside = self.support({})
        outside._is_repository = lambda cwd: False

        self.assertEqual([branch.branch_unreadable(), detached.branch_unreadable(),
                          failing.branch_unreadable(), outside.branch_unreadable()],
                         [False, False, True, False])

    def test_the_answer_is_read_once(self):
        calls = []

        def git(cwd, *args):
            calls.append(args)
            return {HEAD_REF: "main\n", GIT_DIR: ".git\n"}[args]

        sup = planguard.PlanSupport(self.tmp, environ={"CLAUDE_CONFIG_DIR": str(self.config)},
                                    facts=object(), git=git, session_id=SID)

        sup.in_run(), sup.in_run()

        self.assertEqual(len([c for c in calls if c == HEAD_REF]), 1)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class ShallowCheckout(unittest.TestCase):
    """A shallow clone in CI's detached shape. `tools.Git` refuses its history, but the classifier reads
    without that check and says interactive; guard-github must agree (issue 190). Real clones, no injected git."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        isolate_git(self, self.tmp)
        origin = self.tmp / "origin"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        for name in ("a", "b"):  # two commits, so depth 1 really is shallow
            (origin / name).write_text(name)
            git(origin, "add", name)
            git(origin, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", name)
        self.clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(self.clone)], check=True)
        self.assertEqual(git(self.clone, "rev-parse", "--is-shallow-repository"), "true")
        self.environ = {"HOME": str(self.tmp), "CLAUDE_CONFIG_DIR": str(self.tmp / "config")}

    def detach(self):
        git(self.clone, "checkout", "-q", "--detach")

    def plan_branch(self):
        git(self.clone, "checkout", "-q", "-b", BRANCH)

    def support(self):
        return planguard.PlanSupport(self.clone, environ=self.environ, session_id=SID)

    def classify(self):
        script = f'source "{SESSION_MODE}"; sm_classify "{self.clone}" "" 0; printf %s "$sm_mode"'
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "CLAUDE_CONFIG_DIR": str(self.tmp / "config")}
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=True).stdout

    def test_a_shallow_detached_checkout_is_not_a_run(self):
        self.detach()

        self.assertFalse(self.support().in_run())

    def test_a_shallow_checkout_on_a_plan_branch_is_a_run(self):
        self.plan_branch()

        self.assertTrue(self.support().in_run())

    def test_the_classifier_says_interactive_for_the_same_shallow_detached_clone(self):
        self.detach()

        self.assertEqual(self.classify(), "interactive")

    def test_guard_github_allows_a_read_in_a_shallow_detached_checkout_without_e21(self):
        self.detach()
        payload = {"tool_name": "Bash", "tool_input": {"command": "gh pr view 7"}, "cwd": str(self.clone)}

        code, message = guard_github.decide(payload, self.support())

        self.assertEqual((code, message), (guard_github.ALLOW, ""))
        self.assertNotIn("E21", message)


if __name__ == "__main__":
    unittest.main()
