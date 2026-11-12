"""E20: whether every row E1-E23 is shown, from facts no agent can write (ADR 0063 §2).

`decide` is pure: it takes the rows, the manifest and the facts read, and says for each row
whether it is shown and why not. `gather` reads those facts. Every input is a git object named by
a SHA that only an admin's merge sets, re-hashed as it is read; a GitHub job record; or (outside
`--server`) the install's own state:

- the lock, at the project's default-branch head, which must be where the server says it is;
- the instance file and the doctrine record, as objects in the instance checkout at the lock's
  `trimtab_sha`, the record at the latest first-parent commit that added it;
- the manifest, and the tree this checkout is compared with, as objects at the pinned base;
- job records, through one memoised reader: this base's `lint-test` on the pinned base, and each
  project row's job on the default-branch head, for the default branch;
- outside `--server`: this checkout's files against the pinned base's tree, and the config dir's
  hooks and settings.

Every read that fails, or returns the wrong shape, raises GateError: nothing is vouched for. Only
the session-mode hook's recorder writes the pass cache (`trimtab.adapters.enforcement_record`); a
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
from urllib.parse import quote

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
CACHE_SCHEMA = 3
SETTINGS_FILES = ("settings.json", "settings.local.json")
CLEAN_HINT = "remove anything not in the pinned base (`git clean -xdf hooks trimtab`, `git checkout -- .`)"


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
        self._objects: dict[str, tools.Objects] = {}

    def objects(self, repo: Path, allow_shallow: bool = False) -> tools.Objects:
        key = str(repo)
        if key not in self._objects:
            try:
                self._objects[key] = self.git.objects(repo, allow_shallow=allow_shallow)
            except tools.ToolError as err:
                raise GateError(str(err)) from err
        return self._objects[key]

    def text(self, repo: Path, *args: str) -> str:
        try:
            return self.git(repo, *args)
        except tools.ToolError as err:
            raise GateError(str(err)) from err

    def file(self, repo: Path, commit: str, path: str, what: str, allow_shallow: bool = False) -> str:
        try:
            body = self.objects(repo, allow_shallow).file(commit, path)
            if body is None:
                raise GateError(f"{what} has no file {path} at {commit[:12]}")
            return body.decode("utf-8")
        except (tools.ToolError, UnicodeDecodeError) as err:
            raise GateError(f"{what}: {err}") from err

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


def _default_head(reads: _Reads, repository: str) -> tuple[str, str]:
    row = reads.json(f"repos/{repository}", "gh api repository")
    default = row.get("default_branch") if isinstance(row, dict) else None
    if not isinstance(default, str) or not tools.BRANCH.fullmatch(default) or ".." in default:
        raise GateError("gh api repository returned unexpected JSON")
    branch = reads.json(f"repos/{repository}/branches/{quote(default, safe='')}", "gh api branch")
    commit = branch.get("commit") if isinstance(branch, dict) else None
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise GateError("gh api branch returned unexpected JSON")
    return default, sha


def _head_source(reads: _Reads, repository: str, project: Path, default: str, head: str,
                 fetch_into: Path | None) -> tuple[Path, bool]:
    """Where the default-branch head's objects are read from: a clean fetch, or the project as fetched."""
    if fetch_into is not None:
        try:
            tip = reads.git.fetch_clean(repository, default, fetch_into)
        except tools.ToolError as err:
            raise GateError(str(err)) from err
        if tip != head:
            raise GateError(f"the fetched {default} is not {repository}'s {default} on the server")
        return fetch_into, True
    fetched = reads.text(project, "rev-parse", "--verify", "--end-of-options",
                         f"refs/remotes/origin/{default}^{{commit}}").strip()
    if fetched != head:
        raise GateError(f"origin/{default} as fetched is not {repository}'s {default} on the server; "
                        "run `git fetch origin` and retry")
    return project, False


def _lock(reads: _Reads, source: Path, head: str, shallow: bool) -> lock_config.ProjectConfig:
    found, problems = lock_config.parse(reads.file(source, head, lock_config.PATH, "the project", shallow))
    if found is None:
        raise GateError(f"the lock at the default-branch head: {problems[0].message if problems else 'unreadable'}")
    if not lock_config.FULL_SHA.match(found.trimtab_sha):
        raise GateError("the lock's trimtab_sha must be a full SHA for the gate")
    return found


def _instance(reads: _Reads, root: Path, sha: str) -> instance.InstanceFile:
    try:
        held = instance.parse(json.loads(reads.file(root, sha, instance.FILE, "the instance")))
    except (ValueError, instance.InstanceFileError) as err:
        raise GateError(f"the instance file at the lock's trimtab_sha: {err}") from err
    if not SHA.fullmatch(held.base_sha):
        raise GateError("the instance's base.sha must be a full SHA for the gate")
    return held


def _record_rows(reads: _Reads, root: Path, trimtab_sha: str, path: str) -> tuple[Row, ...]:
    try:
        added = reads.objects(root).last_added(trimtab_sha, path)
    except tools.ToolError as err:
        raise GateError(f"the instance's history: {err}") from err
    if added is None:
        raise GateError(f"the instance's history at {trimtab_sha[:12]} never added {path}")
    # The latest first-parent commit that added it: the merge that put the current file there.
    try:
        return record_mod.parse(reads.file(root, added, path, "the instance"))
    except record_mod.RecordError as err:
        raise GateError(f"the doctrine record as merged: {err}") from err


def _entries(reads: _Reads, code: Path, base_sha: str) -> Mapping[str, manifest_mod.Entry]:
    try:
        return manifest_mod.parse(reads.file(code, base_sha, manifest_mod.PATH, "this checkout"))
    except manifest_mod.ManifestError as err:
        raise GateError(f"the manifest at the pinned base: {err}") from err


def _walk(code: Path) -> list[str] | str:
    """Every regular file under the checked dirs, or why the tree cannot be judged."""
    have: list[str] = []
    failed: list[OSError] = []
    for top in CHECKED_DIRS:
        for directory, dirs, files in os.walk(code / top, onerror=failed.append, followlinks=False):
            for name in dirs + files:
                full = Path(directory) / name
                st = os.lstat(full)
                if stat.S_ISLNK(st.st_mode):
                    return "this checkout holds a symlink under hooks/ or trimtab/"
                if stat.S_ISREG(st.st_mode):
                    have.append(full.relative_to(code).as_posix())
                elif not stat.S_ISDIR(st.st_mode):
                    return "this checkout holds a special file under hooks/ or trimtab/"
    if failed:  # a directory it cannot list could hold anything a module would load
        return "this checkout holds a directory under hooks/ or trimtab/ that cannot be listed"
    return have


def file_digest(code: Path, paths: list[str]) -> str:
    """SHA-256 over each path, its executable bit and its bytes: what a guard re-reads to see an edit."""
    h = hashlib.sha256()
    for path in sorted(paths):
        full = code / path
        h.update(path.encode("utf-8") + b"\0" + (b"x" if os.lstat(full).st_mode & stat.S_IXUSR else b"-") + b"\0")
        h.update(full.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def checkout_problem(reads: _Reads, code: Path, base_sha: str) -> str | None:
    """Why this checkout's hooks/ and trimtab/ are not byte for byte the pinned base's, or None.

    The base's tree is read as verified objects, and every file here is hashed against it, so no
    index flag, stat trick, ignore rule or substituted object hides an edit; any file the base's
    tree lacks (an ignored one, a `__pycache__`) is refused, as is a directory that cannot be listed."""
    try:
        if reads.text(code, "rev-parse", "--verify", "HEAD^{commit}").strip() != base_sha:
            return "this checkout is not at the pinned base"
        objects = reads.objects(code)
        want = {p: v for top in CHECKED_DIRS for p, v in objects.files(base_sha, top).items()}
        have = _walk(code)
        if isinstance(have, str):
            return have
        extra = sorted(set(have) - set(want))
        if extra:
            return f"this checkout holds {len(extra)} file(s) the pinned base does not; {CLEAN_HINT}"
        if set(want) - set(have):
            return "this checkout is missing files the pinned base holds under hooks/ or trimtab/"
        for path in have:
            mode, expected = want[path]
            full = code / path
            body = full.read_bytes()
            sha = hashlib.sha1(f"blob {len(body)}\0".encode("ascii") + body).hexdigest()
            executable = bool(os.lstat(full).st_mode & stat.S_IXUSR)
            if sha != expected or executable != (mode == "100755"):
                return f"this checkout's hooks/ or trimtab/ differ from the pinned base; {CLEAN_HINT}"
    except (tools.ToolError, OSError, UnicodeDecodeError) as err:
        raise GateError(f"this checkout cannot be compared with the pinned base ({type(err).__name__})") from err
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


def _home_config() -> Path:
    return Path(os.path.expanduser("~")) / ".claude"


def _registers(settings: object, config: Path, hook: manifest_mod.Hook) -> bool:
    commands = {f'"{config}/hooks/{hook.file}"'}
    # `$HOME/.claude/...` runs the file under the home config dir; it is this config dir's only when they are one.
    if config.resolve() == _home_config().resolve():
        commands.add(f'"$HOME/.claude/hooks/{hook.file}"')
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
    """A digest of the install state a pass rests on, which a guard can re-read locally: the settings
    files, where each hook link points, the code and instance roots, this checkout's HEAD, and every
    file under hooks/ and trimtab/. A change to any voids a pass."""
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
    h.update(head_of(code).encode("ascii") + b"\0")
    have = _walk(code)
    h.update(file_digest(code, have).encode("ascii") if isinstance(have, list) else b"\0unwalkable")
    return h.hexdigest()


def head_of(code: Path) -> str:
    """This checkout's HEAD commit, read from its files so a guard pays no git process for it."""
    git_dir = code / ".git"
    try:
        if git_dir.is_file():  # a worktree's `gitdir:` pointer
            git_dir = (code / git_dir.read_text(encoding="utf-8").partition("gitdir:")[2].strip()).resolve()
        ref = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            name = ref[5:]
            for base in (git_dir, _common(git_dir)):
                if (base / name).is_file():
                    return (base / name).read_text(encoding="utf-8").strip()
            return _packed(_common(git_dir), name)
        return ref
    except (OSError, UnicodeDecodeError):
        return "unreadable"


def _common(git_dir: Path) -> Path:
    pointer = git_dir / "commondir"
    try:
        return (git_dir / pointer.read_text(encoding="utf-8").strip()).resolve()
    except FileNotFoundError:
        return git_dir


def _packed(git_dir: Path, name: str) -> str:
    try:
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            sha, _, ref = line.partition(" ")
            if ref == name:
                return sha
    except FileNotFoundError:
        pass  # no packed refs: the ref is unborn
    return "unborn"


def gather(repository: str, project: Path, instance_root: Path, code: Path, config: Path,
           server: bool = False, run: Run | None = None, git: tools.Git | None = None,
           fetch_into: Path | None = None) -> Facts:
    if not REPOSITORY.fullmatch(repository) or ".." in repository:
        raise GateError(f"{repository!r} is not owner/name")
    if server:
        raise GateError("--server reads the lock's derived rows, which are added with E22's PR")
    reads = _Reads(run, git)
    default, head = _default_head(reads, repository)
    source, shallow = _head_source(reads, repository, project, default, head, fetch_into)
    lock = _lock(reads, source, head, shallow)
    held = _instance(reads, instance_root, lock.trimtab_sha)
    if held.enforcement_record is None:
        raise GateError("the instance file names no enforcement.record")
    if lock.base_sha is not None and lock.base_sha != held.base_sha:
        raise GateError("the lock's base_sha is not the base the instance pins at its trimtab_sha")
    named = next((jobs for repo, jobs in held.enforcement_jobs.items() if repo.casefold() == repository.casefold()),
                 None)
    if named is None:
        raise GateError(f"the instance file names no enforcement jobs for {repository}")
    rows = _record_rows(reads, instance_root, lock.trimtab_sha, held.enforcement_record)
    entries = _entries(reads, code, held.base_sha)
    base_ci = reads.job_problem(held.base_repo, held.base_sha, BASE_WORKFLOW, BASE_JOB,
                                branch=BASE_BRANCH) if entries else None
    if base_ci:
        base_ci = f"this base's CI on the pinned base: {base_ci}"
    hooks = [h for e in entries.values() for h in e.hooks]
    checkout = checkout_problem(reads, code, held.base_sha)
    live = liveness(code, config, project, hooks)
    project_side = {r.id for r in rows if r.side in (PROJECT, BOTH)}
    project_jobs = {rid: reads.job_problem(repository, head, ref.workflow, ref.job, branch=default)
                    for rid, ref in named.items() if rid in project_side}
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


def clear_pass(config: Path, repository: str | None = None) -> None:
    """Remove the cached pass for `repository`, or every cached pass when it is None."""
    try:
        directory = guardstate.directory(config, *CACHE_PARTS, create=False)
    except guardstate.GuardStateError:
        return  # no directory, so no pass to remove
    names = [cache_name(repository)] if repository else [p.name for p in directory.glob("*.json")]
    for name in names:
        try:
            os.unlink(directory / name)
        except FileNotFoundError:
            continue  # nothing cached under that name


def cached_pass(config: Path, repository: str, key: tuple[str, str, str, str]) -> bool:
    """Whether a pass is cached for exactly this key. A missing, planted or malformed cache is no pass."""
    try:
        directory = guardstate.directory(config, *CACHE_PARTS, create=False)
        text = guardstate.read(directory / cache_name(repository))
        body = json.loads(text) if text is not None else None
    except (guardstate.GuardStateError, ValueError):
        return False
    return body == _body(repository, key)
