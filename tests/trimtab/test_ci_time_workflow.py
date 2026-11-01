"""The wiring that runs each guard matrix pass once on a PR, and the unit tests by module (ADR 0054).

A repository-consistency check, as test_draft_ci_workflow is: it reads the
tracked workflow on purpose, because the workflow is what it checks. The
comparison is the one thing here that can run outside GitHub, so
ComparisonTest runs that step's own text in a throwaway repository. GitHub's
behaviour itself is not testable here; ADR 0054 records the trial on real PRs.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

MATERIALISE = "base expectations: materialise the base branch's guard matrix"
SKIPPED_WHEN_IDENTICAL = (
    "guard matrix: jq present",
    "remove jq (the python3 fallback runs below)",
    "guard matrix: python3 fallback",
)
BASE_EXPECTATIONS = (
    "hide jq (the base python3 fallback runs next)",
    "base expectations: python3 fallback",
    "restore jq",
    "base expectations: hold against the PR's hooks (jq present)",
)
SKIP_CONDITION = (
    "${{ !cancelled() && !(github.event_name == 'pull_request' "
    "&& steps.base_matrix.outputs.identical == 'true') }}"
)
MATRIX = "tests/guard-tests.sh"


def step_list() -> list:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["lint-test"]["steps"]


def steps() -> dict:
    return {step["name"]: step for step in step_list() if "name" in step}


class MatrixPassOnceTest(unittest.TestCase):
    def setUp(self):
        self.steps = steps()
        self.order = [step.get("name") for step in step_list()]

    def test_only_a_pull_request_with_an_exact_true_skips_the_later_passes(self):
        # A push is not a pull_request and an unset output is not 'true', so
        # both run every pass under this condition.
        for name in SKIPPED_WHEN_IDENTICAL:
            self.assertEqual(self.steps[name]["if"], SKIP_CONDITION, name)

    def test_the_base_expectations_never_read_the_comparison(self):
        for name in BASE_EXPECTATIONS:
            self.assertEqual(self.steps[name]["if"], "${{ !cancelled() && github.event_name == 'pull_request' }}", name)

    def test_the_base_expectations_run_before_the_passes_they_stand_in_for(self):
        last_base = max(self.order.index(name) for name in BASE_EXPECTATIONS)
        first_skippable = min(self.order.index(name) for name in SKIPPED_WHEN_IDENTICAL)

        self.assertLess(last_base, first_skippable)

    def test_the_comparison_is_made_before_any_step_that_runs_pr_code(self):
        self.assertEqual(self.steps[MATERIALISE]["id"], "base_matrix")
        self.assertLess(self.order.index(MATERIALISE), min(self.order.index(name) for name in BASE_EXPECTATIONS))

    def test_the_rule_usage_matrix_always_runs(self):
        self.assertEqual(self.steps["guard matrix: rule-usage matrix"]["if"], "${{ !cancelled() }}")

    def test_no_step_may_fail_without_failing_the_job(self):
        self.assertFalse([name for name, step in self.steps.items() if "continue-on-error" in step])


class ComparisonTest(unittest.TestCase):
    """The materialise step's own text, on a clone whose HEAD stands in for the merge ref."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # No inherited GIT_DIR, and no operator config reaching the fixture.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(HOME=tmp.name, GIT_CONFIG_NOSYSTEM="1", GITHUB_BASE_REF="main",
                        GITHUB_OUTPUT=str(self.root / "output"))
        self.origin = self.root / "origin"
        self.git(self.root, "init", "-q", "-b", "main", str(self.origin))
        self.write(self.origin, "#!/bin/sh\n")
        # So tests/ outlives the matrix file, as it does in this repository.
        (self.origin / "tests" / "other").write_text("", encoding="utf-8")
        self.git(self.origin, "add", ".")
        self.commit(self.origin)
        self.repo = self.root / "repo"
        self.git(self.root, "clone", "-q", str(self.origin), str(self.repo))

    def git(self, cwd, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                       cwd=cwd, env=self.env, check=True, capture_output=True)

    def commit(self, cwd):
        self.git(cwd, "commit", "-q", "--allow-empty", "-m", "c")

    def write(self, repo, text):
        path = repo / MATRIX
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def identical(self):
        """Runs the step. Returns (exit status, the output's value, or None if it never wrote one)."""
        result = subprocess.run(["bash", "-c", steps()[MATERIALISE]["run"]], cwd=self.repo, env=self.env,
                                capture_output=True, text=True)
        output = self.root / "output"
        lines = output.read_text(encoding="utf-8").splitlines() if output.exists() else []
        values = [line.split("=", 1)[1] for line in lines if line.startswith("identical=")]
        self.assertLessEqual(len(values), 1, lines)
        return result.returncode, values[0] if values else None

    def test_an_unchanged_matrix_file_compares_true(self):
        self.commit(self.repo)

        self.assertEqual(self.identical(), (0, "true"))

    def test_changed_content_compares_false(self):
        self.write(self.repo, "#!/bin/sh\nexit 0\n")
        self.git(self.repo, "add", ".")
        self.commit(self.repo)

        self.assertEqual(self.identical(), (0, "false"))

    def test_a_changed_mode_compares_false(self):
        (self.repo / MATRIX).chmod(0o644)
        self.git(self.repo, "add", ".")
        self.commit(self.repo)

        self.assertEqual(self.identical(), (0, "false"))

    def test_a_deleted_matrix_file_compares_false(self):
        self.git(self.repo, "rm", "-q", MATRIX)
        self.commit(self.repo)

        self.assertEqual(self.identical(), (0, "false"))

    def test_a_symlink_to_the_base_copy_compares_false(self):
        # By bytes it would match: the link resolves to the file the step writes.
        (self.repo / MATRIX).unlink()
        (self.repo / MATRIX).symlink_to("base-guard-tests.sh")
        self.git(self.repo, "add", ".")
        self.commit(self.repo)

        self.assertEqual(self.identical(), (0, "false"))

    def test_a_base_branch_without_the_file_writes_no_true(self):
        self.git(self.origin, "rm", "-q", MATRIX)
        self.commit(self.origin)

        status, value = self.identical()

        self.assertNotEqual(status, 0)
        self.assertNotEqual(value, "true")


class UnitTestsByModuleTest(unittest.TestCase):
    def setUp(self):
        self.run_text = steps()["trimtab: tests"]["run"]

    def test_the_step_stops_on_an_unexpected_failure(self):
        # The loader's call and an empty log directory are loud only under this.
        self.assertEqual(self.run_text.splitlines()[0], "set -euo pipefail")

    def test_fan_out_is_bounded(self):
        self.assertIn("xargs -P4 -n1", self.run_text)

    def test_a_module_cannot_run_forever(self):
        self.assertIn("timeout -k 30s 10m python3 -m unittest", self.run_text)

    def test_a_failed_module_fails_the_step_only_after_the_logs_print(self):
        self.assertIn("|| rc=$?", self.run_text)
        self.assertEqual(self.run_text.splitlines()[-1], 'exit "$rc"')
        self.assertIn('cat "$logs"/*.log', self.run_text)

    def test_what_ran_must_match_what_the_loader_finds(self):
        self.assertIn("countTestCases()", self.run_text)
        self.assertIn('if [[ "$ran" != "$want" ]]', self.run_text)


if __name__ == "__main__":
    unittest.main()
