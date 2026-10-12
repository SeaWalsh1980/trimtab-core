"""The trimtab hooks: pr-body-check.sh (PreToolUse), doctrine-drift.sh and session-path.sh (SessionStart).

Each runs as Claude Code would run it: the script, a JSON payload on stdin,
exit code and streams observed.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from instance_fixture import INSTANCE_JSON, env_for, git, make_instance

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PR_BODY_CHECK = REPO / "hooks" / "pr-body-check.sh"
DRIFT = REPO / "hooks" / "doctrine-drift.sh"
SESSION_PATH = REPO / "hooks" / "session-path.sh"
SETTINGS_BASE = REPO / "settings.base.json"
MARKER = "zz-body-marker-7f3a"
PROJECT_LOCK = '{"trimtab_sha": "0", "id_prefix": "XP", "schema_version": 2}\n'
PROJECT_RULE = "---\nid_prefix: XP-LAYERING\ndescription: x\n---\n\n## 1. Layers\n\n- **REQUIRE** inward imports.\n"


def run(hook, payload, cwd=None, env=None):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([str(hook)], input=text, capture_output=True, text=True, cwd=cwd, env=env, timeout=30)


def bash(command, cwd):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(cwd),
            "tool_input": {"command": command}}


class PrBodyCheck(unittest.TestCase):
    """Builds a fixture instance, and for the project cases a fixture project, in a temporary directory."""

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

    def project(self, *, repository=True):
        """A project with a lock (`XP`) and one rule file (`XP-LAYERING`); a git repository unless told not."""
        root = self.cwd / "project"
        for rel, text in {".claude/trimtab.json": PROJECT_LOCK, ".claude/rules/layering.md": PROJECT_RULE}.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        (root / "src" / "sub").mkdir(parents=True)
        if repository:
            git(root, "init", "-q")
        return root

    def project_body(self):
        return (FIXTURES / "pr-tst2-ambiguous.md").read_text().replace(
            "## Harness items applied\n```yaml\n- id: TST-2", "## Harness items applied\n```yaml\n- id: XP-LAYERING-1")

    def check_in(self, command, cwd):
        return run(PR_BODY_CHECK, bash(command, cwd), env=self.env)

    def test_a_session_in_a_subdirectory_resolves_the_projects_own_ids(self):
        root = self.project()
        (root / "body.md").write_text(self.project_body())

        done = self.check_in(f"gh pr create --title t --body-file {root / 'body.md'}", root / "src" / "sub")

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_body_file_path_is_read_relative_to_the_session_directory(self):
        root = self.project()
        (root / "src" / "sub" / "body.md").write_text(self.project_body())
        (root / "body.md").write_text(f"Only prose here, {MARKER}.\n")

        done = self.check_in("gh pr create --title t --body-file body.md", root / "src" / "sub")

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_outside_a_git_repository_the_directory_itself_is_the_project(self):
        root = self.project(repository=False)
        (root / "body.md").write_text(self.project_body())

        done = self.check_in("gh pr create --title t --body-file body.md", root)

        self.assertEqual(done.returncode, 0, done.stderr)


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
        config = {"trimtab_sha": self.installed, "id_prefix": "PRJ", "schema_version": 2}
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

    def test_a_base_sha_matching_the_base_the_locked_instance_pins_is_silent(self):
        self.lock(base_sha=INSTANCE_JSON["base"]["sha"])

        done = self.start()

        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_a_base_sha_other_than_the_base_the_locked_instance_pins_warns(self):
        self.lock(base_sha="1" * 40)

        done = self.start()

        self.assertEqual(done.returncode, 0)
        message = json.loads(done.stdout)["systemMessage"]
        self.assertIn("base_sha 111111111111 is not the base", message)
        self.assertIn("/trimtab-bump", message)

    def test_a_base_sha_the_locked_instance_cannot_confirm_warns(self):
        (self.instance / "instance.json").unlink()
        git(self.instance, "commit", "-qam", "drop instance.json")
        self.installed = git(self.instance, "rev-parse", "HEAD")
        self.lock(base_sha="1" * 40)

        done = self.start()

        self.assertIn("cannot read the base pinned", json.loads(done.stdout)["systemMessage"])

    def test_a_base_sha_that_is_not_a_full_commit_sha_warns(self):
        self.lock(base_sha="main")

        done = self.start()

        message = json.loads(done.stdout)["systemMessage"]
        self.assertIn("base_sha that is not a full commit SHA", message)
        self.assertIn("remove it and re-run /trimtab-bump", message)

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
        self.lock(schema_version=1)

        done = self.start()

        self.assertIn("schema_version 1 is behind 2", json.loads(done.stdout)["systemMessage"])

    def test_a_malformed_lock_exits_zero(self):
        (self.project / ".claude" / "trimtab.json").write_text("{not json")

        self.assertEqual(self.start().returncode, 0)

    def test_a_malformed_payload_exits_zero(self):
        self.assertEqual(run(DRIFT, "garbage").returncode, 0)


class SessionPath(unittest.TestCase):
    """The CLI reaches a session's Bash calls by name, through CLAUDE_ENV_FILE.

    Builds a fixture config directory, a decoy `trimtab` and the env file in a
    temporary directory. A later Bash call is simulated as Claude Code runs one:
    the env file's contents first, then the command.
    """

    STARTUP = {"hook_event_name": "SessionStart", "source": "startup"}
    INSTALLED = "installed-cli-7c1e"
    DECOY = "decoy-cli-41b9"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.config = root / "config"
        self.decoys = root / "decoys"
        self.env_file = root / "session-env" / "hook.sh"
        self.env_file.parent.mkdir()
        self.cli(self.config / "bin", self.INSTALLED)
        self.cli(self.decoys, self.DECOY)
        self.env = {"PATH": f"{self.decoys}:/usr/bin:/bin", "HOME": str(root / "home"),
                    "CLAUDE_CONFIG_DIR": str(self.config), "CLAUDE_ENV_FILE": str(self.env_file)}

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def cli(directory, marker):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "trimtab").write_text(f"#!/bin/sh\necho {marker}\n")
        (directory / "trimtab").chmod(0o755)

    def start(self, payload=None):
        return run(SESSION_PATH, self.STARTUP if payload is None else payload, env=self.env)

    def later_bash_call(self, command):
        script = f'. "{self.env_file}"\n{command}\n'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env={"PATH": self.env["PATH"]}, timeout=30)

    def test_a_later_bash_call_runs_the_installed_cli_by_name(self):
        self.start()

        done = self.later_bash_call("trimtab --help")

        self.assertEqual(done.stdout.strip(), self.INSTALLED)

    def test_the_installed_cli_wins_over_one_already_on_path(self):
        self.start()

        done = self.later_bash_call("command -v trimtab")

        self.assertEqual(done.stdout.strip(), str(self.config / "bin" / "trimtab"))

    def test_the_line_names_the_config_directory_absolutely(self):
        # Neither ~ nor $HOME: the env file runs in whatever shell Claude Code
        # starts, and a session's HOME is not the guard's to trust.
        self.start()

        text = self.env_file.read_text()

        self.assertIn(f"{self.config}/bin", text)
        self.assertNotIn("~", text)
        self.assertNotIn("$HOME", text)

    def test_starting_again_adds_no_second_line(self):
        self.start()
        self.start({"hook_event_name": "SessionStart", "source": "compact"})

        lines = [line for line in self.env_file.read_text().splitlines() if f"{self.config}/bin" in line]

        self.assertEqual(len(lines), 1)

    def test_lines_other_hooks_wrote_are_kept(self):
        other = 'export OTHER_HOOK_VALUE="kept"\n'
        self.env_file.write_text(other)

        self.start()

        self.assertTrue(self.env_file.read_text().startswith(other))

    def test_without_an_env_file_it_warns_and_exits_zero(self):
        del self.env["CLAUDE_ENV_FILE"]

        done = self.start()

        self.assertEqual(done.returncode, 0)
        self.assertIn("trimtab is not on PATH", json.loads(done.stdout)["systemMessage"])

    def test_without_the_installed_cli_it_warns_and_writes_nothing(self):
        (self.config / "bin" / "trimtab").unlink()

        done = self.start()

        self.assertEqual(done.returncode, 0)
        self.assertIn("trimtab is not on PATH", json.loads(done.stdout)["systemMessage"])
        self.assertFalse(self.env_file.exists())

    def test_without_a_config_dir_setting_it_uses_the_home_default(self):
        home_config = Path(self.env["HOME"]) / ".claude"
        self.cli(home_config / "bin", self.INSTALLED)
        del self.env["CLAUDE_CONFIG_DIR"]

        self.start()

        self.assertIn(f"{home_config}/bin", self.env_file.read_text())

    def test_a_malformed_payload_still_puts_the_cli_on_path(self):
        done = self.start("garbage")

        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.later_bash_call("trimtab").stdout.strip(), self.INSTALLED)

    def test_the_base_settings_register_it_at_session_start(self):
        groups = json.loads(SETTINGS_BASE.read_text())["hooks"]["SessionStart"]

        commands = [h["command"] for g in groups for h in g["hooks"]]

        self.assertIn('"$HOME/.claude/hooks/session-path.sh"', commands)


if __name__ == "__main__":
    unittest.main()
