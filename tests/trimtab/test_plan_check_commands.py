"""The four `trimtab plan-*-check` subcommands (ADR 0063 §5): their plumbing and their exit codes.

0 allows, 1 refuses with the row's reason on stderr, 2 is a failed read. They report only: nothing
here writes the ledger, which §1 gives to the hooks.
"""

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from instance_fixture import make_instance
from test_planchecks import AGENT, BRANCH, FakeFacts, H1, MB, PLAN, REVIEWER
from trimtab import cli, guardstate, planchecks, planledger, planreview, planscope

FLOWS = ".github/" + "workflows/"
PLANS = "docs/" + "plans/"
KEYS = {
    "accounts": {"admin": "the-admin", "agent": AGENT},
    "secret_scan": {"workflow": FLOWS + "scan.yml", "job": "secret-scan"},
    "review": {
        "run": {"workflow": FLOWS + "review.yml", "job": "review", "event": "pull_request"},
        "facts": {"workflow": FLOWS + "facts.yml", "job": "facts"},
        "login": REVIEWER, "rerequest_label": "review:run",
        "tags": {"fixed": "[fixed]", "wrong": "[wrong]", "intentional": "[intentional]", "moot": "[moot]",
                 "deferred": "[deferred]"},
    },
    "fix_limits": {"markers": ["pytest.skip("], "thresholds": ["fail_under"]},
}


class Commands(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.config = self.tmp / "config"
        self.config.mkdir()
        self.instance = make_instance(self.tmp)
        body = json.loads((self.instance / "instance.json").read_text())
        (self.instance / "instance.json").write_text(json.dumps({**body, **KEYS}))
        self.plan_text = "1. First.\n2. Second, which names `pytest.skip('flaky')`.\n"

    def main(self, argv, facts=None, plan_text=None, scope=True):
        out, err = io.StringIO(), io.StringIO()
        found = facts or FakeFacts()
        plan_path = f"{PLANS}authorised/{PLAN}.md"
        text = self.plan_text if plan_text is None else plan_text
        result = planscope.Result(planscope.Ref.PLAN, PLAN,
                                  () if scope else ("src/x.py: outside the plan's scope",))
        with mock.patch.object(cli.planguard, "build_facts", lambda repo, **kw: found), \
                mock.patch.object(cli.planscope, "check", lambda *a, **kw: result), \
                mock.patch.object(cli.planscope, "read_files", lambda repo, sha, paths: {plan_path: text}), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main([*argv, "--instance", str(self.instance)])
        return code, out.getvalue(), err.getvalue()

    def ledger(self, item=2, part=1):
        found = planledger.Ledger(self.config, "owner/proj", PLAN, item, part,
                                  clock=lambda: "2026-11-13T10:00:00+00:00")
        found.start(remote_branch_exists=False)
        return found

    def test_a_branch_that_is_not_a_plan_part_is_a_failed_read(self):
        code, _, err = self.main(["plan-ready-check", "--repo", str(self.tmp), "--branch", "feat/x",
                                  "--head", H1])

        self.assertEqual(code, 2)
        self.assertIn("item-NN-partK", err)

    def test_a_refused_row_exits_1_and_names_itself(self):
        self.ledger()
        code, _, err = self.main(["plan-ready-check", "--repo", str(self.tmp), "--branch", BRANCH,
                                  "--head", H1], facts=FakeFacts(comments=[]))

        self.assertEqual(code, 1)
        self.assertIn("E7", err)

    def test_an_allowed_check_exits_0(self):
        ledger = self.ledger()
        ledger.push(BRANCH, MB, H1, run_env="1")
        facts = FakeFacts(branch_commits=(H1,), push_commits={(MB, H1): frozenset({H1})},
                          activity=(planreview.Activity(MB, H1, AGENT, "2026-11-12T11:00:00Z"),))

        code, out, err = self.main(["plan-resume-check", "--repo", str(self.tmp), "--branch", BRANCH,
                                    "--head", H1], facts=facts, scope=True)

        self.assertEqual((code, err), (0, ""))
        self.assertIn("plan-resume-check", out)

    def test_a_diff_outside_the_plans_scope_refuses_the_resume(self):
        ledger = self.ledger()
        ledger.push(BRANCH, MB, H1, run_env="1")
        facts = FakeFacts(branch_commits=(H1,), push_commits={(MB, H1): frozenset({H1})},
                          activity=(planreview.Activity(MB, H1, AGENT, "2026-11-12T11:00:00Z"),))

        code, _, err = self.main(["plan-resume-check", "--repo", str(self.tmp), "--branch", BRANCH,
                                  "--head", H1], facts=facts, scope=False)

        self.assertEqual(code, 1)
        self.assertIn("E13", err)

    def test_a_part_with_no_ledger_is_a_failed_read_never_an_allow(self):
        code, _, err = self.main(["plan-rerequest-check", "--repo", str(self.tmp), "--branch", BRANCH,
                                  "--head", H1])

        self.assertEqual(code, 2)
        self.assertIn("guard-state", err)  # no ledger for the part: a failed read, never an allow

    def test_a_push_check_without_fix_limits_refuses(self):
        self.ledger()
        body = json.loads((self.instance / "instance.json").read_text())
        body.pop("fix_limits")
        (self.instance / "instance.json").write_text(json.dumps(body))

        code, _, err = self.main(["plan-push-check", "--repo", str(self.tmp), "--branch", BRANCH,
                                  "--old", MB, "--new", H1])

        self.assertEqual(code, 1)
        self.assertIn("E18", err)

    def test_a_push_check_reads_the_named_lines_from_the_plan_at_the_merge_base(self):
        self.ledger()
        from trimtab import planrows
        change = planrows.Change("tests/test_a.py", "M", ("    pytest.skip('flaky')",), 0, False, ())

        refused, _, err = self.main(["plan-push-check", "--repo", str(self.tmp), "--branch", BRANCH,
                                     "--old", MB, "--new", H1],
                                    facts=FakeFacts(changes=(change,)), plan_text="1. First.\n")
        allowed, _, _ = self.main(["plan-push-check", "--repo", str(self.tmp), "--branch", BRANCH,
                                   "--old", MB, "--new", H1], facts=FakeFacts(changes=(change,)))

        self.assertEqual((refused, "E18" in err, allowed), (1, True, 0))

    def test_the_push_check_reads_the_plan_at_the_verified_tip_or_the_commit_sent(self):
        # A bare `origin/<branch>` is a name a session can move; the verified tip is not (round 3).
        asked = []
        real = cli.planacts.limits_for

        def spy(repo, facts, branch, inst, item, at):
            asked.append(at)
            return real(repo, facts, branch, inst, item, at)

        self.ledger()
        facts = FakeFacts(branch_head=H1)
        with mock.patch.object(cli.planacts, "limits_for", spy):
            self.main(["plan-push-check", "--repo", str(self.tmp), "--branch", BRANCH, "--old", MB,
                       "--new", H1], facts=facts)
            self.main(["plan-push-check", "--repo", str(self.tmp), "--branch", BRANCH, "--old", "0" * 40,
                       "--new", H1], facts=facts)

        self.assertEqual(asked, [H1, H1])  # the tip FakeFacts.branch_sha answers, then the commit sent

    def test_a_tip_the_server_does_not_confirm_is_a_failed_read(self):
        self.ledger()

        code, _, err = self.main(["plan-push-check", "--repo", str(self.tmp), "--branch", BRANCH,
                                  "--old", MB, "--new", H1], facts=FakeFacts(raises="branch_head"))

        self.assertEqual(code, 2)

    def test_a_plan_that_cannot_be_read_at_the_merge_base_is_a_failed_read(self):
        self.ledger()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.planguard, "build_facts", lambda repo, **kw: FakeFacts()), \
                mock.patch.object(cli.planscope, "read_files", lambda repo, sha, paths: {}), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["plan-push-check", "--repo", str(self.tmp), "--branch", BRANCH, "--old", MB,
                             "--new", H1, "--instance", str(self.instance)])

        self.assertEqual(code, 2)
        self.assertIn("authorised plan", err.getvalue())

    def test_the_checks_write_nothing_to_the_guard_state(self):
        self.ledger()
        before = (self.config / guardstate.ROOT / "plan-runs").rglob("*")
        names = sorted(p.name for p in before)

        self.main(["plan-ready-check", "--repo", str(self.tmp), "--branch", BRANCH, "--head", H1])

        self.assertEqual(sorted(p.name for p in (self.config / guardstate.ROOT / "plan-runs").rglob("*")),
                         names)

    def test_the_exit_code_follows_the_verdict(self):
        cases = ((planchecks.Verdict(), 0), (planchecks.Verdict(refusal="E7: no"), 1),
                 (planchecks.Verdict(unreadable="gh failed"), 2))
        for verdict, want in cases:
            with self.subTest(verdict=verdict), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli._plan_exit("plan-ready-check", verdict), want)


if __name__ == "__main__":
    unittest.main()
