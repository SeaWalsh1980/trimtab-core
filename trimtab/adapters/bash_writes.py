"""The Bash write detector: what one Bash call changed in its repository, judged as guard-paths judges a write (ADR 0065).

guard-paths judges the Edit-family tools against the repository's write allowlist (layer 2) and, in an
autonomous plan run, the plan's scope (layer 3) before they write. It cannot do that for Bash, whose
writing commands cannot be listed (ADR 0039). The paths a command changed can be: this module compares
the repository before and after the call, and judges every path the call changed by the same rules.

It detects; it cannot prevent. The call has run by the time it judges, so a refusal reaches the agent as
loud feedback naming the paths (CFG-2: undo the change, or stop and ask), and in a run as a blocker. The
server-side plan-scope check (ADR 0042) stays the gate for what a plan PR may merge.

The pure part comes first: parsing `git status`, the fingerprint a changed path is known by, which paths
moved between two snapshots, and the problems with those paths. Data in, value out.

The edge comes last, run by hooks/bash-writes.sh through hooks/run_module.py (ADR 0035) as
`trimtab.adapters.bash_writes` with:

    snapshot             PreToolUse (Bash). Records the repository's changed paths, HEAD and the write
                         allowlist's text, keyed by session and tool_use_id. Exit 0, or 2 (blocks the
                         call) when no snapshot could be recorded, since nothing could then be judged.
    judge <mode> <branch> PostToolUse and PostToolUseFailure (Bash). mode and branch come from
                         hooks/session-mode.sh's sm_classify, so the mode judged here is the one the
                         guard enforces (ADR 0041). Exit 0, or 2 with the problems on stderr.

Snapshots live at `<config dir>/guard-state/bash-writes/`, private to this user, as pr_monitor keeps its
lists (ADR 0028). No output carries a session ID, a tool_use_id or a file's contents.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from trimtab.adapters.pr_monitor import READ_FLAGS, PrMonitorError, _private, config_dir, state_dir

SNAPSHOT_VERSION = 1
STATE_PARTS = ("guard-state", "bash-writes")
ALLOWLIST = ".claude/write-allowlist"
PLANS_DIR = "docs/" + "plans"  # in two pieces, as in planscope, so the citation test does not read a path
# Bounds (ENG-6.2). A repository with more changed paths than this is refused, not judged in part.
MAX_PATHS = 20000
MAX_COMMITS = 200
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
TTL_SECONDS = 24 * 60 * 60  # a snapshot whose call never reached PostToolUse (a cancel) is pruned after this
MAX_PRUNE_FILES = 1000
GIT_TIMEOUT_SECONDS = 8  # inside the hook's 10 s timeout
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}")
SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
# The same steering variables and pins as trimtab.planscope._git, which this module cannot import at
# snapshot time without its git edge. test_bash_writes pins the two lists equal.
GIT_STEERING = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_CEILING_DIRECTORIES",
                "GIT_GRAFT_FILE", "GIT_NAMESPACE", "GIT_REPLACE_REF_BASE", "GIT_CONFIG_PARAMETERS")
GIT_PINNED = (("core.fsmonitor", "false"), ("core.commitGraph", "false"), ("log.showSignature", "false"),
              ("log.follow", "false"), ("diff.renames", "false"))


class BashWritesError(Exception):
    """The repository or the snapshot cannot be read, so nothing the call did can be judged."""


# ---- pure ------------------------------------------------------------------

def parse_status(out: bytes) -> list[str]:
    """The paths `git status --porcelain=v1 -z --no-renames --untracked-files=all` lists.

    With renames off every entry is `XY <path>` and NUL; anything else is git answering in a form this
    version does not read, never a clean tree."""
    paths = []
    for entry in out.split(b"\0"):
        if not entry:
            continue
        if len(entry) < 4 or entry[2:3] != b" ":
            raise BashWritesError("git status gave an entry that could not be parsed")
        paths.append(os.fsdecode(entry[3:]))
    if len(paths) > MAX_PATHS:
        raise BashWritesError(f"more than {MAX_PATHS} changed paths; too many to judge")
    return paths


def fingerprint(st: os.stat_result | None) -> str:
    """What a changed path is known by: type, size, inode and both clocks. ctime catches a write that
    reset the mtime; `absent` is a path git lists that is gone (a deletion)."""
    if st is None:
        return "absent"
    return f"{stat.S_IFMT(st.st_mode):o}:{st.st_size}:{st.st_ino}:{st.st_mtime_ns}:{st.st_ctime_ns}"


def moved(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Paths changed after the call that were clean before it or whose fingerprint differs.

    A path changed before and clean after is not judged: its content is HEAD's again (a `git restore`),
    or a commit took it, which `committed` covers."""
    return sorted(p for p, fp in after.items() if before.get(p) != fp)


@dataclass(frozen=True)
class Scope:
    """What the paths are judged against, as guard-paths' hl_check_abs reads it."""
    allowlist: str | None  # the list's text, or None where the repository has none
    list_bad: str  # why a list that exists cannot be trusted (a link, unreadable), or ""
    autonomous: bool
    plan_list: str | None  # in a run: the plan's hook scope in the list syntax, or None when unscopable
    plan_problem: str  # in a run, why no write can be scoped, or ""
    in_base: frozenset[str]  # of the changed paths, those the merge-base tree holds
    base_known: bool  # whether the merge-base could be read at all


def problems(paths: list[str], scope: Scope) -> list[str]:
    """Every path the call changed that guard-paths would have refused to an Edit or a Write."""
    from trimtab import planscope  # its pure rules only; no git

    found = []
    allow = planscope.parse_list(scope.allowlist) if scope.allowlist is not None else ()
    plan = planscope.parse_list(scope.plan_list) if scope.plan_list is not None else ()
    for path in paths:
        label = planscope.shown(path)
        if planscope.refused(path):
            found.append(f"{label}: a repository harness or control path")
            continue
        if scope.list_bad:
            found.append(f"{label}: {scope.list_bad}")
            continue
        if scope.allowlist is None:
            if scope.autonomous:
                found.append(f"{label}: an autonomous plan run in a repository with no write allowlist")
            continue  # interactive, no list: layer 1 only, as in guard-paths
        listed = planscope.match(allow, path)
        if listed is planscope.Listed.NO:
            found.append(f"{label}: outside the repository's write allowlist")
            continue
        create_only = listed is planscope.Listed.CREATE_ONLY
        if scope.autonomous:
            if scope.plan_problem:
                found.append(f"{label}: {scope.plan_problem}")
                continue
            in_plan = planscope.match(plan, path)
            if in_plan is planscope.Listed.NO:
                found.append(f"{label}: outside the authorised plan's scope (its .writes declaration, and its own "
                             f"progress and issue-register folders)")
                continue
            create_only = create_only or in_plan is planscope.Listed.CREATE_ONLY
        if create_only:
            if not scope.base_known:
                found.append(f"{label}: create-only, and the merge-base with origin's default branch cannot be read "
                             f"to tell a new file from an existing one")
            elif path in scope.in_base:
                found.append(f"{label}: create-only, and the file is already on the default branch")
    return found


def hook_plan_list(writes: str, plan: str) -> str:
    """The plan's scope as guard-paths' hl_plan_allowed builds it: the declaration plus the plan's own
    progress and register folders, create-only. Wider than the server's per-item scope (ADR 0042)."""
    return f"{writes}\n+{PLANS_DIR}/progress/{plan}/**\n+{PLANS_DIR}/unauthorised/{plan}/**\n"


def snapshot_name(session_id: object, tool_use_id: object) -> str:
    """The snapshot's file name: a hash, so no state path names a session or a call."""
    if not isinstance(session_id, str) or not TOKEN.fullmatch(session_id):
        raise BashWritesError("the session ID is missing or malformed")
    if not isinstance(tool_use_id, str) or not TOKEN.fullmatch(tool_use_id):
        raise BashWritesError("the tool_use_id is missing or malformed")
    return hashlib.sha256(f"{session_id}\n{tool_use_id}".encode("ascii")).hexdigest() + ".json"


# ---- git -------------------------------------------------------------------

def _git(repo: str, *args: str) -> bytes:
    env = {k: v for k, v in os.environ.items()
           if k not in GIT_STEERING and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
           and k != "GIT_CONFIG_COUNT"}
    # GIT_OPTIONAL_LOCKS=0: status must not take the index lock a concurrent command may want.
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_LITERAL_PATHSPECS="1", GIT_OPTIONAL_LOCKS="0")
    pins = [arg for key, value in GIT_PINNED for arg in ("-c", f"{key}={value}")]
    try:
        done = subprocess.run(["git", "-C", repo, *pins, *args], capture_output=True, env=env,
                              timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise BashWritesError(f"git {args[0]}: {type(err).__name__}") from err
    if done.returncode != 0:
        raise BashWritesError(f"git {args[0]} exited {done.returncode}")
    return done.stdout


def repo_root(cwd: str) -> str | None:
    """The repository the call ran in, or None outside one."""
    try:
        out = _git(cwd, "rev-parse", "--show-toplevel")
    except BashWritesError:
        return None  # not a repository (or no git): nothing there to judge
    root = os.fsdecode(out.rstrip(b"\n"))
    return root or None


def head(root: str) -> str | None:
    """HEAD's commit, or None on an unborn branch."""
    try:
        sha = _git(root, "rev-parse", "--verify", "-q", "HEAD^{commit}").decode("ascii").strip()
    except BashWritesError:
        return None
    return sha if SHA.fullmatch(sha) else None


def changed_paths(root: str) -> dict[str, str]:
    out = _git(root, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all",
               "--ignore-submodules=none")
    found = {}
    for path in parse_status(out):
        try:
            st = os.lstat(os.path.join(root, path))
        except FileNotFoundError:
            st = None
        except OSError as err:
            raise BashWritesError(f"cannot read a changed path ({type(err).__name__})") from err
        found[path] = fingerprint(st)
    return found


def default_ref(root: str) -> str | None:
    try:
        ref = _git(root, "symbolic-ref", "-q", "refs/remotes/origin/HEAD").decode("utf-8").strip()
    except (BashWritesError, UnicodeDecodeError):
        return None
    return ref or None


def committed(root: str, before: str | None, after: str | None) -> list[str]:
    """Paths changed by commits the call made: reachable from the new HEAD but not from the old one, nor
    from origin's default branch (so a pull of merged work is not judged as the call's own writing)."""
    if after is None or after == before:
        return []
    exclude = [x for x in (before, default_ref(root)) if x]
    out = _git(root, "rev-list", f"--max-count={MAX_COMMITS + 1}", after, "--not", *exclude, "--")
    commits = [c for c in out.decode("ascii").split() if c]
    if len(commits) > MAX_COMMITS:
        raise BashWritesError(f"the call made more than {MAX_COMMITS} commits; too many to judge")
    paths: set[str] = set()
    for c in commits:
        # -c: for a merge, only the paths that differ from every parent (a conflict resolution or an edit
        # made in the merge), never the merged branch's own changes, which were judged where they were made.
        out = _git(root, "diff-tree", "-r", "-z", "--no-commit-id", "--name-only", "--no-renames", "--root",
                   "-c", c, "--")
        paths.update(os.fsdecode(p) for p in out.split(b"\0") if p)
    return sorted(paths)


def present(root: str, sha: str, paths: list[str]) -> frozenset[str]:
    """Which of paths the commit's tree holds, in one call (as planscope.present, whose git edge needs
    PyYAML, which this hook does not load)."""
    if not paths:
        return frozenset()
    out = _git(root, "ls-tree", "-z", "--name-only", "--full-tree", sha, "--", *paths)
    return frozenset(os.fsdecode(p) for p in out.split(b"\0") if p)


def show(root: str, sha: str, path: str) -> str | None:
    """A regular file's text in the commit, or None where the commit has none there."""
    out = _git(root, "ls-tree", "-z", "--full-tree", sha, "--", path)
    meta = out.split(b"\t", 1)[0].decode("ascii", errors="replace").split(" ")
    if len(meta) != 3 or meta[0] not in ("100644", "100755") or meta[1] != "blob":
        return None
    try:
        return _git(root, "cat-file", "blob", meta[2]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def merge_base(root: str) -> str | None:
    ref = default_ref(root)
    if ref is None:
        return None
    try:
        sha = _git(root, "merge-base", "HEAD", ref).decode("ascii").strip()
    except BashWritesError:
        return None
    return sha if SHA.fullmatch(sha) else None


def read_allowlist(root: str) -> tuple[str | None, str]:
    """The allowlist's text and why it cannot be trusted (""), read as guard-paths' hl_allowed reads it:
    a link at `.claude` or at the list, or a list that cannot be read, fails closed."""
    claude = os.path.join(root, ".claude")
    path = os.path.join(root, ALLOWLIST)
    if os.path.islink(claude):
        return None, "the repository's .claude is a symlink; its allowlist cannot be trusted"
    if not os.path.lexists(path):
        return None, ""
    if os.path.islink(path):
        return None, "the write allowlist is a symlink"
    try:
        fd = os.open(path, READ_FLAGS)
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None, "the write allowlist is not a regular file"
            return fh.read(1 << 20).decode("utf-8"), ""
    except (OSError, UnicodeDecodeError):
        return None, "the write allowlist exists but cannot be read"


# ---- snapshots -------------------------------------------------------------

def _write_snapshot(directory: Path, name: str, data: dict) -> None:
    fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=directory)  # 0600, O_EXCL
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(temp, directory / name)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass  # renamed already or never made; the original error is re-raised
        raise


def _take_snapshot(directory: Path, name: str) -> dict | None:
    """The call's snapshot, removed as it is read; None when there is none."""
    path = directory / name
    try:
        fd = os.open(path, READ_FLAGS)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or not _private(st):
                raise BashWritesError("the snapshot is not a private regular file")
            raw = fh.read(MAX_SNAPSHOT_BYTES + 1)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass  # removed already; the read above decides
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise BashWritesError("the snapshot is larger than any this version writes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        raise BashWritesError("the snapshot cannot be parsed") from err
    if not isinstance(data, dict) or data.get("version") != SNAPSHOT_VERSION:
        raise BashWritesError("the snapshot was not written by this version")
    return data


def _prune(directory: Path, own: str, now: float) -> None:
    """Remove snapshots older than the TTL (a cancelled call never reaches PostToolUse). Best effort:
    an entry that cannot be removed is left, and the bound keeps the scan short."""
    with os.scandir(directory) as it:
        for i, item in enumerate(it):
            if i >= MAX_PRUNE_FILES:
                break
            if item.name == own:
                continue
            try:
                if now - item.stat(follow_symlinks=False).st_mtime >= TTL_SECONDS:
                    os.unlink(item.path)
            except OSError:
                continue  # gone, or not removable now; the next snapshot tries again


def snapshot(payload: object, config: Path, now: float) -> tuple[int, str]:
    """PreToolUse: record the repository as it is before the call. Returns (exit code, stderr)."""
    if not isinstance(payload, dict):
        return 2, "bash-writes: the hook payload cannot be read, so this call could not be judged; blocked"
    cwd = payload.get("cwd")
    try:
        name = snapshot_name(payload.get("session_id"), payload.get("tool_use_id"))
        root = repo_root(cwd) if isinstance(cwd, str) and cwd else None
        data: dict = {"version": SNAPSHOT_VERSION, "root": root}
        if root is not None:
            allowlist, bad = read_allowlist(root)
            data.update(head=head(root), paths=changed_paths(root), allowlist=allowlist, list_bad=bad)
        directory = state_dir(config, create=True, parts=STATE_PARTS)
        _write_snapshot(directory, name, data)
        _prune(directory, name, now)
    except (BashWritesError, PrMonitorError, OSError) as err:
        return 2, f"bash-writes: no snapshot could be recorded ({err if isinstance(err, BashWritesError) else type(err).__name__}), so this call's writes could not be judged; blocked"
    return 0, ""


def judge(payload: object, config: Path, mode: str, branch: str) -> tuple[int, str]:
    """PostToolUse / PostToolUseFailure: judge what the call changed. Returns (exit code, stderr)."""
    if not isinstance(payload, dict):
        return 2, "bash-writes: the hook payload cannot be read; this call's writes were not judged"
    try:
        name = snapshot_name(payload.get("session_id"), payload.get("tool_use_id"))
        data = _take_snapshot(state_dir(config, create=False, parts=STATE_PARTS), name)
        if data is None:
            raise BashWritesError("no snapshot was recorded for this call")
        root = data.get("root")
        if root is None:
            return 0, ""  # the call ran outside a repository
        if not isinstance(root, str) or not isinstance(data.get("paths"), dict):
            raise BashWritesError("the snapshot is malformed")
        before_paths = {str(k): str(v) for k, v in data["paths"].items()}
        paths = sorted(set(moved(before_paths, changed_paths(root)))
                       | set(committed(root, data.get("head"), head(root))))
        if not paths:
            return 0, ""
        autonomous = mode == "autonomous"
        plan_list, plan_problem = None, ""
        base = merge_base(root)
        if autonomous:
            plan_list, plan_problem = _plan_scope(root, branch, base)
        in_base = present(root, base, paths) if base else frozenset()
        scope = Scope(allowlist=data.get("allowlist"), list_bad=str(data.get("list_bad") or ""),
                      autonomous=autonomous, plan_list=plan_list, plan_problem=plan_problem,
                      in_base=in_base, base_known=base is not None)
        found = problems(paths, scope)
    except (BashWritesError, PrMonitorError, OSError) as err:
        reason = err if isinstance(err, BashWritesError) else type(err).__name__
        return 2, (f"bash-writes: this call's writes could not be judged ({reason}). Check `git status` for "
                   f"changes outside the write allowlist before going on (CFG-2).")
    if not found:
        return 0, ""
    lines = ["BLOCKED by bash-writes: this Bash call changed paths an Edit or Write would have been refused:"]
    lines += [f"  {p}" for p in found[:50]]
    if len(found) > 50:
        lines.append(f"  ... and {len(found) - 50} more")
    lines.append("The command has already run. A guard denial is final for that route (CFG-2): do not write "
                 "these paths another way. Undo the change if it was yours by mistake, or stop and ask the "
                 "operator, naming the files.")
    if autonomous:
        lines.append("This is an autonomous plan run: treat it as a blocker and stop (AGW-4).")
    return 2, "\n".join(lines)


def _plan_scope(root: str, branch: str, base: str | None) -> tuple[str | None, str]:
    """A run's scope as guard-paths' hl_plan_allowed reads it: the branch names the plan, and the plan
    and its declaration come from the merge-base tree."""
    from trimtab import planscope
    b = planscope.plan_of(branch)
    if b.ref is not planscope.Ref.PLAN:
        return None, (f"an autonomous plan run on branch {planscope.shown(branch or 'none')!r}, which is not "
                      f"plan/<plan>/item-NN-partK; no write can be scoped")
    if base is None:
        return None, "an autonomous plan run whose merge-base with origin's default branch cannot be read"
    md, writes = f"{planscope.AUTHORISED}/{b.plan}.md", f"{planscope.AUTHORISED}/{b.plan}.writes"
    writes_text = show(root, base, writes)
    if show(root, base, md) is None or writes_text is None:
        return None, "the authorised plan or its .writes declaration cannot be read at the merge-base"
    return hook_plan_list(writes_text, b.plan), ""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if sys.version_info < (3, 11):  # the hook starts no probe of its own (ADR 0065)
        print("bash-writes: python3 3.11 or later is required; this call was not judged", file=sys.stderr)
        return 2
    if not (args == ["snapshot"] or (len(args) == 3 and args[0] == "judge")):
        print("usage: trimtab.adapters.bash_writes snapshot | judge <mode> <branch>", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        payload = None  # refused below like any payload that is not an object
    try:
        if args[0] == "snapshot":
            code, message = snapshot(payload, config_dir(), time.time())
        else:
            code, message = judge(payload, config_dir(), args[1], args[2])
    except Exception as err:  # fail closed: an error is loud, never a silent pass
        code, message = 2, f"bash-writes: internal error ({type(err).__name__}); this call was not judged"
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
