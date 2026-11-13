"""E21: the guards obey the gate (ADR 0063 §3).

In a run session, unless the enforcement cache holds a pass for the current key, guard-paths refuses
every Edit, Write, MultiEdit and NotebookEdit and every Bash command except `trimtab check-enforcement`
as the whole command, and guard-github refuses everything that reaches GitHub. A guard that finds no
pass starts the recorder, at most one at a time.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_planchecks import BRANCH
from trimtab import guardstate, roots
from trimtab.adapters import guard_github, plan_gate
from trimtab.enforcement import gate, guardgate, tools

ALLOW, BLOCK = plan_gate.ALLOW, plan_gate.BLOCK
CWD = "/work"


def payload(tool="Write", command=None, cwd=CWD):
    found = {"tool_name": tool, "cwd": cwd, "tool_input": {}}
    if command is not None:
        found["tool_input"] = {"command": command}
    elif tool in plan_gate.WRITE_TOOLS:
        found["tool_input"] = {"file_path": "/work/src/a.py"}
    return found


class ReportsOnly(unittest.TestCase):
    """The one command a run may make while the gate fails."""

    def test_the_report_command_is_allowed_in_its_own_spellings(self):
        for command in ("trimtab check-enforcement", "bin/trimtab check-enforcement",
                        "/usr/bin/trimtab check-enforcement --repository owner/proj",
                        "trimtab check-enforcement --repo ."):
            with self.subTest(command):
                self.assertTrue(plan_gate.reports_only(command))

    def test_anything_more_than_the_report_is_not(self):
        for command in ("trimtab check-enforcement && rm -rf /", "trimtab check-enforcement; echo hi",
                        "trimtab registry", "echo trimtab check-enforcement", "trimtab",
                        "trimtab check-enforcement $(touch /work/x)", "trimtab check-enforcement | tee out",
                        "trimtab check-enforcement > out", "x=1 trimtab check-enforcement",
                        "trimtab check-pr --body-file b"):
            with self.subTest(command):
                self.assertFalse(plan_gate.reports_only(command))


class GuardPathsHalf(unittest.TestCase):
    def decide(self, load, holds=False):
        return plan_gate.decide(load, holds=lambda project, *a, **kw: holds)

    def test_every_write_tool_is_refused_while_the_gate_fails(self):
        for tool in plan_gate.WRITE_TOOLS:
            with self.subTest(tool):
                code, message = self.decide(payload(tool=tool))

                self.assertEqual(code, BLOCK)
                self.assertIn("E21", message)

    def test_every_bash_command_but_the_report_is_refused(self):
        for command in ("ls", "git status", "cat README.md", "python3 -c pass"):
            with self.subTest(command):
                self.assertEqual(self.decide(payload(tool="Bash", command=command))[0], BLOCK)

        self.assertEqual(self.decide(payload(tool="Bash", command="trimtab check-enforcement"))[0], ALLOW)

    def test_a_passing_gate_allows_the_call(self):
        self.assertEqual(self.decide(payload(), holds=True)[0], ALLOW)
        self.assertEqual(self.decide(payload(tool="Bash", command="ls"), holds=True)[0], ALLOW)

    def test_a_tool_the_row_does_not_name_is_not_this_checks_business(self):
        for tool in ("Read", "Glob", "Grep", "Skill"):
            with self.subTest(tool):
                self.assertEqual(self.decide(payload(tool=tool))[0], ALLOW)

    def test_a_call_whose_checkout_cannot_be_read_blocks(self):
        for cwd in ("relative", "", None):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.decide(payload(cwd=cwd))[0], BLOCK)

    def test_a_bash_command_that_is_not_a_string_blocks(self):
        load = {"tool_name": "Bash", "cwd": CWD, "tool_input": {"command": ["ls"]}}

        self.assertEqual(self.decide(load)[0], BLOCK)


class TheResumeCheckOnEveryWrite(unittest.TestCase):
    """E13: the plan-run layer makes no write on a branch whose resume check has not passed for its head."""

    def decide(self, load, branch=BRANCH, refuse=None):
        from trimtab import planguard, planrows

        asked = []

        def resume(support, name, **kw):
            asked.append(name)
            if refuse is not None:
                raise planrows.Refusal(refuse)

        with mock.patch.object(planguard.PlanSupport, "head_branch", lambda self, cwd=None: branch):
            code, message = plan_gate.decide(load, holds=lambda *a, **kw: True, resume=resume)
        return code, message, asked

    def test_a_write_on_a_plan_branch_asks_the_resume_check(self):
        code, _, asked = self.decide(payload())

        self.assertEqual((code, asked), (ALLOW, [BRANCH]))

    def test_a_refused_resume_check_refuses_the_write(self):
        code, message, _ = self.decide(payload(), refuse="E13: the activity chain is broken")

        self.assertEqual(code, BLOCK)
        self.assertIn("E13", message)

    def test_a_branch_that_is_not_a_plan_part_asks_nothing(self):
        code, _, asked = self.decide(payload(), branch="feat/x")

        self.assertEqual((code, asked), (ALLOW, []))

    def test_a_checkout_with_no_branch_asks_nothing(self):
        code, _, asked = self.decide(payload(), branch=None)

        self.assertEqual((code, asked), (ALLOW, []))


class GuardGithubHalf(unittest.TestCase):
    def decide(self, command, holds=False, run=True):
        from test_guard_github_plan_acts import GIT, answered_facts
        from trimtab import planguard

        def git(cwd, *args):
            return GIT[args]

        plan = planguard.PlanSupport(Path(CWD), environ={"TRIMTAB_PLAN_RUN": "1"} if run else {},
                                     facts=answered_facts(), git=git)
        with mock.patch.object(guard_github, "GATE_OK", lambda project, *a, **kw: holds):
            return guard_github.decide({"tool_name": "Bash", "cwd": CWD,
                                        "tool_input": {"command": command}}, plan)

    def test_a_run_reaches_github_with_nothing_while_the_gate_fails(self):
        for command in ("gh pr view 7", "gh api repos/owner/proj", f"git push origin {BRANCH}"):
            with self.subTest(command):
                code, message = self.decide(command)

                self.assertEqual(code, guard_github.BLOCK)
                self.assertIn("E21", message)

    def test_an_interactive_session_is_not_this_rows_business(self):
        self.assertEqual(self.decide("gh pr view 7", run=False)[0], guard_github.ALLOW)

    def test_a_passing_gate_lets_the_rows_decide(self):
        self.assertEqual(self.decide("gh pr view 7", holds=True)[0], guard_github.ALLOW)


class TheRecorderStart(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_recorder_is_started_and_a_second_is_not_while_it_holds_the_start_lock(self):
        started = []

        class Fake:
            def __init__(self, argv, **kw):
                started.append(argv)

        with mock.patch.object(guardgate.subprocess, "Popen", Fake):
            first = guardgate.start_recorder(self.tmp)

        self.assertTrue(first)
        self.assertEqual(len(started), 1)
        self.assertIn(guardgate.MODULE, started[0])

    def test_a_start_the_lock_refuses_starts_nothing(self):
        import fcntl

        directory = guardstate.directory(self.tmp, *gate.CACHE_PARTS, create=True)
        handle = os.open(directory / guardgate.START_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, handle)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = []

        with mock.patch.object(guardgate.subprocess, "Popen", lambda *a, **kw: started.append(a)):
            self.assertFalse(guardgate.start_recorder(self.tmp))

        self.assertEqual(started, [])

    def test_a_project_with_no_origin_is_no_pass(self):
        self.assertFalse(guardgate.holds(self.tmp, start=False))


class TheKey(unittest.TestCase):
    """The current key is three reads, and anything unreadable is no pass."""

    def test_a_repository_that_is_not_owner_name_is_refused(self):
        with self.assertRaises(gate.GateError):
            gate.pinned_key("nope", Path("/"), Path("/"), Path("/"), Path("/"))

    def test_a_failed_read_is_no_pass_never_an_allow(self):
        self.assertFalse(gate.pass_holds("owner/proj", Path("/nonexistent"), Path("/nonexistent"),
                                         Path("/nonexistent"), Path("/nonexistent")))

    def test_the_head_is_read_by_ls_remote_under_its_own_bound(self):
        calls = []

        class FakeGit:
            def ls_remote(self, cwd, url, ref, timeout):
                calls.append((url, ref, timeout))
                return "a" * 40

        self.assertEqual(gate.remote_head(FakeGit(), "owner/proj"), "a" * 40)
        self.assertEqual(calls, [("https://github.com/owner/proj.git", "HEAD", gate.LS_REMOTE_SECONDS)])

    def test_a_remote_that_names_no_head_is_a_failed_read(self):
        class FakeGit:
            def ls_remote(self, cwd, url, ref, timeout):
                raise tools.ToolError("ls-remote failed")

        with self.assertRaises(gate.GateError):
            gate.remote_head(FakeGit(), "owner/proj")


class TheHookBranch(unittest.TestCase):
    """hooks/guard-paths.sh reads the gate only in a run session."""

    def hook(self, load, environ=None):
        env = {**os.environ, "HOOK_ALLOW_HARNESS": "0", **(environ or {})}
        env.pop("HOOK_ALLOW_PATHS", None)
        return subprocess.run(["bash", str(roots.code_root() / "hooks" / "guard-paths.sh")],
                              input=json.dumps(load), capture_output=True, text=True, env=env)

    def test_an_interactive_session_reads_no_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = self.hook({"tool_name": "Bash", "cwd": tmp, "tool_input": {"command": "ls"}})

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_run_session_refuses_a_command_while_the_gate_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = self.hook({"tool_name": "Bash", "cwd": tmp, "tool_input": {"command": "ls"}},
                             {"TRIMTAB_PLAN_RUN": "1", "CLAUDE_CONFIG_DIR": tmp})

        self.assertEqual(done.returncode, 2)
        self.assertIn("E21", done.stderr)

    def test_a_run_session_allows_the_report_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = self.hook({"tool_name": "Bash", "cwd": tmp,
                              "tool_input": {"command": "trimtab check-enforcement"}},
                             {"TRIMTAB_PLAN_RUN": "1", "CLAUDE_CONFIG_DIR": tmp})

        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
