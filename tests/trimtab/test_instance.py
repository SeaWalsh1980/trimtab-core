"""instance.json (stage S3 spec, section 2) and the lock at schema 2 (S3-4): parsers tested from values,
then `trimtab instance --json` at the file boundary."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from instance_fixture import REPO_NAME, env_for, make_instance
from trimtab import config as project_config
from trimtab import instance, roots

TRIMTAB = roots.code_root() / "bin" / "trimtab"

SHA = "a" * 40
VALID = {"schema_version": 1, "repo": "owner/inst", "base": {"repo": "owner/base", "sha": SHA}}


def with_(**changes):
    data = {**VALID, **changes}
    return {k: v for k, v in data.items() if v is not ...}


class Parse(unittest.TestCase):
    def refuses(self, data, word):
        with self.assertRaises(instance.InstanceFileError) as ctx:
            instance.parse(data)
        self.assertIn(word, str(ctx.exception))

    def test_a_minimal_file_parses_with_every_pack_and_no_private_file(self):
        f = instance.parse(VALID)
        self.assertEqual((f.repo, f.base_repo, f.base_sha), ("owner/inst", "owner/base", SHA))
        self.assertIsNone(f.secrets_packs)
        self.assertIsNone(f.secrets_patterns)

    def test_a_short_sha_is_refused(self):
        self.refuses(with_(base={"repo": "owner/base", "sha": "a" * 12}), "base.sha")

    def test_a_branch_name_is_refused(self):
        self.refuses(with_(base={"repo": "owner/base", "sha": "main"}), "base.sha")

    def test_an_uppercase_sha_is_refused(self):
        self.refuses(with_(base={"repo": "owner/base", "sha": "A" * 40}), "base.sha")

    def test_a_url_as_base_repo_is_refused(self):
        self.refuses(with_(base={"repo": "https://github.com/owner/base", "sha": SHA}), "base.repo")

    def test_a_url_as_repo_is_refused(self):
        self.refuses(with_(repo="git@github.com:owner/inst.git"), "repo")

    def test_routine_ids_have_no_field(self):
        self.refuses(with_(routines={"upstream_retro": None}), "routines")

    def test_environment_ids_have_no_field(self):
        self.refuses(with_(cloud={"environment_id": None}), "cloud")

    def test_an_unknown_key_is_refused(self):
        self.refuses(with_(extra=1), "extra")

    def test_another_schema_version_is_refused(self):
        self.refuses(with_(schema_version=2), "schema_version")

    def test_a_pack_list_is_kept_in_order(self):
        f = instance.parse(with_(guards={"secrets_packs": ["zoho", "google"]}))
        self.assertEqual(instance.packs_env(f), "zoho,google")

    def test_none_turns_every_pack_off(self):
        self.assertEqual(instance.packs_env(instance.parse(with_(guards={"secrets_packs": "none"}))), "none")

    def test_an_empty_pack_list_is_refused_as_ambiguous(self):
        self.refuses(with_(guards={"secrets_packs": []}), "secrets_packs")

    def test_an_invalid_pack_name_is_refused(self):
        self.refuses(with_(guards={"secrets_packs": ["Bad_Name"]}), "secrets_packs")

    def test_an_absolute_patterns_path_is_refused(self):
        self.refuses(with_(guards={"secrets_patterns": "/etc/x.patterns"}), "secrets_patterns")

    def test_a_home_relative_patterns_path_is_refused(self):
        self.refuses(with_(guards={"secrets_patterns": "~/x.patterns"}), "secrets_patterns")

    def test_a_patterns_path_climbing_out_is_refused(self):
        self.refuses(with_(guards={"secrets_patterns": "guards/../../x.patterns"}), "secrets_patterns")

    def test_an_unknown_guards_key_is_refused(self):
        self.refuses(with_(guards={"other": 1}), "other")

    def test_not_an_object_is_refused(self):
        self.refuses([], "object")


class PatternsPath(unittest.TestCase):
    """Integration at the file boundary: resolves against a temporary instance root."""

    def test_resolves_to_an_absolute_file_inside_the_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "guards").mkdir()
            (root / "guards" / "secrets.patterns").write_text("# none yet\n")
            f = instance.parse(with_(guards={"secrets_patterns": "guards/secrets.patterns"}))
            self.assertEqual(instance.patterns_path(f, root), root / "guards" / "secrets.patterns")

    def test_a_declared_file_that_is_missing_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = instance.parse(with_(guards={"secrets_patterns": "guards/secrets.patterns"}))
            with self.assertRaises(instance.InstanceFileError):
                instance.patterns_path(f, Path(tmp))

    def test_a_symlink_leaving_the_instance_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as out:
            root = Path(tmp)
            (Path(out) / "x.patterns").write_text("# elsewhere\n")
            (root / "guards").mkdir()
            (root / "guards" / "secrets.patterns").symlink_to(Path(out) / "x.patterns")
            f = instance.parse(with_(guards={"secrets_patterns": "guards/secrets.patterns"}))
            with self.assertRaises(instance.InstanceFileError):
                instance.patterns_path(f, root)

# The lock's schema table (S3-4) lives with the format it migrates to.

LOCK = {"trimtab_sha": "b" * 40, "id_prefix": "INS"}


def lock(**fields):
    return json.dumps({**LOCK, **fields})


class LockSchema(unittest.TestCase):
    """Stage S3 spec, S3-4: each row of the table is one test."""

    def test_schema_2_without_bindings_is_accepted(self):
        config, problems = project_config.parse(lock(schema_version=2))
        self.assertEqual(problems, [])
        self.assertEqual(config.warnings, ())

    def test_schema_2_with_source_is_refused_naming_the_key(self):
        _, problems = project_config.parse(lock(schema_version=2, source="owner/x"))
        self.assertEqual([p.code for p in problems], ["bad-config"])
        self.assertIn("source", problems[0].message)

    def test_schema_2_with_routines_is_refused_naming_the_key(self):
        _, problems = project_config.parse(lock(schema_version=2, routines={}))
        self.assertIn("routines", problems[0].message)

    def test_schema_1_is_accepted_with_a_warning_that_bindings_are_ignored(self):
        config, problems = project_config.parse(lock(schema_version=1, source="owner/x", routines={}))
        self.assertEqual(problems, [])
        self.assertTrue(any("source" in w and "ignored" in w for w in config.warnings))

    def test_schema_1_no_longer_needs_source(self):
        config, problems = project_config.parse(lock(schema_version=1))
        self.assertEqual(problems, [])

    def test_a_newer_schema_is_refused(self):
        _, problems = project_config.parse(lock(schema_version=3))
        self.assertIn("newer", problems[0].message)


class CliInstanceJson(unittest.TestCase):
    """Integration at the file boundary: `trimtab instance --json` against a fixture instance.

    Bootstrap consumes exactly this object (stage S3 spec, section 3), so its shape is a contract.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inst = make_instance(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def run_json(self):
        return subprocess.run([str(TRIMTAB), "instance", "--json", "--instance", str(self.inst)],
                              capture_output=True, text=True, env=env_for(None))

    def test_prints_the_validated_values_and_the_env_bootstrap_writes(self):
        done = self.run_json()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {
            "schema_version": 1, "root": str(self.inst), "repo": REPO_NAME,
            "base": {"repo": "owner/base", "sha": "0" * 40},
            "env": {"TRIMTAB_SECRET_PACKS": None,
                    "TRIMTAB_SECRET_PATTERNS": str(self.inst / "guards" / "secrets.patterns")},
        })

    def test_routine_ids_in_the_file_exit_2_naming_the_key(self):
        data = json.loads((self.inst / instance.FILE).read_text())
        (self.inst / instance.FILE).write_text(json.dumps({**data, "routines": {}}))

        done = self.run_json()

        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn("routines", done.stderr)

    def test_a_declared_pattern_file_that_is_missing_exits_2(self):
        (self.inst / "guards" / "secrets.patterns").unlink()

        done = self.run_json()

        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn("secrets_patterns", done.stderr)
