"""E20: whether every row E1-E23 is shown, from facts no agent can write (ADR 0063 §2).

`decide` is pure: it takes the rows, the manifest and the facts read, and says for each row
whether it is shown and why not. `gather` reads those facts. Every input is either a git object
named by a SHA that only an admin's merge sets, or a GitHub job record:

- the lock, at the project's default-branch head, which must be where the server says it is;
- the instance file and the doctrine record, as objects in the instance checkout at the lock's
  `trimtab_sha`, the record at the merge that added it;
- the manifest, as the object at the pinned base in this checkout;
- job records: this base's `lint-test` on the pinned base, and each project row's job on the
  default-branch head;
- outside `--server`, the config dir's hooks and settings, and this checkout's state.

Every read that fails, or returns the wrong shape, raises GateError: nothing is vouched for.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from trimtab import config as lock_config
from trimtab import guardstate, instance
from trimtab.capture.sources import PER_PAGE, GhApi, Run, SourceError
from trimtab.enforcement import manifest as manifest_mod
from trimtab.enforcement import record as record_mod
from trimtab.enforcement.record import BASE, BOTH, PROJECT, Row

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")
BASE_WORKFLOW = ".github/workflows/ci.yml"
BASE_JOB = "lint-test"
ACTIONS_LIMIT = 200  # runs on one head, or jobs in one run, every attempt; past it, an error
GIT_TIMEOUT_SECONDS = 10
CACHE_PARTS = ("enforcement",)
CACHE_SCHEMA = 1


class GateError(Exception):
    """A fact cannot be read, so no row is vouched for."""


@dataclass(frozen=True)
class Facts:
    """What `gather` read; `decide` judges it."""

    rows: tuple[Row, ...]
    entries: Mapping[str, manifest_mod.Entry]
    base_ci: str | None  # why this base's lint-test on the pinned base is not a pass, or None
    live: Mapping[str, str | None] | None  # hook file -> why it is not live, or None; None in --server
    project_jobs: Mapping[str, str | None]  # row id -> why its job is not a pass, or None; only rows named
    pinned: tuple[str, str, str]  # (trimtab_sha, base_sha, default-branch head): what a pass is cached under


@dataclass(frozen=True)
class RowResult:
    id: str
    problem: str | None  # None: shown


def decide(facts: Facts) -> tuple[RowResult, ...]:
    """Each row, shown or not and why, in the record's order."""
    out = []
    for row in facts.rows:
        problems = []
        if row.side in (BASE, BOTH):
            problems += _base_problems(row, facts)
        elif row.id in facts.entries:
            problems.append("this base's manifest maps it, but the record's owner is the consuming project")
        if row.side in (PROJECT, BOTH):
            if row.id not in facts.project_jobs:
                problems.append("the instance file names no job for it for this project")
            elif facts.project_jobs[row.id]:
                problems.append(facts.project_jobs[row.id])
        out.append(RowResult(row.id, "; ".join(problems) or None))
    return tuple(out)


def _base_problems(row: Row, facts: Facts) -> list[str]:
    entry = facts.entries.get(row.id)
    problem = manifest_mod.entry_problem(row, entry)
    if problem:
        return [problem]
    problems = []
    if facts.base_ci:
        problems.append(facts.base_ci)
    if facts.live is not None:
        for hook in entry.hooks:
            why = facts.live.get(hook.file, "its liveness was not read")
            if why:
                problems.append(f"{hook.file} is not live: {why}")
    return problems


# --- reads --------------------------------------------------------------------------------

def _git(root: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise GateError(f"git {args[0]} in {root.name} failed ({type(err).__name__})") from err
    if done.returncode != 0:
        raise GateError(f"git {args[0]} in {root.name} exited {done.returncode}")
    return done.stdout


def _json(api: GhApi, path: str, what: str, query: dict | None = None):
    try:
        return api.json("GET", path, what, query=query)
    except SourceError as err:
        raise GateError(str(err)) from err


def _default_head(api: GhApi, repository: str, project: Path) -> str:
    row = _json(api, f"repos/{repository}", "gh api repository")
    default = row.get("default_branch") if isinstance(row, dict) else None
    if not isinstance(default, str) or not default:
        raise GateError("gh api repository returned unexpected JSON")
    branch = _json(api, f"repos/{repository}/branches/{default}", "gh api branch")
    commit = branch.get("commit") if isinstance(branch, dict) else None
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise GateError("gh api branch returned unexpected JSON")
    fetched = _git(project, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{default}^{{commit}}").strip()
    if fetched != sha:
        raise GateError(f"origin/{default} as fetched is not {repository}'s {default} on the server; "
                        "run `git fetch origin` and retry")
    return sha


def _lock(project: Path, head: str) -> lock_config.ProjectConfig:
    text = _git(project, "show", f"{head}:{lock_config.PATH}")
    found, problems = lock_config.parse(text)
    if found is None:
        raise GateError(f"the lock at the default-branch head: {problems[0].message if problems else 'unreadable'}")
    if not lock_config.FULL_SHA.match(found.trimtab_sha):
        raise GateError("the lock's trimtab_sha must be a full SHA for the gate")
    return found


def _record_rows(root: Path, trimtab_sha: str, path: str) -> tuple[Row, ...]:
    added = _git(root, "log", "--first-parent", "--diff-filter=A", "--format=%H", trimtab_sha, "--", path).split()
    if not added or not all(SHA.fullmatch(a) for a in added):
        raise GateError(f"the instance's history at {trimtab_sha[:12]} never added {path}")
    merge = added[-1]  # the oldest first-parent commit that added it: the merge that put it there
    try:
        return record_mod.parse(_git(root, "show", f"{merge}:{path}"))
    except record_mod.RecordError as err:
        raise GateError(f"the doctrine record as merged: {err}") from err


def _entries(code: Path, base_sha: str) -> Mapping[str, manifest_mod.Entry]:
    try:
        return manifest_mod.parse(_git(code, "show", f"{base_sha}:{manifest_mod.PATH}"))
    except manifest_mod.ManifestError as err:
        raise GateError(f"the manifest at the pinned base: {err}") from err


def _actions(api: GhApi, path: str, key: str, what: str, query: dict) -> list:
    rows: list = []
    page = 1
    while True:
        data = _json(api, path, what, {**query, "per_page": PER_PAGE, "page": page})
        batch = data.get(key) if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise GateError(f"{what} returned unexpected JSON")
        rows += batch
        if len(rows) > ACTIONS_LIMIT:
            raise GateError(f"{what} holds more than {ACTIONS_LIMIT} rows; refusing a partial list")
        if len(batch) < PER_PAGE:
            return rows
        page += 1


def job_problem(api: GhApi, repository: str, head: str, workflow: str, job: str, event: str = "push") -> str | None:
    """Why `job` of `workflow` on `head` for `event` is not a pass on every attempt, or None."""
    runs = _actions(api, f"repos/{repository}/actions/runs", "workflow_runs", "gh api actions runs",
                    {"head_sha": head, "event": event})
    mine = []
    for run in runs:
        if not isinstance(run, dict) or type(run.get("id")) is not int or not isinstance(run.get("path"), str) \
                or not isinstance(run.get("status"), str) or not isinstance(run.get("head_sha"), str):
            raise GateError("gh api actions runs returned unexpected JSON")
        if run["path"] == workflow and run["head_sha"] == head and run.get("event") == event:
            mine.append(run)
    if not mine:
        return f"no {event} run of {workflow} on {head[:12]}"
    if any(run["status"] != "completed" for run in mine):
        return f"a run of {workflow} on {head[:12]} has not finished"
    for run in mine:
        jobs = _actions(api, f"repos/{repository}/actions/runs/{run['id']}/jobs", "jobs", "gh api actions jobs",
                        {"filter": "all"})
        named = []
        for row in jobs:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise GateError("gh api actions jobs returned unexpected JSON")
            if row["name"] == job:
                named.append(row)
        if not named:
            return f"a run of {workflow} on {head[:12]} lists no {job} job"
        for row in named:
            steps = row.get("steps")
            if row.get("status") != "completed" or row.get("conclusion") != "success" \
                    or not isinstance(steps, list) or not steps or row.get("head_sha") != head:
                return f"a {job} attempt on {head[:12]} is not a completed, passing job with steps"
    return None


def _settings_registers(settings: object, config: Path, hook: manifest_mod.Hook) -> bool:
    commands = {f'"$HOME/.claude/hooks/{hook.file}"', f'"{config}/hooks/{hook.file}"'}
    entries = settings.get("hooks", {}).get(hook.event) if isinstance(settings, dict) \
        and isinstance(settings.get("hooks"), dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("matcher") != hook.matcher:
            continue
        for h in entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []:
            if isinstance(h, dict) and h.get("type") == "command" and h.get("command") in commands:
                return True
    return False


def liveness(code: Path, config: Path, base_sha: str, hooks: list[manifest_mod.Hook]) -> dict[str, str | None]:
    """For each hook file: why it is not installed from this checkout at the pinned base and registered, or None."""
    head = _git(code, "rev-parse", "HEAD").strip()
    dirty = _git(code, "status", "--porcelain", "--untracked-files=all", "--", "hooks", "trimtab").strip()
    checkout = None
    if head != base_sha:
        checkout = "this checkout is not at the pinned base"
    elif dirty:
        checkout = "this checkout's hooks/ or trimtab/ differ from the pinned base"
    try:
        settings = json.loads((config / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        settings = None
    out: dict[str, str | None] = {}
    for hook in hooks:
        why = checkout
        installed = config / "hooks" / hook.file
        if why is None and settings is None:
            why = "the config dir's settings.json cannot be read"
        if why is None and installed.resolve() != (code / "hooks" / hook.file).resolve():
            why = "the config dir's hook is not this checkout's"
        if why is None and not os.access(installed, os.X_OK):
            why = "the installed hook is not executable"
        if why is None and not _settings_registers(settings, config, hook):
            why = f"settings.json does not register it for {hook.event} {hook.matcher}"
        if out.get(hook.file) is None:
            out[hook.file] = why
    return out


def gather(repository: str, project: Path, instance_root: Path, code: Path, config: Path,
           server: bool = False, run: Run | None = None) -> Facts:
    if not REPOSITORY.fullmatch(repository) or ".." in repository:
        raise GateError(f"{repository!r} is not owner/name")
    if server:
        raise GateError("--server reads the lock's derived rows, which are added with E22's PR")
    api = GhApi(run)
    head = _default_head(api, repository, project)
    lock = _lock(project, head)
    try:
        held = instance.at_commit(instance_root, lock.trimtab_sha)
    except instance.InstanceFileError as err:
        raise GateError(f"the instance at the lock's trimtab_sha: {err}") from err
    if held.enforcement_record is None:
        raise GateError("the instance file names no enforcement.record")
    if lock.base_sha is not None and lock.base_sha != held.base_sha:
        raise GateError("the lock's base_sha is not the base the instance pins at its trimtab_sha")
    rows = _record_rows(instance_root, lock.trimtab_sha, held.enforcement_record)
    entries = _entries(code, held.base_sha)
    base_ci = job_problem(api, held.base_repo, held.base_sha, BASE_WORKFLOW, BASE_JOB) if entries else None
    if base_ci:
        base_ci = f"this base's CI on the pinned base: {base_ci}"
    hooks = [h for e in entries.values() for h in e.hooks]
    live = liveness(code, config, held.base_sha, hooks)
    named = held.enforcement_jobs.get(repository, {})
    project_jobs = {rid: job_problem(api, repository, head, ref.workflow, ref.job)
                    for rid, ref in named.items() if rid in record_mod.ROW_IDS}
    return Facts(rows, entries, base_ci, live, project_jobs, (lock.trimtab_sha, held.base_sha, head))


# --- the cache E21 reads -----------------------------------------------------------------

def cache_name(repository: str) -> str:
    return hashlib.sha256(repository.casefold().encode("utf-8")).hexdigest() + ".json"


def record_pass(config: Path, repository: str, key: tuple[str, str, str]) -> None:
    """Cache a pass under (trimtab_sha, base_sha, default-branch head); only a pass is ever written."""
    directory = guardstate.directory(config, *CACHE_PARTS, create=True)
    body = {"schema_version": CACHE_SCHEMA, "repository": repository.casefold(),
            "trimtab_sha": key[0], "base_sha": key[1], "default_head": key[2]}
    guardstate.write(directory / cache_name(repository), json.dumps(body, sort_keys=True) + "\n")


def cached_pass(config: Path, repository: str, key: tuple[str, str, str]) -> bool:
    """Whether a pass is cached for exactly this key. A missing, planted or malformed cache is no pass."""
    try:
        directory = guardstate.directory(config, *CACHE_PARTS, create=False)
        text = guardstate.read(directory / cache_name(repository))
        body = json.loads(text) if text is not None else None
    except (guardstate.GuardStateError, ValueError):
        return False
    return isinstance(body, dict) and body == {
        "schema_version": CACHE_SCHEMA, "repository": repository.casefold(),
        "trimtab_sha": key[0], "base_sha": key[1], "default_head": key[2]}
