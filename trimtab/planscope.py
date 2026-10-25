"""The plan-scope check: what a plan run's PR may change, judged from git alone (ADR 0042, building on ADR 0041).

guard-paths' plan-run layer judges each write in the session, where the agent
can write through the shell or forge its local branch and refs. This check
judges the outcome instead: the diff between a PR's base commit and its head,
with the base and the head branch name taken from the platform's event, which
the agent cannot forge.

A PR from `plan/<plan>/item-NN-partK` may change a path only when all hold:

- the path is not part of the repository's harness or controls (guard-paths'
  layer 1, which no list widens);
- the base commit's write allowlist lists it (layer 2);
- the base commit's `<plan>.writes` declaration lists it, or it is this PR's
  own progress record (`item-NN-partK.md`) or issue-register entry
  (`item-NN-partK-MM.md`, MM two or three digits) in the plan's folders, always create-only (layer 3,
  narrowed to the branch's item and part: the hook allows the whole folders);
- where either list makes it create-only, it is a new file: absent from the
  merge-base and from the base commit;
- it is a regular file: a plan run has no reason to add a link or a submodule.

Lists, the plan and its declaration all come from the base commit, never from
the PR, so a PR cannot widen what judges it. The PR must target the default
branch: a check run belongs to a commit, not to a PR, so a pass against another
base, whose lists the agent could have pushed, would count for the same head's
PR into the default branch. Globs read as guard-paths reads
them (`hl_match_lines`): `**` is `*`, which spans directories, a leading `+`
marks an entry create-only, a trailing `/` means everything under it. Bash's
`[^...]` is read as `[!...]`. A backslash escape is not supported, and bash's
extended forms (`!(x)`, `@(x)`), which `[[ == ]]` enables, are read literally:
both can only narrow what matches, never widen it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase

from trimtab.capture.sources import SourceError, git_bytes

MAX_CHANGES = 5000  # bounded: a diff longer than this is refused, not judged in part (ENG-6.2)
ALLOWLIST = ".claude/write-allowlist"
# Spelt in two pieces so the base's citation test, which refuses any plans
# directory path in a tracked file, does not read this as a citation.
PLANS_DIR = "docs/" + "plans"
AUTHORISED = f"{PLANS_DIR}/authorised"
PLAN_BRANCH = re.compile(r"plan/([A-Za-z0-9][A-Za-z0-9._-]*)/(item-[0-9]+-part[0-9]+)")
HEADS = "refs/heads/"
REGULAR_MODES = {"100644", "100755"}
# The caller's repository-steering variables are dropped, as hooks/session-mode.sh's sm_git does.
GIT_STEERING = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_CEILING_DIRECTORIES")


class PlanScopeError(Exception):
    """Git cannot answer, so no PR is vouched for."""


class Listed(Enum):
    NO = "no"
    YES = "yes"
    CREATE_ONLY = "create-only"


@dataclass(frozen=True)
class Entry:
    pattern: str
    create_only: bool


@dataclass(frozen=True)
class Change:
    status: str  # git's letter: A, D, M or T (renames are off, so a rename is a D and an A)
    path: str
    mode: str  # the mode at the head; 000000 for a deletion


def parse_list(text: str) -> tuple[Entry, ...]:
    """Entries in the allowlist's syntax, read as guard-paths' hl_match_lines reads them."""
    entries = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        create_only = line.startswith("+")
        if create_only:
            line = line[1:].lstrip()
            if not line:  # a bare `+` names no path
                continue
        pat = line.replace("**", "*")
        pat = pat.removeprefix("/").removeprefix("./")
        if pat.endswith("/"):
            pat += "*"
        entries.append(Entry(pat.replace("[^", "[!"), create_only))
    return tuple(entries)


def match(entries: tuple[Entry, ...], path: str) -> Listed:
    """A plain entry wins over a create-only one, as in hl_match_lines."""
    create_only = False
    for e in entries:
        if fnmatchcase(path, e.pattern):
            if not e.create_only:
                return Listed.YES
            create_only = True
    return Listed.CREATE_ONLY if create_only else Listed.NO


def refused(path: str) -> bool:
    """guard-paths' layer 1 (hl_refused): the harness and controls, compared lower-cased."""
    rel = path.lower()
    base = rel.rsplit("/", 1)[-1]
    if rel == ".claude/worktrees" or rel.startswith(".claude/worktrees/"):
        return False
    if rel in (".claude", ".github") or rel.startswith((".claude/", ".github/")):
        return True
    if rel in ("codeowners", "docs/codeowners", ".gitleaks.toml", ".trivyignore", ".bandit-allowlist.yml"):
        return True
    plans = AUTHORISED.lower()
    if rel == plans or rel.startswith(plans + "/"):
        return True
    return base in ("claude.md", "claude.local.md")


class Ref(Enum):
    PLAN = "plan"
    NOT_PLAN = "not a plan branch"
    MALFORMED = "malformed plan branch"


@dataclass(frozen=True)
class Branch:
    ref: Ref
    plan: str = ""
    item: str = ""  # item-NN-partK


def plan_of(head_ref: str) -> Branch:
    """The plan and item a head branch names. Any `plan/` branch that is not `plan/<plan>/item-NN-partK`
    is malformed, never skipped: guard-paths refuses every write a run makes on one."""
    name = head_ref.removeprefix(HEADS)
    if not name.startswith("plan/"):
        return Branch(Ref.NOT_PLAN)
    m = PLAN_BRANCH.fullmatch(name)
    if not m or ".." in m.group(1):
        return Branch(Ref.MALFORMED)
    return Branch(Ref.PLAN, m.group(1), m.group(2))


def scope(writes: str, plan: str, item: str) -> tuple[Entry, ...]:
    """The plan's declaration plus this PR's own progress record and register entries, always
    create-only. Tied to the branch's item and part, so one PR cannot write another item's record and
    steer which record the next session reads as the latest."""
    # Digit classes, not `*`: fnmatch's `*` spans `/`, which would allow nested files the skill never reads.
    register = f"+{PLANS_DIR}/unauthorised/{plan}/{item}-[0-9][0-9]"
    return parse_list(writes + "\n" + f"+{PLANS_DIR}/progress/{plan}/{item}.md\n"
                      + f"{register}.md\n{register}[0-9].md\n")


def judge(changes: list[Change], allowlist: tuple[Entry, ...], plan_scope: tuple[Entry, ...],
          in_base: frozenset[str]) -> list[str]:
    """Every problem with a plan PR's changes; none means in scope.

    in_base holds the changed paths present in the base commit, so a create-only
    path the PR adds is refused when the base already has it.
    """
    problems = []
    for c in changes:
        shown = _shown(c.path)
        if refused(c.path):
            problems.append(f"{shown}: a repository harness or control path; no plan covers it")
            continue
        listed = match(allowlist, c.path)
        if listed is Listed.NO:
            problems.append(f"{shown}: outside the base commit's write allowlist")
            continue
        in_plan = match(plan_scope, c.path)
        if in_plan is Listed.NO:
            problems.append(f"{shown}: outside the plan's scope (its .writes declaration, and this PR's own "
                            f"progress record and register entries)")
            continue
        if c.status != "D" and c.mode not in REGULAR_MODES:
            problems.append(f"{shown}: a link or submodule (mode {c.mode}); a plan run writes regular files only")
            continue
        if Listed.CREATE_ONLY in (listed, in_plan):
            if c.status != "A":
                problems.append(f"{shown}: {_verb(c.status)} a file already in the base, on a create-only path")
            elif c.path in in_base:
                problems.append(f"{shown}: added on a create-only path where the base commit already has it")
    return problems


def _shown(path: str) -> str:
    """A path safe to print: one holding a newline or another control character is escaped, so it
    cannot forge a log line or a CI runner's workflow command."""
    return path if path.isprintable() else ascii(path)


def _verb(status: str) -> str:
    return {"D": "deletes", "M": "modifies", "T": "changes the type of"}.get(status, f"changes ({status})")


@dataclass(frozen=True)
class Result:
    ref: Ref
    plan: str
    problems: tuple[str, ...]


# ---- git -----------------------------------------------------------------

def _git(repo: str, *args: str, stdin: bytes | None = None) -> bytes:
    # Hardened as hooks/session-mode.sh's sm_git is: no steering variables, no
    # replace objects, literal pathspecs, no fsmonitor.
    env = {k: v for k, v in os.environ.items() if k not in GIT_STEERING}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_LITERAL_PATHSPECS="1")
    try:
        return git_bytes(repo, "-c", "core.fsmonitor=false", *args, env=env, stdin=stdin)
    except SourceError as err:
        raise PlanScopeError(str(err)) from err


def _commit(repo: str, rev: str) -> str:
    out = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}")
    return out.decode("ascii").strip()


def read_files(repo: str, sha: str, paths: list[str]) -> dict[str, str | None]:
    """Each path's text in the commit, or None where it has no regular file: two git calls in all."""
    out = _git(repo, "ls-tree", "-z", "--full-tree", sha, "--", *paths)
    objects = {}
    for entry in out.split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.partition(b"\t")
        mode, _, obj = meta.decode("ascii").split(" ")
        if mode in REGULAR_MODES:
            objects[os.fsdecode(name)] = obj
    texts: dict[str, str | None] = dict.fromkeys(paths)
    if not objects:
        return texts
    batch = _git(repo, "cat-file", "--batch", stdin="".join(f"{o}\n" for o in objects.values()).encode("ascii"))
    pos = 0
    for path, obj in objects.items():
        # Each answer is `<object> blob <size>` and the content; anything else (`<object> missing`, a
        # short read) is git failing to answer, never a list to judge.
        try:
            header_end = batch.index(b"\n", pos)
            name, kind, size_text = batch[pos:header_end].decode("ascii").split(" ")
            size = int(size_text)
        except (ValueError, UnicodeDecodeError) as err:
            raise PlanScopeError(f"git cat-file --batch gave an unreadable answer for {path}") from err
        body = batch[header_end + 1:header_end + 1 + size]
        if name != obj or kind != "blob" or len(body) != size:
            raise PlanScopeError(f"git cat-file --batch gave an unreadable answer for {path}")
        pos = header_end + 1 + size + 1
        try:
            texts[path] = body.decode("utf-8")
        except UnicodeDecodeError as err:
            raise PlanScopeError(f"{path} at the base commit is not UTF-8") from err
    return texts


def changes(repo: str, base: str, head: str) -> list[Change]:
    """What `git diff base...head` changes, with renames and copies off."""
    # --ignore-submodules=none: a `submodule.<name>.ignore` setting would otherwise hide a gitlink change.
    # --no-relative: `diff.relative` would otherwise limit the diff to a subdirectory --repo names.
    out = _git(repo, "diff", "--raw", "-z", "--no-renames", "--no-ext-diff", "--no-textconv",
               "--ignore-submodules=none", "--no-relative", "--end-of-options", f"{base}...{head}")
    fields = out.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise PlanScopeError("git diff --raw returned an entry that could not be parsed")
    if len(fields) // 2 > MAX_CHANGES:
        raise PlanScopeError(f"the diff changes more than {MAX_CHANGES} files; split the PR")
    found = []
    for meta, path in zip(fields[0::2], fields[1::2]):
        try:
            _, mode, _, _, status = meta.decode("ascii").lstrip(":").split(" ")
        except (UnicodeDecodeError, ValueError) as err:
            raise PlanScopeError("git diff --raw returned an entry that could not be parsed") from err
        found.append(Change(status[:1], os.fsdecode(path), mode))
    return found


def present(repo: str, sha: str, paths: list[str]) -> frozenset[str]:
    """Which of paths the commit's tree holds, in one call."""
    if not paths:
        return frozenset()
    out = _git(repo, "ls-tree", "-z", "--name-only", "--full-tree", sha, "--", *paths)
    return frozenset(os.fsdecode(p) for p in out.split(b"\0") if p)


def check(repo: str, base: str, head: str, head_ref: str, base_ref: str, default_branch: str) -> Result:
    """Judge a PR. base, head_ref, base_ref and default_branch come from the platform's event; head is
    the PR's head commit, always named: the job runs from a base checkout, where HEAD is the base."""
    branch = plan_of(head_ref)
    if branch.ref is not Ref.PLAN:
        return Result(branch.ref, "", ())
    plan = branch.plan
    if base_ref.removeprefix(HEADS) != default_branch.removeprefix(HEADS):
        return Result(branch.ref, plan, (f"the PR targets {base_ref!r}, not the default branch "
                                         f"{default_branch!r}; only the default branch's lists can scope it",))
    base_sha, head_sha = _commit(repo, base), _commit(repo, head)
    plan_md, writes_path = f"{AUTHORISED}/{plan}.md", f"{AUTHORISED}/{plan}.writes"
    texts = read_files(repo, base_sha, [ALLOWLIST, plan_md, writes_path])
    missing = [what for what, path in (("the write allowlist", ALLOWLIST), ("the authorised plan", plan_md),
                                       ("its .writes declaration", writes_path)) if texts[path] is None]
    if missing:
        return Result(branch.ref, plan, tuple(f"{m} is not a regular file in the base commit; nothing can be "
                                              f"scoped" for m in missing))
    found = changes(repo, base_sha, head_sha)
    added = [c.path for c in found if c.status == "A"]
    problems = judge(found, parse_list(texts[ALLOWLIST]), scope(texts[writes_path], plan, branch.item),
                     present(repo, base_sha, added))
    return Result(branch.ref, plan, tuple(problems))
