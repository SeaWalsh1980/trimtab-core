"""The private and public scrub checks."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trimtab import instance as instance_file
from trimtab import scrub

REPO_NAME = "owner/secret-instance"
CONSUMER = "owner/consumer-one"
ROUTINE_ID = "trig_" + "A1b2C3d4E5f6G7h8J9k0"  # built at run time: never a literal ID in a file
DOCTRINE = "PrivateDoctrine.md"
TRIMTAB = Path(__file__).resolve().parents[2] / "bin" / "trimtab"
PUBLIC_ADR = "0001-a-public-decision.md"
INSTANCE_EMAIL = "operator@example.invalid"
HOOK_EMAIL = "hook@example.invalid"


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


def make_base(root: Path) -> Path:
    """A stand-in for the base checkout, so no test reads this repository's own docs/adr."""
    base = root / "base"
    (base / "docs" / "adr").mkdir(parents=True)
    (base / "docs" / "adr" / PUBLIC_ADR).write_text("# x\n", encoding="utf-8")
    return base


# Set by git while it runs a hook: inherited, they would point every git call in a test at
# the hook's own repository, for reads and for writes.
GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                     "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def isolated_git_env(environ, root: Path) -> dict:
    """environ, with git kept inside root and away from the machine's own config.

    private_terms reads the instance's commit email with `git config`. A fixture instance is
    not a repository, so git would otherwise fall back to the global config of whoever runs
    the tests, or find a repository above the temporary directory.
    """
    env = {k: v for k, v in environ.items() if k not in GIT_LOCATION_VARS}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_CEILING_DIRECTORIES=str(root.resolve()))
    return env


def isolate_git(case: unittest.TestCase, root: Path) -> None:
    patcher = mock.patch.dict(os.environ, isolated_git_env(os.environ, root), clear=True)
    patcher.start()
    case.addCleanup(patcher.stop)


class PrivateScrub(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        isolate_git(self, self.root)
        self.instance = make_instance(self.root)
        self.code_root = make_base(self.root)
        self.tree = self.root / "tree"
        self.tree.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def hits(self, text: str, name: str = "file.md"):
        (self.tree / name).write_text(text, encoding="utf-8")
        terms = scrub.private_terms(self.instance, environ={"HOME": "/home/someone"},
                                    code_root=self.code_root)
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

    def test_the_machines_own_git_identity_is_not_a_term(self):
        terms = scrub.private_terms(self.instance, environ={}, code_root=self.code_root)

        self.assertNotIn("email", terms.literals)

    def test_the_instances_commit_email_is_found(self):
        subprocess.run(["git", "init", "-q", str(self.instance)], check=True)
        subprocess.run(["git", "-C", str(self.instance), "config", "user.email", INSTANCE_EMAIL], check=True)

        self.assertEqual([h.kind for h in self.hits(f"by {INSTANCE_EMAIL}\n")], ["email"])

    def test_a_git_dir_inherited_from_a_hook_is_neither_read_nor_written(self):
        hook_repo = self.root / "hook-repo"
        subprocess.run(["git", "init", "-q", str(hook_repo)], check=True)
        subprocess.run(["git", "-C", str(hook_repo), "config", "user.email", HOOK_EMAIL], check=True)
        # As git exports it to a hook, then isolated again as setUp would.
        hook = mock.patch.dict(os.environ, {"GIT_DIR": str(hook_repo / ".git")})
        hook.start()
        self.addCleanup(hook.stop)
        isolate_git(self, self.root)

        subprocess.run(["git", "init", "-q", str(self.instance)], check=True)
        subprocess.run(["git", "-C", str(self.instance), "config", "user.email", INSTANCE_EMAIL], check=True)

        self.assertEqual([h.kind for h in self.hits(f"by {INSTANCE_EMAIL}\n")], ["email"])
        written = subprocess.run(["git", "-C", str(hook_repo), "config", "user.email"],
                                 capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(written, HOOK_EMAIL)

    def add_doc(self, folder: str, name: str):
        (self.instance / "docs" / folder).mkdir(parents=True, exist_ok=True)
        (self.instance / "docs" / folder / name).write_text("# x\n", encoding="utf-8")

    def test_a_numbered_instance_adr_file_name_is_found(self):
        self.add_doc("adr", "0004-private-decision.md")

        self.assertEqual([h.kind for h in self.hits("see 0004-private-decision.md\n")], ["private-doc"])

    def test_a_numbered_instance_plan_file_name_is_found(self):
        self.add_doc("plans", "0013-private-split.md")

        self.assertEqual([h.kind for h in self.hits("see 0013-private-split.md, section 3\n")], ["private-doc"])

    def test_an_unnumbered_instance_doc_name_is_not_a_term(self):
        self.add_doc("adr", "README.md")

        self.assertEqual(self.hits("see README.md\n"), [])

    def test_a_name_the_base_series_also_carries_is_not_a_term(self):
        self.add_doc("adr", PUBLIC_ADR)

        self.assertEqual(self.hits(f"see {PUBLIC_ADR}\n"), [])

    def test_an_instance_used_as_the_code_root_is_refused(self):
        # Subtracting the code root's series would then subtract the instance's own records.
        self.add_doc("adr", "0004-private-decision.md")

        with self.assertRaises(scrub.ScrubError):
            scrub.private_terms(self.instance, environ={}, code_root=self.instance)

    def use_base(self, base_repo: str):
        data = json.loads((self.instance / instance_file.FILE).read_text())
        data["base"]["repo"] = base_repo
        (self.instance / instance_file.FILE).write_text(json.dumps(data))

    def test_the_bases_own_name_is_not_a_hit_when_a_private_name_prefixes_it(self):
        self.use_base(f"{REPO_NAME}-core")

        self.assertEqual(self.hits(f"see {REPO_NAME}-core\n"), [])

    def test_the_private_name_beside_the_bases_own_is_still_found(self):
        self.use_base(f"{REPO_NAME}-core")

        self.assertEqual([h.kind for h in self.hits(f"{REPO_NAME}-core forks {REPO_NAME}\n")], ["repo"])

    def test_blanking_the_bases_name_never_hides_a_home_path(self):
        # An owner named like the user, with the base cloned into the home directory.
        self.use_base("someone/x-core")

        self.assertEqual([h.kind for h in self.hits("at /home/someone/x-core/hooks/f.sh\n")], ["home"])

    def test_a_private_name_that_starts_with_the_bases_own_is_still_found(self):
        self.use_base("owner/consumer")

        self.assertEqual([h.kind for h in self.hits(f"see {CONSUMER}\n")], ["repo"])

    def test_a_longer_name_that_starts_with_a_private_one_is_found(self):
        self.assertEqual([h.kind for h in self.hits(f"see {REPO_NAME}-api and {REPO_NAME}_old\n")], ["repo"])

    def test_a_repository_name_ending_a_sentence_is_found(self):
        self.assertEqual([h.kind for h in self.hits(f"cloned from {REPO_NAME}.\n")], ["repo"])

    def test_a_repository_name_in_a_url_is_found(self):
        self.assertEqual([h.kind for h in self.hits(f"https://github.com/{REPO_NAME}/pull/1\n")], ["repo"])

    def test_a_repository_name_in_a_clone_url_is_found(self):
        self.assertEqual([h.kind for h in self.hits(f"git clone git@github.com:{REPO_NAME}.git\n")], ["repo"])


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
        isolate_git(self, self.root)
        self.instance = make_instance(self.root)
        self.code_root = make_base(self.root)
        self.tree = self.root / "tree"
        self.tree.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_declared_pattern_file_that_cannot_be_read_is_an_error(self):
        with self.assertRaises(scrub.ScrubError):
            scrub.private_terms(self.instance, environ={scrub.PATTERNS_ENV: str(self.root / "missing")},
                                code_root=self.code_root)

    def test_a_relative_pattern_path_is_an_error_naming_the_variable_not_the_value(self):
        with self.assertRaises(scrub.ScrubError) as ctx:
            scrub.private_terms(self.instance, environ={scrub.PATTERNS_ENV: "rel.patterns"},
                                code_root=self.code_root)

        self.assertIn(scrub.PATTERNS_ENV, str(ctx.exception))
        self.assertNotIn("rel.patterns", str(ctx.exception))

    def test_a_pattern_line_without_a_tab_is_an_error(self):
        patterns = self.root / "private.patterns"
        patterns.write_text("no-tab-here\n", encoding="utf-8")

        with self.assertRaises(scrub.ScrubError):
            scrub.private_terms(self.instance, environ={scrub.PATTERNS_ENV: str(patterns)},
                                code_root=self.code_root)

    def test_a_declared_private_pattern_is_found(self):
        patterns = self.root / "private.patterns"
        patterns.write_text("# comment\n\nacme-[0-9]{4}\tacme key\n", encoding="utf-8")
        (self.tree / "f.txt").write_text("key acme-1234\n", encoding="utf-8")

        terms = scrub.private_terms(self.instance, environ={scrub.PATTERNS_ENV: str(patterns)},
                                    code_root=self.code_root)

        self.assertEqual([h.kind for h in scrub.scan(self.tree, terms)], ["pattern"])

    def test_a_name_inside_a_binary_file_is_still_found(self):
        (self.tree / "blob.bin").write_bytes(b"\xff\xfe\x00" + REPO_NAME.encode() + b"\x00\xff")

        found = scrub.scan(self.tree, scrub.private_terms(self.instance, environ={}, code_root=self.code_root))

        self.assertEqual([(h.path, h.kind) for h in found], [("blob.bin", "repo")])


class CommandLine(unittest.TestCase):
    """What runs before every push to the public base: `bin/trimtab scrub`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.instance = make_instance(self.root)
        self.tree = self.root / "tree"
        self.tree.mkdir()
        self.env = isolated_git_env({k: v for k, v in os.environ.items()
                                     if k not in ("TRIMTAB_INSTANCE", scrub.PATTERNS_ENV)}, self.root)

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
        # A relative path would resolve against the scrub's working directory, which is not the instance.
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
