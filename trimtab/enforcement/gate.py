"""E20: whether every row E1-E23 is shown, from facts no agent can write (ADR 0063 §2).

`decide` is pure: it takes the rows, the manifest and the facts read, and says for each row
whether it is shown and why not. `gather` reads those facts. Every input is a git object named by
a SHA that only an admin's merge sets, a GitHub job record, or (outside `--server`) the install's
own state:

- the lock, at the project's default-branch head, which must be where the server says it is;
- the instance file and the doctrine record, as objects in the instance checkout at the lock's
  `trimtab_sha`, the record at the latest first-parent commit that added it;
- the manifest, as the object at the pinned base in this checkout;
- job records, through one memoised reader: this base's `lint-test` on the pinned base, and each
  project row's job on the default-branch head, for the default branch;
- outside `--server`: this checkout's files against the pinned base's tree, and the config dir's
  hooks and settings.

git and gh are the system's, run with a built environment and replace objects and grafts refused
(`tools`). Every read that fails, or returns the wrong shape, raises GateError: nothing is vouched
for. Only the session-mode hook writes the pass cache (`trimtab.adapters.enforcement_record`); a
command run in a session only reports.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from trimtab import actions, guardstate, instance
from trimtab import config as lock_config
from trimtab.capture.sources import GhApi, Run, SourceError
from trimtab.enforcement import manifest as manifest_mod
from trimtab.enforcement import record as record_mod
from trimtab.enforcement import tools
from trimtab.enforcement.record import BASE, BOTH, PROJECT, Row

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")
BASE_WORKFLOW = ".github/workflows/ci.yml"
BASE_JOB = "lint-test"
BASE_BRANCH = "main"
CHECKED_DIRS = ("hooks", "trimtab")
CACHE_PARTS = ("enforcement",)
CACHE_SCHEMA = 2
SETTINGS_FILES = ("settings.json", "settings.local.json")


class GateError(Exception):
    """A fact cannot be read, so no row is vouched for."""


@dataclass(frozen=True)
class Facts:
    """What `gather` read; `decide` judges it."""

    rows: tuple[Row, ...]
    entries: Mapping[str, manifest_mod.Entry]
    base_ci: str | None  # why this base's lint-test on the pinned base is not a pass, or None
    checkout: str | None  # why this checkout is not the pinned base, or None; None in --server
    live: Mapping[str, str | None] | None  # hook file -> why it is not live, or None; None in --server
    project_jobs: Mapping[str, str | None]  # row id -> why its job is not a pass, or None; only rows named
    pinned: tuple[str, str, str, str]  # (trimtab_sha, base_sha, default head, install digest): the cache key


@dataclass(frozen=True)
class RowResult:
    id: str
    problem: str | None  # None: shown


def decide(facts: Facts) -> tuple[RowResult, ...]:
    """Each row, shown or not and why, in the record's order."""
    out = []
    for row in facts.rows:
        problems = []
        problem = manifest_mod.entry_problem(row, facts.entries.get(row.id))
        if problem:
            problems.append(problem)
        elif row.side in (BASE, BOTH):
            problems += _base_problems(facts.entries[row.id], facts)
        if row.side in (PROJECT, BOTH):
            if row.id not in facts.project_jobs:
                problems.append("the instance file names no job for it for this project")
            elif facts.project_jobs[row.id]:
                problems.append(facts.project_jobs[row.id])
        out.append(RowResult(row.id, "; ".join(problems) or None))
    return tuple(out)


def _base_problems(entry: manifest_mod.Entry, facts: Facts) -> list[str]:
    problems = []
    if facts.base_ci:
        problems.append(facts.base_ci)
    if facts.checkout:
        problems.append(facts.checkout)  # every base row, hooked or not: the code that shows it is this checkout
    if facts.live is not None:
        for hook in entry.hooks:
            why = facts.live.get(hook.file, "its liveness was not read")
            if why:
                problems.append(f"{hook.file} is not live: {why}")
    return problems


# --- reads --------------------------------------------------------------------------------

class _Reads:
    """The hardened tools for one gather, with every failure as GateError."""

    def __init__(self, run: Run | None, git: tools.Git | None):
        try:
            self.git = git or tools.Git()
            self.api = GhApi(run or tools.gh_runner())
        except tools.ToolError as err:
            raise GateError(str(err)) from err
        self.jobs = actions.Reader(self.api)

    def text(self, repo: Path, *args: str) -> str:
        try:
            return self.git(repo, *args)
        except tools.ToolError as err:
            raise GateError(str(err)) from err

    def raw(self, repo: Path, *args: str) -> bytes:
        try:
            return self.git.bytes(repo, *args)
        except tools.ToolError as err:
            raise GateError(str(err)) from err

    def json(self, path: str, what: str):
        try:
            return self.api.json("GET", path, what)
        except (SourceError, tools.ToolError) as err:
            raise GateError(str(err)) from err

    def job_problem(self, *args, **kwargs) -> str | None:
        try:
            return self.jobs.job_problem(*args, **kwargs)
        except (actions.ActionsError, tools.ToolError) as err:
            raise GateError(str(err)) from err


def _default_head(reads: _Reads, repository: str, project: Path, fetch: bool) -> tuple[str, str]:
    row = reads.json(f"repos/{repository}", "gh api repository")
    default = row.get("default_branch") if isinstance(row, dict) else None
    if not isinstance(default, str) or not default or default.startswith("-") or ".." in default:
        raise GateError("gh api repository returned unexpected JSON")
    if fetch:
        try:
            reads.git.fetch(project, default)
        except tools.ToolError as err:
            raise GateError(str(err)) from err
    branch = reads.json(f"repos/{repository}/branches/{default}", "gh api branch")
    commit = branch.get("commit") if isinstance(branch, dict) else None
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise GateError("gh api branch returned unexpected JSON")
    fetched = reads.text(project, "rev-parse", "--verify", "--end-of-options",
                         f"refs/remotes/origin/{default}^{{commit}}").strip()
    if fetched != sha:
        raise GateError(f"origin/{default} as fetched is not {repository}'s {default} on the server; "
                        "run `git fetch origin` and retry")
    return default, sha


def _lock(reads: _Reads, project: Path, head: str) -> lock_config.ProjectConfig:
    found, problems = lock_config.parse(reads.text(project, "show", f"{head}:{lock_config.PATH}"))
    if found is None:
        raise GateError(f"the lock at the default-branch head: {problems[0].message if problems else 'unreadable'}")
    if not lock_config.FULL_SHA.match(found.trimtab_sha):
        raise GateError("the lock's trimtab_sha must be a full SHA for the gate")
    return found


def _instance(reads: _Reads, root: Path, sha: str) -> instance.InstanceFile:
    try:
        return instance.parse(json.loads(reads.text(root, "show", f"{sha}:{instance.FILE}")))
    except (ValueError, instance.InstanceFileError) as err:
        raise GateError(f"the instance file at the lock's trimtab_sha: {err}") from err


def _record_rows(reads: _Reads, root: Path, trimtab_sha: str, path: str) -> tuple[Row, ...]:
    added = reads.text(root, "log", "--first-parent", "--diff-filter=A", "--format=%H", trimtab_sha,
                       "--", path).split()
    if not added or not all(SHA.fullmatch(a) for a in added):
        raise GateError(f"the instance's history at {trimtab_sha[:12]} never added {path}")
    # The latest first-parent commit that added it: the merge that put the current file there.
    try:
        return record_mod.parse(reads.text(root, "show", f"{added[0]}:{path}"))
    except record_mod.RecordError as err:
        raise GateError(f"the doctrine record as merged: {err}") from err


def _entries(reads: _Reads, code: Path, base_sha: str) -> Mapping[str, manifest_mod.Entry]:
    try:
        return manifest_mod.parse(reads.text(code, "show", f"{base_sha}:{manifest_mod.PATH}"))
    except manifest_mod.ManifestError as err:
        raise GateError(f"the manifest at the pinned base: {err}") from err


def checkout_problem(reads: _Reads, code: Path, base_sha: str) -> str | None:
    """Why this checkout's hooks/ and trimtab/ are not byte for byte the pinned base's, or None.

    Every file is hashed, so no index flag (skip-worktree, assume-unchanged), stat trick or ignore
    rule can hide an edit, and any file the base's tree lacks (an ignored one, a `__pycache__`
    a module would load) is refused."""
    if reads.text(code, "rev-parse", "--verify", "HEAD^{commit}").strip() != base_sha:
        return "this checkout is not at the pinned base"
    want: dict[str, tuple[str, str]] = {}
    for entry in reads.raw(code, "ls-tree", "-r", "-z", "--full-tree", base_sha, "--", *CHECKED_DIRS).split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        mode, kind, sha = meta.decode("ascii").split(" ")
        if kind != "blob" or mode not in ("100644", "100755"):
            return "the pinned base holds an entry under hooks/ or trimtab/ that is not a plain file"
        want[path.decode("utf-8")] = (mode, sha)
    have: list[str] = []
    for top in CHECKED_DIRS:
        for directory, dirs, files in os.walk(code / top, followlinks=False):
            for name in dirs + files:
                full = Path(directory) / name
                st = os.lstat(full)
                if stat.S_ISLNK(st.st_mode):
                    return "this checkout holds a symlink under hooks/ or trimtab/"
                if stat.S_ISREG(st.st_mode):
                    have.append(full.relative_to(code).as_posix())
                elif not stat.S_ISDIR(st.st_mode):
                    return "this checkout holds a special file under hooks/ or trimtab/"
    extra = sorted(set(have) - set(want))
    if extra:
        return f"this checkout holds {len(extra)} file(s) the pinned base does not, under hooks/ or trimtab/"
    if set(want) - set(have):
        return "this checkout is missing files the pinned base holds under hooks/ or trimtab/"
    if any("\n" in p for p in have):
        return "this checkout holds a path with a line break"
    paths = sorted(have)
    hashed = reads.text(code, "hash-object", "--no-filters", "--", *paths).split() if paths else []
    for path, sha in zip(paths, hashed):
        mode, expected = want[path]
        executable = bool(os.lstat(code / path).st_mode & stat.S_IXUSR)
        if sha != expected or executable != (mode == "100755"):
            return "this checkout's hooks/ or trimtab/ differ from the pinned base"
    return None


UNREADABLE = object()


def _settings(directory: Path, required: str | None) -> list[object]:
    """Each settings file's JSON; {} for an absent optional one; UNREADABLE for a bad or absent required one."""
    out: list[object] = []
    for name in SETTINGS_FILES:
        try:
            out.append(json.loads((directory / name).read_text(encoding="utf-8")))
        except FileNotFoundError:
            out.append(UNREADABLE if name == required else {})
        except (OSError, ValueError, UnicodeDecodeError):
            out.append(UNREADABLE)
    return out


def _registers(settings: object, config: Path, hook: manifest_mod.Hook) -> bool:
    commands = {f'"$HOME/.claude/hooks/{hook.file}"', f'"{config}/hooks/{hook.file}"'}
    table = settings.get("hooks") if isinstance(settings, dict) else None
    entries = table.get(hook.event) if isinstance(table, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("matcher") != hook.matcher:
            continue
        for h in entry.get("hooks") if isinstance(entry.get("hooks"), list) else []:
            if isinstance(h, dict) and h.get("type") == "command" and h.get("command") in commands:
                return True
    return False


def liveness(code: Path, config: Path, project: Path, hooks: list[manifest_mod.Hook]) -> dict[str, str | None]:
    """For each hook file: why it is not installed from this checkout, registered and enabled, or None."""
    user = _settings(config, required="settings.json")
    local = _settings(project / ".claude", required=None)
    disabled = any(isinstance(s, dict) and s.get("disableAllHooks") is True for s in user + local)
    out: dict[str, str | None] = {}
    for hook in hooks:
        installed = config / "hooks" / hook.file
        why = None
        if any(s is UNREADABLE for s in user + local):
            why = "a settings file cannot be read"
        elif disabled:
            why = "a settings file sets disableAllHooks"
        elif installed.resolve() != (code / "hooks" / hook.file).resolve():
            why = "the config dir's hook is not this checkout's"
        elif not os.access(installed, os.X_OK):
            why = "the installed hook is not executable"
        elif not _registers(user[0], config, hook):
            why = f"settings.json does not register it for {hook.event} {hook.matcher}"
        if out.get(hook.file) is None:
            out[hook.file] = why
    return out


def install_digest(code: Path, config: Path, project: Path, instance_root: Path, hooks) -> str:
    """A digest of the install state a pass rests on and a guard can re-read cheaply: the settings
    files, where each hook link points, and the code and instance roots. A change to any voids a pass."""
    h = hashlib.sha256()
    for directory in (config, project / ".claude"):
        for name in SETTINGS_FILES:
            try:
                h.update((directory / name).read_bytes())
            except FileNotFoundError:
                h.update(b"\0absent")
            h.update(b"\0")
    for file in sorted({hk.file for hk in hooks}):
        h.update(str((config / "hooks" / file).resolve()).encode("utf-8") + b"\0")
    for root in (code, instance_root):
        h.update(str(root.resolve()).encode("utf-8") + b"\0")
    return h.hexdigest()


def gather(repository: str, project: Path, instance_root: Path, code: Path, config: Path,
           server: bool = False, run: Run | None = None, git: tools.Git | None = None,
           fetch: bool = False) -> Facts:
    if not REPOSITORY.fullmatch(repository) or ".." in repository:
        raise GateError(f"{repository!r} is not owner/name")
    if server:
        raise GateError("--server reads the lock's derived rows, which are added with E22's PR")
    reads = _Reads(run, git)
    default, head = _default_head(reads, repository, project, fetch)
    lock = _lock(reads, project, head)
    held = _instance(reads, instance_root, lock.trimtab_sha)
    if held.enforcement_record is None:
        raise GateError("the instance file names no enforcement.record")
    if lock.base_sha is not None and lock.base_sha != held.base_sha:
        raise GateError("the lock's base_sha is not the base the instance pins at its trimtab_sha")
    rows = _record_rows(reads, instance_root, lock.trimtab_sha, held.enforcement_record)
    entries = _entries(reads, code, held.base_sha)
    base_ci = reads.job_problem(held.base_repo, held.base_sha, BASE_WORKFLOW, BASE_JOB,
                                branch=BASE_BRANCH) if entries else None
    if base_ci:
        base_ci = f"this base's CI on the pinned base: {base_ci}"
    hooks = [h for e in entries.values() for h in e.hooks]
    checkout = checkout_problem(reads, code, held.base_sha)
    live = liveness(code, config, project, hooks)
    named = next((jobs for repo, jobs in held.enforcement_jobs.items() if repo.casefold() == repository.casefold()),
                 {})
    project_jobs = {rid: reads.job_problem(repository, head, ref.workflow, ref.job, branch=default)
                    for rid, ref in named.items()}
    digest = install_digest(code, config, project, instance_root, hooks)
    return Facts(rows, entries, base_ci, checkout, live, project_jobs,
                 (lock.trimtab_sha, held.base_sha, head, digest))


# --- the cache E21 reads -----------------------------------------------------------------

def cache_name(repository: str) -> str:
    return hashlib.sha256(repository.casefold().encode("utf-8")).hexdigest() + ".json"


def _body(repository: str, key: tuple[str, str, str, str]) -> dict:
    return {"schema_version": CACHE_SCHEMA, "repository": repository.casefold(), "trimtab_sha": key[0],
            "base_sha": key[1], "default_head": key[2], "install_digest": key[3]}


def record_pass(config: Path, repository: str, key: tuple[str, str, str, str]) -> None:
    """Cache a pass. Called only by the session-mode hook's recorder, and only on a pass."""
    directory = guardstate.directory(config, *CACHE_PARTS, create=True)
    guardstate.write(directory / cache_name(repository), json.dumps(_body(repository, key), sort_keys=True) + "\n")


def clear_pass(config: Path, repository: str) -> None:
    """Remove any cached pass: a run that did not pass leaves none behind."""
    try:
        directory = guardstate.directory(config, *CACHE_PARTS, create=False)
    except guardstate.GuardStateError:
        return  # no directory, so no pass to remove
    try:
        os.unlink(directory / cache_name(repository))
    except FileNotFoundError:
        pass  # nothing cached


def cached_pass(config: Path, repository: str, key: tuple[str, str, str, str]) -> bool:
    """Whether a pass is cached for exactly this key. A missing, planted or malformed cache is no pass."""
    try:
        directory = guardstate.directory(config, *CACHE_PARTS, create=False)
        text = guardstate.read(directory / cache_name(repository))
        body = json.loads(text) if text is not None else None
    except (guardstate.GuardStateError, ValueError):
        return False
    return body == _body(repository, key)
