"""Routine specs render from templates with instance values (ADR 0008)."""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from instance_fixture import REPO_NAME, env_for, make_instance
from trimtab import roots, routines
from trimtab.ids import ANY_ID_SHAPE

TRIMTAB = roots.code_root() / "bin" / "trimtab"
ENV_ID = "placeholder-environment"
# The session's own MCP server: its PR subscription asks the operator (a stall
# when unattended) and feeds PR events into the run.
REMOTE_SESSION_SERVER = "mcp__claude-code-remote"
REMOTE_SUBSCRIBE = "mcp__claude-code-remote__subscribe_pr_activity"
# The guard probe: one exact command, and no other refusal stands in for it.
PROBE_CLAUSES = ("`ls ~/.claude/hooks`", "`ls <that path>/hooks`",
                 "refusal of any other command does not satisfy this check",
                 "Quote the exact command and the refusal text")
# What a run writes carries no reference to the run: the platform's default
# attribution adds a session link to commits and PR bodies.
NO_RUN_REFERENCE_CLAUSES = ("`Claude-Session:`", "`claude.ai/code/session_…`",
                            "This overrides any default attribution", "`trimtab verify-body`")


def prompt_of(spec):
    return spec["job_config"]["ccr"]["events"][0]["data"]["message"]["content"]


class Render(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inst = make_instance(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_upstream_retro_spec_renders_with_no_placeholder_left(self):
        spec = routines.render("upstream-retro", routines.values(self.inst, {"environment_id": ENV_ID}))

        text = json.dumps(spec)
        self.assertNotIn("$", text)
        self.assertEqual(spec["job_config"]["ccr"]["environment_id"], ENV_ID)
        self.assertIn(REPO_NAME, text)

    def test_the_spec_stays_disabled_with_no_connectors(self):
        spec = routines.render("upstream-retro", routines.values(self.inst, {"environment_id": ENV_ID}))

        self.assertIs(spec["enabled"], False)
        self.assertEqual(spec["mcp_connections"], [])
        self.assertIn("/trimtab-upstream-retro", spec["job_config"]["ccr"]["events"][0]["data"]["message"]["content"])

    def test_the_session_refuses_the_remote_session_mcp_tools(self):
        spec = routines.render("upstream-retro", routines.values(self.inst, {"environment_id": ENV_ID}))

        refused = spec["job_config"]["ccr"]["session_context"]["disallowed_tools"]
        self.assertTrue({REMOTE_SESSION_SERVER, REMOTE_SUBSCRIBE} <= set(refused))

    def test_the_session_names_no_model_so_it_runs_the_platform_default(self):
        spec = routines.render("upstream-retro", routines.values(self.inst, {"environment_id": ENV_ID}))

        self.assertNotIn("model", spec["job_config"]["ccr"]["session_context"])

    def test_the_guard_probe_requires_the_exact_command_and_no_other(self):
        spec = routines.render("upstream-retro", routines.values(self.inst, {"environment_id": ENV_ID}))

        prompt = prompt_of(spec)
        self.assertEqual([c for c in PROBE_CLAUSES if c not in prompt], [])

    def test_the_prompt_forbids_a_run_reference_in_what_it_writes(self):
        spec = routines.render("upstream-retro", routines.values(self.inst, {"environment_id": ENV_ID}))

        prompt = prompt_of(spec)
        self.assertEqual([c for c in NO_RUN_REFERENCE_CLAUSES if c not in prompt], [])

    def test_the_source_is_the_instances_repository(self):
        spec = routines.render("upstream-retro", routines.values(self.inst, {"environment_id": ENV_ID}))

        sources = spec["job_config"]["ccr"]["session_context"]["sources"]
        self.assertEqual(sources, [{"git_repository": {"url": f"https://github.com/{REPO_NAME}"}}])

    def test_the_name_carries_the_instances_repository(self):
        # The session step finds the routine by this name, not by a stored ID.
        spec = routines.render("upstream-retro", routines.values(self.inst, {"environment_id": "placeholder"}))

        self.assertIn(REPO_NAME, spec["name"])

    def test_the_rendered_spec_holds_no_id_shape(self):
        spec = routines.render("upstream-retro", routines.values(self.inst, {"environment_id": "placeholder"}))

        self.assertIsNone(ANY_ID_SHAPE.search(json.dumps(spec)))

    def test_an_instance_without_an_instance_file_is_an_error_not_a_blank_repository(self):
        inst = make_instance(Path(self.tmp.name) / "bare", instance_json=False)

        with self.assertRaises(roots.InstanceInvalid):
            routines.values(inst, {"environment_id": "placeholder"})

    def test_a_missing_value_is_an_error_naming_it(self):
        with self.assertRaises(routines.RenderError) as caught:
            routines.render("upstream-retro", routines.values(self.inst, {}))

        self.assertIn("environment_id", str(caught.exception))

    def test_a_value_holding_a_quote_stays_one_string(self):
        smuggled = 'x", "enabled": true, "y": "z'

        spec = routines.render("upstream-retro", routines.values(self.inst, {"environment_id": smuggled}))

        self.assertIs(spec["enabled"], False)
        self.assertEqual(spec["job_config"]["ccr"]["environment_id"], smuggled)

    def test_a_value_named_prompt_json_is_refused(self):
        given = routines.values(self.inst, {"environment_id": ENV_ID, "prompt_json": '"x"'})

        with self.assertRaises(routines.RenderError) as caught:
            routines.render("upstream-retro", given)

        self.assertIn("prompt_json", str(caught.exception))

    def test_an_unknown_routine_is_an_error(self):
        with self.assertRaises(routines.RenderError):
            routines.render("no-such-routine", {})

    def test_a_routine_name_cannot_leave_the_templates_directory(self):
        with self.assertRaises(routines.RenderError):
            routines.render("../templates/upstream-retro", {"environment_id": ENV_ID, "instance_repo": REPO_NAME})


PROJECT_REPO = "owner/project"


class PerProjectTemplate:
    """Shared cases for a routine bound to one project: rendered with --set project_repo."""

    ROUTINE = ""
    COMMAND = ""
    NOUN = ""
    CRON = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inst = make_instance(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def rendered(self, **overrides: str) -> dict:
        given = {"environment_id": ENV_ID, "project_repo": PROJECT_REPO, **overrides}
        return routines.render(self.ROUTINE, routines.values(self.inst, given))

    def test_it_renders_with_no_placeholder_left(self):
        spec = self.rendered()

        text = json.dumps(spec)
        self.assertNotIn("$", text)
        self.assertEqual(spec["job_config"]["ccr"]["environment_id"], ENV_ID)

    def test_it_stays_disabled_with_no_connectors(self):
        spec = self.rendered()

        self.assertIs(spec["enabled"], False)
        self.assertEqual(spec["mcp_connections"], [])

    def test_its_prompt_runs_the_command(self):
        spec = self.rendered()

        self.assertIn(self.COMMAND, spec["job_config"]["ccr"]["events"][0]["data"]["message"]["content"])

    def test_its_sources_are_the_instance_then_the_project(self):
        spec = self.rendered()

        sources = spec["job_config"]["ccr"]["session_context"]["sources"]
        self.assertEqual(sources, [{"git_repository": {"url": f"https://github.com/{REPO_NAME}"}},
                                   {"git_repository": {"url": f"https://github.com/{PROJECT_REPO}"}}])

    def test_its_name_carries_the_project_repository(self):
        # The session step finds the routine by this name, not by a stored ID.
        spec = self.rendered()

        self.assertEqual(spec["name"], f"Trimtab {self.NOUN} · {PROJECT_REPO}")

    def test_it_runs_on_its_own_schedule(self):
        spec = self.rendered()

        self.assertEqual(spec["cron_expression"], self.CRON)

    def test_it_runs_with_the_same_tools_and_model_as_the_upstream_retro(self):
        upstream = routines.render("upstream-retro", routines.values(self.inst, {"environment_id": ENV_ID}))

        spec = self.rendered()

        context, expected = spec["job_config"]["ccr"]["session_context"], upstream["job_config"]["ccr"]["session_context"]
        keys = ("allowed_tools", "disallowed_tools", "model")
        self.assertEqual([context.get(k) for k in keys], [expected.get(k) for k in keys])

    def test_the_session_refuses_the_remote_session_mcp_tools(self):
        spec = self.rendered()

        refused = spec["job_config"]["ccr"]["session_context"]["disallowed_tools"]
        self.assertTrue({REMOTE_SESSION_SERVER, REMOTE_SUBSCRIBE} <= set(refused))

    def test_the_guard_probe_requires_the_exact_command_and_no_other(self):
        spec = self.rendered()

        prompt = prompt_of(spec)
        self.assertEqual([c for c in PROBE_CLAUSES if c not in prompt], [])

    def test_the_prompt_forbids_a_run_reference_in_what_it_writes(self):
        spec = self.rendered()

        prompt = prompt_of(spec)
        self.assertEqual([c for c in NO_RUN_REFERENCE_CLAUSES if c not in prompt], [])

    def test_the_rendered_spec_holds_no_id_shape(self):
        spec = self.rendered()

        self.assertIsNone(ANY_ID_SHAPE.search(json.dumps(spec)))

    def test_a_missing_project_repository_is_an_error_naming_it(self):
        with self.assertRaises(routines.RenderError) as caught:
            routines.render(self.ROUTINE, routines.values(self.inst, {"environment_id": ENV_ID}))

        self.assertIn("project_repo", str(caught.exception))

    def test_a_missing_environment_is_an_error_naming_it(self):
        with self.assertRaises(routines.RenderError) as caught:
            routines.render(self.ROUTINE, routines.values(self.inst, {"project_repo": PROJECT_REPO}))

        self.assertIn("environment_id", str(caught.exception))

    def test_a_value_holding_a_quote_stays_one_string(self):
        smuggled = 'x", "enabled": true, "y": "z'

        spec = self.rendered(project_repo=smuggled)

        self.assertIs(spec["enabled"], False)
        self.assertEqual(spec["name"], f"Trimtab {self.NOUN} · {smuggled}")


class ConformanceTemplate(PerProjectTemplate, unittest.TestCase):
    ROUTINE = "conformance"
    COMMAND = "/trimtab-conformance"
    NOUN = "conformance"
    CRON = "0 4 * * 4"


class RetroTemplate(PerProjectTemplate, unittest.TestCase):
    ROUTINE = "retro"
    COMMAND = "/trimtab-retro"
    NOUN = "retro"
    CRON = "0 7 * * 6"


class Values(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inst = make_instance(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_repository_comes_from_the_instances_lock(self):
        found = routines.values(self.inst, {})

        self.assertEqual(found, {"instance_repo": REPO_NAME})

    def test_a_value_passed_in_wins_over_the_instances(self):
        found = routines.values(self.inst, {"instance_repo": "owner/other"})

        self.assertEqual(found["instance_repo"], "owner/other")


class RenderCommand(unittest.TestCase):
    """`trimtab routine render` prints a spec and writes nothing: read-only by default."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inst = make_instance(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def run_render(self, *args: str, instance: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([str(TRIMTAB), "routine", "render", "upstream-retro", *args],
                              capture_output=True, text=True, env=env_for(instance), cwd=self.tmp.name)

    def test_it_prints_the_rendered_spec(self):
        done = self.run_render("--set", f"environment_id={ENV_ID}", instance=self.inst)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["job_config"]["ccr"]["environment_id"], ENV_ID)

    def test_it_writes_nothing(self):
        before = sorted(p.relative_to(self.tmp.name) for p in Path(self.tmp.name).rglob("*"))

        self.run_render("--set", f"environment_id={ENV_ID}", instance=self.inst)

        self.assertEqual(sorted(p.relative_to(self.tmp.name) for p in Path(self.tmp.name).rglob("*")), before)

    def test_a_missing_value_exits_2_naming_it(self):
        done = self.run_render(instance=self.inst)

        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn("environment_id", done.stderr)

    def test_a_set_without_an_equals_sign_exits_2(self):
        done = self.run_render("--set", "environment_id", instance=self.inst)

        self.assertEqual(done.returncode, 2)
        self.assertIn("KEY=VALUE", done.stderr)

    def test_no_instance_exits_2(self):
        done = self.run_render("--set", f"environment_id={ENV_ID}", instance=None)

        self.assertEqual(done.returncode, 2)
        self.assertIn(roots.ENV, done.stderr)


class SetupScript(unittest.TestCase):
    """environment-setup.sh finds the one instance clone among the sources, or fails loudly.

    The sources are looked for under $HOME and beside the directory the script
    starts in: the cloud starts it beside the clone, with $HOME elsewhere.
    """

    SCRIPT = roots.code_root() / "routines" / "environment-setup.sh"
    BOOTSTRAPPED = "bootstrap instance --no-timer\nbootstrap instance --check --no-timer\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.start = root / "start"
        self.home.mkdir()
        self.start.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, name: str, *, rules: bool = True, under: Path | None = None) -> Path:
        clone = (under or self.home) / name
        clone.mkdir()
        if rules:
            (clone / "rules").mkdir()
        stub = clone / "bootstrap.sh"
        stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "${{BASH_SOURCE%/*}}/calls"\n'
                        f'echo "bootstrap {name} $*"\n')
        stub.chmod(0o755)
        return clone

    def run_setup(self, *, home: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(self.SCRIPT)], capture_output=True, text=True,
                              cwd=self.start,
                              env={"HOME": str(home or self.home), "PATH": "/usr/bin:/bin"})

    def test_it_bootstraps_and_checks_the_one_instance_clone(self):
        self.source("instance")
        self.source("project", rules=False)

        done = self.run_setup()

        self.assertEqual((done.returncode, done.stdout), (0, self.BOOTSTRAPPED))

    def test_a_clone_beside_the_starting_directory_is_found_when_home_holds_none(self):
        self.source("instance", under=self.start)
        self.source("project", rules=False)

        done = self.run_setup()

        self.assertEqual((done.returncode, done.stdout), (0, self.BOOTSTRAPPED))

    def test_one_clone_reached_from_home_and_the_starting_directory_counts_once(self):
        self.source("instance", under=self.start)

        done = self.run_setup(home=self.start)

        self.assertEqual((done.returncode, done.stdout), (0, self.BOOTSTRAPPED))

    def test_one_clone_linked_into_home_counts_once(self):
        clone = self.source("instance", under=self.start)
        (self.home / "linked").symlink_to(clone)

        done = self.run_setup()

        self.assertEqual((done.returncode, done.stdout), (0, self.BOOTSTRAPPED))

    def test_both_bootstrap_runs_skip_the_weekly_report_timer(self):
        clone = self.source("instance")

        self.run_setup()

        self.assertEqual((clone / "calls").read_text().splitlines(), ["--no-timer", "--check --no-timer"])

    def test_a_project_clone_beside_the_instance_clone_is_not_taken_for_one(self):
        # A per-project routine clones the instance and the project; the
        # project holds path-scoped rules but neither the shim nor a root rules/.
        self.source("instance", under=self.start)
        (self.start / "project" / ".claude" / "rules").mkdir(parents=True)

        done = self.run_setup()

        self.assertEqual((done.returncode, done.stdout), (0, self.BOOTSTRAPPED))

    def test_no_instance_clone_fails_loudly(self):
        self.source("project", rules=False)
        self.source("other", rules=False, under=self.start)

        done = self.run_setup()

        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("no instance clone", done.stderr)

    def test_two_instance_clones_fail_rather_than_guess(self):
        self.source("first")
        self.source("second")

        done = self.run_setup()

        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("more than one instance clone", done.stderr)

    def test_a_clone_under_home_and_another_beside_the_starting_directory_fail_rather_than_guess(self):
        self.source("first")
        self.source("second", under=self.start)

        done = self.run_setup()

        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("more than one instance clone", done.stderr)


if __name__ == "__main__":
    unittest.main()
