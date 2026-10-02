"""Section 11 slice acceptance test, end to end through `bin/trimtab`.

A scratch project adopts Trimtab (`.claude/trimtab.json`); its merged PRs come
from a fixture file standing in for GitHub. The base registry is a fixture
instance's (hermetic: no real doctrine is read), so `TST-2` is its second section.
"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from instance_fixture import REPO_NAME, env_for, make_instance

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRIMTAB = REPO / "bin" / "trimtab"

CONFIG = {
    "source": REPO_NAME, "trimtab_sha": "0" * 40, "id_prefix": "SCR",
    "adopted_at": "2026-09-27T00:00:00Z", "baseline_pr": 100, "schema_version": 1,
}


AS_OF = "2026-10-01"


def pr(number, fixture, merged_at):
    return {"number": number, "body": (FIXTURES / fixture).read_text(encoding="utf-8"),
            "merged_at": merged_at, "labels": []}


class Slice(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "scratch"
        self.env = env_for(make_instance(Path(self._tmp.name)))
        (self.project / ".claude").mkdir(parents=True)
        (self.project / ".claude" / "trimtab.json").write_text(json.dumps(CONFIG), encoding="utf-8")
        self.pulls = Path(self._tmp.name) / "pulls.json"
        self.pulls.write_text(json.dumps([
            pr(99, "pr-tst2-ambiguous.md", "2026-09-20T00:00:00Z"),  # below the baseline
            pr(101, "pr-tst2-ambiguous.md", "2026-09-28T00:00:00Z"),
            pr(102, "pr-tst2-ambiguous.md", "2026-09-29T00:00:00Z"),
            pr(103, "pr-no-block.md", "2026-09-30T00:00:00Z"),
        ]), encoding="utf-8")
        self.ledger = Path(self._tmp.name) / "ledger.json"

    def tearDown(self):
        self._tmp.cleanup()

    def trimtab(self, *args, ok=True):
        done = subprocess.run([str(TRIMTAB), *args], capture_output=True, text=True, cwd=self.project,
                              env=self.env)
        if ok:
            self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def source(self):
        # A fixed end for the decay window, so the test does not depend on the date it runs.
        return ["--fixture", str(self.pulls), "--ledger", str(self.ledger), "--as-of", AS_OF]

    def apply(self, limit="50"):
        dry = self.trimtab("ingest", "--dry-run", *self.source(), "--limit", limit).stdout
        token = re.search(r"^confirm: (\w+)$", dry, re.M).group(1)
        return self.trimtab("ingest", "--apply", "--confirm", token, *self.source(), "--limit", limit).stdout

    def test_ingest_dry_run_reports_coverage_and_tallies(self):
        out = self.trimtab("ingest", "--dry-run", *self.source()).stdout

        self.assertIn("coverage: 2/3", out)
        self.assertIn("TST-2: 2", out)
        self.assertIn("PRs at or below #100", out)

    def test_propose_prints_exactly_one_upstream_issue(self):
        out = self.trimtab("propose", "--dry-run", *self.source()).stdout

        self.assertEqual(out.count("--- would open:"), 1)
        self.assertIn(f"would open: issue on {REPO_NAME}", out)
        self.assertIn("[TST-2]", out)

    def test_check_pr_fails_an_unknown_id(self):
        done = self.trimtab("check-pr", "--body-file", str(FIXTURES / "pr-unknown-id.md"), ok=False)

        self.assertEqual(done.returncode, 1)
        self.assertIn("unknown-id", done.stderr)

    def test_check_pr_fails_prose_instead_of_yaml(self):
        done = self.trimtab("check-pr", "--body-file", str(FIXTURES / "pr-prose.md"), ok=False)

        self.assertEqual(done.returncode, 1)
        self.assertIn("not-yaml", done.stderr)

    def test_check_pr_passes_a_valid_block_from_stdin(self):
        body = (FIXTURES / "pr-tst2-ambiguous.md").read_text(encoding="utf-8")

        done = subprocess.run([str(TRIMTAB), "check-pr", "--body-file", "-"], input=body,
                              capture_output=True, text=True, cwd=self.project, env=self.env)

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_stopped_run_resumes_without_double_counting(self):
        self.apply(limit="1")
        self.apply()

        records = json.loads(self.ledger.read_text())["records"]
        self.assertEqual([r["number"] for r in records], [101, 102, 103])
        out = self.trimtab("ingest", "--dry-run", *self.source()).stdout
        self.assertIn("TST-2: 2", out)

    def test_an_immediate_rerun_has_nothing_to_ingest(self):
        self.apply()

        out = self.trimtab("ingest", "--dry-run", *self.source()).stdout

        self.assertIn("to record: 0 PRs", out)
        self.assertNotIn("confirm:", out)

    def test_apply_without_the_dry_run_token_changes_nothing(self):
        done = self.trimtab("ingest", "--apply", "--confirm", "000000000000", *self.source(), ok=False)

        self.assertEqual(done.returncode, 3)
        self.assertFalse(self.ledger.exists())
        self.assertNotIn("trimtab-ingested", self.pulls.read_text())


if __name__ == "__main__":
    unittest.main()
