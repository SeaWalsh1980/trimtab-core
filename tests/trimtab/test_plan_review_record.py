"""E11's review recorder and E23's sixth-round refusal (ADR 0063 §5), through guard-paths' branch.

The adapter is tested on payloads, with a fake of this codebase's own `PlanSupport`; the hook's own
branch is tested by running `hooks/guard-paths.sh` with a Skill payload.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import plan_fixture
from test_planchecks import BRANCH, FakeFacts, H1, PLAN
from trimtab import planledger, roots
from trimtab.adapters import plan_review_record as recorder

ALLOW, BLOCK = recorder.ALLOW, recorder.BLOCK
CWD = "/work"


def setUpModule():
    plan_fixture.set_up()


def tearDownModule():
    plan_fixture.tear_down()


class Support:
    def __init__(self, facts, branch=BRANCH, head=H1, plan_run=True):
        self.facts, self.cwd, self.plan_run = facts, Path(CWD), plan_run
        self._branch, self._head = branch, head

    def head_branch(self, cwd=None):
        return self._branch

    def head_sha(self, cwd=None):
        return self._head


def payload(event="PostToolUse", skill="code-review", tool="Skill", cwd=CWD):
    return {"hook_event_name": event, "tool_name": tool, "tool_input": {"skill": skill}, "cwd": cwd}


class PassName(unittest.TestCase):
    def test_the_three_passes_are_read_through_their_spellings(self):
        for spelt, want in (("code-review", "code-review"), ("/code-review", "code-review"),
                            ("plugin:code-review", "code-review"), ("code-review ultra", "code-review"),
                            ("trimtab-review", "trimtab-review"), ("security-review", "security-review")):
            with self.subTest(spelt):
                self.assertEqual(recorder.pass_name(spelt), want)

    def test_anything_else_is_no_pass(self):
        for spelt in ("ship", "", None, 7, "reviewer", "code-review-extra"):
            with self.subTest(spelt):
                self.assertIsNone(recorder.pass_name(spelt))


class Recorder(unittest.TestCase):
    def ledger(self):
        return plan_fixture.ledger()

    def test_a_completed_pass_on_a_plan_branch_is_recorded_with_the_checkouts_head(self):
        code, _ = recorder.decide(payload(), Support(FakeFacts(open_pr=None)))

        self.assertEqual(code, ALLOW)
        self.assertEqual([(e["type"], e["head"], e["pass"], e["before_draft"])
                          for e in self.ledger().events() if e["type"] == "round"][-1:],
                         [("round", H1, "code-review", True)])

    def test_a_round_after_the_pr_opened_is_not_a_pre_draft_round(self):
        code, _ = recorder.decide(payload(skill="security-review"), Support(FakeFacts()))

        self.assertEqual(code, ALLOW)
        self.assertEqual([e["before_draft"] for e in self.ledger().events()
                          if e["type"] == "round" and e["pass"] == "security-review"][-1:], [False])

    def test_a_pass_on_another_branch_records_nothing(self):
        before = len(self.ledger().events())

        code, _ = recorder.decide(payload(), Support(FakeFacts(), branch="feat/x"))

        self.assertEqual((code, len(self.ledger().events())), (ALLOW, before))

    def test_a_call_that_is_not_a_review_pass_reads_nothing(self):
        for load in (payload(skill="ship"), payload(tool="Bash"), payload(event="SessionStart")):
            with self.subTest(load=load):
                self.assertEqual(recorder.decide(load, Support(FakeFacts()))[0], ALLOW)

    def test_a_checkout_the_recorder_cannot_read_blocks(self):
        code, message = recorder.decide(payload(cwd="relative"), Support(FakeFacts()))

        self.assertEqual(code, BLOCK)
        self.assertIn("checkout", message)

    def test_a_head_that_cannot_be_read_blocks(self):
        code, message = recorder.decide(payload(), Support(FakeFacts(), head=None))

        self.assertEqual(code, BLOCK)
        self.assertIn("E11", message)

    def test_a_failed_fact_read_blocks_rather_than_recording_a_round(self):
        code, message = recorder.decide(payload(), Support(FakeFacts(raises="open_pr")))

        self.assertEqual(code, BLOCK)
        self.assertIn("ADR 0063", message)

    def test_a_sixth_pre_draft_round_refuses_at_its_first_pass(self):
        ledger = plan_fixture.ledger(part=plan_fixture.PART)
        for n in range(5):
            ledger.round(f"{n}" * 40, "code-review", before_draft=True)

        # A head of its own, so this test does not read a round another test recorded.
        code, message = recorder.decide(payload(event="PreToolUse"),
                                        Support(FakeFacts(open_pr=None), head="9" * 40))

        self.assertEqual(code, BLOCK)
        self.assertIn("E23", message)


class TheHookBranch(unittest.TestCase):
    """hooks/guard-paths.sh hands a Skill call to the recorder and nothing else."""

    def run_hook(self, load):
        hook = roots.code_root() / "hooks" / "guard-paths.sh"
        return subprocess.run(["bash", str(hook)], input=json.dumps(load), capture_output=True, text=True)

    def test_a_skill_call_on_no_plan_branch_passes_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_hook({"hook_event_name": "PostToolUse", "tool_name": "Skill",
                                  "tool_input": {"skill": "code-review"}, "cwd": tmp})

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_skill_call_that_is_not_a_review_pass_passes_through(self):
        done = self.run_hook({"hook_event_name": "PostToolUse", "tool_name": "Skill",
                              "tool_input": {"skill": "ship"}, "cwd": "/"})

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_skill_call_whose_checkout_cannot_be_read_blocks(self):
        done = self.run_hook({"hook_event_name": "PostToolUse", "tool_name": "Skill",
                              "tool_input": {"skill": "code-review"}, "cwd": "nowhere"})

        self.assertEqual(done.returncode, 2)
        self.assertIn("guard-paths", done.stderr)


if __name__ == "__main__":
    unittest.main()
