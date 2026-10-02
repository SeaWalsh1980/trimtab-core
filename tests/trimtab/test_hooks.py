"""The two unregistered hooks: pr-body-check.sh (PreToolUse) and doctrine-drift.sh (SessionStart).

Each runs as Claude Code would run it: the script, a JSON payload on stdin,
exit code and streams observed.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from instance_fixture import REPO_NAME, env_for, git, make_instance

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PR_BODY_CHECK = REPO / "hooks" / "pr-body-check.sh"
DRIFT = REPO / "hooks" / "doctrine-drift.sh"
MARKER = "zz-body-marker-7f3a"


def run(hook, payload, cwd=None, env=None):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([str(hook)], input=text, capture_output=True, text=True, cwd=cwd, env=env, timeout=30)


def bash(command, cwd):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(cwd),
            "tool_input": {"command": command}}


class PrBodyCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        (self.cwd / "good.md").write_text((FIXTURES / "pr-tst2-ambiguous.md").read_text())
        (self.cwd / "bad.md").write_text(f"Only prose here, {MARKER}.\n")
        self.env = env_for(make_instance(self.cwd))

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, command):
        return run(PR_BODY_CHECK, bash(command, self.cwd), env=self.env)

    def test_a_command_without_gh_pr_is_allowed_silently(self):
        done = self.check("ls -la")

        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_create_with_a_valid_body_file_is_allowed(self):
        self.assertEqual(self.check("gh pr create --draft --title t --body-file good.md").returncode, 0)

    def test_create_with_a_body_file_lacking_the_block_is_blocked(self):
        done = self.check("gh pr create --draft --title t --body-file bad.md")

        self.assertEqual(done.returncode, 2)
        self.assertIn("missing-section", done.stderr)

    def test_the_block_message_never_quotes_the_body(self):
        done = self.check("gh pr create --title t --body-file bad.md")

        self.assertNotIn(MARKER, done.stderr + done.stdout)

    def test_create_with_an_inline_body_lacking_the_block_is_blocked(self):
        done = self.check(f"gh pr create --title t --body 'prose {MARKER}'")

        self.assertEqual(done.returncode, 2)
        self.assertNotIn(MARKER, done.stderr)

    def test_create_with_fill_is_blocked(self):
        self.assertEqual(self.check("gh pr create --fill").returncode, 2)

    def test_create_in_the_browser_is_left_to_ci(self):
        self.assertEqual(self.check("gh pr create --web").returncode, 0)

    def test_edit_without_a_body_is_allowed(self):
        self.assertEqual(self.check("gh pr edit 12 --add-label x").returncode, 0)

    def test_edit_with_a_bad_body_file_is_blocked(self):
        self.assertEqual(self.check("git status && gh pr edit 12 -F bad.md").returncode, 2)

    def test_gh_pr_inside_another_commands_argument_is_allowed(self):
        self.assertEqual(self.check("git commit -m 'run gh pr create --fill later'").returncode, 0)

    def test_a_body_built_by_command_substitution_is_left_to_ci(self):
        self.assertEqual(self.check('gh pr create --title t --body "$(cat bad.md)"').returncode, 0)

    def test_an_unreadable_body_file_fails_open(self):
        self.assertEqual(self.check("gh pr create --title t --body-file missing.md").returncode, 0)

    def test_a_malformed_payload_fails_open(self):
        done = run(PR_BODY_CHECK, '{"tool_input": {"command": "gh pr create --fill"', env=self.env)

        self.assertEqual(done.returncode, 0)

    def test_without_an_instance_a_bad_body_is_allowed_and_says_why(self):
        done = run(PR_BODY_CHECK, bash("gh pr create --title t --body-file bad.md", self.cwd), env=env_for(None))

        self.assertEqual(done.returncode, 0)
        self.assertIn("TRIMTAB_INSTANCE is not set", done.stderr)


class DoctrineDrift(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.instance = make_instance(Path(self._tmp.name), commit=True)
        self.installed = git(self.instance, "rev-parse", "HEAD")
        self.env = env_for(self.instance)
        self.project = Path(self._tmp.name) / "project"
        (self.project / ".claude").mkdir(parents=True)
        git(self.project, "init", "-q")

    def tearDown(self):
        self._tmp.cleanup()

    def advance_instance(self, rel):
        (self.instance / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.instance / rel).write_text("changed\n")
        git(self.instance, "add", "-A")
        git(self.instance, "commit", "-qm", "change")

    def lock(self, **overrides):
        config = {"source": REPO_NAME, "trimtab_sha": self.installed, "id_prefix": "PRJ",
                  "schema_version": 1}
        config.update(overrides)
        (self.project / ".claude" / "trimtab.json").write_text(json.dumps(config))

    def start(self):
        return run(DRIFT, {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(self.project)},
                   env=self.env)

    def test_without_an_instance_it_warns_and_exits_zero(self):
        self.lock()
        self.env = env_for(None)

        done = self.start()

        self.assertEqual(done.returncode, 0)
        self.assertIn("TRIMTAB_INSTANCE is not set", json.loads(done.stdout)["systemMessage"])

    def test_a_lock_behind_only_non_doctrine_changes_is_silent(self):
        self.lock()
        self.advance_instance("docs/notes.md")

        done = self.start()

        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_a_lock_behind_a_doctrine_change_warns(self):
        self.lock()
        self.advance_instance("rules/Example.md")

        done = self.start()

        self.assertEqual(done.returncode, 0)
        self.assertIn("doctrine has changed", json.loads(done.stdout)["systemMessage"])

    def test_a_project_that_has_not_adopted_is_silent(self):
        done = self.start()

        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_a_lock_matching_the_installed_sha_is_silent(self):
        self.lock()

        done = self.start()

        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_a_lock_that_differs_warns_and_exits_zero(self):
        self.lock(trimtab_sha="0" * 40)

        done = self.start()

        self.assertEqual(done.returncode, 0)
        self.assertIn("systemMessage", json.loads(done.stdout))

    def test_a_lock_without_a_sha_warns_and_exits_zero(self):
        self.lock()
        cfg = json.loads((self.project / ".claude" / "trimtab.json").read_text())
        del cfg["trimtab_sha"]
        (self.project / ".claude" / "trimtab.json").write_text(json.dumps(cfg))

        done = self.start()

        self.assertEqual(done.returncode, 0)
        self.assertIn("trimtab_sha", json.loads(done.stdout)["systemMessage"])

    def test_an_older_schema_version_warns(self):
        self.lock(schema_version=0)

        done = self.start()

        self.assertIn("schema_version", json.loads(done.stdout)["systemMessage"])

    def test_a_malformed_lock_exits_zero(self):
        (self.project / ".claude" / "trimtab.json").write_text("{not json")

        self.assertEqual(self.start().returncode, 0)

    def test_a_malformed_payload_exits_zero(self):
        self.assertEqual(run(DRIFT, "garbage").returncode, 0)


if __name__ == "__main__":
    unittest.main()
