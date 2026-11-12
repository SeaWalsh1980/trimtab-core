"""The trimtab hooks: pr-body-check.sh (PreToolUse), doctrine-drift.sh and session-path.sh (SessionStart).

Each runs as Claude Code would run it: the script, a JSON payload on stdin,
exit code and streams observed. The last section reads every file under
hooks/ for its shebang (ADR 0064).
"""

import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from hook_fixture import PATH_RESET, NoCanonicalReset, hooks_with_path, swap_reset
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

    def test_a_rest_create_with_a_valid_body_file_is_allowed(self):
        done = self.check("gh api -X POST repos/o/r/pulls -f title=t -f head=b -f base=main "
                          "-F draft=true -F body=@good.md")

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_rest_create_with_a_body_file_lacking_the_block_is_blocked(self):
        done = self.check("gh api -X POST repos/o/r/pulls -f title=t -f head=b -f base=main -F body=@bad.md")

        self.assertEqual(done.returncode, 2)
        self.assertNotIn(MARKER, done.stderr + done.stdout)

    def test_a_rest_create_with_an_inline_body_lacking_the_block_is_blocked(self):
        self.assertEqual(self.check(f"gh api repos/o/r/pulls -f title=t -f 'body=prose {MARKER}'").returncode, 2)

    def test_a_rest_create_without_a_body_is_blocked(self):
        self.assertEqual(self.check("gh api --method=POST repos/o/r/pulls -f title=t -f head=b").returncode, 2)

    def test_a_rest_create_whose_input_file_lacks_the_block_is_blocked(self):
        (self.cwd / "pull.json").write_text(json.dumps({"title": "t", "body": f"prose {MARKER}"}))

        self.assertEqual(self.check("gh api -X POST repos/o/r/pulls --input pull.json").returncode, 2)

    def test_a_rest_create_whose_input_file_has_a_valid_body_is_allowed(self):
        (self.cwd / "pull.json").write_text(json.dumps({"title": "t", "body": (self.cwd / "good.md").read_text()}))

        done = self.check("gh api -X POST repos/o/r/pulls --input pull.json")

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_rest_edit_with_a_bad_body_file_is_blocked(self):
        self.assertEqual(self.check("gh api -X PATCH repos/o/r/pulls/12 -F body=@bad.md").returncode, 2)

    def test_a_rest_edit_without_a_body_is_allowed(self):
        self.assertEqual(self.check("gh api -X PATCH repos/o/r/pulls/12 -f title=t").returncode, 0)

    def test_a_rest_read_of_pulls_is_allowed(self):
        self.assertEqual(self.check("gh api repos/o/r/pulls/12").returncode, 0)

    def test_a_rest_call_to_another_endpoint_is_allowed(self):
        self.assertEqual(self.check("gh api -X POST repos/o/r/issues -f title=t -F body=@bad.md").returncode, 0)

    def test_a_rest_call_that_names_no_pulls_is_allowed_silently(self):
        done = self.check("gh api repos/o/r/labels/x --jq .name")

        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_the_shipped_commands_create_line_with_a_bad_body_is_blocked(self):
        # The owner and repository are one variable: the form the retro and conformance methods use.
        done = self.check('gh api -X POST "repos/$REPO/pulls" -f title=t -f head=b -f base="$DEFAULT" '
                          "-F draft=true -F body=@bad.md --jq .html_url")

        self.assertEqual(done.returncode, 2)

    def test_a_create_inside_a_command_substitution_is_still_checked(self):
        # The shipped commands capture the new PR's number for `trimtab verify-body` (ADR 0024).
        done = self.check('N=$(gh api -X POST "repos/$REPO/pulls" -f title=t -f head=b -F body=@bad.md '
                          '--jq .number) && trimtab verify-body --repo "$REPO" --pull "$N" --body-file bad.md')

        self.assertEqual(done.returncode, 2)

    def test_the_shipped_commands_create_line_with_a_valid_body_is_allowed(self):
        done = self.check('gh api -X POST "repos/$REPO/pulls" -f title=t -f head=b -f base="$DEFAULT" '
                          "-F draft=true -F body=@good.md --jq .html_url")

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_create_through_ghs_owner_and_repo_placeholders_is_checked(self):
        done = self.check("gh api -X POST 'repos/{owner}/{repo}/pulls' -f title=t -F body=@bad.md")

        self.assertEqual(done.returncode, 2)

    def test_a_create_through_a_full_url_is_checked(self):
        done = self.check("gh api -X POST https://api.github.com/repos/o/r/pulls -F body=@bad.md")

        self.assertEqual(done.returncode, 2)

    def test_an_edit_of_a_pull_named_by_variables_is_checked(self):
        self.assertEqual(self.check('gh api -X PATCH "repos/$REPO/pulls/$N" -F body=@bad.md').returncode, 2)

    def test_a_method_attached_to_its_flag_is_read(self):
        self.assertEqual(self.check("gh api -XPATCH repos/o/r/pulls/12 -F body=@bad.md").returncode, 2)

    def test_with_input_the_file_is_the_body_and_fields_are_not(self):
        # gh sends the --input file as the body and fields as query parameters.
        (self.cwd / "pull.json").write_text(json.dumps({"title": "t", "body": f"prose {MARKER}"}))

        done = self.check("gh api -X POST repos/o/r/pulls -F body=@good.md --input pull.json")

        self.assertEqual(done.returncode, 2)

    def stub_python(self):
        """A python3 that only records that it ran, first on the hook's PATH; returns (hook, marker)."""
        stub, marker = self.cwd / "stub-bin", self.cwd / "python-ran"
        stub.mkdir()
        (stub / "python3").write_text(f"#!/bin/sh\n: > '{marker}'\n")
        (stub / "python3").chmod(0o755)
        hooks = hooks_with_path(self.cwd / "stub-hooks", f"{stub}:/usr/bin:/bin")
        return hooks / PR_BODY_CHECK.name, marker

    def test_a_read_of_pulls_never_starts_python(self):
        hook, marker = self.stub_python()

        run(hook, bash('gh api "repos/$REPO/pulls?state=open&per_page=100" --jq .', self.cwd), env=self.env)

        self.assertFalse(marker.exists())

    def test_a_write_to_pulls_starts_python(self):
        hook, marker = self.stub_python()

        run(hook, bash('gh api -X POST "repos/$REPO/pulls" -F body=@bad.md', self.cwd), env=self.env)

        self.assertTrue(marker.exists())

    def test_a_rest_body_from_stdin_is_left_to_ci(self):
        self.assertEqual(self.check("gh api -X POST repos/o/r/pulls --input - < pull.json").returncode, 0)

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


# ---- shebangs (ADR 0064) ---------------------------------------------------
# settings.base.json runs each hook by its path, so the kernel reads the
# shebang. `#!/usr/bin/env bash` takes bash from Claude Code's inherited PATH,
# which on Debian puts the user-writable ~/.local/bin first: a planted
# ~/.local/bin/bash would replace every guard before any line of it ran.

HOOKS = REPO / "hooks"
TRUSTED_DIRS = {"/bin", "/usr/bin"}
# Named, not any binary in a trusted directory: a wrapper such as nice, time,
# xargs or busybox takes its own interpreter from PATH, which is the hole.
INTERPRETERS = {"bash", "python3"}


def shebang_fault(first_line):
    """Why a file's first line is an unsafe shebang, or None when it is safe or absent."""
    if not first_line.startswith(b"#!"):
        return None
    if b"\r" in first_line:
        return "carriage return in the shebang: the kernel looks for an interpreter named with it"
    words = first_line[2:].decode("utf-8", "replace").split()
    if not words:
        return "empty shebang"
    interpreter = PurePosixPath(words[0])
    if interpreter.name == "env":
        return f"{interpreter} resolves the interpreter from PATH"
    # PurePosixPath folds `/bin/bash/`, `/bin//bash` and `/bin/./bash` into
    # `/bin/bash`; the kernel does not (a trailing slash is ENOTDIR), so only
    # the path as spelled is judged.
    if str(interpreter) != words[0] or str(interpreter.parent) not in TRUSTED_DIRS:
        return f"{words[0]} is outside {' and '.join(sorted(TRUSTED_DIRS))}"
    if interpreter.name not in INTERPRETERS:
        return f"{words[0]} is not one of {', '.join(sorted(INTERPRETERS))}"
    return None


def first_line(path):
    with path.open("rb") as handle:
        return handle.readline().rstrip(b"\n")


def missing_shebang(path, line):
    """Why a file must have a shebang and has none, or None."""
    if line.startswith(b"#!"):
        return None
    if path.suffix == ".sh" or os.access(path, os.X_OK):
        return "no shebang: run by its path, it falls to the calling shell"
    return None


class ShebangFault(unittest.TestCase):
    """The checker itself, so the repository scan below cannot pass vacuously."""

    def test_an_absolute_interpreter_in_bin_is_safe(self):
        self.assertIsNone(shebang_fault(b"#!/bin/bash"))

    def test_an_absolute_interpreter_in_usr_bin_is_safe(self):
        self.assertIsNone(shebang_fault(b"#!/usr/bin/python3"))

    def test_a_file_without_a_shebang_is_not_judged(self):
        self.assertIsNone(shebang_fault(b'"""A module."""'))

    def test_env_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#!/usr/bin/env bash"))

    def test_env_with_a_space_after_the_bang_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#! /usr/bin/env bash"))

    def test_env_in_bin_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#!/bin/env bash"))

    def test_an_interpreter_in_a_user_writable_directory_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#!/home/user/.local/bin/bash"))

    def test_an_interpreter_in_usr_local_bin_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#!/usr/local/bin/bash"))

    def test_a_relative_interpreter_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#!bash"))

    def test_a_bare_bang_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#!"))

    def test_a_carriage_return_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#!/bin/bash\r"))

    def test_a_wrapper_that_searches_path_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#!/usr/bin/nice bash"))

    def test_busybox_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#!/bin/busybox bash"))

    def test_a_directory_as_interpreter_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#!/bin/"))

    def test_a_dot_dot_escape_from_bin_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#!/bin/../tmp/bash"))

    def test_a_double_leading_slash_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#!//bin/bash"))

    def test_a_trailing_slash_is_refused(self):
        self.assertIsNotNone(shebang_fault(b"#!/bin/bash/"))


class MissingShebang(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_shell_hook_without_one_is_refused(self):
        self.assertIsNotNone(missing_shebang(PurePosixPath("hooks/x.sh"), b"# no shebang"))

    def test_an_executable_without_one_is_refused(self):
        tool = self.dir / "tool"
        tool.write_bytes(b"echo hi\n")
        tool.chmod(0o755)

        self.assertIsNotNone(missing_shebang(tool, b"echo hi"))

    def test_a_module_run_by_python3_is_not_judged(self):
        module = self.dir / "module.py"
        module.write_bytes(b'"""A module."""\n')
        module.chmod(0o644)

        self.assertIsNone(missing_shebang(module, b'"""A module."""'))


class HookShebangs(unittest.TestCase):
    def setUp(self):
        self.files = sorted(p for p in HOOKS.rglob("*") if p.is_file())

    def test_every_hook_names_bash_or_python3_in_bin_or_usr_bin(self):
        faults = {str(p.relative_to(REPO)): shebang_fault(first_line(p)) for p in self.files}

        self.assertEqual({path: why for path, why in faults.items() if why}, {})

    def test_every_shell_or_executable_hook_has_a_shebang(self):
        faults = {str(p.relative_to(REPO)): missing_shebang(p, first_line(p)) for p in self.files}

        self.assertEqual({path: why for path, why in faults.items() if why}, {})

    def test_the_scan_sees_the_guards(self):
        names = {p.name for p in self.files}

        self.assertLessEqual({"guard-paths.sh", "guard-bash.sh", "session-mode.sh"}, names)


# ---- PATH reset (ADR 0065) -------------------------------------------------
# The absolute shebang fixes bash, but each hook still names python3, jq, git
# and the rest bare, and the inherited PATH puts the user-writable
# ~/.local/bin first. So the first statement of every shell hook is the reset.

SHELL_HOOKS = sorted((REPO / "hooks").glob("*.sh"))


def planted_names():
    """Every word in a shell hook that names an executable in /usr/bin or /bin: each is planted.

    Derived from the hooks rather than listed, so a tool a hook starts using is
    planted without anyone remembering to add it.
    """
    words = set()
    for hook in SHELL_HOOKS:
        words |= set(re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", hook.read_text(encoding="utf-8")))
    return sorted(w for w in words if any((Path(d) / w).is_file() and os.access(Path(d) / w, os.X_OK)
                                          for d in ("/usr/bin", "/bin")))


def first_statement(path):
    """The first line of a shell hook that is neither the shebang, blank, nor a comment."""
    for line in path.read_text(encoding="utf-8").split("\n")[1:]:
        if line.strip() and not line.lstrip().startswith("#"):
            return line
    return None


def plant(directory, marker, names):
    """Recording stubs for <names> in <directory>; returns the directory."""
    directory.mkdir(parents=True)
    for name in names:
        (directory / name).write_text(f"#!/bin/sh\necho {shlex.quote(name)} >> {shlex.quote(str(marker))}\nexit 1\n")
        (directory / name).chmod(0o755)
    return directory


def event(name, cwd, **fields):
    return json.dumps({"hook_event_name": name, "session_id": "path-reset", "cwd": str(cwd), **fields})


def hook_input(hook, cwd):
    """(payload, args) that take <hook> past its fast path, to where it runs tools."""
    def tool(name, tool_input, event_name="PreToolUse", **extra):
        return event(event_name, cwd, tool_name=name, tool_input=tool_input, **extra)
    start = event("SessionStart", cwd, source="startup")
    return {
        "guard-paths.sh": (tool("Read", {"file_path": "/p/app/main.py"}), []),
        "guard-bash.sh": (tool("Bash", {"command": "git push origin main"}), []),
        "guard-secrets.sh": (tool("Write", {"file_path": "/p/app/x.py", "content": "print(42)"}), []),
        "guard-security.sh": (tool("Edit", {"file_path": "/p/.github/workflows/ci.yml"}), []),
        "guard-mcp.sh": (tool("mcp__srv__create_item", {"name": "x"}), []),
        "guard-github.sh": (tool("Bash", {"command": "gh pr create --draft"}), []),
        "pr-body-check.sh": (tool("Bash", {"command": "gh pr create --draft"}), []),
        "pr-monitor-record.sh": (tool("Bash", {"command": "gh pr create --draft"}, "PostToolUse",
                                      tool_response={"stdout": "", "stderr": "", "interrupted": False}), []),
        "rule-usage.sh": (tool("Skill", {"skill": "trimtab-plan"}), []),
        "session-scratch.sh": (start, ["start"]),
    }.get(hook.name, (start, []))


class HookPathReset(unittest.TestCase):
    """Each hook runs with planted recorders in ~/.local/bin, first on PATH as on Debian."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        home = self.root / "home"
        self.local_bin = home / ".local" / "bin"
        self.names = planted_names()
        self.env = {"PATH": f"{self.local_bin}:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(self.root),
                    "CLAUDE_CONFIG_DIR": str(home / ".claude")}

    def tearDown(self):
        self._tmp.cleanup()

    def ran_planted(self, hook, marker):
        """The planted tools <hook> ran, in order, with recorders written to <marker>."""
        if marker.exists():
            marker.unlink()
        if self.local_bin.exists():
            for stub in self.local_bin.iterdir():
                stub.unlink()
            self.local_bin.rmdir()
        plant(self.local_bin, marker, self.names)
        payload, args = hook_input(hook, self.root)
        subprocess.run([str(hook), *args], input=payload, capture_output=True, text=True,
                       cwd=self.root, env=self.env, timeout=30)
        return marker.read_text().split() if marker.exists() else []

    def test_every_shell_hook_resets_path_before_its_first_command(self):
        faults = {p.name: first_statement(p) for p in SHELL_HOOKS}

        self.assertEqual({name: line for name, line in faults.items() if line != PATH_RESET}, {})

    def test_no_hook_runs_a_tool_from_the_inherited_path(self):
        for hook in SHELL_HOOKS:
            with self.subTest(hook=hook.name):
                self.assertEqual(self.ran_planted(hook, self.root / f"{hook.name}.ran"), [])

    def test_each_hook_does_run_a_planted_tool_when_its_reset_keeps_the_inherited_path(self):
        # The control for the case above, per hook: a copy whose reset puts
        # ~/.local/bin first again, as the inherited PATH does. Each must run a
        # planted tool, so an empty record above is the reset's doing and not a
        # payload that stops before any tool.
        copies = hooks_with_path(self.root / "control", f"{self.local_bin}:/usr/bin:/bin")
        for hook in SHELL_HOOKS:
            with self.subTest(hook=hook.name):
                self.assertNotEqual(self.ran_planted(copies / hook.name, self.root / f"{hook.name}.ctl"), [])

    def test_the_planted_tools_include_what_the_guards_start(self):
        self.assertLessEqual({"python3", "jq", "git", "cat", "timeout", "sha256sum"}, set(self.names))

    def test_the_scan_sees_every_shell_hook(self):
        names = {p.name for p in SHELL_HOOKS}

        self.assertLessEqual({"guard-github.sh", "guard-mcp.sh", "pr-body-check.sh", "session-path.sh"}, names)


class HooksWithPath(unittest.TestCase):
    """The copy helper itself, so a stub case cannot run a copy that differs from its hook."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_copy_differs_from_its_hook_only_in_the_reset(self):
        copies = hooks_with_path(self.root / "c", "/stub")
        for hook in SHELL_HOOKS:
            with self.subTest(hook=hook.name):
                self.assertEqual((copies / hook.name).read_text(encoding="utf-8"),
                                 hook.read_text(encoding="utf-8").replace(PATH_RESET, "export PATH=/stub"))

    def test_a_path_with_a_space_is_quoted(self):
        self.assertIn("export PATH='/a b:/usr/bin'", swap_reset(f"#!/bin/bash\n{PATH_RESET}\n", "/a b:/usr/bin"))

    def test_a_hook_without_the_reset_is_refused(self):
        with self.assertRaises(NoCanonicalReset):
            swap_reset("#!/bin/bash\nset -u\n", "/stub")

    def test_a_hook_with_two_resets_is_refused(self):
        with self.assertRaises(NoCanonicalReset):
            swap_reset(f"#!/bin/bash\n{PATH_RESET}\n{PATH_RESET}\n", "/stub")

    def test_a_hooks_directory_without_the_package_beside_it_is_refused(self):
        lone = self.root / "lone" / "hooks"
        lone.mkdir(parents=True)

        with self.assertRaises(FileNotFoundError):
            hooks_with_path(self.root / "c", "/stub", hooks=lone)


if __name__ == "__main__":
    unittest.main()
