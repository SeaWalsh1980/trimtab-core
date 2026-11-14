"""E22's lock field: the rows `--server` reads, derived by `trimtab bump` and nowhere else (ADR 0063 §2, §7)."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from enforcement_fixture import OWNERS, record_text
from trimtab import bump
from trimtab import config as project_config
from trimtab import instance as instance_file
from trimtab.enforcement import gate
from trimtab.enforcement import record as record_mod

BASE_SHA = "b" * 40
REPOSITORY = "owner/project"
ADR_DIR = "docs/" + "adr/"  # in two pieces: the citation test reads a whole path as a citation
RECORD_PATH = ADR_DIR + "0001-the-record.md"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def instance_json(jobs=None, record=RECORD_PATH):
    body = {"schema_version": 1, "repo": "owner/inst", "base": {"repo": "owner/base", "sha": BASE_SHA}}
    if record:
        body["enforcement"] = {"record": record, "projects": {REPOSITORY: jobs if jobs is not None else {
            "E1": {"workflow": ".github/workflows/review.yml", "job": "review"}}}}
    return json.dumps(body)


def derived_rows(text=None):
    return {r.id: {"owner": r.owner, "sha256": r.sha256} for r in record_mod.parse(text or record_text())}


def good_field():
    return {"base_repo": "owner/base", "rows": derived_rows(),
            "jobs": {"E1": {"workflow": ".github/workflows/review.yml", "job": "review"}}, "instance": {}}


def lock_text(field=None, base_sha=BASE_SHA):
    body = {"trimtab_sha": "a" * 40, "id_prefix": "SCR", "schema_version": 2, "base_sha": base_sha}
    if field is not None:
        body["enforcement"] = field
    return json.dumps(body)


class LockFieldReader(unittest.TestCase):
    def test_a_lock_without_the_field_reads_as_none(self):
        found, problems = project_config.parse(lock_text())

        self.assertEqual((problems, found.enforcement), ([], None))

    def test_a_derived_field_reads_back_whole(self):
        found, problems = project_config.parse(lock_text(good_field()))

        self.assertEqual(problems, [])
        self.assertEqual(found.enforcement, good_field())

    def test_a_field_without_a_base_sha_is_refused(self):
        found, problems = project_config.parse(lock_text(good_field(), base_sha=None))

        self.assertIsNone(found)
        self.assertIn("enforcement", problems[0].message)

    def test_a_missing_row_is_refused(self):
        field = good_field()
        del field["rows"]["E22"]

        found, problems = project_config.parse(lock_text(field))

        self.assertIsNone(found)
        self.assertIn("E22", problems[0].message)

    def test_an_extra_row_is_refused(self):
        field = good_field()
        field["rows"]["E24"] = field["rows"]["E1"]

        found, _ = project_config.parse(lock_text(field))

        self.assertIsNone(found)

    def test_an_unknown_owner_is_refused(self):
        field = good_field()
        field["rows"]["E5"]["owner"] = "somebody"

        found, _ = project_config.parse(lock_text(field))

        self.assertIsNone(found)

    def test_a_short_row_digest_is_refused(self):
        field = good_field()
        field["rows"]["E5"]["sha256"] = "abc"

        found, _ = project_config.parse(lock_text(field))

        self.assertIsNone(found)

    def test_a_job_for_no_row_is_refused(self):
        field = good_field()
        field["jobs"]["E99"] = {"workflow": ".github/workflows/x.yml", "job": "x"}

        found, _ = project_config.parse(lock_text(field))

        self.assertIsNone(found)

    def test_a_workflow_outside_the_workflows_directory_is_refused(self):
        field = good_field()
        field["jobs"]["E1"]["workflow"] = "../elsewhere.yml"

        found, _ = project_config.parse(lock_text(field))

        self.assertIsNone(found)

    def test_an_unknown_key_in_the_field_is_refused(self):
        field = good_field()
        field["extra"] = 1

        found, _ = project_config.parse(lock_text(field))

        self.assertIsNone(found)

    def test_the_plan_keys_the_instance_reader_accepts_are_kept(self):
        field = good_field()
        field["instance"] = {"accounts": {"admin": "the-admin", "agent": "the-agent"},
                             "secret_scan": {"workflow": ".github/workflows/scan.yml", "job": "scan"},
                             "fix_limits": {"markers": ["pytest.skip("]}}

        found, problems = project_config.parse(lock_text(field))

        self.assertEqual((problems, found.enforcement["instance"]), ([], field["instance"]))

    def test_a_key_the_plan_rows_do_not_read_is_refused(self):
        field = good_field()
        field["instance"] = {"guards": {}}

        found, _ = project_config.parse(lock_text(field))

        self.assertIsNone(found)

    def test_a_plan_key_the_instance_reader_refuses_is_refused(self):
        field = good_field()
        field["instance"] = {"secret_scan": {"workflow": "scan.yml", "job": "scan"}}

        found, problems = project_config.parse(lock_text(field))

        self.assertIsNone(found)
        self.assertIn("instance", problems[0].message)

    def test_a_field_with_no_instance_object_is_refused(self):
        field = good_field()
        del field["instance"]

        found, _ = project_config.parse(lock_text(field))

        self.assertIsNone(found)

    def test_a_bad_base_repository_is_refused(self):
        field = good_field()
        field["base_repo"] = "not a repo"

        found, _ = project_config.parse(lock_text(field))

        self.assertIsNone(found)


class Derivation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.base, self.project = tmp / "base", tmp / "project"
        (self.base / "rules").mkdir(parents=True)
        (self.base / "rules" / "Example.md").write_text("---\nid_prefix: TST\n---\n\n## 1. A\n\n- **PREFER** x.\n")
        (self.base / "docs" / "adr").mkdir(parents=True)
        (self.base / instance_file.FILE).write_text(instance_json())
        (self.base / RECORD_PATH).write_text(record_text())
        git(self.base, "init", "-q")
        git(self.base, "add", "-A")
        git(self.base, "commit", "-qm", "v1")
        self.v1 = git(self.base, "rev-parse", "HEAD")
        (self.project / ".claude").mkdir(parents=True)
        self.lock = self.project / ".claude" / "trimtab.json"
        self.lock.write_text('{\n  "trimtab_sha": "%s",\n  "id_prefix": "SCR",\n  "schema_version": 2\n}\n' % self.v1)

    def tearDown(self):
        self._tmp.cleanup()

    def commit_new(self, record=None, jobs=None):
        # The record counts as of the commit that ADDED it, so a changed record is a new file the instance names.
        path = RECORD_PATH
        if record is not None:
            path = ADR_DIR + "0002-the-next-record.md"
            (self.base / path).write_text(record)
        (self.base / instance_file.FILE).write_text(instance_json(jobs, record=path))
        (self.base / "rules" / "Example.md").write_text("---\nid_prefix: TST\n---\n\n## 1. A\n\n- **PREFER** y.\n")
        git(self.base, "add", "-A")
        git(self.base, "commit", "-qm", "v2")
        return git(self.base, "rev-parse", "HEAD")

    def test_the_field_holds_each_rows_owner_and_digest_from_the_record_as_merged(self):
        field = gate.derive_lock_enforcement(self.base, self.v1, REPOSITORY)

        self.assertEqual(field["rows"], derived_rows())

    def test_the_field_holds_this_projects_jobs_and_the_base_repository(self):
        field = gate.derive_lock_enforcement(self.base, self.v1, REPOSITORY)

        self.assertEqual(field["jobs"], {"E1": {"workflow": ".github/workflows/review.yml", "job": "review"}})
        self.assertEqual(field["base_repo"], "owner/base")

    def test_the_field_carries_the_instance_keys_the_plan_rows_read_and_no_others(self):
        body = json.loads(instance_json())
        body["secret_scan"] = {"workflow": ".github/workflows/scan.yml", "job": "scan"}
        body["guards"] = {"secrets_packs": "none"}
        (self.base / instance_file.FILE).write_text(json.dumps(body))
        git(self.base, "commit", "-qam", "scan")
        sha = git(self.base, "rev-parse", "HEAD")

        field = gate.derive_lock_enforcement(self.base, sha, REPOSITORY)

        self.assertEqual(field["instance"], {"secret_scan": body["secret_scan"]})

    def test_a_repository_the_instance_does_not_name_gets_no_jobs(self):
        field = gate.derive_lock_enforcement(self.base, self.v1, "owner/other")

        self.assertEqual(field["jobs"], {})

    def test_the_project_name_compares_without_case(self):
        field = gate.derive_lock_enforcement(self.base, self.v1, "OWNER/Project")

        self.assertEqual(list(field["jobs"]), ["E1"])

    def test_an_instance_with_no_record_derives_nothing(self):
        (self.base / instance_file.FILE).write_text(instance_json(record=None))
        git(self.base, "commit", "-qam", "no record")
        sha = git(self.base, "rev-parse", "HEAD")

        self.assertIsNone(gate.derive_lock_enforcement(self.base, sha, REPOSITORY))

    def test_a_record_that_does_not_parse_fails_loudly(self):
        sha = self.commit_new(record="# nothing\n")

        with self.assertRaises(gate.GateError):
            gate.derive_lock_enforcement(self.base, sha, REPOSITORY)

    def test_the_plan_carries_the_derived_field(self):
        todo = bump.plan(self.project, self.base, self.v1, REPOSITORY)

        self.assertEqual(todo.enforcement_new["rows"], derived_rows())

    def test_apply_writes_a_lock_whose_field_equals_the_derivation(self):
        new = self.commit_new(record=record_text().replace("condition E5", "a changed condition E5"))
        todo = bump.plan(self.project, self.base, new, REPOSITORY)

        bump.apply(self.project, todo, bump.confirmation(todo))

        written, problems = project_config.load(self.project)
        self.assertEqual(problems, [])
        self.assertEqual(written.enforcement, todo.enforcement_new)
        self.assertNotEqual(written.enforcement["rows"]["E5"], derived_rows()["E5"])

    def test_apply_keeps_the_lock_formatting_and_replaces_an_older_field_in_place(self):
        first = bump.plan(self.project, self.base, self.v1, REPOSITORY)
        bump.apply(self.project, first, bump.confirmation(first))
        new = self.commit_new(record=record_text().replace("condition E6", "changed E6"))
        second = bump.plan(self.project, self.base, new, REPOSITORY)

        bump.apply(self.project, second, bump.confirmation(second))

        text = self.lock.read_text()
        self.assertEqual(text.count('"enforcement"'), 1)
        self.assertTrue(text.startswith('{\n  "trimtab_sha": "%s",' % new))
        self.assertEqual(project_config.load(self.project)[0].enforcement, second.enforcement_new)

    def test_a_lock_that_has_the_old_field_but_the_same_shas_still_has_something_to_bump(self):
        first = bump.plan(self.project, self.base, self.v1, REPOSITORY)
        bump.apply(self.project, first, bump.confirmation(first))
        self.assertTrue(bump.plan(self.project, self.base, self.v1, REPOSITORY).is_current)
        hand = json.loads(self.lock.read_text())
        hand["enforcement"]["rows"]["E5"]["sha256"] = "0" * 64
        self.lock.write_text(json.dumps(hand, indent=2) + "\n")

        self.assertFalse(bump.plan(self.project, self.base, self.v1, REPOSITORY).is_current)

    def test_a_current_lock_with_no_field_has_something_to_bump(self):
        self.assertFalse(bump.plan(self.project, self.base, self.v1, REPOSITORY).is_current)

    def test_the_confirmation_changes_when_the_derived_field_does(self):
        before = bump.plan(self.project, self.base, self.v1, REPOSITORY)
        new = self.commit_new(record=record_text().replace("condition E7", "changed E7"))
        after = bump.plan(self.project, self.base, new, REPOSITORY)

        self.assertNotEqual(bump.confirmation(before), bump.confirmation(after))

    def test_a_record_instance_with_no_repository_given_refuses_the_plan(self):
        with self.assertRaises(bump.BumpError):
            bump.plan(self.project, self.base, self.v1)

    def test_an_instance_with_no_record_plans_as_before_and_writes_no_field(self):
        (self.base / instance_file.FILE).write_text(instance_json(record=None))
        git(self.base, "commit", "-qam", "no record")
        new = git(self.base, "rev-parse", "HEAD")
        todo = bump.plan(self.project, self.base, new)

        bump.apply(self.project, todo, bump.confirmation(todo))

        self.assertNotIn("enforcement", self.lock.read_text())

    def test_apply_refuses_when_the_field_moved_since_the_dry_run(self):
        todo = bump.plan(self.project, self.base, self.v1, REPOSITORY)
        self.lock.write_text(self.lock.read_text().rstrip().rstrip("}").rstrip()
                             + ',\n  "enforcement": %s\n}\n' % json.dumps(good_field()))

        with self.assertRaises(bump.BumpError):
            bump.apply(self.project, todo, bump.confirmation(todo))

    def test_the_owners_the_fixture_uses_are_ones_the_record_reader_knows(self):
        self.assertTrue(set(OWNERS.values()) <= set(record_mod.OWNERS))


if __name__ == "__main__":
    unittest.main()
