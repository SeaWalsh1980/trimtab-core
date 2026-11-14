"""E22: `check-enforcement --server` shows each row from the lock's derived field, with no instance (ADR 0063 §7).

Real git in temporary repositories; gh, a true external system, faked (TST-2)."""

import dataclasses
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from enforcement_fixture import OWNERS, record_text, row_line
from gh_fake import FakeGh
from test_enforcement_gate import (BASE_ROWS, BASE_REPO, PROJECT, PROJECT_ROWS, RECORD, World, by_number, commit_all,
                                   git, problems)
from trimtab import cli, config as project_config, guardstate
from trimtab.enforcement import gate, tools
from trimtab.enforcement.gate import GateError

NO_INSTANCE = Path("/nonexistent-instance")


class ServerWorld(World):
    def derived(self):
        return gate.derive_lock_enforcement(self.inst, self.trimtab_sha, PROJECT, git=self.git)

    def commit_server_lock(self, field="derived", base_sha=None):
        lock = {"trimtab_sha": self.trimtab_sha, "id_prefix": "CT", "schema_version": 2,
                "base_sha": base_sha or self.base_sha}
        if field is not None:
            lock["enforcement"] = self.derived() if field == "derived" else field
        (self.proj / ".claude" / "trimtab.json").write_text(json.dumps(lock))
        self.head = commit_all(self.proj, "server lock")
        git(self.proj, "update-ref", "refs/remotes/origin/main", self.head)
        return self.head

    def server(self, fake=None):
        return gate.gather(PROJECT, self.proj, NO_INSTANCE, self.code, self.config, run=fake or self.gh(),
                           git=self.git, server=True)

    def shown(self, fake=None):
        return problems(gate.decide(self.server(fake)))


class ServerGathers(ServerWorld):
    def setUp(self):
        super().setUp()
        self.commit_server_lock()

    def test_shows_every_row_from_the_lock_with_no_instance_checkout(self):
        self.assertEqual(self.shown(), {})

    def test_refuses_a_lock_with_no_derived_field(self):
        self.commit_server_lock(field=None)

        with self.assertRaises(GateError):
            self.server()

    def test_refuses_a_lock_with_a_field_but_no_base_sha(self):
        lock = {"trimtab_sha": self.trimtab_sha, "id_prefix": "CT", "schema_version": 2, "enforcement": self.derived()}
        (self.proj / ".claude" / "trimtab.json").write_text(json.dumps(lock))
        self.head = commit_all(self.proj, "no base")
        git(self.proj, "update-ref", "refs/remotes/origin/main", self.head)

        with self.assertRaises(GateError):
            self.server()

    def test_refuses_a_checkout_that_is_not_the_locks_base(self):
        (self.code / "stray").write_text("x\n")
        commit_all(self.code, "a commit past the pinned base")

        with self.assertRaises(GateError):
            self.server()

    def test_refuses_a_fetch_that_is_not_the_servers_head(self):
        with self.assertRaises(GateError):
            self.server(self.gh(server_head="e" * 40))

    def test_shows_no_base_row_when_the_bases_ci_failed_on_the_locks_base(self):
        self.assertEqual(by_number(self.shown(self.gh(base_job="failure"))), BASE_ROWS)

    def test_reads_the_base_repository_from_the_lock_not_a_default(self):
        field = self.derived()
        field["base_repo"] = "other/core"

        self.commit_server_lock(field=field)

        with self.assertRaises(GateError):  # nothing is answered for other/core, so the read fails
            self.server()

    def test_shows_no_project_row_whose_job_failed(self):
        self.assertEqual(list(self.shown(self.gh(failing="E2"))), ["E2"])

    def test_e22s_own_project_half_is_the_running_job_so_it_needs_no_push_run(self):
        # plan-gate runs on pull_request only, so it never has a push run on the default branch to be judged by.
        self.assertEqual(self.shown(self.gh(failing="E22")), {})

    def test_e22s_project_half_needs_no_job_in_the_field_either(self):
        field = self.derived()
        field["jobs"].pop("E22", None)
        self.commit_server_lock(field=field)

        self.assertEqual(self.shown(), {})

    def test_e22s_base_half_is_still_judged_in_server_mode(self):
        self.assertIn("E22", self.shown(self.gh(base_job="failure")))

    def test_e22_is_not_shown_without_its_manifest_entry_in_server_mode(self):
        entries = dict(self.server().entries)
        del entries["E22"]

        results = gate.decide(dataclasses.replace(self.server(), entries=entries))

        self.assertEqual([r.id for r in results if r.problem], ["E22"])

    def test_the_exemption_is_e22s_alone(self):
        self.assertEqual(list(self.shown(self.gh(failing="E21"))), [])  # E21 is a base row: no project job to fail
        self.assertEqual(list(self.shown(self.gh(failing="E4"))), ["E4"])

    def test_shows_no_project_row_the_field_names_no_job_for(self):
        field = self.derived()
        del field["jobs"]["E3"]
        self.commit_server_lock(field=field)

        self.assertEqual(list(self.shown()), ["E3"])

    def test_shows_no_base_row_whose_text_the_lock_records_differently(self):
        field = self.derived()
        field["rows"]["E7"]["sha256"] = "0" * 64
        self.commit_server_lock(field=field)

        self.assertEqual(list(self.shown()), ["E7"])

    def test_judges_a_rows_owner_by_the_lock_not_the_manifest(self):
        field = self.derived()
        field["rows"]["E7"]["owner"] = "Consuming project"
        self.commit_server_lock(field=field)

        self.assertIn("E7", self.shown())


class ServerInstance(ServerWorld):
    """The instance's plan-run keys as the lock holds them, for `plan-ready-check --server`."""

    KEYS = {"accounts": {"admin": "the-admin", "agent": "the-agent"},
            "secret_scan": {"workflow": ".github/workflows/scan.yml", "job": "scan"}}

    def setUp(self):
        super().setUp()
        body = json.loads((self.inst / "instance.json").read_text())
        (self.inst / "instance.json").write_text(json.dumps({**body, **self.KEYS}))
        self.trimtab_sha = commit_all(self.inst, "plan keys")
        self.commit_server_lock()

    def held(self, fake=None):
        return gate.server_instance(PROJECT, self.proj, self.code, run=fake or self.gh(), git=self.git)

    def test_reads_the_agent_and_scan_job_from_the_lock_with_no_instance(self):
        found = self.held()

        self.assertEqual((found.agent, found.secret_scan.job), ("the-agent", "scan"))

    def test_names_the_project_and_the_pinned_base(self):
        found = self.held()

        self.assertEqual((found.repo, found.base_repo, found.base_sha), (PROJECT, BASE_REPO, self.base_sha))

    def test_refuses_a_lock_with_no_derived_field(self):
        self.commit_server_lock(field=None)

        with self.assertRaises(GateError):
            self.held()

    def test_refuses_a_checkout_that_is_not_the_locks_base(self):
        (self.code / "stray").write_text("x\n")
        commit_all(self.code, "past the pinned base")

        with self.assertRaises(GateError):
            self.held()

    def test_refuses_a_fetch_that_is_not_the_servers_head(self):
        with self.assertRaises(GateError):
            self.held(self.gh(server_head="e" * 40))


class NonServerChecksTheField(ServerWorld):
    def test_accepts_a_lock_whose_field_is_what_the_instance_gives(self):
        self.commit_server_lock()

        self.assertEqual(self.found(), {})

    def test_refuses_a_lock_whose_field_the_instance_does_not_give(self):
        field = self.derived()
        field["rows"]["E7"]["sha256"] = "0" * 64
        self.commit_server_lock(field=field)

        with self.assertRaises(GateError):
            self.gather()

    def test_a_lock_without_the_field_still_passes_outside_server(self):
        self.assertEqual(self.found(), {})


class ServerCommand(ServerWorld):
    def setUp(self):
        super().setUp()
        self.commit_server_lock()

    def main(self, fake, *extra):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(tools, "gh_runner", lambda auth=None: fake), mock.patch.object(tools, "GhAuth", lambda: None), \
                mock.patch.object(tools, "Git", lambda **kw: self.git), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}), \
                mock.patch.object(cli.roots, "code_root", lambda: self.code), \
                mock.patch.dict(os.environ, {"TRIMTAB_INSTANCE": ""}), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["check-enforcement", "--server", "--repository", PROJECT, "--repo", str(self.proj), *extra])
        return code, out.getvalue(), err.getvalue()

    def test_exits_0_with_no_instance_given_and_writes_no_pass(self):
        code, out, _ = self.main(self.gh())

        self.assertEqual((code, out.count(" shown\n"), (self.config / guardstate.ROOT).exists()), (0, 23, False))

    def test_exits_1_when_a_project_job_failed(self):
        code, out, _ = self.main(self.gh(failing="E1"))

        self.assertEqual((code, "E1 not shown" in out), (1, True))

    def test_exits_2_when_the_lock_has_no_field(self):
        self.commit_server_lock(field=None)

        code, _, err = self.main(self.gh())

        self.assertEqual(code, 2)
        self.assertIn("nothing is vouched for", err)


if __name__ == "__main__":
    unittest.main()
