"""The private and public scrub checks (stage S2 spec, sections 3 and 6)."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from trimtab import instance as instance_file
from trimtab import scrub

REPO_NAME = "owner/secret-instance"
CONSUMER = "owner/consumer-one"
ROUTINE_ID = "trig_" + "A1b2C3d4E5f6G7h8J9k0"  # built at run time: never a literal ID in a file
DOCTRINE = "PrivateDoctrine.md"
TRIMTAB = Path(__file__).resolve().parents[2] / "bin" / "trimtab"


def make_instance(root: Path) -> Path:
    inst = root / "instance"
    (inst / "rules").mkdir(parents=True)
    (inst / "rules" / DOCTRINE).write_text("---\nid_prefix: PRV\n---\n# 1. x\n", encoding="utf-8")
    (inst / instance_file.FILE).write_text(json.dumps(
        {"schema_version": 1, "repo": REPO_NAME, "base": {"repo": "owner/base", "sha": "0" * 40}}))
    (inst / "consumers.json").write_text(json.dumps({"schema_version": 1, "consumers": [{"repo": CONSUMER}]}))
    (inst / "routines").mkdir()
    (inst / "routines" / "spec.json").write_text(json.dumps({"id": ROUTINE_ID}))
    return inst


class PrivateScrub(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.instance = make_instance(self.root)
        self.tree = self.root / "tree"
        self.tree.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def hits(self, text: str, name: str = "file.md"):
        (self.tree / name).write_text(text, encoding="utf-8")
        terms = scrub.private_terms(self.instance, environ={"HOME": "/home/someone"})
        return scrub.scan(self.tree, terms)

    def test_a_clean_tree_has_no_hits(self):
        self.assertEqual(self.hits("nothing private here\n"), [])

    def test_the_instance_repository_is_found_by_class(self):
        found = self.hits(f"see {REPO_NAME} for details\n")

        self.assertEqual([(h.path, h.line, h.kind) for h in found], [("file.md", 1, "repo")])

    def test_a_consumer_repository_is_found(self):
        self.assertEqual([h.kind for h in self.hits(f"x\n{CONSUMER}\n")], ["repo"])

    def test_a_routine_id_from_the_instance_is_found(self):
        self.assertEqual([h.kind for h in self.hits(f"id {ROUTINE_ID}\n")], ["id"])

    def test_a_doctrine_filename_is_found(self):
        self.assertEqual([h.kind for h in self.hits(f"cited in rules/{DOCTRINE}\n")], ["doctrine-file"])

    def test_the_home_path_is_found(self):
        self.assertEqual([h.kind for h in self.hits("path /home/someone/x\n")], ["home"])

    def test_a_repository_name_in_another_case_is_still_found(self):
        self.assertEqual([h.kind for h in self.hits(f"see {REPO_NAME.upper()}\n")], ["repo"])

    def test_a_hit_never_carries_the_matched_text(self):
        found = self.hits(f"see {REPO_NAME}\n")

        self.assertNotIn(REPO_NAME, repr(found))


class PublicScrub(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def kinds(self, text: str, name: str = "doc.md", self_repo=None):
        target = self.tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        # Only this file's hits: a test that writes two files must not see the first one's.
        return [h.kind for h in scrub.scan(self.tree, scrub.public_terms(self_repo)) if h.path == name]

    def test_an_id_shape_is_found(self):
        self.assertEqual(self.kinds("env_" + "0123456789abcdefABCD" + "\n"), ["id-shape"])

    def test_a_home_path_is_found(self):
        self.assertEqual(self.kinds("/Users/alex/project\n"), ["home-path"])

    def test_a_github_repo_url_is_found_unless_it_is_this_repository(self):
        self.assertEqual(self.kinds("https://github.com/owner/other\n"), ["repo-url"])
        self.assertEqual(self.kinds("https://github.com/owner/base\n", name="b.md", self_repo="owner/base"), [])

    def test_a_repo_field_outside_tests_is_found_but_not_inside_them(self):
        self.assertEqual(self.kinds('{"repo": "owner/x"}\n', name="cfg.json"), ["repo-field"])
        self.assertEqual(self.kinds('{"repo": "owner/x"}\n', name="tests/fixture.json"), [])

    def test_an_id_shape_inside_tests_is_still_found(self):
        self.assertEqual(self.kinds("trig_" + "0123456789abcdefABCD" + "\n", name="tests/t.py"), ["id-shape"])


class PrivateScrubInputs(unittest.TestCase):
    """The deny-list is built as declared, or the check refuses to vouch for the tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.instance = make_instance(self.root)
        self.tree = self.root / "tree"
        self.tree.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_declared_pattern_file_that_cannot_be_read_is_an_error(self):
        with self.assertRaises(scrub.ScrubError):
            scrub.private_terms(self.instance, environ={scrub.PATTERNS_ENV: str(self.root / "missing")})

    def test_a_relative_pattern_path_is_an_error_naming_the_variable_not_the_value(self):
        with self.assertRaises(scrub.ScrubError) as ctx:
            scrub.private_terms(self.instance, environ={scrub.PATTERNS_ENV: "rel.patterns"})

        self.assertIn(scrub.PATTERNS_ENV, str(ctx.exception))
        self.assertNotIn("rel.patterns", str(ctx.exception))

    def test_a_pattern_line_without_a_tab_is_an_error(self):
        patterns = self.root / "private.patterns"
        patterns.write_text("no-tab-here\n", encoding="utf-8")

        with self.assertRaises(scrub.ScrubError):
            scrub.private_terms(self.instance, environ={scrub.PATTERNS_ENV: str(patterns)})

    def test_a_declared_private_pattern_is_found(self):
        patterns = self.root / "private.patterns"
        patterns.write_text("# comment\n\nacme-[0-9]{4}\tacme key\n", encoding="utf-8")
        (self.tree / "f.txt").write_text("key acme-1234\n", encoding="utf-8")

        terms = scrub.private_terms(self.instance, environ={scrub.PATTERNS_ENV: str(patterns)})

        self.assertEqual([h.kind for h in scrub.scan(self.tree, terms)], ["pattern"])

    def test_a_name_inside_a_binary_file_is_still_found(self):
        (self.tree / "blob.bin").write_bytes(b"\xff\xfe\x00" + REPO_NAME.encode() + b"\x00\xff")

        found = scrub.scan(self.tree, scrub.private_terms(self.instance, environ={}))

        self.assertEqual([(h.path, h.kind) for h in found], [("blob.bin", "repo")])


class CommandLine(unittest.TestCase):
    """What runs before every push to the public base: `bin/trimtab scrub`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.instance = make_instance(self.root)
        self.tree = self.root / "tree"
        self.tree.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in ("TRIMTAB_INSTANCE", scrub.PATTERNS_ENV)}

    def tearDown(self):
        self.tmp.cleanup()

    def scrub(self, *args):
        return subprocess.run([str(TRIMTAB), "scrub", "--tree", str(self.tree), *args],
                              capture_output=True, text=True, env=self.env)

    def test_a_planted_hit_exits_1_naming_the_class_but_not_the_text(self):
        (self.tree / "README.md").write_text(f"clone {REPO_NAME}\n", encoding="utf-8")

        done = self.scrub("--instance", str(self.instance))

        self.assertEqual(done.returncode, 1)
        self.assertIn("README.md:1: repo", done.stdout)
        self.assertNotIn(REPO_NAME, done.stdout + done.stderr)

    def test_a_clean_tree_exits_0(self):
        (self.tree / "README.md").write_text("nothing private\n", encoding="utf-8")

        self.assertEqual(self.scrub("--instance", str(self.instance)).returncode, 0)

    def test_the_public_check_needs_no_instance(self):
        (self.tree / "README.md").write_text("nothing private\n", encoding="utf-8")

        self.assertEqual(self.scrub("--public").returncode, 0)

    def test_the_private_check_without_an_instance_exits_2(self):
        self.assertEqual(self.scrub().returncode, 2)

    def test_a_tree_that_does_not_exist_exits_2(self):
        for extra in (("--instance", str(self.instance)), ("--public",)):
            with self.subTest(extra=extra):
                done = subprocess.run([str(TRIMTAB), "scrub", "--tree", str(self.root / "missing"), *extra],
                                      capture_output=True, text=True, env=self.env)

                self.assertEqual(done.returncode, 2)

    def test_a_tree_that_is_a_file_exits_2(self):
        (self.root / "file.txt").write_text("x\n", encoding="utf-8")

        done = subprocess.run([str(TRIMTAB), "scrub", "--tree", str(self.root / "file.txt"), "--public"],
                              capture_output=True, text=True, env=self.env)

        self.assertEqual(done.returncode, 2)

    def test_a_malformed_instance_file_exits_2_rather_than_dropping_the_repository(self):
        (self.instance / instance_file.FILE).write_text(json.dumps({"repo": REPO_NAME}))
        (self.tree / "README.md").write_text(f"clone {REPO_NAME}\n", encoding="utf-8")

        done = self.scrub("--instance", str(self.instance))

        self.assertEqual(done.returncode, 2)
        self.assertNotIn(REPO_NAME, done.stdout + done.stderr)

    def test_an_instance_without_an_instance_file_exits_2(self):
        (self.instance / instance_file.FILE).unlink()

        done = self.scrub("--instance", str(self.instance))

        self.assertEqual(done.returncode, 2)
        self.assertIn(instance_file.FILE, done.stderr)

    def test_a_relative_pattern_path_exits_2_naming_the_variable_but_not_the_value(self):
        # Stage S3 spec, S3-6: relative to the scrub's working directory is not the instance.
        (self.tree / "rel.patterns").write_text("acme-[0-9]{4}\tacme key\n", encoding="utf-8")
        self.env[scrub.PATTERNS_ENV] = "rel.patterns"

        done = subprocess.run([str(TRIMTAB), "scrub", "--tree", str(self.tree), "--instance", str(self.instance)],
                              capture_output=True, text=True, env=self.env, cwd=self.tree)

        self.assertEqual(done.returncode, 2)
        self.assertIn(scrub.PATTERNS_ENV, done.stderr)
        self.assertNotIn("rel.patterns", done.stdout + done.stderr)

    def test_a_malformed_consumers_list_exits_2_not_1(self):
        (self.instance / "consumers.json").write_text("[not json")

        self.assertEqual(self.scrub("--instance", str(self.instance)).returncode, 2)
