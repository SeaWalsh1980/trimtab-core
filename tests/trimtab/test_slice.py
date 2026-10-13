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
    "trimtab_sha": "0" * 40, "id_prefix": "SCR",
    "adopted_at": "2026-09-27T00:00:00Z", "baseline_pr": 100, "schema_version": 2,
}


AS_OF = "2026-10-01"
# The tail of a routine, environment or session ID; tests prepend the prefix.
RUN_ID_TAIL = "01" + "A1b2C3d4E5f6G7h8J9k0"


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

    def test_check_pr_fails_a_session_link_after_a_valid_block(self):
        body = (FIXTURES / "pr-tst2-ambiguous.md").read_text(encoding="utf-8")
        body += "\nhttps://claude.ai/code/" + "session_" + RUN_ID_TAIL + "\n"

        done = subprocess.run([str(TRIMTAB), "check-pr", "--body-file", "-"], input=body,
                              capture_output=True, text=True, cwd=self.project, env=self.env)

        self.assertEqual(done.returncode, 1)
        self.assertIn("id-shape", done.stderr)
        self.assertNotIn(RUN_ID_TAIL, done.stderr)

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


SCHEMA_ONLY_RULE = "---\nid_prefix: SCR-LAYERING\n---\n\n## 1. Layers\n\n- **REQUIRE** ports own their types.\n"


def schema_only_body(*ids):
    applied = "".join(f"- id: {i}\n  why: applied\n" for i in ids)
    return (f"## Harness items applied\n```yaml\n{applied}```\n\n"
            "## Harness feedback\n```yaml\n```\n\n## Process cost\n```yaml\n```\n")


class SchemaOnlyCli(unittest.TestCase):
    """`check-pr --schema-only` as a consumer's CI runs it: no instance anywhere.

    Builds a scratch project (a lock and one rule file) in a temporary directory.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "scratch"
        (self.project / ".claude" / "rules").mkdir(parents=True)
        (self.project / ".claude" / "trimtab.json").write_text(json.dumps(CONFIG), encoding="utf-8")
        (self.project / ".claude" / "rules" / "layering.md").write_text(SCHEMA_ONLY_RULE, encoding="utf-8")
        self.body = Path(self._tmp.name) / "body.md"

    def tearDown(self):
        self._tmp.cleanup()

    def check_pr(self, body, *extra, env=None):
        self.body.write_text(body, encoding="utf-8")
        return subprocess.run([str(TRIMTAB), "check-pr", "--schema-only", "--project", str(self.project),
                               "--body-file", str(self.body), *extra],
                              capture_output=True, text=True, cwd=self.project, env=env or env_for(None))

    def test_a_valid_block_passes_with_no_instance_set(self):
        done = self.check_pr(schema_only_body("ENG-2", "SCR-LAYERING-1"))

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("PR block OK (schema only: base IDs checked for shape)", done.stdout)

    def test_an_instance_in_the_environment_is_ignored(self):
        missing = Path(self._tmp.name) / "no-such-instance"

        done = self.check_pr(schema_only_body("ENG-2"), env=env_for(missing))

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_naming_an_instance_is_a_usage_error_naming_both_flags(self):
        done = self.check_pr(schema_only_body("ENG-2"), "--instance", self._tmp.name)

        self.assertEqual(done.returncode, 2)
        self.assertIn("--schema-only", done.stderr)
        self.assertIn("--instance", done.stderr)

    def test_a_bad_block_fails_naming_sections_and_fields_not_body_text(self):
        done = self.check_pr(schema_only_body("SCR-LAYERING-zz9secret").replace("## Process cost", "## Cost"))

        self.assertEqual(done.returncode, 1)
        self.assertIn("Process cost", done.stderr)
        self.assertNotIn("zz9secret", done.stderr)

    def test_an_unknown_project_id_fails(self):
        done = self.check_pr(schema_only_body("SCR-LAYERING-zz9secret"))

        self.assertEqual(done.returncode, 1)
        self.assertIn("unknown-id", done.stderr)
        self.assertNotIn("zz9secret", done.stderr)

    def test_a_run_reference_in_the_body_fails_naming_its_line_not_its_value(self):
        for prefix in ("session_", "cse_", "trig_", "env_"):
            with self.subTest(prefix=prefix):
                reference = prefix + RUN_ID_TAIL  # built at run time: never a literal ID in a file
                body = schema_only_body("ENG-2") + f"\nhttps://claude.ai/code/{reference}\n"

                done = self.check_pr(body)

                self.assertEqual(done.returncode, 1)
                self.assertIn("id-shape", done.stderr)
                self.assertIn(f"line {body.count(chr(10))}", done.stderr)
                self.assertNotIn(RUN_ID_TAIL, done.stderr)

    def test_a_short_word_with_an_id_prefix_passes(self):
        done = self.check_pr(schema_only_body("ENG-2") + "\nThe env_file and session_url settings.\n")

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_long_word_in_one_case_with_no_digit_passes(self):
        # Quoted release notes name options like these; an ID's tail mixes cases and digits.
        # Built at run time, so the file holds no literal the scrub reads as an ID.
        notes = "\n> Adds " + "env_" + "PRODUCTIONDATABASEURL and " + "session_" + "cookiesamesitestrict.\n"

        done = self.check_pr(schema_only_body("ENG-2") + notes)

        self.assertEqual(done.returncode, 0, done.stderr)


class IdsCli(unittest.TestCase):
    """`trimtab ids FILE...`: the files a run is about to commit, checked for a run reference only.

    Writes the files in a temporary directory that holds no lock and names no instance.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text):
        (self.dir / name).write_text(text, encoding="utf-8")
        return name

    def ids(self, *names):
        return subprocess.run([str(TRIMTAB), "ids", *names], capture_output=True, text=True, cwd=self.dir,
                              env=env_for(None))

    def test_a_file_holding_a_run_reference_fails_naming_it_and_its_line_not_the_value(self):
        name = self.write("report.md", "# Report\n\nfindings\n" + "_Generated in " + "session_" + RUN_ID_TAIL + "_\n")

        done = self.ids(name)

        self.assertEqual(done.returncode, 1)
        self.assertIn("report.md: line 4", done.stderr)
        self.assertNotIn(RUN_ID_TAIL, done.stderr)

    def test_several_files_are_checked_in_one_call_and_only_the_one_with_a_reference_is_named(self):
        clean = self.write("clean.md", "# Clean\n")
        dirty = self.write("dirty.md", "x " + "trig_" + RUN_ID_TAIL + "\n")

        done = self.ids(clean, dirty)

        self.assertEqual(done.returncode, 1)
        self.assertIn("dirty.md", done.stderr)
        self.assertNotIn("clean.md", done.stderr)

    def test_clean_files_pass_with_no_block_lock_or_instance(self):
        names = [self.write("a.md", "# A\n"), self.write("b.md", "No harness block, and none is needed.\n")]

        done = self.ids(*names)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("no run reference in 2 file(s)", done.stdout)

    def test_a_file_that_is_not_text_is_refused_naming_it_without_a_traceback(self):
        (self.dir / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00")

        done = self.ids("image.png")

        self.assertEqual(done.returncode, 2)
        self.assertIn("image.png", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_a_missing_file_is_refused_naming_it_without_a_traceback(self):
        done = self.ids("gone.md")

        self.assertEqual(done.returncode, 2)
        self.assertIn("gone.md", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_no_file_is_a_usage_error(self):
        done = self.ids()

        self.assertEqual(done.returncode, 2)

    def test_check_pr_refuses_a_missing_body_file_without_a_traceback(self):
        done = subprocess.run([str(TRIMTAB), "check-pr", "--schema-only", "--project", str(self.dir),
                               "--body-file", str(self.dir / "gone.md")],
                              capture_output=True, text=True, cwd=self.dir, env=env_for(None))

        self.assertEqual(done.returncode, 2)
        self.assertIn("gone.md", done.stderr)
        self.assertNotIn("Traceback", done.stderr)


if __name__ == "__main__":
    unittest.main()
