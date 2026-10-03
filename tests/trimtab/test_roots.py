"""The package's two roots (stage S2 spec, section 3)."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from instance_fixture import REPO_NAME, env_for, make_instance
from trimtab import config as project_config
from trimtab import instance as instance_file
from trimtab import roots


class InstanceRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.instance = Path(self.tmp.name) / "instance"
        (self.instance / "rules").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_explicit_path_wins_over_the_environment(self):
        other = Path(self.tmp.name) / "other"
        (other / "rules").mkdir(parents=True)

        found = roots.instance_root(str(self.instance), environ={roots.ENV: str(other)})

        self.assertEqual(found, self.instance.resolve())

    def test_environment_is_used_when_no_path_is_given(self):
        found = roots.instance_root(None, environ={roots.ENV: str(self.instance)})

        self.assertEqual(found, self.instance.resolve())

    def test_no_path_and_no_environment_is_a_typed_error_naming_both_ways(self):
        with self.assertRaises(roots.InstanceNotSet) as caught:
            roots.instance_root(None, environ={})

        self.assertIn("--instance", str(caught.exception))
        self.assertIn(roots.ENV, str(caught.exception))

    def test_a_directory_without_rules_is_not_an_instance(self):
        bare = Path(self.tmp.name) / "bare"
        bare.mkdir()

        with self.assertRaises(roots.InstanceInvalid):
            roots.instance_root(str(bare), environ={})

    def test_a_missing_path_is_not_an_instance(self):
        with self.assertRaises(roots.InstanceInvalid):
            roots.instance_root(str(Path(self.tmp.name) / "nope"), environ={})

    def test_code_root_is_this_checkout(self):
        self.assertTrue((roots.code_root() / "trimtab" / "roots.py").is_file())


class NoDoctrineRootInCode(unittest.TestCase):
    def test_no_module_defines_base_root(self):
        package = roots.code_root() / "trimtab"

        offenders = [p.relative_to(package).as_posix() for p in package.rglob("*.py")
                     if "BASE_ROOT" in p.read_text(encoding="utf-8")]

        self.assertEqual(offenders, [])

    def test_no_repository_literal_in_code_commands_or_agents(self):
        top = roots.code_root()
        paths = [*(top / "trimtab").rglob("*.py"), *(top / "commands").glob("*.md"),
                 *(top / "agents").glob("*.md")]

        offenders = [p.relative_to(top).as_posix() for p in paths
                     if "SeaWalsh1980/" in p.read_text(encoding="utf-8")]

        self.assertEqual(offenders, [])


TRIMTAB = roots.code_root() / "bin" / "trimtab"
LOCK = project_config.PATH


class CommandLine(unittest.TestCase):
    def test_without_an_instance_the_cli_exits_2_and_names_both_ways(self):
        env = {k: v for k, v in os.environ.items() if k != roots.ENV}

        done = subprocess.run([str(TRIMTAB), "registry", "--check"], capture_output=True, text=True, env=env)

        self.assertEqual(done.returncode, 2)
        self.assertIn("--instance", done.stderr)
        self.assertIn(roots.ENV, done.stderr)

    def test_instance_root_without_an_instance_exits_2_and_prints_no_root(self):
        # The agents' snippet stops on the exit status and must never read a root from stdout here.
        done = subprocess.run([str(TRIMTAB), "instance", "--root"], capture_output=True, text=True,
                              env=env_for(None))

        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn(roots.ENV, done.stderr)

    def test_instance_root_of_a_directory_without_rules_exits_2_and_prints_no_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([str(TRIMTAB), "instance", "--root"],
                                  capture_output=True, text=True, env=env_for(Path(tmp)))

        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn("not an instance", done.stderr)

    def test_instance_prints_the_root_and_the_repository_from_its_instance_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_instance(Path(tmp))
            env = {k: v for k, v in os.environ.items() if k != roots.ENV}

            done = subprocess.run([str(TRIMTAB), "instance", "--instance", str(inst)],
                                  capture_output=True, text=True, env=env)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), [f"root: {inst}", f"repo: {REPO_NAME}"])

    def test_instance_repo_prints_the_repository_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_instance(Path(tmp))
            env = {k: v for k, v in os.environ.items() if k != roots.ENV}

            done = subprocess.run([str(TRIMTAB), "instance", "--repo", "--instance", str(inst)],
                                  capture_output=True, text=True, env=env)

        self.assertEqual((done.returncode, done.stdout), (0, f"{REPO_NAME}\n"))

    def test_instance_root_prints_the_root_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_instance(Path(tmp))

            done = subprocess.run([str(TRIMTAB), "instance", "--root"],
                                  capture_output=True, text=True, env=env_for(inst))

        self.assertEqual((done.returncode, done.stdout), (0, f"{inst}\n"), done.stderr)

    def test_instance_root_does_not_need_a_readable_lock(self):
        # The agents need only the doctrine; a lock the CLI cannot read must not stop them.
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_instance(Path(tmp))
            (inst / project_config.PATH).write_text("{not json", encoding="utf-8")

            done = subprocess.run([str(TRIMTAB), "instance", "--root"],
                                  capture_output=True, text=True, env=env_for(inst))

        self.assertEqual((done.returncode, done.stdout), (0, f"{inst}\n"), done.stderr)

    def test_registry_will_not_write_to_an_instance_taken_from_the_environment(self):
        # In a worktree the environment names the canonical checkout: a write there would land
        # in the live control plane, not in the branch being edited.
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_instance(Path(tmp))
            env = {**os.environ, roots.ENV: str(inst)}

            done = subprocess.run([str(TRIMTAB), "registry"], capture_output=True, text=True, env=env)

            self.assertEqual(done.returncode, 2)
            self.assertIn("--instance", done.stderr)
            self.assertFalse((inst / "HARNESS.md").exists())

    def test_registry_writes_to_an_instance_named_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_instance(Path(tmp))

            done = subprocess.run([str(TRIMTAB), "registry", "--instance", str(inst)],
                                  capture_output=True, text=True)

            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue((inst / "HARNESS.md").is_file())


class InstanceRepo(unittest.TestCase):
    """Integration at the file boundary: the repository comes from instance.json (stage S3 spec, S3-4)."""

    def test_the_repository_comes_from_the_instance_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_instance(Path(tmp))

            self.assertEqual(roots.instance_repo(inst), REPO_NAME)

    def test_a_malformed_instance_file_is_a_typed_error_not_no_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_instance(Path(tmp))
            (inst / instance_file.FILE).write_text("{not json")

            with self.assertRaises(roots.InstanceInvalid):
                roots.instance_repo(inst)

    def test_an_instance_without_an_instance_file_is_a_typed_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_instance(Path(tmp), instance_json=False)

            with self.assertRaises(roots.InstanceInvalid) as ctx:
                roots.instance_repo(inst)

            self.assertIn(instance_file.FILE, str(ctx.exception))

    def test_the_lock_no_longer_names_the_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_instance(Path(tmp), instance_json=False)
            (inst / LOCK).write_text(json.dumps({"source": REPO_NAME, "trimtab_sha": "0" * 40,
                                                 "id_prefix": "INS", "schema_version": 1}))

            with self.assertRaises(roots.InstanceInvalid):
                roots.instance_repo(inst)
