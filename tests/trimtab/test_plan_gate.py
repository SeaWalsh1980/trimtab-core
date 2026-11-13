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

import plan_fixture
from instance_fixture import make_instance
from test_planchecks import BRANCH, H1
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
    """The one command a run may make while the gate fails: the installed bin/trimtab, by its path."""

    def setUp(self):
        self.code = roots.code_root()
        self.installed = (self.code / "bin" / "trimtab").resolve()

    def test_the_installed_binary_named_by_a_path_is_allowed(self):
        for command in (f"{self.installed} check-enforcement",
                        f"{self.installed} check-enforcement --repository owner/proj",
                        "bin/trimtab check-enforcement --repo ."):
            with self.subTest(command):
                self.assertTrue(plan_gate.reports_only(command, self.code))

    def test_only_the_subcommands_own_options_and_their_values_may_follow(self):
        for tail in ("--server", "--repository owner/proj", "--repo . --server", "--instance /x",
                     "--repository=owner/proj"):
            with self.subTest(tail):
                self.assertTrue(plan_gate.reports_only(f"{self.installed} check-enforcement {tail}", self.code))
        for tail in ("extra", "--bogus", "--repo", "--repo --server", "--server extra", "-x",
                     "--repository a b"):
            with self.subTest(tail):
                self.assertFalse(plan_gate.reports_only(f"{self.installed} check-enforcement {tail}",
                                                        self.code))

    def test_another_binary_of_the_same_name_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            planted = Path(tmp) / "trimtab"
            planted.write_text("#!/bin/sh\nexit 0\n")
            planted.chmod(0o755)

            for command in (f"{planted} check-enforcement", "./trimtab check-enforcement",
                            "trimtab check-enforcement"):
                with self.subTest(command):
                    self.assertFalse(plan_gate.reports_only(command, Path(tmp)))

    def test_anything_more_than_the_report_is_not(self):
        destructive = f"{self.installed} check-enforcement && " + "rm -" + "rf /"
        for command in (destructive, f"{self.installed} check-enforcement; echo hi",
                        f"{self.installed} registry", f"echo {self.installed} check-enforcement",
                        f"{self.installed}", f"{self.installed} check-enforcement $(touch /work/x)",
                        f"{self.installed} check-enforcement | tee out",
                        f"{self.installed} check-enforcement > out",
                        f"x=1 {self.installed} check-enforcement",
                        f"{self.installed} check-pr --body-file b"):
            with self.subTest(command):
                self.assertFalse(plan_gate.reports_only(command, self.code))


def stable_install():
    """Patches that fix the install digest, applied wherever a test arms a pass and wherever it checks one.

    The digest covers every file under `hooks/` and `trimtab/`, and a fresh checkout has no `__pycache__`:
    Python writes `.pyc` files as the test imports modules, so a digest taken before and after differs and a
    pass armed first never holds. CI failed on exactly that while the suite passed in a populated tree. The
    key's other elements (lock, base, head) stay real; the digest's own binding is tested in test_enforcement_gate."""
    return [mock.patch.object(gate, "snapshot", lambda *a, **kw: object()),
            mock.patch.object(gate, "install_digest", lambda install: "d" * 64)]


class RealGate:
    """A real enforcement cache, armed or not, so a case is decided by `guardgate.holds` itself.

    Nothing here stands in for the decision: a refusal comes from a config directory with no pass, and an
    allow from one holding a pass for the current key — which is what makes the manifest's mutation test
    bite for both halves of E21 (ADR 0063 §2, §3)."""

    REPOSITORY = "SeaWalsh1980/trimtab-core"

    def __init__(self, case, *, armed: bool):
        self.tmp = Path(tempfile.mkdtemp())
        case.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.config = self.tmp / "config"
        self.config.mkdir(mode=0o700)
        self.code = roots.code_root()
        self.instance = make_instance(self.tmp)
        self.head = "d" * 40
        self.armed = armed
        self.project = self.code if armed else self.tmp  # outside a checkout there is no origin: no pass
        self.pinned = ("e" * 40, gate.head_of(self.code))
        if armed:
            patches = stable_install()
            for patch in patches:
                patch.start()
            try:
                armed_key = gate.pinned_key(self.REPOSITORY, self.project, self.instance, self.code,
                                            self.config, head=self.head, pinned=self.pinned)
            finally:
                for patch in reversed(patches):
                    patch.stop()
            directory = gate.guardstate.directory(self.config, *gate.CACHE_PARTS, create=True)
            gate.guardstate.write(directory / gate.EPOCH, "f" * 32)
            gate.record_pass(self.config, self.REPOSITORY, armed_key, "f" * 32)

    def patches(self):
        env = {"CLAUDE_CONFIG_DIR": str(self.config), "TRIMTAB_INSTANCE": str(self.instance),
               "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/root")}
        out = [mock.patch.dict(os.environ, env, clear=True)]
        if self.armed:  # the one read that would go to the network
            out.append(mock.patch.object(gate, "remote_head", lambda git, repo, auth=None: self.head))
            out.extend(stable_install())
        return out


class GuardPathsHalf(unittest.TestCase):
    def decide(self, load, holds=False):
        """`holds` says whether a real pass is armed; `guardgate.holds` itself then decides."""
        found = RealGate(self, armed=holds)
        load = {**load, "cwd": str(found.project)}
        patches = found.patches()
        for patch in patches:
            patch.start()
        try:
            return plan_gate.decide(load)
        finally:
            for patch in reversed(patches):
                patch.stop()

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

        report = f"{(roots.code_root() / 'bin' / 'trimtab').resolve()} check-enforcement"
        self.assertEqual(self.decide(payload(tool="Bash", command=report,
                                             cwd=str(roots.code_root())))[0], ALLOW)

    def test_a_passing_gate_allows_the_call(self):
        self.assertEqual(self.decide(payload(), holds=True)[0], ALLOW)
        self.assertEqual(self.decide(payload(tool="Bash", command="ls"), holds=True)[0], ALLOW)

    def test_a_tool_neither_the_row_nor_this_tightening_names_is_not_judged(self):
        for tool in ("Read", "Glob", "Grep", "WebFetch"):
            with self.subTest(tool):
                self.assertEqual(self.decide(payload(tool=tool))[0], ALLOW)

    def test_a_review_pass_is_refused_too_while_the_gate_fails(self):
        # Tighter than E21's own list: a round minted while the gate fails would count for E10, E11
        # and E23 (ADR 0063 Amendment 3).
        code, message = self.decide(payload(tool="Skill"))

        self.assertEqual(code, BLOCK)
        self.assertIn("E21", message)

    def test_a_review_pass_with_the_gate_passed_needs_no_resume_check(self):
        self.assertEqual(self.decide(payload(tool="Skill"), holds=True)[0], ALLOW)

    def test_a_call_whose_checkout_cannot_be_read_blocks(self):
        for cwd in ("relative", "", None):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.decide(payload(cwd=cwd))[0], BLOCK)

    def test_a_bash_command_that_is_not_a_string_blocks(self):
        load = {"tool_name": "Bash", "cwd": CWD, "tool_input": {"command": ["ls"]}}

        self.assertEqual(self.decide(load)[0], BLOCK)


class TheResumeCheckOnEveryWrite(unittest.TestCase):
    """E13 through the real `planacts.resume_ok`: the plan-run layer makes no write on a branch whose
    resume check has not passed for its head.

    Each case is judged by what the guard answers, never by which function it called (TST-2)."""

    def setUp(self):
        plan_fixture.set_up()
        self.addCleanup(plan_fixture.tear_down)
        self.branch, self.ledger = plan_fixture.own_part(self)

    NO_BRANCH = object()  # `branch=None` means "this test's own part"; this one means git named none

    def decide(self, load, branch=None, facts=None, scope_ok=True, unreadable=True, by_dir=None):
        from test_planchecks import AGENT, MB, FakeFacts
        from trimtab import planacts, planguard, planreview, planscope

        events = (planreview.Activity(MB, H1, AGENT, "2026-11-12T11:00:00Z"),)
        found = facts if facts is not None else FakeFacts(
            branch_commits=(H1,), activity=events, merge_base=MB,
            push_commits={(MB, H1): frozenset({H1})})
        named = None if branch is self.NO_BRANCH else (self.branch if branch is None else branch)

        class Stub:
            def __init__(self, where):
                self.cwd, self.facts, self.plan_run = where, found, True

            def head_branch(self, cwd=None):
                for prefix, found_branch in (by_dir or {}).items():
                    if str(cwd) == prefix or str(cwd).startswith(prefix + "/"):
                        return found_branch
                return named

            def branch_unreadable(self, cwd=None):
                return unreadable

        result = planscope.Result(planscope.Ref.PLAN, plan_fixture.PLAN,
                                  () if scope_ok else ("src/x.py: outside",))
        with mock.patch.object(plan_gate.planguard, "PlanSupport", Stub), \
                mock.patch.object(guardgate, "holds", lambda *a, **kw: True), \
                mock.patch.object(planacts.planscope, "check", lambda *a, **kw: result), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(plan_fixture._state["config"])}):
            return plan_gate.decide(load)

    def test_a_write_whose_history_the_ledger_vouches_for_is_allowed(self):
        self.ledger.push(self.branch, "a" * 40, H1, run_env="1")

        self.assertEqual(self.decide(payload())[0], ALLOW)

    def test_a_write_the_resume_check_refuses_is_refused(self):
        code, message = self.decide(payload())  # no ledger entry covers the branch's commit

        self.assertEqual(code, BLOCK)
        self.assertIn("E13", message)

    def test_a_diff_outside_the_plans_scope_refuses_the_write(self):
        self.ledger.push(self.branch, "a" * 40, H1, run_env="1")

        code, message = self.decide(payload(), scope_ok=False)

        self.assertEqual(code, BLOCK)
        self.assertIn("E13", message)

    def test_a_branch_that_is_not_a_plan_part_is_allowed_with_no_resume_read(self):
        from test_planchecks import FakeFacts

        self.assertEqual(self.decide(payload(), branch="feat/x", facts=FakeFacts(raises="activity"))[0],
                         ALLOW)

    def test_a_file_written_in_another_checkout_of_a_plan_branch_is_judged_there(self):
        # E13 followed `cwd` alone, so an Edit of a file in a worktree or sibling clone sitting on a plan
        # branch went unjudged while the session stood on another branch (CI review).
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        (tmp / "here").mkdir()
        (tmp / "other" / "src").mkdir(parents=True)
        load = {"tool_name": "Write", "cwd": str(tmp / "here"),
                "tool_input": {"file_path": str(tmp / "other" / "src" / "new.py")}}
        branches = {str(tmp / "here"): "main", str(tmp / "other"): self.branch}

        code, message = self.decide(load, by_dir=branches)  # nothing in the ledger covers that branch

        self.assertEqual(code, BLOCK)
        self.assertIn("E13", message)

    def test_a_file_in_the_sessions_own_checkout_is_judged_by_that_checkout(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        (tmp / "here" / "src").mkdir(parents=True)
        load = {"tool_name": "Edit", "cwd": str(tmp / "here"),
                "tool_input": {"file_path": str(tmp / "here" / "src" / "a.py")}}

        self.assertEqual(self.decide(load, by_dir={str(tmp / "here"): "main"})[0], ALLOW)

    def test_a_file_in_another_checkout_on_an_ordinary_branch_is_allowed(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        (tmp / "here").mkdir()
        (tmp / "other").mkdir()
        load = {"tool_name": "Write", "cwd": str(tmp / "here"),
                "tool_input": {"file_path": str(tmp / "other" / "new.py")}}

        code, _ = self.decide(load, by_dir={str(tmp / "here"): "main", str(tmp / "other"): "feat/x"})

        self.assertEqual(code, ALLOW)

    def test_a_report_command_is_refused_when_the_payload_gives_no_usable_directory(self):
        # `reports_only` used to resolve it against the hook process's own directory first.
        report = f"{(roots.code_root() / 'bin' / 'trimtab').resolve()} check-enforcement"
        for cwd in ("relative", "", None):
            with self.subTest(cwd=cwd):
                load = {"tool_name": "Bash", "tool_input": {"command": report}, "cwd": cwd}

                self.assertEqual(self.decide(load)[0], BLOCK)

    def test_a_detached_head_is_no_plan_branch_and_judges_nothing(self):
        # CI checks a pull request out detached, and so does a bisect; refusing it would stop a run from
        # running even the command that switches back. File writes off a plan branch are guard-paths'
        # own refusal (ADR 0041), and a git that fails to answer still refuses, below.
        from test_planchecks import FakeFacts

        code, _ = self.decide(payload(), branch=self.NO_BRANCH, unreadable=False,
                              facts=FakeFacts(raises="activity"))

        self.assertEqual(code, ALLOW)

    def test_a_checkout_whose_branch_cannot_be_read_refuses_in_a_run(self):
        code, message = self.decide(payload(), branch=self.NO_BRANCH)

        self.assertEqual(code, BLOCK)
        self.assertIn("cannot be read", message)


class GuardGithubHalf(unittest.TestCase):
    def decide(self, command, holds=False, run=True, branch=BRANCH):
        from test_guard_github_plan_acts import GIT, HEAD_REF, PUSH_REF, answered_facts
        from trimtab import planguard

        answers = {**GIT, HEAD_REF: f"{branch}\n", PUSH_REF: f"refs/remotes/origin/{branch}\n"}

        def git(cwd, *args):
            return answers[args]

        found = RealGate(self, armed=holds)
        plan = planguard.PlanSupport(found.project, environ={"TRIMTAB_PLAN_RUN": "1"} if run else {},
                                     facts=answered_facts(), git=git)
        patches = found.patches()
        for patch in patches:
            patch.start()
        try:
            return guard_github.decide({"tool_name": "Bash", "cwd": str(found.project),
                                        "tool_input": {"command": command}}, plan)
        finally:
            for patch in reversed(patches):
                patch.stop()

    def test_a_run_reaches_github_with_nothing_while_the_gate_fails(self):
        for command in ("gh pr view 7", "gh api repos/owner/proj", f"git push origin {BRANCH}"):
            with self.subTest(command):
                code, message = self.decide(command)

                self.assertEqual(code, guard_github.BLOCK)
                self.assertIn("E21", message)

    def test_an_interactive_session_is_not_this_rows_business(self):
        # No flag and no plan/* branch: a plan/* branch alone makes it a run (ADR 0041's signals).
        self.assertEqual(self.decide("gh pr view 7", run=False, branch="feature/x")[0],
                         guard_github.ALLOW)

    def test_a_plan_branch_without_the_flag_is_judged_as_a_run(self):
        code, message = self.decide("gh pr view 7", run=False)

        self.assertEqual(code, guard_github.BLOCK)
        self.assertIn("E21", message)

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

    def test_the_start_lock_is_held_for_the_recorders_life_so_a_second_is_not_started(self):
        # The child inherits the lock, so a refused call that arrives while it still runs finds it held
        # (the lock used to be released the moment the child was spawned, so each refused call started
        # another recorder; review round 3). Closing the child's copy frees it again.
        import fcntl
        children = []

        class Child:
            def __init__(self, argv, **kw):
                children.append([os.dup(fd) for fd in kw["pass_fds"]])

        with mock.patch.object(guardgate.subprocess, "Popen", Child):
            first = guardgate.start_recorder(self.tmp)
            second = guardgate.start_recorder(self.tmp)

        self.assertEqual((first, second, len(children)), (True, False, 1))
        directory = guardstate.directory(self.tmp, *gate.CACHE_PARTS, create=False)
        probe = os.open(directory / guardgate.START_LOCK, os.O_RDWR)
        self.addCleanup(os.close, probe)
        with self.assertRaises(OSError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # held by the child's copy
        for fd in children[0]:
            os.close(fd)
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the child is gone: free again

    def test_a_recorder_that_cannot_be_spawned_leaves_no_lock_behind(self):
        import fcntl

        def broken(argv, **kw):
            raise OSError("no such interpreter")

        with mock.patch.object(guardgate.subprocess, "Popen", broken):
            self.assertFalse(guardgate.start_recorder(self.tmp))

        directory = guardstate.directory(self.tmp, *gate.CACHE_PARTS, create=False)
        probe = os.open(directory / guardgate.START_LOCK, os.O_RDWR)
        self.addCleanup(os.close, probe)
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

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


class TheAdaptersEntryPoints(unittest.TestCase):
    """Both adapters block on a payload they cannot read (ADR 0001, SEC-2.5)."""

    def run_module(self, module, payload_text):
        argv = [str(roots.code_root() / "hooks" / "run_module.py"), str(roots.code_root()), module]
        return subprocess.run(["python3", "-I", "-S", "-B", *argv], input=payload_text,
                              capture_output=True, text=True)

    def test_a_payload_that_is_not_json_blocks(self):
        for module in ("trimtab.adapters.plan_gate", "trimtab.adapters.plan_review_record"):
            with self.subTest(module):
                done = self.run_module(module, "not json")

                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn("blocked", done.stderr.lower())

    def test_a_payload_that_is_not_an_object_blocks(self):
        for module in ("trimtab.adapters.plan_gate", "trimtab.adapters.plan_review_record"):
            with self.subTest(module):
                done = self.run_module(module, "[1, 2]")

                self.assertEqual(done.returncode, 2, done.stderr)

    def test_a_call_neither_adapter_judges_passes(self):
        load = json.dumps({"tool_name": "Read", "hook_event_name": "PreToolUse", "cwd": "/",
                           "tool_input": {"file_path": "/x"}})
        for module in ("trimtab.adapters.plan_gate", "trimtab.adapters.plan_review_record"):
            with self.subTest(module):
                done = self.run_module(module, load)

                self.assertEqual(done.returncode, 0, done.stderr)


class TheKey(unittest.TestCase):
    """The current key, what a pass is accepted for, and what reading it costs (ADR 0063 §3)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.config = self.tmp / "config"
        self.config.mkdir(mode=0o700)
        self.code = roots.code_root()
        self.head = "d" * 40
        self.lock, self.base = "e" * 40, gate.head_of(self.code)
        for patch in stable_install():
            patch.start()
            self.addCleanup(patch.stop)

    def key(self, **kw):
        return gate.pinned_key("owner/proj", self.code, self.code, self.code, self.config,
                               head=self.head, pinned=(self.lock, self.base), **kw)

    def arm(self, epoch="f" * 32, key=None):
        """Write a pass as the recorder does, under a current epoch."""
        directory = gate.guardstate.directory(self.config, *gate.CACHE_PARTS, create=True)
        gate.guardstate.write(directory / gate.EPOCH, epoch)
        gate.record_pass(self.config, "owner/proj", key or self.key(), epoch)

    def test_the_key_names_the_lock_the_base_the_head_and_the_install_digest_in_that_order(self):
        got = self.key()

        self.assertEqual(got[:3], (self.lock, self.base, self.head))
        self.assertRegex(got[3], r"^[0-9a-f]{64}$")
        self.assertEqual(got, self.key())  # the same install reads as the same key

    def test_a_pass_for_this_key_holds_and_one_for_another_head_does_not(self):
        self.arm()

        with mock.patch.object(gate, "remote_head", lambda git, repo, auth=None: self.head):
            self.assertTrue(gate.pass_holds("owner/proj", self.code, self.code, self.code, self.config))
        with mock.patch.object(gate, "remote_head", lambda git, repo, auth=None: "9" * 40):
            self.assertFalse(gate.pass_holds("owner/proj", self.code, self.code, self.code, self.config))

    def test_a_pass_from_an_earlier_epoch_does_not_hold(self):
        self.arm()
        directory = gate.guardstate.directory(self.config, *gate.CACHE_PARTS, create=True)
        gate.guardstate.write(directory / gate.EPOCH, "a" * 32)

        with mock.patch.object(gate, "remote_head", lambda git, repo, auth=None: self.head):
            self.assertFalse(gate.pass_holds("owner/proj", self.code, self.code, self.code, self.config))

    def test_a_pass_whose_lock_or_base_differs_does_not_hold(self):
        self.arm(key=("1" * 40, self.base, self.head, "x"))

        with mock.patch.object(gate, "remote_head", lambda git, repo, auth=None: self.head):
            self.assertFalse(gate.pass_holds("owner/proj", self.code, self.code, self.code, self.config))

    def test_no_cached_pass_is_no_pass(self):
        with mock.patch.object(gate, "remote_head", lambda git, repo, auth=None: self.head):
            self.assertFalse(gate.pass_holds("owner/proj", self.code, self.code, self.code, self.config))

    def test_a_repository_that_is_not_owner_name_is_refused(self):
        with self.assertRaises(gate.GateError):
            gate.pinned_key("nope", Path("/"), Path("/"), Path("/"), Path("/"))

    def test_a_failed_read_is_no_pass_never_an_allow(self):
        self.assertFalse(gate.pass_holds("owner/proj", Path("/nonexistent"), Path("/nonexistent"),
                                         Path("/nonexistent"), Path("/nonexistent")))

    def test_the_head_is_read_by_ls_remote_under_its_own_bound(self):
        calls = []

        class FakeGit:
            def ls_remote(self, cwd, url, ref, timeout, auth=None):
                calls.append((url, ref, timeout))
                return "a" * 40

        self.assertEqual(gate.remote_head(FakeGit(), "owner/proj"), "a" * 40)
        self.assertEqual(calls, [("https://github.com/owner/proj.git", "HEAD", gate.LS_REMOTE_SECONDS)])

    def test_a_remote_that_names_no_head_is_a_failed_read(self):
        class FakeGit:
            def ls_remote(self, cwd, url, ref, timeout, auth=None):
                raise tools.ToolError("ls-remote failed")

        with self.assertRaises(gate.GateError):
            gate.remote_head(FakeGit(), "owner/proj")


class LsRemote(unittest.TestCase):
    """`Git.ls_remote` reads the SHA of one ref from the command's output, or fails (ADR 0063 §3)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])

    def git(self, out, code=0):
        git = tools.Git.__new__(tools.Git)
        git._binary, git._env, git._timeout = "/bin/true", {}, 1
        git._raw = lambda cwd, *args, **kw: out.encode()
        return git

    def recording(self, out=None):
        """A Git whose `_raw` records each call as (cwd, args) and answers `out` for ls-remote."""
        calls = []
        git = tools.Git.__new__(tools.Git)
        git._binary, git._env, git._timeout = "/bin/true", {}, 1

        def raw(cwd, *args, **kw):
            calls.append((Path(cwd), args))
            return (out or f"{'a' * 40}\tHEAD\n").encode() if "ls-remote" in args else b""

        git._raw = raw
        return git, calls

    def test_the_pinned_credential_helper_is_given_when_an_auth_is(self):
        git, calls = self.recording()

        class Auth:
            binary = "/usr/bin/gh"
            env = {"GH_CONFIG_DIR": "/tmp/empty", "GH_HOST": "github.com", "GH_TOKEN": "x"}

        git.ls_remote(self.tmp, "https://example.invalid/x.git", "HEAD", 1, Auth())

        listing = [args for _, args in calls if "ls-remote" in args][0]
        self.assertIn("credential.helper=!/usr/bin/gh auth git-credential", listing)

    def test_the_read_runs_inside_a_bare_repository_of_its_own(self):
        # Git discovers a repository upward from its directory and reads that repository's config, so
        # an `insteadOf` planted in a repository above the temp dir would answer for the key's head
        # (review round 2). A bare repository made for the read is found first, with an empty config.
        git, calls = self.recording()

        git.ls_remote(self.tmp, "https://example.invalid/x.git", "HEAD", 1)

        made = [args for _, args in calls if "init" in args]
        listing = [(cwd, args) for cwd, args in calls if "ls-remote" in args][0]
        self.assertEqual(len(made), 1)
        self.assertIn("--bare", made[0])
        self.assertEqual(listing[0], self.tmp / "ls-remote.git")
        self.assertEqual(made[0][-1], str(self.tmp / "ls-remote.git"))

    def test_a_failed_init_is_a_failed_read(self):
        git = tools.Git.__new__(tools.Git)
        git._binary, git._env, git._timeout = "/bin/true", {}, 1

        def raw(cwd, *args, **kw):
            raise tools.ToolError("init failed")

        git._raw = raw

        with self.assertRaises(tools.ToolError):
            git.ls_remote(self.tmp, "https://example.invalid/x.git", "HEAD", 1)

    def test_the_named_refs_sha_is_returned(self):
        out = f"{'a' * 40}\tHEAD\n{'b' * 40}\trefs/heads/main\n"

        self.assertEqual(self.git(out).ls_remote(self.tmp, "https://example.invalid/x.git", "HEAD", 1),
                         "a" * 40)

    def test_another_refs_line_is_not_taken_for_it(self):
        out = f"{'b' * 40}\trefs/heads/main\n"

        with self.assertRaises(tools.ToolError):
            self.git(out).ls_remote(self.tmp, "https://example.invalid/x.git", "HEAD", 1)

    def test_a_short_or_absent_sha_is_a_failure(self):
        for out in ("", "\n", "deadbeef\tHEAD\n", "\tHEAD\n", f"{'a' * 40} HEAD\n"):
            with self.subTest(out=out), self.assertRaises(tools.ToolError):
                self.git(out).ls_remote(self.tmp, "https://example.invalid/x.git", "HEAD", 1)


    def test_a_probe_directory_that_already_exists_is_a_failed_read_not_reused(self):
        # A same-uid process could plant `ls-remote.git/config` between the temp dir's creation and the
        # init, and `git init` on an existing directory keeps that config; creating it exclusively makes
        # the planted case fail closed (review round 3).
        (self.tmp / "ls-remote.git").mkdir()
        git, calls = self.recording()

        with self.assertRaises(tools.ToolError):
            git.ls_remote(self.tmp, "https://example.invalid/x.git", "HEAD", 1)

        self.assertEqual(calls, [])


class TheHookBranch(unittest.TestCase):
    """hooks/guard-paths.sh reads the gate only in a run session."""

    def hook(self, load, environ=None):
        """The hook's environment is built, never copied: a developer's `TRIMTAB_PLAN_RUN` or
        `TRIMTAB_INSTANCE` would otherwise decide the case, and CI and a shell would disagree (TST-1)."""
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/root"),
               "HOOK_ALLOW_HARNESS": "0", **(environ or {})}
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

    def test_a_run_session_with_a_passing_gate_falls_through_to_the_path_guard(self):
        # The gate is read, passes, and the call then meets the ordinary path checks: a write to a
        # protected path is still refused, by the layer below (ADR 0039).
        with tempfile.TemporaryDirectory() as tmp:
            done = self.hook({"tool_name": "Write", "cwd": tmp,
                              "tool_input": {"file_path": f"{tmp}/CLAUDE.md"}},
                             {"TRIMTAB_PLAN_RUN": "1", "CLAUDE_CONFIG_DIR": tmp,
                              "HOOK_ALLOW_PATHS": "1"})

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_run_session_blocks_when_the_classifier_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / "hooks"
            hooks.mkdir()
            (hooks / "guard-paths.sh").write_text(
                (roots.code_root() / "hooks" / "guard-paths.sh").read_text())
            done = subprocess.run(["bash", str(hooks / "guard-paths.sh")],
                                  input=json.dumps({"tool_name": "Bash", "cwd": tmp,
                                                    "tool_input": {"command": "ls"}}),
                                  capture_output=True, text=True,
                                  env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                       "HOME": tmp, "TRIMTAB_PLAN_RUN": "1",
                                       "CLAUDE_CONFIG_DIR": tmp})

        self.assertEqual(done.returncode, 2)
        self.assertIn("classifier", done.stderr)

    def test_a_run_session_allows_the_report_command(self):
        report = f"{(roots.code_root() / 'bin' / 'trimtab').resolve()} check-enforcement"
        with tempfile.TemporaryDirectory() as tmp:
            done = self.hook({"tool_name": "Bash", "cwd": tmp, "tool_input": {"command": report}},
                             {"TRIMTAB_PLAN_RUN": "1", "CLAUDE_CONFIG_DIR": tmp})

        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
