"""Routine specs render from templates with instance values (stage S2 spec, section 4)."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from instance_fixture import REPO_NAME, env_for, make_instance
from trimtab import roots, routines

TRIMTAB = roots.code_root() / "bin" / "trimtab"
ENV_ID = "placeholder-environment"


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

    def test_the_source_is_the_instances_repository(self):
        spec = routines.render("upstream-retro", routines.values(self.inst, {"environment_id": ENV_ID}))

        sources = spec["job_config"]["ccr"]["session_context"]["sources"]
        self.assertEqual(sources, [{"git_repository": {"url": f"https://github.com/{REPO_NAME}"}}])

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
    """environment-setup.sh finds the one instance clone among the sources, or fails loudly."""

    SCRIPT = roots.code_root() / "routines" / "environment-setup.sh"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, name: str, *, rules: bool = True) -> Path:
        clone = self.home / name
        clone.mkdir()
        if rules:
            (clone / "rules").mkdir()
        stub = clone / "bootstrap.sh"
        stub.write_text(f'#!/usr/bin/env bash\necho "bootstrap {name} $*"\n')
        stub.chmod(0o755)
        return clone

    def run_setup(self) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(self.SCRIPT)], capture_output=True, text=True,
                              env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"})

    def test_it_bootstraps_and_checks_the_one_instance_clone(self):
        self.source("instance")
        self.source("project", rules=False)

        done = self.run_setup()

        self.assertEqual((done.returncode, done.stdout), (0, "bootstrap instance \nbootstrap instance --check\n"))

    def test_no_instance_clone_fails_loudly(self):
        self.source("project", rules=False)

        done = self.run_setup()

        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("no instance clone", done.stderr)

    def test_two_instance_clones_fail_rather_than_guess(self):
        self.source("first")
        self.source("second")

        done = self.run_setup()

        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("more than one instance clone", done.stderr)


if __name__ == "__main__":
    unittest.main()
