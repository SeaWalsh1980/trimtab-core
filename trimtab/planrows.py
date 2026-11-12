"""The plan-run write rows E12, E14-E17 and E19 as pure decisions (ADR 0063 §5, AGW-4).

guard-github calls this when a command writes a `plan/*` ref. Everything here is a function of
values handed in, so a test can set each fact: what the command writes (`read_push`, or the REST
spelling the adapter reads), the environment (`plan_run`), and what GitHub and git say (`Facts`).
`trimtab/planfacts.py` is the `Facts` that asks them. A failed read is a refusal, never a pass.

Rows judged, in order, for each ref write:

- E16: deleting a `plan/*` ref refuses, in any mode;
- E15: a non-fast-forward write refuses, in any mode (`--force`, `--force-with-lease`,
  `--force-if-includes`, a `+` refspec, `--mirror`), which is tighter than ADR 0036;
- E14: a `plan/*` write refuses unless `TRIMTAB_PLAN_RUN=1` is in the hook's environment;
- E17: a push to a branch whose open PR is ready refuses, unless the push has the E7
  register-commit shape;
- E19: while the secret-scan job's records for the branch's latest pushed head fail or cannot
  be read, only a push touching only the part's progress record goes through;
- E12: a push that would make the fourth CI-fix round refuses.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

PLAN_PREFIX = "plan/"
MAX_ROUNDS = 3  # AGW-4: at most three CI fix rounds
# `plan/NNNN-<slug>/item-NN-partK` (AGW-1): the plan, then the part this branch is.
ITEM_BRANCH = re.compile(r"plan/(?P<plan>[0-9]{4}-[a-z0-9-]+)/item-(?P<item>[0-9]{2})-part(?P<part>[0-9]+)")
REGISTER_FILE = re.compile(r"item-(?P<item>[0-9]{2})-part(?P<part>[0-9]+)-[0-9]{2}\.md")
LINK_LINE = re.compile(r"\[[^\]\n]*\]\((?P<target>[^)\s]+)\)")


class Refusal(Exception):
    """A row refuses the write; the message names the row."""


class FactsError(Exception):
    """A fact a row needs could not be read, so the row refuses."""


@dataclass(frozen=True)
class Write:
    """One ref write: the branch, the local source it takes, and how it writes."""

    branch: str
    source: str | None = None
    force: bool = False
    delete: bool = False


@dataclass(frozen=True)
class PushRead:
    writes: tuple[Write, ...] = ()
    broad: str | None = None  # "mirror", "prune" or "all": writes this reading cannot list
    unreadable: str | None = None  # why a destination cannot be read


@dataclass(frozen=True)
class PrState:
    number: int
    draft: bool
    created: str = ""  # RFC 3339, UTC: when the PR opened


@dataclass(frozen=True)
class Push:
    """A branch-activity event: the head before and after."""

    before: str
    after: str


@dataclass(frozen=True)
class Change:
    """One path a push changes against the branch's remote head."""

    path: str
    status: str  # "A", "M" or "D"
    added: tuple[str, ...] = ()
    removed: int = 0


class Facts:
    """What the rows read from GitHub and git. A failed read raises FactsError."""

    def open_pr(self, branch: str) -> PrState | None: raise NotImplementedError
    def branch_head(self, branch: str) -> str | None: raise NotImplementedError
    def secret_scan(self, head: str) -> str: raise NotImplementedError  # pass, pending, fail or unreadable
    def changes(self, branch: str, source: str | None, head: str) -> Sequence[Change]: raise NotImplementedError
    def pushes(self, branch: str) -> Sequence[Push]: raise NotImplementedError
    def failing_heads(self, branch: str) -> set[str]: raise NotImplementedError
    def facts_run_heads(self, branch: str) -> set[str]: raise NotImplementedError
    def conflicted(self, number: int) -> bool: raise NotImplementedError
    def register_answer(self, branch: str, before: str, after: str) -> bool: raise NotImplementedError
    def local_plan_branches(self) -> Sequence[str]: raise NotImplementedError
    def pr_head(self, number: int, repo: str | None) -> str | None: raise NotImplementedError  # None: not a PR


# ---- reading `git push` ------------------------------------------------------------------

_LONG = {
    "all": "all", "branches": "all", "prune": "prune", "mirror": "mirror", "delete": "delete", "tags": "tags",
    "follow-tags": "flag", "no-follow-tags": "flag", "signed": "optional", "no-signed": "flag",
    "atomic": "flag", "no-atomic": "flag", "push-option": "valued", "receive-pack": "valued",
    "exec": "valued", "repo": "repo", "force-with-lease": "force-optional", "no-force-with-lease": "flag",
    "force-if-includes": "force", "no-force-if-includes": "flag", "force": "force", "dry-run": "flag",
    "porcelain": "flag", "thin": "flag", "no-thin": "flag", "quiet": "flag", "verbose": "flag",
    "progress": "flag", "no-progress": "flag", "recurse-submodules": "valued", "no-recurse-submodules": "flag",
    "verify": "flag", "no-verify": "flag", "ipv4": "flag", "ipv6": "flag", "set-upstream": "flag",
    "no-set-upstream": "flag", "help": "flag",
}
_SHORT_FLAGS = set("nvqu46")


def plan_branch(name: str) -> bool:
    """A branch under `plan/`. Case-folded: a case-insensitive filesystem or remote must not be a way round."""
    return name.casefold().startswith(PLAN_PREFIX)


def _long_name(name: str) -> str | None:
    if name in _LONG:
        return name
    found = [k for k in _LONG if k.startswith(name)]
    return found[0] if len(found) == 1 else None  # git refuses an ambiguous abbreviation; so does this


def read_push(args: Sequence[tuple[str, bool]], current: Callable[[], str | None],
              head: Callable[[], str | None] | None = None) -> PushRead:
    """The ref writes of `git push <args>`; `args` are (text, dynamic) pairs after `push`.

    `current` names the branch a bare push resolves to (git rev-parse --symbolic-full-name @{push}) and
    `head` the one a `HEAD` refspec does (the checked-out branch; `current` when not given). None, or any
    word the shell builds, makes the reading unreadable, which refuses."""
    force = delete = tags = False
    broad: str | None = None
    has_repo = False
    positional: list[tuple[str, bool]] = []
    i = 0
    end = False
    while i < len(args):
        text, dynamic = args[i]
        i += 1
        if end or not text.startswith("-") or text == "-":
            positional.append((text, dynamic))
            continue
        if dynamic:
            return PushRead(unreadable="an option built by the shell")
        if text == "--":
            end = True
            continue
        if text.startswith("--"):
            name, eq, value = text[2:].partition("=")
            key = _long_name(name)
            if key is None:
                return PushRead(unreadable=f"an option this guard does not read ({text.split('=')[0]})")
            kind = _LONG[key]
            if kind in ("valued", "repo") and not eq:
                if i >= len(args):
                    return PushRead(unreadable=f"--{key} has no value")
                i += 1
            if kind == "repo":
                has_repo = True
            elif kind in ("force", "force-optional"):
                force = True
            elif kind == "delete":
                delete = True
            elif kind == "tags":
                tags = True
            elif kind == "mirror":
                broad, force = "mirror", True
            elif kind == "prune":
                broad = broad if broad == "mirror" else "prune"
            elif kind == "all":
                broad = broad or "all"
            continue
        letters = text[1:]
        for pos, ch in enumerate(letters):
            if ch == "f":
                force = True
            elif ch == "d":
                delete = True
            elif ch in _SHORT_FLAGS:
                continue
            elif ch == "o":
                if not letters[pos + 1:]:
                    if i >= len(args):
                        return PushRead(unreadable="-o has no value")
                    i += 1
                break
            else:
                return PushRead(unreadable=f"an option this guard does not read (-{ch})")
    refspecs = positional if has_repo else positional[1:]
    if broad is not None and not refspecs:
        return PushRead(broad=broad)
    writes: list[Write] = []
    j = 0
    while j < len(refspecs):
        text, dynamic = refspecs[j]
        j += 1
        if dynamic or "*" in text or "[" in text or "?" in text:
            return PushRead(unreadable="a refspec built by the shell or holding a pattern")
        if text == "tag" and j < len(refspecs):
            j += 1  # `tag <name>` pushes refs/tags/<name>
            continue
        plus = text.startswith("+")
        spec = text[1:] if plus else text
        source: str | None
        if ":" in spec:
            source, _, dest = spec.partition(":")
            if not dest:
                return PushRead(unreadable="a refspec with no destination")
            gone = source == ""
        else:
            source, dest, gone = spec, spec, False
        if dest in ("HEAD", "@"):
            dest = (head or current)() or ""
            if not dest:
                return PushRead(unreadable="HEAD does not resolve to a branch")
        if dest.startswith("refs/heads/"):
            dest = dest[len("refs/heads/"):]
        elif dest.startswith("heads/"):
            dest = dest[len("heads/"):]
        elif dest.startswith("refs/"):
            continue  # tags, notes and the like: not a branch
        if not dest:
            return PushRead(unreadable="a refspec whose destination is empty")
        writes.append(Write(dest, None if gone or delete else source, force or plus, gone or delete))
    if not refspecs:
        if broad == "all":
            return PushRead(broad=broad)
        if tags:
            return PushRead()
        branch = current()
        if not branch:
            return PushRead(unreadable="a bare push does not resolve to a branch")
        writes.append(Write(branch, "HEAD", force, delete))
    return PushRead(writes=tuple(writes), broad=broad)


# ---- the diff shapes ---------------------------------------------------------------------

def _item(branch: str) -> re.Match | None:
    return ITEM_BRANCH.fullmatch(branch)


def _record_path(m: re.Match) -> str:
    return f"docs/plans/progress/{m['plan']}/item-{m['item']}-part{m['part']}.md"


def _register_dir(m: re.Match) -> str:
    return f"docs/plans/unauthorised/{m['plan']}/"


def progress_only(changes: Sequence[Change], branch: str) -> bool:
    """The push touches only this part's progress record (E19's one allowed push)."""
    m = _item(branch)
    if m is None or not changes:
        return False
    record = _record_path(m)
    return all(c.path == record and c.status in ("A", "M") for c in changes)


def register_shape(changes: Sequence[Change], branch: str) -> bool:
    """The E7 register-commit shape: new files in this plan's register folder for this item and part,
    and added lines in this part's progress record that hold nothing but a link to one of them."""
    m = _item(branch)
    if m is None or not changes:
        return False
    folder, record = _register_dir(m), _record_path(m)
    added_files = {c.path for c in changes if c.status == "A" and c.path.startswith(folder)}
    for c in changes:
        if c.path == record:
            if c.status not in ("A", "M") or c.removed:
                return False
            for line in c.added:
                link = LINK_LINE.fullmatch(line.strip())
                if link is None:
                    return False
                target = link["target"]
                relative = posixpath.normpath(posixpath.join(posixpath.dirname(record), target))
                if target not in added_files and relative not in added_files:
                    return False
        elif c.path in added_files:
            name = posixpath.basename(c.path)
            found = REGISTER_FILE.fullmatch(name)
            if (found is None or found["item"] != m["item"] or found["part"] != m["part"]
                    or posixpath.dirname(c.path) + "/" != folder):
                return False
        else:
            return False
    return True


# ---- the rows, one decision function each (the mutation test replaces one at a time) ------------

def check_e16(branch: str | None) -> None:
    """E16: deleting a `plan/*` ref, or closing a PR whose head is one, is refused in any spelling."""
    if branch is not None and plan_branch(branch):
        raise Refusal("E16: deleting a plan/* ref, or closing a PR whose head is one, is refused in any "
                      "spelling (ADR 0063 §5)")


def check_e15(write: Write) -> None:
    """E15: a non-fast-forward write to `plan/*` is refused, in any mode."""
    if write.force:
        raise Refusal("E15: a non-fast-forward write to plan/* is refused: no --force, --force-with-lease, "
                      "--force-if-includes, + refspec or --mirror (ADR 0063 §5)")


def check_e14(plan_run: bool) -> None:
    """E14: a `plan/*` ref is written only by a plan run."""
    if not plan_run:
        raise Refusal("E14: a plan/* ref is written only by a plan run, with TRIMTAB_PLAN_RUN=1 in claude's "
                      "environment (ADR 0063 §5)")


def check_e17(pr: PrState | None, register: Callable[[], bool]) -> None:
    """E17: a push to a branch whose open PR is ready is refused, unless it has the register-commit shape."""
    if pr is not None and not pr.draft and not register():
        raise Refusal("E17: the branch's open PR is ready, so a push is refused; return it to draft first "
                      "(ADR 0063 §5, AGW-4)")


def check_e19(state: str, progress: Callable[[], bool]) -> None:
    """E19: while the secret scan fails or cannot be read, only a progress-record-only push goes through."""
    if state in ("fail", "unreadable") and not progress():
        raise Refusal("E19: the secret-scan job failed or cannot be read on the branch's latest head; only a "
                      "push touching only this part's progress record is allowed (ADR 0063 §5)")


def counted_heads(pushes: Iterable[Push], *, failing: set[str], facts: set[str], register_only: set[str]) -> set[str]:
    """The heads that each count as one CI-fix round: answered by a later push, and failing or reviewed.

    A head counts once however many jobs, attempts or answers it has. `register_only` holds the heads
    whose every answer has the register-commit shape; they are excluded."""
    answered = {p.before for p in pushes}
    return {h for h in answered if (h in failing or h in facts) and h not in register_only}


def check_e12(branch: str, head: str, facts: Facts, pr: PrState | None, register: Callable[[], bool]) -> None:
    """E12: a push that would make the fourth CI-fix round is refused."""
    pushes = list(facts.pushes(branch))
    failing, reviewed = set(facts.failing_heads(branch)), set(facts.facts_run_heads(branch))
    candidates = {p.before for p in pushes} & (failing | reviewed)
    register_only = {h for h in candidates
                     if all(facts.register_answer(branch, p.before, p.after) for p in pushes if p.before == h)}
    counted = counted_heads(pushes, failing=failing, facts=reviewed, register_only=register_only)
    current = head in failing or head in reviewed
    if not current and pr is not None and head not in counted:
        current = facts.conflicted(pr.number)  # ADR 0063 §5a: a conflict with the default branch is a round
    total = len(counted | ({head} if current and not register() else set()))
    if total > MAX_ROUNDS:
        raise Refusal(f"E12: this push would be CI-fix round {total}; the limit is {MAX_ROUNDS} "
                      "(ADR 0063 §5, AGW-4)")


# ---- one ref write -----------------------------------------------------------------------

def judge_write(write: Write, *, plan_run: bool, facts: Facts) -> None:
    """Refuse (Refusal) a write to a `plan/*` ref that breaks E12 or E14-E17 or E19; other refs pass untouched."""
    if not plan_branch(write.branch):
        return
    if write.delete:
        check_e16(write.branch)
    check_e15(write)
    check_e14(plan_run)
    try:
        _judge_run_write(write, facts)
    except FactsError as err:
        raise Refusal(f"a fact the plan rows need is unreadable ({err}); refusing (ADR 0063 §5)") from err


def _judge_run_write(write: Write, facts: Facts) -> None:
    branch = write.branch
    pr = facts.open_pr(branch)
    head = facts.branch_head(branch)
    if head is None:  # the branch does not exist yet: no PR, no job records, no pushes to count
        return
    cache: list[Sequence[Change]] = []

    def changes() -> Sequence[Change]:
        if not cache:
            cache.append(facts.changes(branch, write.source, head))
        return cache[0]

    def register() -> bool:
        return register_shape(changes(), branch)

    check_e17(pr, register)
    check_e19(facts.secret_scan(head), lambda: progress_only(changes(), branch))
    check_e12(branch, head, facts, pr, register)


def judge_close(head: str | None) -> None:
    """E16 for a PR close: a PR whose head is `plan/*` is not closed, nor its branch deleted with it."""
    check_e16(head)


def judge_broad(kind: str, *, plan_run: bool, facts: Facts) -> None:
    """A push this guard cannot list refs for: `--mirror` and `--prune` refuse; `--all` is judged ref by ref."""
    if kind == "mirror":
        check_e15(Write(PLAN_PREFIX, force=True))
    if kind == "prune":
        check_e16(PLAN_PREFIX)
    try:
        branches = list(facts.local_plan_branches())
    except FactsError as err:
        raise Refusal(f"the local plan/* branches are unreadable ({err}); --all is refused") from err
    for branch in branches:
        judge_write(Write(branch, branch), plan_run=plan_run, facts=facts)
