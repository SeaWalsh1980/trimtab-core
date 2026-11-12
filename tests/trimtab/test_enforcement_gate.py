"""E20: `trimtab check-enforcement` shows each row from facts no agent can write (ADR 0063 §2).

Real git in temporary repositories; gh, a true external system, faked (TST-2)."""

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from enforcement_fixture import OWNERS, record_text, row_line
from gh_fake import FakeGh
from test_scrub import isolate_git
from trimtab import cli, guardstate
from trimtab.enforcement import gate, manifest, record, tools
from trimtab.enforcement.gate import Facts, GateError

PROJECT = "owner/project"
INSTANCE_REPO = "owner/instance"
BASE_REPO = "owner/core"
ADR_DIR = "docs/" + "adr/"  # in two pieces: the citation test reads a whole path as a citation
RECORD = ADR_DIR + "0001-the-record.md"
GITHUB_HOOK = {"file": "guard-github.sh", "event": "PreToolUse", "matcher": "Bash"}
PATHS_HOOK = {"file": "guard-paths.sh", "event": "PreToolUse", "matcher": "Edit"}
ROWS = {r.id: r for r in record.parse(record_text())}
BASE_ROWS = [rid for rid, r in ROWS.items() if r.side != record.PROJECT]
HOOKED_ROWS = [rid for rid, r in ROWS.items() if r.hooks]
GITHUB_ROWS = [rid for rid in HOOKED_ROWS if "guard-github.sh" in ROWS[rid].hooks]
PROJECT_ROWS = [rid for rid, r in ROWS.items() if r.side != record.BASE]
PINNED = ("a" * 40, "b" * 40, "c" * 40, "d" * 64)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit_all(repo, message):
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def init(repo):
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")


def hooks_for(rid):
    row = ROWS[rid]
    if row.hooks == frozenset({"guard-paths.sh"}):
        return [PATHS_HOOK]
    return [GITHUB_HOOK] if row.hooks else []


def every_base_entry():
    return {rid: {"row_sha256": ROWS[rid].sha256, "hooks": hooks_for(rid),
                  "tests": ["test_enforcement_record.ParsesTheTable.test_reads_every_row_in_order"],
                  "decision": {"function": "trimtab.enforcement.gate:decide", "allow": 0}}
            for rid in BASE_ROWS}


def run(head, path, run_id, event="push", branch="main"):
    return {"id": run_id, "head_sha": head, "head_branch": branch, "path": path, "status": "completed",
            "event": event}


def job(head, name, conclusion="success"):
    return {"name": name, "head_sha": head, "status": "completed", "conclusion": conclusion,
            "steps": [{"name": "s"}], "runner_id": 1, "runner_name": "r"}


def facts(**overrides):
    entries = manifest.parse(json.dumps({"schema_version": 1, "rows": every_base_entry()}))
    hooks = {h.file for e in entries.values() for h in e.hooks}
    base = dict(rows=tuple(ROWS.values()), entries=entries, base_ci=None, checkout=None,
                live={h: None for h in hooks}, project_jobs={rid: None for rid in PROJECT_ROWS}, pinned=PINNED)
    base.update(overrides)
    return Facts(**base)


def problems(results):
    return {r.id: r.problem for r in results if r.problem}


def by_number(ids):
    return sorted(ids, key=lambda r: int(r[1:]))


class Decides(unittest.TestCase):
    def test_shows_every_row_when_every_fact_holds(self):
        self.assertEqual(problems(gate.decide(facts())), {})

    def test_shows_no_base_row_while_this_bases_ci_failed(self):
        self.assertEqual(by_number(problems(gate.decide(facts(base_ci="CI failed")))), BASE_ROWS)

    def test_shows_no_base_row_hooked_or_not_on_a_changed_checkout(self):
        self.assertEqual(by_number(problems(gate.decide(facts(checkout="changed")))), BASE_ROWS)

    def test_shows_no_row_whose_hook_is_not_live(self):
        found = problems(gate.decide(facts(live={"guard-github.sh": "not linked", "guard-paths.sh": None})))

        self.assertEqual(by_number(found), GITHUB_ROWS)

    def test_skips_liveness_on_the_server(self):
        self.assertEqual(problems(gate.decide(facts(live=None))), {})

    def test_shows_no_row_whose_hook_liveness_was_never_read(self):
        self.assertEqual(by_number(problems(gate.decide(facts(live={})))), HOOKED_ROWS)

    def test_shows_no_base_row_the_manifest_leaves_out(self):
        entries = dict(facts().entries)
        del entries["E9"]

        self.assertEqual(list(problems(gate.decide(facts(entries=entries)))), ["E9"])

    def test_shows_no_project_row_the_manifest_maps(self):
        entries = dict(facts().entries)
        entries["E1"] = entries["E20"]

        self.assertIn("consuming project", problems(gate.decide(facts(entries=entries)))["E1"])

    def test_shows_no_project_row_the_instance_names_no_job_for(self):
        jobs = {rid: None for rid in PROJECT_ROWS if rid != "E1"}

        self.assertEqual(list(problems(gate.decide(facts(project_jobs=jobs)))), ["E1"])

    def test_shows_no_project_row_whose_job_failed(self):
        jobs = {**facts().project_jobs, "E3": "a job failed"}

        self.assertEqual(problems(gate.decide(facts(project_jobs=jobs))), {"E3": "a job failed"})

    def test_e22_needs_its_base_entry(self):
        entries = dict(facts().entries)
        del entries["E22"]

        self.assertEqual(list(problems(gate.decide(facts(entries=entries)))), ["E22"])

    def test_e22_needs_its_project_job(self):
        jobs = {rid: None for rid in PROJECT_ROWS if rid != "E22"}

        self.assertEqual(list(problems(gate.decide(facts(project_jobs=jobs)))), ["E22"])


class World(unittest.TestCase):
    """Three repositories (project, instance, this base's checkout) and a config dir, all real."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        isolate_git(self, self.tmp)
        self.git = tools.Git(environ={"HOME": str(self.tmp)})
        self.code, self.inst, self.proj, self.config = (self.tmp / n for n in ("code", "instance", "project", "config"))
        init(self.code)
        (self.code / "hooks").mkdir()
        for name in ("guard-github.sh", "guard-paths.sh"):
            (self.code / "hooks" / name).write_text("#!/bin/sh\n")
            (self.code / "hooks" / name).chmod(0o755)
        self.write_manifest(every_base_entry())
        self.base_sha = commit_all(self.code, "base")
        init(self.inst)
        (self.inst / "README").write_text("x\n")
        commit_all(self.inst, "root")
        self.merge_record(record_text())
        self.jobs = {rid: {"workflow": f".github/workflows/{rid.lower()}.yml", "job": rid.lower()}
                     for rid in PROJECT_ROWS}
        self.trimtab_sha = self.commit_instance()
        init(self.proj)
        self.head = self.commit_lock(self.trimtab_sha)
        self.config.mkdir()
        (self.config / "hooks").mkdir()
        for name in ("guard-github.sh", "guard-paths.sh"):
            (self.config / "hooks" / name).symlink_to(self.code / "hooks" / name)
        self.register()

    def merge_record(self, text, branch="add-record"):
        git(self.inst, "checkout", "-q", "-b", branch)
        (self.inst / "docs" / "adr").mkdir(parents=True, exist_ok=True)
        (self.inst / RECORD).write_text(text)
        commit_all(self.inst, "add the record")
        git(self.inst, "checkout", "-q", "main")
        git(self.inst, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "merge", "-q", "--no-ff",
            "-m", "merge", branch)
        return git(self.inst, "rev-parse", "HEAD")

    def write_manifest(self, rows):
        path = self.code / manifest.PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "rows": rows}))

    def commit_instance(self, projects=None):
        body = {"schema_version": 1, "repo": INSTANCE_REPO, "base": {"repo": BASE_REPO, "sha": self.base_sha},
                "enforcement": {"record": RECORD, "projects": projects or {PROJECT: self.jobs}}}
        (self.inst / "instance.json").write_text(json.dumps(body))
        return commit_all(self.inst, "instance")

    def commit_lock(self, trimtab_sha, base_sha=None):
        lock = {"trimtab_sha": trimtab_sha, "id_prefix": "CT", "schema_version": 2}
        if base_sha:
            lock["base_sha"] = base_sha
        (self.proj / ".claude").mkdir(exist_ok=True)
        (self.proj / ".claude" / "trimtab.json").write_text(json.dumps(lock))
        head = commit_all(self.proj, "lock")
        git(self.proj, "update-ref", "refs/remotes/origin/main", head)
        return head

    def register(self, command=None, extra=None):
        command = command or f'"{self.config}/hooks/{{}}"'
        hooks = [{"matcher": "Bash", "hooks": [{"type": "command", "command": command.format("guard-github.sh")}]},
                 {"matcher": "Edit", "hooks": [{"type": "command", "command": command.format("guard-paths.sh")}]}]
        (self.config / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": hooks}, **(extra or {})}))

    def gh(self, server_head=None, base_job="success", failing=None, branch="main"):
        answers = {f"repos/{PROJECT}": json.dumps({"default_branch": "main"}),
                   f"repos/{PROJECT}/branches/main": json.dumps({"commit": {"sha": server_head or self.head}}),
                   f"repos/{BASE_REPO}/actions/runs":
                       json.dumps({"workflow_runs": [run(self.base_sha, gate.BASE_WORKFLOW, 1)]}),
                   f"repos/{BASE_REPO}/actions/runs/1/jobs": json.dumps({"jobs": [job(self.base_sha, "lint-test",
                                                                                      base_job)]})}
        runs = [run(self.head, ref["workflow"], 100 + n, branch=branch) for n, ref in enumerate(self.jobs.values())]
        answers[f"repos/{PROJECT}/actions/runs"] = json.dumps({"workflow_runs": runs})
        for n, (rid, ref) in enumerate(self.jobs.items()):
            answers[f"repos/{PROJECT}/actions/runs/{100 + n}/jobs"] = json.dumps(
                {"jobs": [job(self.head, ref["job"], "failure" if rid == failing else "success")]})
        return FakeGh(answers)

    def gather(self, fake=None, repository=PROJECT, **kwargs):
        return gate.gather(repository, self.proj, self.inst, self.code, self.config, run=fake or self.gh(),
                           git=self.git, **kwargs)

    def found(self, fake=None, **kwargs):
        return problems(gate.decide(self.gather(fake, **kwargs)))


class Gathers(World):
    def test_shows_every_row_on_a_complete_install(self):
        self.assertEqual(self.found(), {})

    def test_reads_the_lock_at_the_default_branch_head_not_the_working_tree(self):
        (self.proj / ".claude" / "trimtab.json").write_text(json.dumps(
            {"trimtab_sha": "f" * 40, "id_prefix": "CT", "schema_version": 2}))

        self.assertEqual(self.gather().pinned[0], self.trimtab_sha)

    def test_refuses_a_fetch_that_is_not_the_servers_head(self):
        with self.assertRaises(GateError):
            self.gather(self.gh(server_head="e" * 40))

    def test_refuses_a_project_never_fetched(self):
        git(self.proj, "update-ref", "-d", "refs/remotes/origin/main")

        with self.assertRaises(GateError):
            self.gather()

    def test_refuses_a_lock_with_a_short_trimtab_sha(self):
        self.head = self.commit_lock(self.trimtab_sha[:12])

        with self.assertRaises(GateError):
            self.gather()

    def test_refuses_a_lock_that_does_not_parse(self):
        (self.proj / ".claude" / "trimtab.json").write_text("{")
        self.head = commit_all(self.proj, "broken lock")
        git(self.proj, "update-ref", "refs/remotes/origin/main", self.head)

        with self.assertRaises(GateError):
            self.gather()

    def test_ignores_a_replace_ref_that_forges_the_instance_file(self):
        body = json.loads(git(self.inst, "show", f"{self.trimtab_sha}:instance.json"))
        body["enforcement"]["projects"] = {}
        (self.inst / "instance.json").write_text(json.dumps(body))
        forged = commit_all(self.inst, "forged")
        git(self.inst, "replace", self.trimtab_sha, forged)

        self.assertEqual(self.found(), {})

    def test_refuses_an_instance_with_a_grafts_file(self):
        grafts = Path(git(self.inst, "rev-parse", "--path-format=absolute", "--git-path", "info/grafts"))
        grafts.write_text(self.trimtab_sha + "\n")

        with self.assertRaises(GateError):
            self.gather()

    def test_reads_the_record_as_merged_not_as_edited_later(self):
        lines = "".join(row_line(r, condition="loosened" if r == "E7" else None) for r in OWNERS)
        (self.inst / RECORD).write_text(record_text(lines=lines))
        self.head = self.commit_lock(commit_all(self.inst, "edit the record"))

        self.assertEqual(self.found(), {})

    def test_reads_the_record_last_added_when_it_was_removed_and_added_again(self):
        git(self.inst, "rm", "-q", RECORD)
        commit_all(self.inst, "remove the record")
        lines = "".join(row_line(r, condition="tightened" if r == "E7" else None) for r in OWNERS)
        self.head = self.commit_lock(self.merge_record(record_text(lines=lines), branch="re-add"))

        self.assertEqual(list(self.found()), ["E7"])  # the manifest was written for the first text

    def test_refuses_a_record_deleted_at_the_locks_commit(self):
        git(self.inst, "rm", "-q", RECORD)
        self.head = self.commit_lock(commit_all(self.inst, "remove the record"))

        with self.assertRaises(GateError):
            self.gather()

    def test_refuses_a_record_the_history_never_added(self):
        body = json.loads((self.inst / "instance.json").read_text())
        body["enforcement"]["record"] = ADR_DIR + "0002-missing.md"
        (self.inst / "instance.json").write_text(json.dumps(body))
        self.head = self.commit_lock(commit_all(self.inst, "missing record"))

        with self.assertRaises(GateError):
            self.gather()

    def test_refuses_an_instance_that_names_no_record(self):
        body = json.loads((self.inst / "instance.json").read_text())
        del body["enforcement"]
        (self.inst / "instance.json").write_text(json.dumps(body))
        self.head = self.commit_lock(commit_all(self.inst, "no record"))

        with self.assertRaises(GateError):
            self.gather()

    def test_refuses_a_lock_whose_base_is_not_the_instances(self):
        self.head = self.commit_lock(self.trimtab_sha, base_sha="0" * 40)

        with self.assertRaises(GateError):
            self.gather()

    def test_refuses_a_manifest_that_breaks_its_schema(self):
        (self.code / manifest.PATH).write_text("{}")
        self.base_sha = commit_all(self.code, "bad manifest")
        self.head = self.commit_lock(self.commit_instance())

        with self.assertRaises(GateError):
            self.gather()

    def test_shows_no_base_row_when_this_bases_ci_failed(self):
        self.assertEqual(by_number(self.found(self.gh(base_job="failure"))), BASE_ROWS)

    def test_shows_no_project_row_whose_job_failed(self):
        self.assertEqual(list(self.found(self.gh(failing="E2"))), ["E2"])

    def test_shows_no_project_row_whose_run_is_for_another_branch(self):
        self.assertEqual(by_number(self.found(self.gh(branch="plan/x"))), PROJECT_ROWS)

    def test_finds_the_projects_jobs_whatever_the_case(self):
        self.head = self.commit_lock(self.commit_instance(projects={PROJECT.upper(): self.jobs}))

        self.assertEqual(self.found(), {})

    def test_server_mode_fails_until_e22_lands(self):
        with self.assertRaises(GateError):
            self.gather(server=True)


class JudgesTheCheckout(World):
    def assertCheckoutRefused(self):
        self.assertEqual(by_number(self.found()), BASE_ROWS)

    def test_refuses_a_checkout_off_the_pinned_base(self):
        (self.code / "README").write_text("x\n")
        commit_all(self.code, "later")

        self.assertCheckoutRefused()

    def test_refuses_an_edited_hook(self):
        (self.code / "hooks" / "guard-github.sh").write_text("#!/bin/sh\nexit 0\n")

        self.assertCheckoutRefused()

    def test_refuses_an_edit_hidden_by_skip_worktree(self):
        git(self.code, "update-index", "--skip-worktree", "hooks/guard-github.sh")
        (self.code / "hooks" / "guard-github.sh").write_text("#!/bin/sh\nexit 0\n")

        self.assertCheckoutRefused()

    def test_refuses_an_ignored_bytecode_file(self):
        (self.code / ".gitignore").write_text("__pycache__/\n")
        pycache = self.code / "trimtab" / "enforcement" / "__pycache__"
        pycache.mkdir()
        (pycache / "gate.cpython-313.pyc").write_bytes(b"forged")

        self.assertCheckoutRefused()

    def test_refuses_a_symlink(self):
        (self.code / "hooks" / "extra.sh").symlink_to("/bin/true")

        self.assertIn("symlink", self.found()["E20"])

    @unittest.skipIf(os.geteuid() == 0, "root lists a 0111 directory, so the case cannot be staged")
    def test_refuses_a_directory_it_cannot_list(self):
        pycache = self.code / "trimtab" / "__pycache__"
        pycache.mkdir()
        (pycache / "cli.cpython-313.pyc").write_bytes(b"forged")
        pycache.chmod(0o111)
        self.addCleanup(pycache.chmod, 0o755)

        self.assertIn("cannot be listed", self.found()["E20"])

    def test_refuses_a_substituted_tree_object_for_the_base(self):
        # A well-formed tree, validly compressed, stored under the genuine tree's name: only the re-hash sees it.
        tree = git(self.code, "rev-parse", f"{self.base_sha}:hooks")
        objects = Path(git(self.code, "rev-parse", "--path-format=absolute", "--git-path", "objects"))
        listing = git(self.code, "ls-tree", tree).replace("guard-paths.sh", "guard-pathz.sh")
        forged = subprocess.run(["git", "-C", str(self.code), "mktree"], input=listing + "\n", text=True,
                                capture_output=True, check=True).stdout.strip()
        loose, source = objects / tree[:2] / tree[2:], objects / forged[:2] / forged[2:]
        loose.chmod(0o644)
        loose.write_bytes(source.read_bytes())

        with self.assertRaises(GateError):
            self.gather()

    def test_refuses_a_top_level_dir_that_is_a_symlink(self):
        real = self.tmp / "real-hooks"
        (self.code / "hooks").rename(real)
        (self.code / "hooks").symlink_to(real)

        self.assertIn("not a plain directory", self.found()["E20"])

    def test_refuses_a_head_it_cannot_read(self):
        head = Path(git(self.code, "rev-parse", "--path-format=absolute", "--git-path", "HEAD"))
        head.unlink()
        os.mkfifo(head)

        with self.assertRaises(GateError):
            self.gather()

    def test_refuses_a_hook_whose_mode_changed(self):
        (self.code / "hooks" / "guard-github.sh").chmod(0o644)

        self.assertCheckoutRefused()

    def test_refuses_a_missing_file(self):
        (self.code / "hooks" / "guard-paths.sh").unlink()

        self.assertCheckoutRefused()

    def test_reads_the_manifest_at_the_pinned_base_not_the_working_tree(self):
        self.write_manifest({})

        found = self.found()

        self.assertEqual([r for r, why in found.items() if "does not map" in why], [])


class JudgesLiveness(World):
    def test_refuses_an_unregistered_hook(self):
        (self.config / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": []}}))

        self.assertEqual(by_number(self.found()), HOOKED_ROWS)

    def test_refuses_a_hook_registered_for_another_matcher(self):
        command = f'"{self.config}/hooks/{{}}"'
        hooks = [{"matcher": "Write", "hooks": [{"type": "command", "command": command.format("guard-github.sh")}]},
                 {"matcher": "Edit", "hooks": [{"type": "command", "command": command.format("guard-paths.sh")}]}]
        (self.config / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": hooks}}))

        self.assertEqual(by_number(self.found()), GITHUB_ROWS)

    def test_refuses_the_home_command_form_for_a_config_dir_elsewhere(self):
        self.register(command='"$HOME/.claude/hooks/{}"')

        self.assertEqual(by_number(self.found()), HOOKED_ROWS)

    def test_accepts_the_home_command_form_when_the_config_dir_is_home(self):
        self.register(command='"$HOME/.claude/hooks/{}"')

        with mock.patch.object(gate, "_home_config", lambda: self.config):
            self.assertEqual(self.found(), {})

    def test_refuses_an_installed_hook_that_is_another_file(self):
        (self.config / "hooks" / "guard-github.sh").unlink()
        (self.config / "hooks" / "guard-github.sh").write_text("#!/bin/sh\n")
        (self.config / "hooks" / "guard-github.sh").chmod(0o755)  # executable, so only the link check refuses it

        self.assertEqual(by_number(self.found()), GITHUB_ROWS)

    def test_refuses_an_unreadable_settings_file(self):
        (self.config / "settings.json").write_text("{")

        self.assertEqual(by_number(self.found()), HOOKED_ROWS)

    def test_refuses_an_unreadable_optional_settings_file(self):
        (self.proj / ".claude" / "settings.local.json").write_text("{")

        self.assertEqual(by_number(self.found()), HOOKED_ROWS)

    def test_refuses_when_settings_disable_every_hook(self):
        self.register(extra={"disableAllHooks": True})

        self.assertEqual(by_number(self.found()), HOOKED_ROWS)

    def test_refuses_when_the_projects_local_settings_disable_every_hook(self):
        (self.proj / ".claude" / "settings.local.json").write_text(json.dumps({"disableAllHooks": True}))

        self.assertEqual(by_number(self.found()), HOOKED_ROWS)


class DigestsTheInstall(World):
    def test_a_registration_change_changes_the_digest(self):
        before = self.gather().pinned[3]
        self.register(extra={"other": 1})

        self.assertNotEqual(self.gather().pinned[3], before)

    def test_a_relinked_hook_changes_the_digest(self):
        before = self.gather().pinned[3]
        (self.config / "hooks" / "guard-github.sh").unlink()
        (self.config / "hooks" / "guard-github.sh").symlink_to(self.tmp / "elsewhere.sh")

        self.assertNotEqual(self.gather().pinned[3], before)

    def digest(self, code=None, instance_root=None):
        hooks = [manifest.Hook(**GITHUB_HOOK)]
        return gate.install_digest(gate.snapshot(code or self.code, self.config, self.proj, instance_root or self.inst, hooks))

    def test_an_edited_file_changes_the_digest(self):
        before = self.digest()
        (self.code / "hooks" / "guard-github.sh").write_text("#!/bin/sh\nexit 0\n")

        self.assertNotEqual(self.digest(), before)

    def test_a_changed_mode_changes_the_digest(self):
        before = self.digest()
        (self.code / "hooks" / "guard-github.sh").chmod(0o644)

        self.assertNotEqual(self.digest(), before)

    def test_another_head_changes_the_digest(self):
        before = self.digest()
        (self.code / "README").write_text("x\n")
        commit_all(self.code, "later")

        self.assertNotEqual(self.digest(), before)

    def test_another_instance_root_changes_the_digest(self):
        self.assertNotEqual(self.digest(instance_root=self.tmp), self.digest())

    def test_the_projects_settings_change_the_digest(self):
        before = self.digest()
        (self.proj / ".claude" / "settings.json").write_text("{}")

        self.assertNotEqual(self.digest(), before)


class ReadsOnlyWhatItJudges(World):
    def test_refuses_an_instance_that_names_no_jobs_for_this_project(self):
        self.head = self.commit_lock(self.commit_instance(projects={"other/repo": self.jobs}))

        with self.assertRaises(GateError):
            self.gather()

    def test_reads_no_job_for_a_row_this_base_owns(self):
        jobs = {**self.jobs, "E5": {"workflow": ".github/workflows/unread.yml", "job": "unread"}}
        self.head = self.commit_lock(self.commit_instance(projects={PROJECT: jobs}))

        self.assertEqual(self.found(), {})  # no answer is faked for E5's job: reading it would fail the gate

    def test_shows_no_base_row_whose_ci_ran_on_another_branch(self):
        fake = self.gh()
        fake.answers[f"repos/{BASE_REPO}/actions/runs"] = json.dumps(
            {"workflow_runs": [run(self.base_sha, gate.BASE_WORKFLOW, 1, branch="feature")]})

        self.assertEqual(by_number(self.found(fake)), BASE_ROWS)


class CachesAPass(unittest.TestCase):
    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.config)])

    def test_reads_back_the_pass_it_wrote(self):
        gate.record_pass(self.config, PROJECT, PINNED)

        self.assertTrue(gate.cached_pass(self.config, PROJECT, PINNED))

    def test_a_pass_for_any_other_key_part_is_no_pass(self):
        gate.record_pass(self.config, PROJECT, PINNED)
        others = [tuple("e" * len(v) if i == n else v for i, v in enumerate(PINNED)) for n in range(4)]

        self.assertEqual([gate.cached_pass(self.config, PROJECT, k) for k in others], [False] * 4)

    def test_clearing_removes_the_pass(self):
        gate.record_pass(self.config, PROJECT, PINNED)
        gate.clear_pass(self.config, PROJECT)

        self.assertFalse(gate.cached_pass(self.config, PROJECT, PINNED))

    def test_clearing_with_nothing_cached_is_harmless(self):
        gate.clear_pass(self.config, PROJECT)

        self.assertFalse(gate.cached_pass(self.config, PROJECT, PINNED))

    def test_no_cache_is_no_pass(self):
        self.assertFalse(gate.cached_pass(self.config, PROJECT, PINNED))

    def test_a_malformed_cache_is_no_pass(self):
        gate.record_pass(self.config, PROJECT, PINNED)
        path = self.config / guardstate.ROOT / gate.CACHE_PARTS[0] / gate.cache_name(PROJECT)
        path.write_text("{")

        self.assertFalse(gate.cached_pass(self.config, PROJECT, PINNED))

    def test_a_planted_symlink_is_no_pass(self):
        directory = guardstate.directory(self.config, *gate.CACHE_PARTS, create=True)
        real = self.config / "elsewhere.json"
        real.write_text(json.dumps(gate._body(PROJECT, PINNED)))
        (directory / gate.cache_name(PROJECT)).symlink_to(real)

        self.assertFalse(gate.cached_pass(self.config, PROJECT, PINNED))

    def test_a_directory_others_can_write_is_no_pass(self):
        gate.record_pass(self.config, PROJECT, PINNED)
        os.chmod(self.config / guardstate.ROOT, 0o777)

        self.assertFalse(gate.cached_pass(self.config, PROJECT, PINNED))


class TheCommand(World):
    def setUp(self):
        super().setUp()
        (self.inst / "rules").mkdir()

    def main(self, fake):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(tools, "gh_runner", lambda auth=None: fake), mock.patch.object(tools, "GhAuth", lambda: None), \
                mock.patch.object(tools, "Git", lambda **kw: self.git), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}), \
                mock.patch.object(cli.roots, "code_root", lambda: self.code), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["check-enforcement", "--repository", PROJECT, "--repo", str(self.proj),
                             "--instance", str(self.inst)])
        return code, out.getvalue()

    def cached(self):
        return (self.config / guardstate.ROOT).exists()

    def test_exits_0_and_writes_nothing_when_every_row_is_shown(self):
        code, out = self.main(self.gh())

        self.assertEqual((code, out.count(" shown\n"), self.cached()), (0, 23, False))

    def test_exits_1_when_a_row_is_not_shown(self):
        code, out = self.main(self.gh(failing="E1"))

        self.assertEqual((code, "E1 not shown" in out), (1, True))

    def test_exits_2_on_a_failed_read(self):
        code, _ = self.main(FakeGh(returncode=1, stderr="HTTP 500"))

        self.assertEqual(code, 2)

    def test_exits_2_on_an_unexpected_error(self):
        with mock.patch.object(gate, "decide", side_effect=RuntimeError("boom")):
            code, _ = self.main(self.gh())

        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
