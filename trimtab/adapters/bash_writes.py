"""The Bash write detector: what one Bash call changed in its repository, judged as guard-paths judges a write (ADR 0066).

guard-paths judges the Edit-family tools against the repository's write allowlist (layer 2) and, in an
autonomous plan run, the plan's scope (layer 3) before they write. It cannot do that for Bash, whose
writing commands cannot be listed (ADR 0039). The paths a command changed can be: this module compares
the repository before and after the call, and judges every path the call changed by the same rules.

It detects; it cannot prevent, and it is no defence against a command built to evade it (ADR 0066
lists the routes). The call has run by the time it judges, so a refusal reaches the agent as loud
feedback naming the paths (CFG-2: undo the change, or stop and ask), and in a run as a blocker. The
merge gates (CODEOWNERS, admin merge, the plan-scope check of ADR 0042) stay the guarantee.

The pure part comes first: parsing git's listings, the fingerprint a path is known by, which paths moved
between two snapshots, and the problems with those paths. Data in, value out.

The edge comes last, run by hooks/bash-writes.sh through hooks/run_module.py (ADR 0035) as
`trimtab.adapters.bash_writes` with:

    snapshot <mode> <branch>  PreToolUse (Bash). Records the repository's changed and index-flagged
                              paths, every branch and remote-tracking tip, the write allowlist's text
                              and the mode, keyed by session and tool_use_id. Exit 0, or 2 (blocks the
                              call) when no snapshot could be recorded, since nothing could be judged.
    judge <mode> <branch>     PostToolUse and PostToolUseFailure (Bash). Exit 0, or 2 with the
                              problems, or with why the call could not be judged, on stderr.

mode and branch come from hooks/session-mode.sh's sm_classify, so the mode judged here is the one the
guard enforces (ADR 0041); the call is judged autonomous when either phase, or the judged repository's
own branch, says so. Snapshots live at `<config dir>/guard-state/bash-writes/`, private to this user,
as pr_monitor keeps its lists (ADR 0028). No output carries a session ID, a tool_use_id or a file's
contents. Each phase has one deadline for all its git calls, inside the hook's 10 s timeout.
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

SNAPSHOT_VERSION = 3
STATE_PARTS = ("guard-state", "bash-writes")
ALLOWLIST = ".claude/write-allowlist"
PLANS_DIR = "docs/" + "plans"  # in two pieces, as in planscope, so the citation test does not read a path
MODES = ("interactive", "autonomous")
# Bounds (ENG-6.2). More than these is refused as too much to judge, never judged in part.
MAX_PATHS = 20000
MAX_REFS = 5000
MAX_COMMITS = 200
MAX_LIST_BYTES = 1 << 20
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
TTL_SECONDS = 24 * 60 * 60  # a snapshot whose call never reached a post event (a cancel) is pruned after this
MAX_PRUNE_FILES = 1000
# One deadline per phase for every git call together (ENG-6.4), well inside the hook's 10 s timeout:
# a hook killed at its timeout does not report, so the adapter must give up first and say so.
PHASE_SECONDS = 7.0
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}")
SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
# The same steering variables and pins as trimtab.planscope._git, which this module does not import for
# its git edge (it would load PyYAML). test_bash_writes pins the two lists equal.
GIT_STEERING = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_CEILING_DIRECTORIES",
                "GIT_GRAFT_FILE", "GIT_NAMESPACE", "GIT_REPLACE_REF_BASE", "GIT_CONFIG_PARAMETERS")
GIT_PINNED = (("core.fsmonitor", "false"), ("core.commitGraph", "false"), ("log.showSignature", "false"),
              ("log.follow", "false"), ("diff.renames", "false"))
# And the settings that decide whether git status sees a change at all, which a command can weaken in
# the repository's own config (found in review): pinned to git's defaults for this detector's reads.
STATUS_PINNED = (("core.trustctime", "true"), ("core.checkStat", "default"), ("core.fileMode", "true"),
                 ("core.untrackedCache", "false"), ("core.ignoreCase", "false"))


class BashWritesError(Exception):
    """The repository or the snapshot cannot be read, so nothing the call did can be judged."""


# ---- pure ------------------------------------------------------------------

def parse_status(out: bytes) -> dict[str, str]:
    """Each path `git status --porcelain=v1 -z --no-renames --untracked-files=all` lists, with its XY
    status. A path listed twice (a staged deletion and the same path untracked) keeps both.

    With renames off every entry is `XY <path>` and NUL; anything else is git answering in a form this
    version does not read, never a clean tree."""
    found: dict[str, str] = {}
    for entry in out.split(b"\0"):
        if not entry:
            continue
        if len(entry) < 4 or entry[2:3] != b" ":
            raise BashWritesError("git status gave an entry that could not be parsed")
        path = os.fsdecode(entry[3:])
        found[path] = found.get(path, "") + entry[:2].decode("ascii", errors="replace")
        if len(found) > MAX_PATHS:
            raise BashWritesError(f"more than {MAX_PATHS} changed paths; too many to judge")
    return found


def parse_flagged(out: bytes, on_disk=lambda path: True) -> dict[str, str]:
    """Tracked paths `git ls-files -v -z` marks skip-worktree (`S`) or assume-unchanged (a lower-case
    tag), which `git status` never lists however they change, with their tag.

    Only a flagged path that is on the disk can have been edited behind git's back. A sparse checkout
    flags every file it leaves out, and those are not on the disk, so they are skipped: the count of
    flagged paths then follows what is checked out, not the size of the repository."""
    found: dict[str, str] = {}
    for entry in out.split(b"\0"):
        if not entry:
            continue
        if len(entry) < 3 or entry[1:2] != b" ":
            raise BashWritesError("git ls-files gave an entry that could not be parsed")
        tag = entry[:1].decode("ascii", errors="replace")
        if tag == "S" or tag.islower():
            path = os.fsdecode(entry[2:])
            if not on_disk(path):
                continue
            found[path] = tag
            if len(found) > MAX_PATHS:
                raise BashWritesError(f"more than {MAX_PATHS} flagged paths on the disk; too many to judge")
    return found


def fingerprint(status: str, st: os.stat_result | None) -> str:
    """What a path is known by: git's status for it, then type, size, inode and both clocks. ctime
    catches a write that reset the mtime; the status catches a change to the index alone."""
    if st is None:
        return f"{status}|absent"
    return f"{status}|{stat.S_IFMT(st.st_mode):o}:{st.st_size}:{st.st_ino}:{st.st_mtime_ns}:{st.st_ctime_ns}"


def _restored(before: str | None, after: str) -> bool:
    """Whether a path git listed as changed before the call is clean now, seen only by the harness walk:
    its content is HEAD's again (a `git restore`), or a commit took it, which the commit check covers.
    An untracked path (`??`) that went quiet was not restored; it was hidden, so it is judged."""
    if before is None or not after.startswith("[fs]|"):
        return False
    status = before.split("|", 1)[0]
    return bool(status) and not status.startswith("[fs]") and "??" not in status


# Index-only moves of a path the call did not touch: `git add` of a path that was already modified,
# untracked or deleted. The same lstat part with one of these status moves is staging, not writing.
# Anything else that moves the status alone (`git rm --cached` leaves `D` and `??`) is still reported.
STAGED = frozenset({(" M", "M "), ("??", "A "), (" D", "D "), (" T", "T ")})


def _staged_only(before: str | None, after: str) -> bool:
    if before is None:
        return False
    old, _, old_stat = before.partition("|")
    new, _, new_stat = after.partition("|")
    return old_stat == new_stat and (old, new) in STAGED


def moved(before: dict[str, str], after: dict[str, str], on_disk) -> list[str]:
    """Paths listed after the call that were not listed before it or whose fingerprint differs, and
    harness paths listed before and gone after, from git's listing and from the disk.

    A path changed before and clean after is not judged: its content is HEAD's again (a `git
    restore`), or a commit took it, which the commit check covers. A harness entry git does not track
    (a gitignored `CLAUDE.local.md`, `.claude/` with nothing tracked) has no HEAD content to return
    to, so its going from the disk is a write (found in review); `on_disk(path)` says whether a path
    is still there."""
    from trimtab import planscope  # its pure rules only; no git

    changed = {p for p, fp in after.items() if before.get(p) != fp
               and not _restored(before.get(p), fp) and not _staged_only(before.get(p), fp)}
    gone = {p for p in before if p not in after and planscope.refused(p) and not on_disk(p)}
    return sorted(changed | gone)


def judged_mode(pre_mode: str, post_mode: str, pre_branch: str, root_branch: str) -> bool:
    """Whether the call is judged as an autonomous run: the mode only tightens (ADR 0041), so either
    phase's classification, or a plan branch in the judged repository at either phase, makes it one."""
    for mode in (pre_mode, post_mode):
        if mode not in MODES:
            raise BashWritesError(f"the session mode {mode!r} is not one the classifier gives")
    return "autonomous" in (pre_mode, post_mode) or any(b.startswith("plan/") for b in (pre_branch, root_branch))


@dataclass(frozen=True)
class Scope:
    """What the paths are judged against, as guard-paths' hl_check_abs reads it."""
    allowlist: str | None  # the list's text, or None where the repository has none
    list_bad: str  # why a list that exists cannot be trusted (a link, unreadable), or ""
    autonomous: bool
    plan_list: str | None  # in a run: the plan's hook scope in the list syntax, or None when unscopable
    plan_problem: str  # in a run, why no write can be scoped, or ""
    in_base: frozenset[str]  # of the create-only paths, those the merge-base tree holds
    base_known: bool  # whether the merge-base could be read at all


def create_only_paths(paths: list[str], allowlist: str | None, plan_list: str | None) -> list[str]:
    """The paths a create-only entry on either list covers: the only ones whose presence on the default
    branch needs reading."""
    from trimtab import planscope  # its pure rules only; no git

    allow = planscope.parse_list(allowlist) if allowlist is not None else ()
    plan = planscope.parse_list(plan_list) if plan_list is not None else ()
    return [p for p in paths if planscope.create_only(allow, plan, p)]


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


def report(found: list[str], autonomous: bool) -> str:
    lines = ["BLOCKED by bash-writes: this Bash call changed paths an Edit or Write would have been refused:"]
    lines += [f"  {p}" for p in found[:50]]
    if len(found) > 50:
        lines.append(f"  ... and {len(found) - 50} more")
    lines.append("The command has already run. A guard denial is final for that route (CFG-2): do not write "
                 "these paths another way. Undo the change if it was yours by mistake, or stop and ask the "
                 "operator, naming the files.")
    if autonomous:
        lines.append("This is an autonomous plan run: treat it as a blocker and stop (AGW-4).")
    return "\n".join(lines)


# ---- git -------------------------------------------------------------------

class Deadline:
    """One time budget for every git call in a phase."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self.clock = clock
        self.end = clock() + seconds

    def remaining(self) -> float:
        left = self.end - self.clock()
        if left <= 0:
            raise BashWritesError("the phase ran out of time before git answered")
        return left


# Layer-1 files git may never list: `.claude/` is commonly gitignored, and its settings, hooks and rules
# take effect in the session without reaching a PR. Fingerprinted from the filesystem, not from git.
HARNESS_FILES = ("CLAUDE.md", "CLAUDE.local.md")
HARNESS_DIR = ".claude"
HARNESS_SKIP = ".claude/worktrees"


def harness_paths(root: str, deadline: "Deadline | None" = None) -> dict[str, os.stat_result]:
    """Every entry under the root's `.claude/` (but `worktrees/`), and the root's instruction files, by
    lstat: links are recorded, never followed. Bounded by MAX_PATHS, and by the phase deadline when one
    is given: it is checked once per directory, so a slow filesystem runs out of time here, loudly,
    before the hook's own timeout kills the hook silently."""
    found: dict[str, os.stat_result] = {}
    for name in HARNESS_FILES:
        try:
            found[name] = os.lstat(os.path.join(root, name))
        except FileNotFoundError:
            continue  # absent: a later write shows as a new entry
    top = os.path.join(root, HARNESS_DIR)
    try:
        st = os.lstat(top)
    except FileNotFoundError:
        return found
    found[HARNESS_DIR] = st
    if not stat.S_ISDIR(st.st_mode):
        return found
    pending = [HARNESS_DIR]
    while pending:
        if deadline is not None:
            deadline.remaining()
        rel = pending.pop()
        with os.scandir(os.path.join(root, rel)) as it:
            for entry in it:
                child = f"{rel}/{entry.name}"
                if child == HARNESS_SKIP:
                    continue
                found[child] = entry.stat(follow_symlinks=False)
                if len(found) > MAX_PATHS:
                    raise BashWritesError(f"more than {MAX_PATHS} harness entries; too many to judge")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(child)
    return found


class Repo:
    """A repository named by its root, read with git told the root outright: `--git-dir` and
    `--work-tree` outrank `core.bare`, `core.worktree` and any steering variable the command set."""

    def __init__(self, root: str, deadline: Deadline):
        self.root = root
        self.deadline = deadline

    def run(self, *args: str, ok: tuple[int, ...] = (0,), stdin: bytes | None = None) -> tuple[int, bytes]:
        env = {k: v for k, v in os.environ.items()
               if k not in GIT_STEERING and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
               and k != "GIT_CONFIG_COUNT"}
        # GIT_OPTIONAL_LOCKS=0: status must not take the index lock a concurrent command may want.
        env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_LITERAL_PATHSPECS="1", GIT_OPTIONAL_LOCKS="0")
        pins = [arg for key, value in GIT_PINNED + STATUS_PINNED for arg in ("-c", f"{key}={value}")]
        argv = ["git", f"--git-dir={os.path.join(self.root, '.git')}", f"--work-tree={self.root}",
                "-C", self.root, *pins, *args]
        try:
            done = subprocess.run(argv, capture_output=True, env=env, input=stdin,
                                  timeout=self.deadline.remaining())
        except (OSError, subprocess.TimeoutExpired) as err:
            raise BashWritesError(f"git {args[0]}: {type(err).__name__}") from err
        if done.returncode not in ok:
            raise BashWritesError(f"git {args[0]} exited {done.returncode}")
        return done.returncode, done.stdout

    def out(self, *args: str, stdin: bytes | None = None) -> bytes:
        return self.run(*args, stdin=stdin)[1]

    def head(self) -> str | None:
        """HEAD's commit, or None on an unborn branch; any other failure raises."""
        rc, out = self.run("rev-parse", "--verify", "-q", "HEAD^{commit}", ok=(0, 1))
        if rc == 0:
            sha = out.decode("ascii", errors="replace").strip()
            if SHA.fullmatch(sha):
                return sha
            raise BashWritesError("git rev-parse named HEAD in a form this version does not read")
        # Unborn only when HEAD is a symbolic ref to a branch that has no commit yet.
        rc, ref = self.run("symbolic-ref", "-q", "HEAD", ok=(0, 1))
        if rc == 0 and ref.startswith(b"refs/heads/"):
            rc, _ = self.run("show-ref", "--verify", "-q", ref.decode("utf-8", errors="replace").strip(),
                             ok=(0, 1))
            if rc == 1:
                return None
        raise BashWritesError("HEAD names no commit")

    def branch(self) -> str:
        """The checked-out branch, or "" when HEAD is detached; read as hooks/session-mode.sh's sm_branch."""
        rc, ref = self.run("symbolic-ref", "-q", "HEAD", ok=(0, 1))
        text = ref.decode("utf-8", errors="replace").strip()
        return text.removeprefix("refs/heads/") if rc == 0 and text.startswith("refs/heads/") else ""

    def listed(self) -> dict[str, str]:
        """Every path git status lists, and every index-flagged path, with its fingerprint."""
        status = parse_status(self.out("status", "--porcelain=v1", "-z", "--no-renames",
                                       "--untracked-files=all", "--ignore-submodules=none"))
        def on_disk(path: str) -> bool:
            return os.path.lexists(os.path.join(self.root, path))

        for path, tag in parse_flagged(self.out("ls-files", "-v", "-z"), on_disk).items():
            status[path] = status.get(path, "") + f"[{tag}]"
        if len(status) > MAX_PATHS:
            raise BashWritesError(f"more than {MAX_PATHS} paths to fingerprint; too many to judge")
        try:
            harness = harness_paths(self.root, self.deadline)
        except OSError as err:
            raise BashWritesError(f"cannot read the repository's harness files ({type(err).__name__})") from err
        found = {path: fingerprint("[fs]", st) for path, st in harness.items() if path not in status}
        for path, xy in status.items():
            self.deadline.remaining()  # one clock read per path: the lstat loop is under the deadline too
            try:
                st = os.lstat(os.path.join(self.root, path))
            except FileNotFoundError:
                st = None
            except OSError as err:
                raise BashWritesError(f"cannot read a changed path ({type(err).__name__})") from err
            found[path] = fingerprint(xy, st)
        return found

    def tips(self) -> list[str]:
        """Every local branch and remote-tracking tip, and HEAD's commit."""
        out = self.out("for-each-ref", "--format=%(objectname)", "refs/heads", "refs/remotes")
        shas = {s for s in out.decode("ascii", errors="replace").split() if SHA.fullmatch(s)}
        head = self.head()
        if head:
            shas.add(head)
        if len(shas) > MAX_REFS:
            raise BashWritesError(f"more than {MAX_REFS} branch tips; too many to judge")
        return sorted(shas)

    def committed(self, before: list[str], taken: int) -> list[str]:
        """Paths changed by commits the call made on any branch: reachable from a tip now that was not a
        tip before, from no tip before, and committed since the snapshot was taken. The time is what
        tells the call's own commits, pushed or not, from others' work a fetch or pull brought in. A
        merge counts only the paths that differ from every parent."""
        new = sorted(set(self.tips()) - set(before))
        if not new:
            return []
        # A second's slack for the clock's resolution; a commit dated earlier on purpose is evasion.
        out = self.out("rev-list", f"--max-count={MAX_COMMITS + 1}", f"--max-age={taken - 1}", *new,
                       "--not", *before, "--")
        commits = [c for c in out.decode("ascii", errors="replace").split() if c]
        if len(commits) > MAX_COMMITS:
            raise BashWritesError(f"the call made more than {MAX_COMMITS} commits; too many to judge")
        if not commits:
            return []
        out = self.out("diff-tree", "--stdin", "-r", "-z", "--no-commit-id", "--name-only", "--no-renames",
                       "--root", "-c", stdin="".join(f"{c}\n" for c in commits).encode("ascii"))
        return sorted({os.fsdecode(p) for p in out.split(b"\0") if p})

    def merge_base(self) -> str | None:
        rc, ref = self.run("symbolic-ref", "-q", "refs/remotes/origin/HEAD", ok=(0, 1))
        if rc != 0:
            return None
        rc, out = self.run("merge-base", "HEAD", ref.decode("utf-8", errors="replace").strip(), ok=(0, 1))
        sha = out.decode("ascii", errors="replace").strip()
        return sha if rc == 0 and SHA.fullmatch(sha) else None

    def present(self, sha: str, paths: list[str]) -> frozenset[str]:
        """Which of paths the commit's tree holds, in one call."""
        if not paths:
            return frozenset()
        out = self.out("ls-tree", "-z", "--name-only", "--full-tree", sha, "--", *paths)
        return frozenset(os.fsdecode(p) for p in out.split(b"\0") if p)

    def show(self, sha: str, path: str) -> str | None:
        """A regular file's text in the commit, or None where the commit has none there."""
        out = self.out("ls-tree", "-z", "--full-tree", sha, "--", path)
        meta = out.split(b"\t", 1)[0].decode("ascii", errors="replace").split(" ")
        if len(meta) != 3 or meta[0] not in ("100644", "100755") or meta[1] != "blob":
            return None
        try:
            return self.out("cat-file", "blob", meta[2]).decode("utf-8")
        except UnicodeDecodeError:
            return None


def find_root(cwd: object) -> str | None:
    """The repository the call ran in: the nearest ancestor holding a `.git` entry, as guard-paths'
    hl_find_root finds it (a worktree is its own repository), or None outside one. A cwd that is not
    an absolute, existing directory cannot be placed, so it raises."""
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise BashWritesError("the hook payload names no working directory that exists")
    d = os.path.normpath(cwd)
    while True:
        if os.path.lexists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def read_allowlist(root: str) -> tuple[str | None, str]:
    """The allowlist's text and why it cannot be trusted (""), read as guard-paths' hl_allowed reads it:
    a link at `.claude` or at the list, a list that cannot be read, or one larger than any written by
    hand, fails closed."""
    claude = os.path.join(root, ".claude")
    path = os.path.join(root, ALLOWLIST)
    try:
        if stat.S_ISLNK(os.lstat(claude).st_mode):
            return None, "the repository's .claude is a symlink; its allowlist cannot be trusted"
    except FileNotFoundError:
        return None, ""
    except OSError:
        return None, "the repository's .claude cannot be read; its allowlist cannot be trusted"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, ""
    except OSError:
        return None, "the write allowlist exists but cannot be read"
    if stat.S_ISLNK(st.st_mode):
        return None, "the write allowlist is a symlink"
    try:
        fd = os.open(path, READ_FLAGS)
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None, "the write allowlist is not a regular file"
            raw = fh.read(MAX_LIST_BYTES + 1)
    except OSError:
        return None, "the write allowlist exists but cannot be read"
    if len(raw) > MAX_LIST_BYTES:
        return None, "the write allowlist is larger than any this version reads"
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "the write allowlist is not UTF-8"


# ---- snapshots -------------------------------------------------------------

def _write_snapshot(directory: Path, name: str, data: dict) -> None:
    """Replace the snapshot atomically, as pr_monitor._write_list replaces a list."""
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
    """Remove snapshots older than the TTL (a cancelled call reaches no post event). Best effort: an
    entry that cannot be removed now is left for the next snapshot, and the bound keeps the scan short."""
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


def _valid_snapshot(data: dict) -> dict:
    """The snapshot's fields, or BashWritesError when any is missing or of another shape."""
    root = data.get("root")
    if root is None:
        return data
    paths, tips = data.get("paths"), data.get("tips")
    if (not isinstance(root, str) or not isinstance(paths, dict) or not isinstance(tips, list)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in paths.items())
            or not all(isinstance(t, str) and SHA.fullmatch(t) for t in tips)
            or not isinstance(data.get("allowlist"), (str, type(None)))
            or not isinstance(data.get("list_bad"), str)
            or data.get("mode") not in MODES or not isinstance(data.get("branch"), str)
            or not isinstance(data.get("taken"), int) or isinstance(data.get("taken"), bool)):
        raise BashWritesError("the snapshot is malformed")
    return data


def snapshot(payload: object, config: Path, now: float, mode: str, branch: str,
             deadline: Deadline) -> tuple[int, str]:
    """PreToolUse: record the repository as it is before the call. Returns (exit code, stderr)."""
    try:
        if not isinstance(payload, dict):
            raise BashWritesError("the hook payload cannot be read")
        if mode not in MODES:
            raise BashWritesError(f"the session mode {mode!r} is not one the classifier gives")
        name = snapshot_name(payload.get("session_id"), payload.get("tool_use_id"))
        root = find_root(payload.get("cwd"))
        data: dict = {"version": SNAPSHOT_VERSION, "root": root}
        if root is not None:
            repo = Repo(root, deadline)
            allowlist, bad = read_allowlist(root)
            data.update(paths=repo.listed(), tips=repo.tips(), allowlist=allowlist, list_bad=bad,
                        mode=mode, branch=repo.branch() or branch, taken=int(now))
        directory = state_dir(config, create=True, parts=STATE_PARTS)
        _write_snapshot(directory, name, data)
        _prune(directory, name, now)
    except (BashWritesError, PrMonitorError, OSError) as err:
        reason = err if isinstance(err, BashWritesError) else type(err).__name__
        return 2, f"bash-writes: no snapshot could be recorded ({reason}), so this call's writes could not be judged; blocked"
    return 0, ""


def judge(payload: object, config: Path, mode: str, branch: str, deadline: Deadline) -> tuple[int, str]:
    """PostToolUse / PostToolUseFailure: judge what the call changed. Returns (exit code, stderr)."""
    try:
        if not isinstance(payload, dict):
            raise BashWritesError("the hook payload cannot be read")
        name = snapshot_name(payload.get("session_id"), payload.get("tool_use_id"))
        data = _take_snapshot(state_dir(config, create=False, parts=STATE_PARTS), name)
        if data is None:
            raise BashWritesError("no snapshot was recorded for this call. If this follows every Bash call, "
                                  "the pre and post events do not share a tool_use_id: export "
                                  "HOOK_ALLOW_BASH_WRITES=1 and report it (ADR 0066)")
        data = _valid_snapshot(data)
        root = data["root"]
        if root is None:
            return 0, ""  # the call started outside any repository
        repo = Repo(root, deadline)
        def on_disk(path: str) -> bool:
            return os.path.lexists(os.path.join(root, path))

        paths = sorted(set(moved(data["paths"], repo.listed(), on_disk))
                       | set(repo.committed(data["tips"], data["taken"])))
        if not paths:
            return 0, ""
        root_branch = repo.branch()
        autonomous = judged_mode(data["mode"], mode, data["branch"], root_branch)
        plan_list, plan_problem = None, ""
        base = repo.merge_base()
        if autonomous:
            plan_list, plan_problem = _plan_scope(repo, root_branch or data["branch"] or branch, base)
        need = create_only_paths(paths, data["allowlist"], plan_list)
        in_base = repo.present(base, need) if base else frozenset()
        found = problems(paths, Scope(allowlist=data["allowlist"], list_bad=data["list_bad"], autonomous=autonomous,
                                      plan_list=plan_list, plan_problem=plan_problem, in_base=in_base,
                                      base_known=base is not None))
    except (BashWritesError, PrMonitorError, OSError) as err:
        reason = err if isinstance(err, BashWritesError) else type(err).__name__
        return 2, (f"bash-writes: this call's writes could not be judged ({reason}). Check `git status` for "
                   f"changes outside the write allowlist before going on (CFG-2).")
    return (2, report(found, autonomous)) if found else (0, "")


def _plan_scope(repo: Repo, branch: str, base: str | None) -> tuple[str | None, str]:
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
    writes_text = repo.show(base, writes)
    if repo.show(base, md) is None or writes_text is None:
        return None, "the authorised plan or its .writes declaration cannot be read at the merge-base"
    return hook_plan_list(writes_text, b.plan), ""


def main(argv: list[str] | None = None) -> int:
    deadline = Deadline(PHASE_SECONDS)
    args = sys.argv[1:] if argv is None else argv
    if sys.version_info < (3, 11):  # the hook starts no probe of its own (ADR 0066)
        print("bash-writes: python3 3.11 or later is required; this call was not judged", file=sys.stderr)
        return 2
    if len(args) != 3 or args[0] not in ("snapshot", "judge"):
        print("usage: trimtab.adapters.bash_writes snapshot|judge <mode> <branch>", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        payload = None  # refused below like any payload that is not an object
    try:
        if args[0] == "snapshot":
            code, message = snapshot(payload, config_dir(), time.time(), args[1], args[2], deadline)
        else:
            code, message = judge(payload, config_dir(), args[1], args[2], deadline)
    except Exception as err:  # fail closed: an error is loud, never a silent pass
        code, message = 2, f"bash-writes: internal error ({type(err).__name__}); this call was not judged"
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
