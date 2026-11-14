"""E22's workflow template: `templates/workflows/plan-gate.yml` (ADR 0063 §7).

A repository-consistency check, as test_plan_scope_workflow is: it reads the shipped template on purpose, because
the template is what it checks. A project adopts it as its own control change, so what it must hold is pinned
here: a broken wiring fails closed in CI, which stays green, so none of these would otherwise be noticed."""

import re
import unittest
from pathlib import Path

import yaml

from test_workflow_pins import requirements_of

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "workflows" / "plan-gate.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
TEXT = TEMPLATE.read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(TEXT)
JOB = WORKFLOW["jobs"]["plan-gate"]
STEPS = JOB["steps"]
FULL_SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def step_named(fragment: str) -> dict:
    found = [s for s in STEPS if fragment in str(s.get("name", ""))]
    assert len(found) == 1, f"{len(found)} steps named like {fragment!r}"
    return found[0]


def commands() -> str:
    return "\n".join(str(s.get("run", "")) for s in STEPS)


class Trigger(unittest.TestCase):
    def test_only_pull_request_events_start_it(self):
        # PyYAML reads the bare key `on` as True.
        self.assertEqual(set(WORKFLOW[True]), {"pull_request"})

    def test_a_ready_mark_starts_it_as_well_as_a_push(self):
        types = WORKFLOW[True]["pull_request"]["types"]

        self.assertEqual(set(types), {"opened", "synchronize", "reopened", "ready_for_review"})

    def test_an_edit_of_a_pr_starts_nothing(self):
        self.assertNotIn("edited", WORKFLOW[True]["pull_request"]["types"])

    def test_the_job_is_named_plan_gate(self):
        self.assertEqual(list(WORKFLOW["jobs"]), ["plan-gate"])
        self.assertEqual(JOB["name"], "plan-gate")

    def test_it_runs_only_for_plan_heads_by_a_job_condition_not_a_trigger_filter(self):
        self.assertIn("startsWith(github.event.pull_request.head.ref, 'plan/')", JOB["if"])
        self.assertNotIn("branches", WORKFLOW[True]["pull_request"])


class Permissions(unittest.TestCase):
    def test_it_reads_the_repository_and_writes_nothing(self):
        perms = WORKFLOW["permissions"]

        self.assertEqual(set(perms.values()), {"read"})

    def test_it_asks_for_no_more_than_the_reads_it_makes(self):
        self.assertEqual(set(WORKFLOW["permissions"]), {"contents", "pull-requests", "actions"})

    def test_no_job_widens_the_permissions(self):
        self.assertNotIn("permissions", JOB)


class Pins(unittest.TestCase):
    def test_every_action_is_pinned_by_full_sha(self):
        uses = [s["uses"] for s in STEPS if "uses" in s]

        self.assertTrue(uses)
        self.assertTrue(all(FULL_SHA.match(u) for u in uses), uses)

    def test_the_actions_are_the_ones_this_repository_pins_in_plan_scope(self):
        scope = yaml.safe_load((ROOT / ".github" / "workflows" / "plan-scope.yml").read_text(encoding="utf-8"))
        pinned = {s["uses"] for s in scope["jobs"]["plan-scope"]["steps"] if "uses" in s}

        self.assertTrue({s["uses"] for s in STEPS if "uses" in s} <= pinned)

    def test_it_installs_the_same_pyyaml_as_ci(self):
        self.assertEqual(requirements_of(TEXT), requirements_of(CI.read_text(encoding="utf-8")))


class WhichCodeRuns(unittest.TestCase):
    def test_the_project_is_checked_out_at_the_prs_base_commit_never_its_head(self):
        first = STEPS[0]

        self.assertTrue(first["uses"].startswith("actions/checkout@"))
        self.assertEqual(first["with"]["ref"], "${{ github.event.pull_request.base.sha }}")
        self.assertEqual(first["with"]["fetch-depth"], 0)

    def test_no_checkout_takes_the_heads_ref_or_sha(self):
        for step in STEPS:
            if "uses" in step and "checkout" in step["uses"]:
                ref = str(step.get("with", {}).get("ref", ""))
                self.assertNotIn("head", ref.lower(), step)
                self.assertNotIn("merge", ref.lower(), step)

    def test_the_code_that_judges_is_the_base_the_lock_pins(self):
        step = step_named("the base the lock pins")

        self.assertEqual(step["with"]["ref"], "${{ steps.lock.outputs.base_sha }}")
        self.assertEqual(step["with"]["repository"], "${{ steps.lock.outputs.base_repo }}")

    def test_the_lock_is_read_from_the_base_commits_tree_and_both_values_are_shape_checked(self):
        run = step_named("which base")["run"]

        self.assertIn("base_sha", run)
        self.assertRegex(run, r"\[0-9a-f\]\{40\}")
        self.assertIn("GITHUB_OUTPUT", run)

    def test_a_pr_not_aimed_at_the_default_branch_is_refused_before_any_base_code_is_chosen(self):
        # Otherwise a PR against a branch of the agent's own carries a lock of its own choosing, the job passes,
        # and a retarget to the default branch (no trigger) keeps the pass.
        step = step_named("which base")

        self.assertEqual(step["env"]["BASE_REF"], "${{ github.event.pull_request.base.ref }}")
        self.assertEqual(step["env"]["DEFAULT_BRANCH"], "${{ github.event.repository.default_branch }}")
        self.assertRegex(step["run"], r'"\$BASE_REF" != "\$DEFAULT_BRANCH"')
        self.assertLess(step["run"].index("BASE_REF"), step["run"].index("python3"))

    def test_the_base_repository_is_held_to_a_plain_owner_and_name(self):
        run = step_named("which base")["run"]
        pattern = re.search(r're\.fullmatch\(r"([^"]+)", repo\)', run)

        self.assertIsNotNone(pattern)
        shape = re.compile(pattern.group(1))
        for bad in ("owner/..", "owner/.", "../name", "./name", "a/b\n"):
            self.assertIsNone(shape.fullmatch(bad), bad)
        for good in ("acme/.github", "acme/core", "a.b/c..d"):
            self.assertIsNotNone(shape.fullmatch(good), good)

    def test_both_commands_run_from_the_pinned_base_checkout(self):
        for line in (l for l in commands().splitlines() if "trimtab" in l and "--server" in l):
            self.assertIn(".trimtab-base/bin/trimtab", line)

    def test_the_project_checkouts_own_bin_is_never_run(self):
        self.assertNotRegex(commands(), r"(?<![\w./-])(\./)?bin/trimtab")


class WhatItRuns(unittest.TestCase):
    def test_it_runs_check_enforcement_server(self):
        self.assertRegex(commands(), r"trimtab check-enforcement --server")

    def test_it_runs_plan_ready_check_server(self):
        self.assertRegex(commands(), r"trimtab plan-ready-check --server")

    def test_a_draft_runs_the_gate_but_not_the_ready_check(self):
        # A failed run on a head the PR later marks ready would void the approver's pass (every attempt counts),
        # so the draft's run judges only the rows that do not wait for a review.
        self.assertIn("pull_request.draft", str(step_named("plan-ready-check").get("if")))
        self.assertNotIn("draft", str(step_named("check-enforcement").get("if", "")))

    def test_the_header_tells_the_approver_a_draft_pass_is_not_enough(self):
        header = TEXT.split("\non:")[0]

        self.assertIn("draft=false", header)
        self.assertIn("base is the default branch", header)
        self.assertIn("vouches for nothing", header)
        self.assertIn("plan-ready-check", header)

    def test_the_gate_runs_before_the_ready_check(self):
        names = [s.get("name", "") for s in STEPS]

        self.assertLess(next(i for i, n in enumerate(names) if "check-enforcement" in n),
                        next(i for i, n in enumerate(names) if "plan-ready-check" in n))

    def test_the_branch_head_must_be_the_one_the_event_names(self):
        self.assertRegex(commands(), r"rev-parse[^\n]*origin/\$HEAD_REF")

    def test_a_head_from_a_fork_is_refused(self):
        self.assertIn("HEAD_REPO", commands())


class Injection(unittest.TestCase):
    def test_no_event_value_is_spliced_into_a_script(self):
        for step in STEPS:
            self.assertNotRegex(str(step.get("run", "")), r"\$\{\{", step)

    def test_the_token_is_given_to_the_two_commands_through_the_environment(self):
        for fragment in ("check-enforcement", "plan-ready-check"):
            env = step_named(fragment)["env"]
            self.assertEqual(env["GH_TOKEN"], "${{ github.token }}")

    def test_it_names_no_secret(self):
        self.assertNotIn("secrets.", TEXT)


if __name__ == "__main__":
    unittest.main()
