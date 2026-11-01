"""The wiring that runs each guard matrix pass once on a PR, and the unit tests by module (ADR 0054).

A repository-consistency check, as test_draft_ci_workflow is: it reads the
tracked workflow on purpose, because the workflow is what it checks. GitHub's
behaviour itself is not testable here; ADR 0054 records the trial on real PRs.
"""

import unittest
from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

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


def steps() -> dict:
    job = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["lint-test"]
    return {step["name"]: step for step in job["steps"] if "name" in step}


class MatrixPassOnceTest(unittest.TestCase):
    def setUp(self):
        self.steps = steps()

    def test_only_a_pull_request_with_an_exact_true_skips_the_later_passes(self):
        # A push is not a pull_request and an unset output is not 'true', so
        # both run every pass under this condition.
        for name in SKIPPED_WHEN_IDENTICAL:
            self.assertEqual(self.steps[name]["if"], SKIP_CONDITION, name)

    def test_the_base_expectations_never_read_the_comparison(self):
        for name in BASE_EXPECTATIONS:
            self.assertEqual(self.steps[name]["if"], "${{ !cancelled() && github.event_name == 'pull_request' }}", name)

    def test_the_comparison_is_made_in_the_step_that_writes_the_base_file(self):
        step = self.steps["base expectations: materialise the base branch's guard matrix"]

        self.assertEqual(step["id"], "base_matrix")
        self.assertLess(step["run"].index("tests/base-guard-tests.sh"), step["run"].index("cmp -s"))

    def test_the_rule_usage_matrix_always_runs(self):
        self.assertEqual(self.steps["guard matrix: rule-usage matrix"]["if"], "${{ !cancelled() }}")

    def test_no_step_is_left_ungated(self):
        self.assertFalse([name for name, step in self.steps.items() if "continue-on-error" in step])


class UnitTestsByModuleTest(unittest.TestCase):
    def setUp(self):
        self.run_text = steps()["trimtab: tests"]["run"]

    def test_fan_out_is_bounded(self):
        self.assertIn("xargs -P4 -n1", self.run_text)

    def test_a_failed_module_fails_the_step(self):
        self.assertIn("|| rc=$?", self.run_text)
        self.assertIn('exit "$rc"', self.run_text)

    def test_what_ran_must_match_what_the_loader_finds(self):
        self.assertIn("countTestCases()", self.run_text)
        self.assertIn('if [[ "$ran" != "$want" ]]', self.run_text)


if __name__ == "__main__":
    unittest.main()
