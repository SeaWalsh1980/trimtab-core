"""The plan-run ledger (ADR 0063 §1): append-only, private, keyed by repo, plan and part."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from trimtab import guardstate, planledger as pl

REPO, PLAN = "owner/proj", "0008-slug"
H1, H2 = "1" * 40, "2" * 40
NOW = "2026-11-13T10:00:00+00:00"


def clock():
    return NOW


class LedgerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Path(tmp.name)
        os.chmod(self.cfg, 0o700)

    def ledger(self, part=1, item=2):
        return pl.Ledger(self.cfg, REPO, PLAN, item, part, clock=clock)


class PathTest(LedgerCase):
    def test_path_is_keyed_by_repo_hash_plan_item_and_part(self):
        key = hashlib.sha256(REPO.encode()).hexdigest()
        path = self.ledger(part=3, item=2).path
        self.assertEqual(path, self.cfg / "guard-state" / "plan-runs" / key / PLAN / "item-02-part3.jsonl")

    def test_a_plan_name_that_climbs_is_refused(self):
        with self.assertRaises(pl.LedgerError):
            pl.Ledger(self.cfg, REPO, "../x", 1, 1, clock=clock)


class StartTest(LedgerCase):
    def test_a_part_with_no_ledger_is_absent_not_empty(self):
        self.assertFalse(self.ledger().exists())

    def test_start_creates_a_ledger_when_the_remote_branch_is_absent(self):
        led = self.ledger()
        led.start(remote_branch_exists=False)
        self.assertTrue(led.exists())
        self.assertEqual(led.events(), [])

    def test_start_refuses_when_the_remote_branch_already_exists(self):
        led = self.ledger()
        with self.assertRaises(pl.LedgerError):
            led.start(remote_branch_exists=True)
        self.assertFalse(led.exists())

    def test_start_on_an_existing_ledger_keeps_its_events(self):
        led = self.ledger()
        led.start(remote_branch_exists=False)
        led.push("plan/x", H1, H2, run_env="1")
        led.start(remote_branch_exists=True)  # a ledger that exists is not a new start
        self.assertEqual(len(led.events()), 1)


class AppendTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.led = self.ledger()
        self.led.start(remote_branch_exists=False)

    def test_each_event_is_a_versioned_line_with_the_injected_time(self):
        self.led.push("plan/x", H1, H2, run_env="1")
        (ev,) = self.led.events()
        self.assertEqual((ev["v"], ev["type"], ev["time"]), (1, "push", NOW))
        self.assertEqual((ev["branch"], ev["old"], ev["new"], ev["run"]), ("plan/x", H1, H2, "1"))

    def test_events_keep_their_order(self):
        self.led.push("plan/x", H1, H2, run_env="1")
        self.led.round(H2, "trimtab-review", before_draft=True)
        self.led.draft(7, H2)
        self.led.rerequest(H2, "ci")
        self.led.stop("e5")
        self.led.conflict(H2)
        self.assertEqual([e["type"] for e in self.led.events()],
                         ["push", "round", "draft", "rerequest", "stop", "conflict"])

    def test_the_same_pass_on_the_same_head_counts_once(self):
        self.led.round(H1, "code-review", before_draft=True)
        self.led.round(H1, "code-review", before_draft=True)
        self.assertEqual(len(self.led.events()), 1)

    def test_a_repeated_stop_reason_is_written_once(self):
        self.led.stop("e5")
        self.led.stop("e5")
        self.assertEqual(len(self.led.events()), 1)

    def test_an_unknown_pass_is_refused(self):
        with self.assertRaises(pl.LedgerError):
            self.led.round(H1, "other-review", before_draft=True)

    def test_a_head_that_is_not_a_full_sha_is_refused(self):
        with self.assertRaises(pl.LedgerError):
            self.led.round("abc", "code-review", before_draft=True)

    def test_append_without_a_started_ledger_is_refused(self):
        with self.assertRaises(pl.LedgerError):
            self.ledger(part=9).stop("x")

    def test_a_draft_and_a_conflict_on_the_same_head_are_written_once(self):
        self.led.draft(7, H1)
        self.led.draft(7, H1)
        self.led.conflict(H1)
        self.led.conflict(H1)
        self.assertEqual([e["type"] for e in self.led.events()], ["draft", "conflict"])

    def test_every_re_request_is_written(self):
        self.led.rerequest(H1, "ci")
        self.led.rerequest(H1, "ci")
        self.assertEqual(len(self.led.events()), 2)


class ReadTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.led = self.ledger()
        self.led.start(remote_branch_exists=False)

    def test_a_malformed_line_is_an_error_not_skipped(self):
        self.led.stop("a")
        with open(self.led.path, "a") as fh:
            fh.write("not json\n")
        with self.assertRaises(pl.LedgerError):
            self.led.events()

    def test_a_line_with_an_unknown_version_is_an_error(self):
        with open(self.led.path, "a") as fh:
            fh.write('{"v": 2, "type": "stop", "time": "t", "reason": "x"}\n')
        with self.assertRaises(pl.LedgerError):
            self.led.events()

    def test_a_planted_symlink_for_the_file_is_refused(self):
        target = self.cfg / "elsewhere"
        target.write_text("")
        self.led.path.unlink()
        os.symlink(target, self.led.path)
        with self.assertRaises(pl.LedgerError):
            self.led.events()
        with self.assertRaises(pl.LedgerError):
            self.led.stop("x")
        self.assertEqual(target.read_text(), "")

    def test_a_line_missing_the_fields_its_type_needs_is_a_ledger_error(self):
        with open(self.led.path, "a") as fh:
            fh.write('{"v": 1, "type": "round", "time": "t"}\n')
        with self.assertRaises(pl.LedgerError):
            self.led.pre_draft_heads()
        with self.assertRaises(pl.LedgerError):
            self.led.complete_rounds()

    def test_a_symlinked_plan_directory_is_refused(self):
        plan_dir = self.led.path.parent
        moved = plan_dir.with_name("moved")
        plan_dir.rename(moved)
        os.symlink(moved, plan_dir)
        with self.assertRaises(pl.LedgerError):
            self.led.events()
        with self.assertRaises(pl.LedgerError):
            self.led.stop("x")

    def test_a_file_open_to_other_users_is_refused(self):
        os.chmod(self.led.path, 0o644)
        with self.assertRaises(pl.LedgerError):
            self.led.events()

    def test_an_oversized_ledger_is_refused_for_append(self):
        with open(self.led.path, "ab") as fh:
            fh.write(b"x" * (guardstate.MAX_BYTES + 1))
        with self.assertRaises(pl.LedgerError):
            self.led.stop("late")


class RoundsTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.led = self.ledger()
        self.led.start(remote_branch_exists=False)

    def test_a_round_is_complete_when_all_three_passes_are_on_one_head(self):
        for p in pl.PASSES:
            self.led.round(H1, p, before_draft=True)
        self.assertEqual(self.led.complete_rounds(), [H1])

    def test_pre_draft_heads_lists_each_head_with_a_pass_before_the_draft_once(self):
        self.led.round(H1, "code-review", before_draft=True)
        self.led.round(H1, "trimtab-review", before_draft=True)
        self.led.round(H2, "code-review", before_draft=False)
        self.assertEqual(self.led.pre_draft_heads(), [H1])

    def test_passes_spread_over_two_heads_are_not_a_complete_round(self):
        self.led.round(H1, "trimtab-review", before_draft=True)
        self.led.round(H1, "code-review", before_draft=True)
        self.led.round(H2, "security-review", before_draft=True)
        self.assertEqual(self.led.complete_rounds(), [])


if __name__ == "__main__":
    unittest.main()
