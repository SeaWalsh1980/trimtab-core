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
from trimtab.enforcement import gate, manifest, record
from trimtab.enforcement.gate import Facts, GateError

PROJECT = "owner/project"
INSTANCE_REPO = "owner/instance"
BASE_REPO = "owner/core"
RECORD = "docs/adr/0001-the-record.md"
HOOK = {"file": "guard-github.sh", "event": "PreToolUse", "matcher": "Bash"}
ROWS = {r.id: r for r in record.parse(record_text())}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit_all(repo, message):
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def init(repo):
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")


def entry_json(rid, hooks=(HOOK,)):
    return {"row_sha256": ROWS[rid].sha256, "hooks": list(hooks), "tests": ["test_enforcement_gate.Decides.test_x"],
            "decision": {"function": "trimtab.enforcement.gate:decide", "allow": 0}}


def every_base_entry():
    rows = {}
    for rid, row in ROWS.items():
        if row.side == record.PROJECT:
            continue
        rows[rid] = entry_json(rid, hooks=(HOOK,) if "guard-github.sh" in row.hooks else ())
        if row.hooks == frozenset({"guard-paths.sh"}):
            rows[rid] = entry_json(rid, hooks=({**HOOK, "file": "guard-paths.sh", "matcher": "Edit"},))
    return rows


def run(head, path, status="completed", event="push", run_id=1):
    return {"id": run_id, "head_sha": head, "path": path, "status": status, "event": event}


def job(head, name, conclusion="success", status="completed", steps=1):
    return {"name": name, "head_sha": head, "status": status, "conclusion": conclusion,
            "steps": [{"name": "s"}] * steps}


def facts(**overrides):
    entries = manifest.parse(json.dumps({"schema_version": 1, "rows": every_base_entry()}))
    hooks = {h.file for e in entries.values() for h in e.hooks}
    base = dict(rows=tuple(ROWS.values()), entries=entries, base_ci=None, live={h: None for h in hooks},
                project_jobs={rid: None for rid, r in ROWS.items() if r.side != record.BASE},
                pinned=("a" * 40, "b" * 40, "c" * 40))
    base.update(overrides)
    return Facts(**base)


def problems(results):
    return {r.id: r.problem for r in results if r.problem}


class Decides(unittest.TestCase):
    def test_x(self):
        """A stand-in test id the manifests in this file name."""

    def test_shows_every_row_when_every_fact_holds(self):
        self.assertEqual(problems(gate.decide(facts())), {})

    def test_shows_no_base_row_while_this_bases_ci_failed(self):
        found = problems(gate.decide(facts(base_ci="this base's CI failed")))

        self.assertEqual(sorted(found, key=lambda r: int(r[1:])),
                         [r for r, row in ROWS.items() if row.side != record.PROJECT])

    def test_shows_no_row_whose_hook_is_not_live(self):
        found = problems(gate.decide(facts(live={"guard-github.sh": "not linked", "guard-paths.sh": None})))

        self.assertIn("E14", found)

    def test_skips_liveness_on_the_server(self):
        self.assertNotIn("E14", problems(gate.decide(facts(live=None))))

    def test_shows_no_base_row_the_manifest_leaves_out(self):
        entries = dict(facts().entries)
        del entries["E9"]

        self.assertIn("does not map", problems(gate.decide(facts(entries=entries)))["E9"])

    def test_shows_no_project_row_the_instance_names_no_job_for(self):
        jobs = {rid: None for rid in ("E2", "E3", "E4", "E22")}

        self.assertIn("names no job", problems(gate.decide(facts(project_jobs=jobs)))["E1"])

    def test_shows_no_project_row_whose_job_failed(self):
        jobs = {**facts().project_jobs, "E3": "a job failed"}

        self.assertEqual(problems(gate.decide(facts(project_jobs=jobs))), {"E3": "a job failed"})

    def test_needs_both_sides_for_e22(self):
        entries = dict(facts().entries)
        del entries["E22"]

        self.assertIn("E22", problems(gate.decide(facts(entries=entries))))


class ReadsJobRecords(unittest.TestCase):
    HEAD = "d" * 40
    WORKFLOW = ".github/workflows/ci.yml"

    def problem(self, runs, jobs):
        fake = FakeGh({f"repos/{BASE_REPO}/actions/runs": json.dumps({"workflow_runs": runs}),
                       **{f"repos/{BASE_REPO}/actions/runs/{rid}/jobs": json.dumps({"jobs": j}) for rid, j in jobs.items()}})
        return gate.job_problem(gate.GhApi(fake), BASE_REPO, self.HEAD, self.WORKFLOW, "lint-test")

    def test_passes_when_every_attempt_passed(self):
        jobs = {1: [job(self.HEAD, "lint-test"), job(self.HEAD, "lint-test"), job(self.HEAD, "other", "failure")]}

        self.assertIsNone(self.problem([run(self.HEAD, self.WORKFLOW)], jobs))

    def test_refuses_when_one_attempt_failed(self):
        jobs = {1: [job(self.HEAD, "lint-test"), job(self.HEAD, "lint-test", "failure")]}

        self.assertIn("not a completed, passing", self.problem([run(self.HEAD, self.WORKFLOW)], jobs))

    def test_refuses_with_no_run_of_the_workflow(self):
        self.assertIn("no push run", self.problem([run(self.HEAD, ".github/workflows/other.yml")], {}))

    def test_refuses_a_run_on_another_event(self):
        self.assertIn("no push run", self.problem([run(self.HEAD, self.WORKFLOW, event="pull_request")], {}))

    def test_refuses_an_unfinished_run(self):
        self.assertIn("not finished", self.problem([run(self.HEAD, self.WORKFLOW, status="in_progress")], {}))

    def test_refuses_a_run_with_no_such_job(self):
        self.assertIn("lists no", self.problem([run(self.HEAD, self.WORKFLOW)], {1: [job(self.HEAD, "other")]}))

    def test_refuses_a_job_with_no_steps(self):
        self.assertIn("with steps", self.problem([run(self.HEAD, self.WORKFLOW)], {1: [job(self.HEAD, "lint-test", steps=0)]}))

    def test_raises_on_unexpected_json(self):
        with self.assertRaises(GateError):
            self.problem([{"id": "x"}], {})

    def test_raises_past_its_bound(self):
        runs = [run(self.HEAD, self.WORKFLOW, run_id=n) for n in range(1, 100)]
        fake = FakeGh({f"repos/{BASE_REPO}/actions/runs": [json.dumps({"workflow_runs": runs})] * 5})

        with self.assertRaises(GateError):
            gate.job_problem(gate.GhApi(fake), BASE_REPO, self.HEAD, self.WORKFLOW, "lint-test")

    def test_raises_on_a_failed_read(self):
        fake = FakeGh(returncode=1, stderr="HTTP 403")

        with self.assertRaises(GateError):
            gate.job_problem(gate.GhApi(fake), BASE_REPO, self.HEAD, self.WORKFLOW, "lint-test")


class World(unittest.TestCase):
    """Three repositories (project, instance, this base's checkout) and a config dir, all real."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        isolate_git(self, self.tmp)
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
        git(self.inst, "checkout", "-q", "-b", "add-record")
        (self.inst / "docs" / "adr").mkdir(parents=True)
        (self.inst / RECORD).write_text(record_text())
        commit_all(self.inst, "add the record")
        git(self.inst, "checkout", "-q", "main")
        git(self.inst, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "merge", "-q", "--no-ff",
            "-m", "merge", "add-record")
        self.jobs = {rid: {"workflow": f".github/workflows/{rid.lower()}.yml", "job": rid.lower()}
                     for rid, r in ROWS.items() if r.side != record.BASE}
        self.trimtab_sha = self.commit_instance()
        init(self.proj)
        self.head = self.commit_lock(self.trimtab_sha)
        self.config.mkdir()
        (self.config / "hooks").mkdir()
        for name in ("guard-github.sh", "guard-paths.sh"):
            (self.config / "hooks" / name).symlink_to(self.code / "hooks" / name)
        self.register()

    def write_manifest(self, rows):
        path = self.code / manifest.PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "rows": rows}))

    def commit_instance(self, **extra):
        body = {"schema_version": 1, "repo": INSTANCE_REPO, "base": {"repo": BASE_REPO, "sha": self.base_sha},
                "enforcement": {"record": RECORD, "projects": {PROJECT: self.jobs}}, **extra}
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

    def register(self, matcher_for=lambda h: h["matcher"]):
        hooks = [{"matcher": "Bash", "hooks": [{"type": "command", "command": '"$HOME/.claude/hooks/guard-github.sh"'}]},
                 {"matcher": "Edit", "hooks": [{"type": "command", "command": '"$HOME/.claude/hooks/guard-paths.sh"'}]}]
        (self.config / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": hooks}}))

    def gh(self, server_head=None, base_jobs=None, project_job=None):
        answers = {f"repos/{PROJECT}": json.dumps({"default_branch": "main"}),
                   f"repos/{PROJECT}/branches/main": json.dumps({"commit": {"sha": server_head or self.head}}),
                   f"repos/{BASE_REPO}/actions/runs":
                       json.dumps({"workflow_runs": [run(self.base_sha, gate.BASE_WORKFLOW, run_id=1)]}),
                   f"repos/{BASE_REPO}/actions/runs/1/jobs":
                       json.dumps({"jobs": base_jobs or [job(self.base_sha, "lint-test")]})}
        runs = [run(self.head, ref["workflow"], run_id=100 + n) for n, ref in enumerate(self.jobs.values())]
        answers[f"repos/{PROJECT}/actions/runs"] = json.dumps({"workflow_runs": runs})
        for n, (rid, ref) in enumerate(self.jobs.items()):
            conclusion = "failure" if project_job == rid else "success"
            answers[f"repos/{PROJECT}/actions/runs/{100 + n}/jobs"] = json.dumps(
                {"jobs": [job(self.head, ref["job"], conclusion)]})
        return FakeGh(answers)

    def gather(self, fake=None, **kwargs):
        return gate.gather(PROJECT, self.proj, self.inst, self.code, self.config, run=fake or self.gh(), **kwargs)


class Gathers(World):
    def test_shows_every_row_on_a_complete_install(self):
        self.assertEqual(problems(gate.decide(self.gather())), {})

    def test_reads_the_lock_at_the_default_branch_head_not_the_working_tree(self):
        (self.proj / ".claude" / "trimtab.json").write_text(json.dumps(
            {"trimtab_sha": "f" * 40, "id_prefix": "CT", "schema_version": 2}))

        self.assertEqual(self.gather().pinned[0], self.trimtab_sha)

    def test_refuses_a_fetch_that_is_not_the_servers_head(self):
        with self.assertRaises(GateError):
            self.gather(self.gh(server_head="e" * 40))

    def test_reads_the_record_as_merged_not_as_edited_later(self):
        lines = "".join(row_line(r, condition="loosened" if r == "E7" else None) for r in OWNERS)
        (self.inst / RECORD).write_text(record_text(lines=lines))
        self.head = self.commit_lock(commit_all(self.inst, "edit the record"))

        self.assertEqual(problems(gate.decide(self.gather())), {})

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

    def test_reads_the_manifest_at_the_pinned_base_not_the_working_tree(self):
        self.write_manifest({})

        found = problems(gate.decide(self.gather()))

        # Still mapped from the pinned base; the edited tree only makes the hooks not live.
        self.assertEqual([r for r, why in found.items() if "does not map" in why or "differ" not in why], [])

    def test_shows_no_base_row_when_this_bases_ci_failed(self):
        found = problems(gate.decide(self.gather(self.gh(base_jobs=[job(self.base_sha, "lint-test", "failure")]))))

        self.assertIn("this base's CI", found["E14"])

    def test_shows_no_project_row_whose_job_failed(self):
        self.assertEqual(list(problems(gate.decide(self.gather(self.gh(project_job="E2"))))), ["E2"])

    def test_shows_no_base_row_on_a_checkout_off_the_pinned_base(self):
        (self.code / "hooks" / "guard-github.sh").write_text("#!/bin/sh\nexit 0\n")

        self.assertIn("differ from the pinned base", problems(gate.decide(self.gather()))["E14"])

    def test_shows_no_row_whose_hook_is_not_registered(self):
        (self.config / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": []}}))

        self.assertIn("does not register", problems(gate.decide(self.gather()))["E14"])

    def test_shows_no_row_whose_installed_hook_is_another_file(self):
        (self.config / "hooks" / "guard-github.sh").unlink()
        (self.config / "hooks" / "guard-github.sh").write_text("#!/bin/sh\n")

        self.assertIn("not this checkout's", problems(gate.decide(self.gather()))["E14"])

    def test_server_mode_fails_until_e22_lands(self):
        with self.assertRaises(GateError):
            self.gather(server=True)


class CachesAPass(unittest.TestCase):
    PINNED = ("a" * 40, "b" * 40, "c" * 40)

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.config)])

    def test_reads_back_the_pass_it_wrote(self):
        gate.record_pass(self.config, PROJECT, self.PINNED)

        self.assertTrue(gate.cached_pass(self.config, PROJECT, self.PINNED))

    def test_a_pass_for_another_default_head_is_no_pass(self):
        gate.record_pass(self.config, PROJECT, self.PINNED)

        self.assertFalse(gate.cached_pass(self.config, PROJECT, (*self.PINNED[:2], "d" * 40)))

    def test_no_cache_is_no_pass(self):
        self.assertFalse(gate.cached_pass(self.config, PROJECT, self.PINNED))

    def test_a_planted_symlink_is_no_pass(self):
        directory = guardstate.directory(self.config, *gate.CACHE_PARTS, create=True)
        real = self.config / "elsewhere.json"
        real.write_text("{}")
        (directory / gate.cache_name(PROJECT)).symlink_to(real)

        self.assertFalse(gate.cached_pass(self.config, PROJECT, self.PINNED))

    def test_a_directory_others_can_write_is_no_pass(self):
        gate.record_pass(self.config, PROJECT, self.PINNED)
        os.chmod(self.config / guardstate.ROOT, 0o777)

        self.assertFalse(gate.cached_pass(self.config, PROJECT, self.PINNED))


class TheCommand(World):
    def main(self, fake):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("trimtab.capture.sources._subprocess_run", fake), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}), \
                mock.patch.object(cli.roots, "code_root", lambda: self.code), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["check-enforcement", "--repository", PROJECT, "--repo", str(self.proj),
                             "--instance", str(self.inst)])
        return code, out.getvalue()

    def setUp(self):
        super().setUp()
        (self.inst / "rules").mkdir()

    def test_exits_0_and_caches_when_every_row_is_shown(self):
        code, out = self.main(self.gh())

        self.assertEqual((code, out.count(" shown\n"), gate.cached_pass(self.config, PROJECT,
                                                                       (self.trimtab_sha, self.base_sha, self.head))),
                         (0, 23, True))

    def test_exits_1_and_caches_nothing_when_a_row_is_not_shown(self):
        code, out = self.main(self.gh(project_job="E1"))

        self.assertEqual((code, "E1 not shown" in out, gate.cached_pass(
            self.config, PROJECT, (self.trimtab_sha, self.base_sha, self.head))), (1, True, False))

    def test_exits_2_on_a_failed_read(self):
        code, _ = self.main(FakeGh(returncode=1, stderr="HTTP 500"))

        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
