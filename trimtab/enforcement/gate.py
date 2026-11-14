"""E20: whether every row E1-E23 is shown, from facts no agent can write (ADR 0063 §2).

`decide` is pure: it takes the rows, the manifest and the facts read, and says for each row
whether it is shown and why not. `gather` reads those facts. Every input is a git object named by
a SHA that only an admin's merge sets, re-hashed as it is read; a GitHub job record; or (outside
`--server`) one snapshot of the install's own state, which the checks judge and the digest seals:

- the lock, at the project's default-branch head, which must be where the server says it is;
- the instance file and the doctrine record, as objects in the instance checkout at the lock's
  `trimtab_sha`, the record at the latest first-parent commit that added it;
- the manifest, and the tree this checkout is compared with, as objects at the pinned base;
- job records, through one memoised reader: this base's `lint-test` on the pinned base, and each
  project row's job on the default-branch head, for the default branch;
- outside `--server`: `snapshot`, read once, of this checkout's files, its HEAD, the settings
  files and where each hook link points.

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
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from trimtab import actions, guardstate, instance
from trimtab import config as lock_config
from trimtab.capture.ghapi import GhApi, Run, SourceError
from trimtab.enforcement import manifest as manifest_mod
from trimtab.enforcement import record as record_mod
from trimtab.enforcement import tools
from trimtab.enforcement.record import BASE, BOTH, PROJECT, DerivedRow, Row

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")
BASE_WORKFLOW = ".github/workflows/ci.yml"
BASE_JOB = "lint-test"
BASE_BRANCH = "main"
CHECKED_DIRS = ("hooks", "trimtab")
CACHE_PARTS = ("enforcement",)
CACHE_SCHEMA = 4
EPOCH = ".epoch"
EPOCH_SHAPE = re.compile(r"[0-9a-f]{32}")
SETTINGS_FILES = ("settings.json", "settings.local.json")
CLEAN_HINT = "remove anything not in the pinned base (`git clean -xdf hooks trimtab`, `git checkout -- .`)"
MAX_SETTINGS_BYTES = 1 << 20
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class GateError(Exception):
    """A fact cannot be read, so no row is vouched for."""


@dataclass(frozen=True)
class Facts:
    """What `gather` read; `decide` judges it."""

    rows: tuple[Row | DerivedRow, ...]
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


# --- the install snapshot: read once, judged and sealed from the same bytes ---------------

def _read_regular(path: Path, limit: int) -> bytes | None:
    """A regular file's bytes, or None when there is none; a FIFO, a link or anything else raises OSError."""
    try:
        fd = os.open(path, READ_FLAGS)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise OSError(f"{path.name} is not a regular file")
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise OSError(f"{path.name} is larger than {limit} bytes")
    return data


@dataclass(frozen=True)
class Install:
    """One read of the install state a pass rests on."""

    files: Mapping[str, tuple[bool, bytes]] | str  # path -> (executable, bytes) under the checked dirs, or why not
    head: str  # this checkout's HEAD commit
    settings: Mapping[str, bytes | None]  # "<config|project>/<name>" -> bytes, None when absent
    links: Mapping[str, str]  # hook file -> where the config dir's hooks/<file> resolves
    roots: tuple[str, str] = field(default=("", ""))  # the code and instance roots, resolved


def _walk(code: Path) -> dict[str, tuple[bool, bytes]] | str:
    """Every regular file under the checked dirs with its bytes, or why the tree cannot be judged."""
    out: dict[str, tuple[bool, bytes]] = {}
    failed: list[OSError] = []
    for top in CHECKED_DIRS:
        root = code / top
        if not stat.S_ISDIR(os.lstat(root).st_mode):  # a symlinked or missing top is not this checkout's
            return f"this checkout's {top}/ is not a plain directory"
        for directory, dirs, files in os.walk(root, onerror=failed.append, followlinks=False):
            for name in dirs + files:
                full = Path(directory) / name
                st = os.lstat(full)
                if stat.S_ISLNK(st.st_mode):
                    return "this checkout holds a symlink under hooks/ or trimtab/"
                if stat.S_ISREG(st.st_mode):
                    rel = full.relative_to(code).as_posix()
                    try:
                        rel.encode("utf-8")
                    except UnicodeEncodeError:
                        return "this checkout holds a file name under hooks/ or trimtab/ that is not UTF-8"
                    body = _read_regular(full, 1 << 24)
                    if body is None:  # gone between the listing and the read: the tree is changing under us
                        raise OSError(f"{rel} vanished while the checkout was read")
                    out[rel] = (bool(st.st_mode & stat.S_IXUSR), body)
                elif not stat.S_ISDIR(st.st_mode):
                    return "this checkout holds a special file under hooks/ or trimtab/"
    if failed:  # a directory it cannot list could hold anything a module would load
        return "this checkout holds a directory under hooks/ or trimtab/ that cannot be listed"
    return out


def head_of(code: Path) -> str:
    """This checkout's HEAD commit, read from its files so a guard pays no git process for it; any layout this
    does not know (a reftable repository, a FIFO, a broken pointer) raises OSError, so no digest is made."""
    git_dir = code / ".git"
    if stat.S_ISREG(os.lstat(git_dir).st_mode):  # a worktree's `gitdir:` pointer
        pointer = (_read_regular(git_dir, 4096) or b"").decode("utf-8").strip()
        if not pointer.startswith("gitdir: "):
            raise OSError("the .git file is not a gitdir pointer")
        git_dir = (code / pointer[len("gitdir: "):]).resolve()
    head = (_read_regular(git_dir / "HEAD", 4096) or b"").decode("utf-8").strip()
    if SHA.fullmatch(head):
        return head
    if not head.startswith("ref: refs/"):
        raise OSError("HEAD is neither a commit nor a ref")
    name = head[len("ref: "):]
    common = git_dir
    pointer = _read_regular(git_dir / "commondir", 4096)
    if pointer is not None:
        common = (git_dir / pointer.decode("utf-8").strip()).resolve()
    # As git does: only its per-worktree namespaces live in a worktree's own dir; every other ref is shared.
    per_worktree = name.startswith(("refs/bisect/", "refs/worktree/", "refs/rewritten/"))
    if ".." in name.split("/"):
        raise OSError("HEAD names a ref outside refs/")
    for base in ((git_dir,) if per_worktree else (common,)):
        value = _read_regular(base / name, 4096)
        if value is not None and SHA.fullmatch(value.decode("utf-8").strip()):
            return value.decode("utf-8").strip()
    packed = _read_regular(common / "packed-refs", 1 << 24)
    for line in (packed or b"").decode("utf-8").splitlines():
        sha, _, ref = line.partition(" ")
        if ref == name and SHA.fullmatch(sha):
            return sha
    raise OSError("HEAD names a ref this checkout does not hold")


def snapshot(code: Path, config: Path, project: Path, instance_root: Path, hooks) -> Install:
    """The install state, read once: what the checks judge and the digest seals (so nothing changed between
    a check and the digest can slip into a pass)."""
    try:
        settings: dict[str, bytes | None] = {}
        for label, directory in (("config", config), ("project", project / ".claude")):
            for name in SETTINGS_FILES:
                settings[f"{label}/{name}"] = _read_regular(directory / name, MAX_SETTINGS_BYTES)
        links = {h.file: str((config / "hooks" / h.file).resolve()) for h in hooks}
        return Install(_walk(code), head_of(code), settings, links,
                       (str(code.resolve()), str(instance_root.resolve())))
    except (OSError, UnicodeError) as err:
        raise GateError(f"the install cannot be read ({type(err).__name__}: {err})") from err


def install_digest(install: Install) -> str:
    """SHA-256 over the snapshot, every part length-prefixed so no two snapshots share a digest."""
    h = hashlib.sha256()

    def part(data: bytes) -> None:
        h.update(len(data).to_bytes(8, "big") + data)

    for key in sorted(install.settings):
        part(key.encode("utf-8"))
        value = install.settings[key]
        part(b"\0absent" if value is None else b"\1" + value)
    for file in sorted(install.links):
        part(file.encode("utf-8"))
        part(install.links[file].encode("utf-8"))
    for root in install.roots:
        part(root.encode("utf-8"))
    part(install.head.encode("ascii"))
    if isinstance(install.files, str):
        part(b"\0unwalkable")
    else:
        for path in sorted(install.files):
            executable, body = install.files[path]
            part(path.encode("utf-8"))
            part(b"x" if executable else b"-")
            part(body)
    return h.hexdigest()


# --- reads --------------------------------------------------------------------------------

class _Reads:
    """The hardened tools for one gather, with every failure as GateError."""

    def __init__(self, run: Run | None, git: tools.Git | None):
        try:
            self.git = git or tools.Git()
            self.auth = None
            if run is None:
                self.auth = tools.GhAuth()
                run = tools.gh_runner(self.auth)
        except tools.ToolError as err:
            raise GateError(str(err)) from err
        self.api = GhApi(run)
        self.jobs = actions.Reader(self.api)
        self._objects: dict[tuple[str, bool], tools.Objects] = {}

    def objects(self, repo: Path, allow_shallow: bool = False) -> tools.Objects:
        key = (str(repo), allow_shallow)
        if key not in self._objects:
            try:
                self._objects[key] = self.git.objects(repo, allow_shallow=allow_shallow)
            except tools.ToolError as err:
                raise GateError(str(err)) from err
        return self._objects[key]

    def close(self) -> None:
        for objects in self._objects.values():
            try:
                objects.close()
            except OSError:
                continue  # one batch process that broke must not keep the others open or mask the real error

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
            tip = reads.git.fetch_clean(repository, default, fetch_into, reads.auth or tools.GhAuth())
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
        objects = reads.objects(root)
        if objects.entry(trimtab_sha, path) is None:  # the record must be there now, not only once
            raise GateError(f"the instance has no {path} at {trimtab_sha[:12]}")
        added = objects.last_added(trimtab_sha, path)
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


def checkout_problem(reads: _Reads, code: Path, base_sha: str, install: Install) -> str | None:
    """Why the snapshot of hooks/ and trimtab/ is not byte for byte the pinned base's, or None. The base's
    tree is read as verified objects; any file it lacks (an ignored one, a `__pycache__`) is refused."""
    if install.head != base_sha:
        return "this checkout is not at the pinned base"
    if isinstance(install.files, str):
        return install.files
    try:
        objects = reads.objects(code)
        want = {p: v for top in CHECKED_DIRS for p, v in objects.files(base_sha, top).items()}
    except tools.ToolError as err:
        raise GateError(f"the pinned base's tree: {err}") from err
    extra = sorted(set(install.files) - set(want))
    if extra:
        return f"this checkout holds {len(extra)} file(s) the pinned base does not; {CLEAN_HINT}"
    if set(want) - set(install.files):
        return "this checkout is missing files the pinned base holds under hooks/ or trimtab/"
    for path, (executable, body) in install.files.items():
        mode, expected = want[path]
        sha = hashlib.sha1(f"blob {len(body)}\0".encode("ascii") + body).hexdigest()
        if sha != expected or executable != (mode == "100755"):
            return f"this checkout's hooks/ or trimtab/ differ from the pinned base; {CLEAN_HINT}"
    return None


UNREADABLE = object()


def _parsed(install: Install, key: str, required: bool) -> object:
    raw = install.settings.get(key)
    if raw is None:
        return UNREADABLE if required else {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return UNREADABLE


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


def liveness(code: Path, config: Path, install: Install, hooks: list[manifest_mod.Hook]) -> dict[str, str | None]:
    """For each hook file: why the snapshot shows it not installed from this checkout, registered and enabled."""
    parsed = [_parsed(install, f"config/{SETTINGS_FILES[0]}", True), _parsed(install, f"config/{SETTINGS_FILES[1]}", False),
              _parsed(install, f"project/{SETTINGS_FILES[0]}", False), _parsed(install, f"project/{SETTINGS_FILES[1]}", False)]
    disabled = any(isinstance(s, dict) and s.get("disableAllHooks") is True for s in parsed)
    files = install.files if isinstance(install.files, Mapping) else {}
    out: dict[str, str | None] = {}
    for hook in hooks:
        why = None
        entry = files.get(f"hooks/{hook.file}")
        if any(s is UNREADABLE for s in parsed):
            why = "a settings file cannot be read"
        elif disabled:
            why = "a settings file sets disableAllHooks"
        elif install.links.get(hook.file) != str((code / "hooks" / hook.file).resolve()):
            why = "the config dir's hook is not this checkout's"
        elif entry is None or not entry[0]:
            why = "the installed hook is not executable"
        elif not _registers(parsed[0], config, hook):
            why = f"settings.json does not register it for {hook.event} {hook.matcher}"
        if out.get(hook.file) is None:
            out[hook.file] = why
    return out


def gather(repository: str, project: Path, instance_root: Path, code: Path, config: Path,
           server: bool = False, run: Run | None = None, git: tools.Git | None = None,
           fetch_into: Path | None = None) -> Facts:
    if not REPOSITORY.fullmatch(repository) or ".." in repository:
        raise GateError(f"{repository!r} is not owner/name")
    reads = _Reads(run, git)
    try:
        if server:
            return _gather_server(reads, repository, project, code, fetch_into)
        return _gather(reads, repository, project, instance_root, code, config, fetch_into)
    finally:
        reads.close()


def _no_network(*_args, **_kwargs):
    raise GateError("this read makes no network call")


def _plan_keys(reads: _Reads, root: Path, sha: str) -> dict:
    """The instance keys the plan-run rows read (`lock_config.PLAN_KEYS`), as written at `sha`: the lock carries
    them so the server job, which has no instance checkout, judges by the same agent, review and limits."""
    try:
        raw = json.loads(reads.file(root, sha, instance.FILE, "the instance"))
    except ValueError as err:
        raise GateError("the instance file is not valid JSON") from err
    return {k: raw[k] for k in lock_config.PLAN_KEYS if isinstance(raw, dict) and k in raw}


def _derived(held: instance.InstanceFile, rows: tuple[Row, ...], repository: str, keys: dict) -> dict:
    named = next((jobs for repo, jobs in held.enforcement_jobs.items() if repo.casefold() == repository.casefold()), {})
    return {"base_repo": held.base_repo,
            "rows": {r.id: {"owner": r.owner, "sha256": r.sha256} for r in rows},
            "jobs": {rid: {"workflow": ref.workflow, "job": ref.job} for rid, ref in named.items()},
            "instance": keys}


def derive_lock_enforcement(instance_root: Path, trimtab_sha: str, repository: str,
                            git: tools.Git | None = None) -> dict | None:
    """The lock's derived `enforcement` field for `repository` at `trimtab_sha`, or None when the instance there
    names no doctrine record. Read as `gather` reads, from verified objects in the instance checkout, the record
    at the commit that added it; no network. `trimtab bump` writes this and `--server` trusts it (ADR 0063 §7)."""
    if not REPOSITORY.fullmatch(repository) or ".." in repository:
        raise GateError(f"{repository!r} is not owner/name")
    if not lock_config.is_commit_sha(trimtab_sha):
        raise GateError(f"{trimtab_sha!r} is not a commit name")
    reads = _Reads(_no_network, git)
    try:
        held = _instance(reads, instance_root, trimtab_sha)
        if held.enforcement_record is None:
            return None
        return _derived(held, _record_rows(reads, instance_root, trimtab_sha, held.enforcement_record), repository,
                        _plan_keys(reads, instance_root, trimtab_sha))
    finally:
        reads.close()


def server_instance(repository: str, project: Path, code: Path, run: Run | None = None,
                    git: tools.Git | None = None, fetch_into: Path | None = None) -> instance.InstanceFile:
    """The instance's plan-run keys (agent, review, secret scan, fix limits) as the lock holds them, for the
    server job's plan checks. Read exactly as `--server` reads the rows: the lock at the default-branch head, as
    the server reports it, and `code` the commit its `base_sha` names (ADR 0063 §7)."""
    if not REPOSITORY.fullmatch(repository) or ".." in repository:
        raise GateError(f"{repository!r} is not owner/name")
    reads = _Reads(run, git)
    try:
        lock = _server_lock(reads, repository, project, code, fetch_into)[0]
        body = {"schema_version": instance.SCHEMA_VERSION, "repo": repository,
                "base": {"repo": lock.enforcement["base_repo"], "sha": lock.base_sha},
                **lock.enforcement["instance"]}
        try:
            return instance.parse(body)
        except instance.InstanceFileError as err:
            raise GateError(f"the lock's instance keys: {err}") from err
    finally:
        reads.close()


def _server_lock(reads: _Reads, repository: str, project: Path, code: Path, fetch_into: Path | None):
    """(lock, default branch, its head): the lock `--server` trusts, after its derived field is known to be there
    and `code` to be the base it names."""
    default, head = _default_head(reads, repository)
    source, shallow = _head_source(reads, repository, project, default, head, fetch_into)
    lock = _lock(reads, source, head, shallow)
    if lock.enforcement is None or lock.base_sha is None:
        raise GateError("the lock has no derived enforcement field; run /trimtab-bump, which writes it")
    if reads.text(code, "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}").strip() != lock.base_sha:
        raise GateError("this checkout is not at the lock's base_sha, so it is not the base the rows are judged by")
    return lock, default, head


def _gather_server(reads: _Reads, repository: str, project: Path, code: Path, fetch_into: Path | None) -> Facts:
    """`--server`: the rows come from the lock's derived field, since CI has no instance checkout (ADR 0015).

    The lock is the default-branch head's, which only an admin's merge moves; `code` must be the pinned base
    itself, so the manifest is read, and the commands run, from the base the project was reviewed against."""
    lock, default, head = _server_lock(reads, repository, project, code, fetch_into)
    field = lock.enforcement
    rows = tuple(DerivedRow(rid, row["owner"], row["sha256"]) for rid, row in field["rows"].items())
    entries = _entries(reads, code, lock.base_sha)
    base_ci = reads.job_problem(field["base_repo"], lock.base_sha, BASE_WORKFLOW, BASE_JOB,
                                branch=BASE_BRANCH) if entries else None
    if base_ci:
        base_ci = f"this base's CI on the pinned base: {base_ci}"
    project_side = {r.id for r in rows if r.side in (PROJECT, BOTH)}
    project_jobs = {rid: reads.job_problem(repository, head, ref["workflow"], ref["job"], branch=default)
                    for rid, ref in field["jobs"].items() if rid in project_side}
    return Facts(rows, entries, base_ci, None, None, project_jobs, (lock.trimtab_sha, lock.base_sha, head, "server"))


def _gather(reads: _Reads, repository: str, project: Path, instance_root: Path, code: Path, config: Path,
            fetch_into: Path | None) -> Facts:
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
    if lock.enforcement is not None and lock.enforcement != _derived(
            held, rows, repository, _plan_keys(reads, instance_root, lock.trimtab_sha)):
        raise GateError("the lock's derived enforcement field is not what the instance and record at its "
                        "trimtab_sha give; run /trimtab-bump")
    entries = _entries(reads, code, held.base_sha)
    base_ci = reads.job_problem(held.base_repo, held.base_sha, BASE_WORKFLOW, BASE_JOB,
                                branch=BASE_BRANCH) if entries else None
    if base_ci:
        base_ci = f"this base's CI on the pinned base: {base_ci}"
    project_side = {r.id for r in rows if r.side in (PROJECT, BOTH)}
    project_jobs = {rid: reads.job_problem(repository, head, ref.workflow, ref.job, branch=default)
                    for rid, ref in named.items() if rid in project_side}
    hooks = [h for e in entries.values() for h in e.hooks]
    # Last, after every network read: the one snapshot both judged and sealed.
    install = snapshot(code, config, project, instance_root, hooks)
    checkout = checkout_problem(reads, code, held.base_sha, install)
    live = liveness(code, config, install, hooks)
    return Facts(rows, entries, base_ci, checkout, live, project_jobs,
                 (lock.trimtab_sha, held.base_sha, head, install_digest(install)))


# --- the cache E21 reads -----------------------------------------------------------------

def cache_name(repository: str) -> str:
    return hashlib.sha256(repository.casefold().encode("utf-8")).hexdigest() + ".json"


def current_epoch(config: Path) -> str | None:
    """The cache's current epoch, or None when there is none or it cannot be read as a private file of the
    right shape (a planted link, another owner): never a value a pass or a recorder could match."""
    try:
        directory = guardstate.directory(config, *CACHE_PARTS, create=False)
        text = guardstate.read(directory / EPOCH)
    except guardstate.GuardStateError:
        return None
    return text if text and EPOCH_SHAPE.fullmatch(text) else None


def _body(repository: str, key: tuple[str, str, str, str], epoch: str) -> dict:
    return {"schema_version": CACHE_SCHEMA, "repository": repository.casefold(), "trimtab_sha": key[0],
            "base_sha": key[1], "default_head": key[2], "install_digest": key[3], "epoch": epoch}


def record_pass(config: Path, repository: str, key: tuple[str, str, str, str], epoch: str) -> None:
    """Cache a pass under `epoch`. Called only by the session-mode hook's recorder, and only on a pass."""
    if not EPOCH_SHAPE.fullmatch(epoch):
        raise GateError("a pass is written only under an epoch")
    directory = guardstate.directory(config, *CACHE_PARTS, create=True)
    guardstate.write(directory / cache_name(repository),
                     json.dumps(_body(repository, key, epoch), sort_keys=True) + "\n")


def clear_pass(config: Path, repository: str | None = None) -> None:
    """Remove the cached pass for `repository`, or every cached pass when it is None. A cache directory that
    is there but not private raises GuardStateError: it cannot be trusted to hold no pass."""
    path = Path(config) / guardstate.ROOT
    for part in CACHE_PARTS:
        path = path / part
    if not os.path.lexists(path):
        return  # no directory, so no pass to remove
    directory = guardstate.directory(config, *CACHE_PARTS, create=False)
    names = [cache_name(repository)] if repository else [p.name for p in directory.glob("*.json")]
    for name in names:
        try:
            os.unlink(directory / name)
        except FileNotFoundError:
            continue  # nothing cached under that name


LS_REMOTE_SECONDS = 3  # the default-branch head's bound on every guarded call (ADR 0063 §3)


def _auth():
    """The gh authentication the clean fetch uses, or None where it cannot be built (an anonymous read
    then fails closed on a private repository)."""
    try:
        return tools.GhAuth()
    except tools.ToolError:
        return None


def _no_gh(argv, stdin=None):
    raise GateError("the current key is read without the API")


def remote_head(git: tools.Git, repository: str, auth=None) -> str:
    """The default branch's head, by `git ls-remote <url> HEAD` under 3 s, with only this environment's
    pinned configuration: no `insteadOf` of the project's can answer with a stale head (ADR 0063 §3)."""
    scratch = tools.private_tempdir()
    try:
        return git.ls_remote(scratch, f"https://github.com/{repository}.git", "HEAD", LS_REMOTE_SECONDS,
                             auth)
    except tools.ToolError as err:
        raise GateError(f"the default-branch head could not be read ({err})") from err
    finally:
        tools.remove_tree(scratch)


def pinned_key(repository: str, project: Path, instance_root: Path, code: Path, config: Path,
               git: tools.Git | None = None, head: str | None = None,
               pinned: tuple[str, str] | None = None) -> tuple[str, str, str, str]:
    """The key a pass must carry to be this session's (ADR 0063 §3): the lock's `trimtab_sha`, the base the
    instance pins at it, the default-branch head, and the install digest.

    With `pinned` given — the `(trimtab_sha, base_sha)` the pass carries, which a guard has — the lock and the instance are
    not read as objects at all: both are files of the default branch, so they can only change when its
    head does, and that head is read here. A guard needs that, because the objects at a head the server
    has just moved to are not in the project's checkout until something fetches, and while the gate fails
    a run may run no command that could (E21)."""
    if not REPOSITORY.fullmatch(repository) or ".." in repository:
        raise GateError(f"{repository!r} is not owner/name")
    reads = _Reads(_no_gh, git)
    try:
        found = head or remote_head(reads.git, repository, _auth())
        if pinned is None:
            lock = _lock(reads, project, found, False)
            trimtab_sha, base = lock.trimtab_sha, _instance(reads, instance_root, lock.trimtab_sha).base_sha
        else:
            trimtab_sha, base = pinned
        entries = _entries(reads, code, base)
        hooks = [h for e in entries.values() for h in e.hooks]
        install = snapshot(code, config, project, instance_root, hooks)
        return (trimtab_sha, base, found, install_digest(install))
    finally:
        reads.close()


def cached_body(config: Path, repository: str) -> dict | None:
    """The pass as it was written, or None where there is none this guard can read."""
    try:
        directory = guardstate.directory(config, *CACHE_PARTS, create=False)
        text = guardstate.read(directory / cache_name(repository))
        body = json.loads(text) if text is not None else None
    except (guardstate.GuardStateError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def pass_holds(repository: str, project: Path, instance_root: Path, code: Path, config: Path,
               git: tools.Git | None = None) -> bool:
    """Whether the cache holds a pass for the current key. Anything unreadable is no pass (E21 refuses).

    The lock and base come from the pass, and the key is then rebuilt and compared whole, so a pass is
    accepted only for the default-branch head `ls-remote` reports now and for this install's digest."""
    body = cached_body(config, repository)
    if body is None or not all(isinstance(body.get(k), str) for k in ("trimtab_sha", "base_sha")):
        return False
    try:
        key = pinned_key(repository, project, instance_root, code, config, git,
                         pinned=(body["trimtab_sha"], body["base_sha"]))
        return cached_pass(config, repository, key)
    except (GateError, tools.ToolError, OSError, ValueError):
        return False


def cached_pass(config: Path, repository: str, key: tuple[str, str, str, str]) -> bool:
    """Whether a pass is cached for exactly this key under the current epoch. A missing, planted or malformed
    cache, a pass from an earlier epoch, or no epoch at all is no pass."""
    epoch = current_epoch(config)
    if epoch is None:
        return False
    return cached_body(config, repository) == _body(repository, key, epoch)
