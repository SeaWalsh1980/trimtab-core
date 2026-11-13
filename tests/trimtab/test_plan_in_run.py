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

from trimtab import planguard
from trimtab.enforcement.tools import ToolError

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

    def test_the_answer_is_read_once(self):
        calls = []

        def git(cwd, *args):
            calls.append(args)
            return {HEAD_REF: "main\n", GIT_DIR: ".git\n"}[args]

        sup = planguard.PlanSupport(self.tmp, environ={"CLAUDE_CONFIG_DIR": str(self.config)},
                                    facts=object(), git=git, session_id=SID)

        sup.in_run(), sup.in_run()

        self.assertEqual(len([c for c in calls if c == HEAD_REF]), 1)


if __name__ == "__main__":
    unittest.main()
