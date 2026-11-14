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
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass

PLAN_PREFIX = "plan/"
PLANS = "docs/" + "plans/"  # the project's plan folders (AGW-1), in two pieces: this repository has none
# Three CI-fix rounds: the owner's decision (ADR 0063 §5a). AGW-4's text still says two; that rule edit is the owner's.
MAX_ROUNDS = 3
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
    force: bool = False  # a force flag given with a broad push applies to every ref it writes
    delete: bool = False
    remote: str | None = None  # the repository word: a remote's name or a URL (None: the default remote)
    bare: bool = False  # no refspec was given, so configuration chooses what is pushed
    colonless: bool = False  # a refspec had no `:dst`, so `remote.<r>.push` can map it elsewhere
    head_ref: bool = False  # a destination was taken from HEAD, so the checked-out branch decides it
    dry: bool = False  # `--dry-run` / `-n`: it writes nothing, so a ledger entry for it would vouch for nothing


@dataclass(frozen=True)
class PrState:
    number: int
    draft: bool
    created: str = ""  # RFC 3339, UTC: when the PR opened
    base: str = ""  # the branch the PR merges into


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
    binary: bool = False  # git shows no lines for it, so its content cannot be judged
    removed_lines: tuple[str, ...] = ()  # the removed lines' text (E18 judges test definitions and thresholds)


class Facts:
    """What the rows read from GitHub and git. A failed read raises FactsError."""

    def open_pr(self, branch: str) -> PrState | None: raise NotImplementedError
    def branch_head(self, branch: str) -> str | None: raise NotImplementedError
    def secret_scan(self, head: str, branch: str) -> str: raise NotImplementedError  # pass/pending/fail/unreadable
    def changes(self, branch: str, source: str | None, head: str) -> Sequence[Change]: raise NotImplementedError
    def pushes(self, branch: str) -> Sequence[Push]: raise NotImplementedError
    def failing_heads(self, branch: str) -> set[str]: raise NotImplementedError
    def facts_run_heads(self, branch: str) -> set[str]: raise NotImplementedError
    def conflicted(self, number: int) -> bool: raise NotImplementedError
    def register_answer(self, branch: str, before: str, after: str) -> bool: raise NotImplementedError
    def local_plan_branches(self) -> Sequence[str]: raise NotImplementedError
    def pr_head(self, number: int, repo: str | None) -> str | None: raise NotImplementedError  # None: not a PR
    def pr_base(self, number: int, repo: str | None) -> str | None: raise NotImplementedError  # None: not a PR
    def repository(self) -> str: raise NotImplementedError  # owner/name the facts are read for


# ---- reading `git push` ------------------------------------------------------------------

_LONG = {
    "all": "all", "branches": "all", "prune": "prune", "mirror": "mirror", "delete": "delete", "tags": "tags",
    "follow-tags": "flag", "no-follow-tags": "flag", "signed": "optional", "no-signed": "flag",
    "atomic": "flag", "no-atomic": "flag", "push-option": "valued", "receive-pack": "valued",
    "exec": "valued", "repo": "repo", "force-with-lease": "force-optional", "no-force-with-lease": "flag",
    "force-if-includes": "force", "no-force-if-includes": "flag", "force": "force", "dry-run": "dry",
    "porcelain": "flag", "thin": "flag", "no-thin": "flag", "quiet": "flag", "verbose": "flag",
    "progress": "flag", "no-progress": "flag", "recurse-submodules": "valued", "no-recurse-submodules": "flag",
    "verify": "flag", "no-verify": "flag", "ipv4": "flag", "ipv6": "flag", "set-upstream": "flag",
    "no-set-upstream": "flag", "help": "flag",
}
_SHORT_FLAGS = set("vqu46")  # `n` is --dry-run, which is read above, not a flag to pass over


def normalise(value) -> str | None:
    """A branch name from an API field: trimmed, without a leading `refs/heads/` or `heads/`; None if not text."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    for prefix in ("refs/heads/", "heads/"):
        if name.casefold().startswith(prefix):
            return name[len(prefix):]
    return name


def plan_branch(name: str) -> bool:
    """A branch under `plan/`. Case-folded: a case-insensitive filesystem or remote must not be a way round."""
    return (normalise(name) or "").casefold().startswith(PLAN_PREFIX)


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
    force = delete = tags = dry = False
    broad: str | None = None
    has_repo = False
    repo_value: str | None = None
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
                return PushRead(unreadable="an option this guard does not read")
            kind = _LONG[key]
            if kind in ("valued", "repo") and not eq:
                if i >= len(args):
                    return PushRead(unreadable=f"--{key} has no value")
                value = args[i][0]
                i += 1
            if kind == "repo":
                has_repo, repo_value = True, value
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
            elif kind == "dry":
                dry = True
            continue
        letters = text[1:]
        for pos, ch in enumerate(letters):
            if ch == "n":
                dry = True
            elif ch == "f":
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
                return PushRead(unreadable="a short option this guard does not read")
    refspecs = positional if has_repo else positional[1:]
    remote = repo_value if has_repo else (positional[0][0] if positional else None)
    # A first word the shell builds may be an option (`$F origin plan/x`), which would hide a force flag.
    maybe_option = not has_repo and bool(positional) and positional[0][1]
    if broad is not None and not refspecs:
        return PushRead(broad=broad, force=force, delete=delete, remote=remote)
    writes: list[Write] = []
    colonless = head_ref = False
    j = 0
    while j < len(refspecs):
        text, dynamic = refspecs[j]
        j += 1
        if dynamic or "*" in text or "[" in text or "?" in text or "{}" in text:  # {} is find -exec's placeholder
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
            colonless = True
        if dest in ("HEAD", "@"):
            head_ref = True
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
            return PushRead(broad=broad, force=force, delete=delete, remote=remote)
        if tags:
            return PushRead(remote=remote)
        branch = current()
        if not branch:
            return PushRead(unreadable="a bare push does not resolve to a branch")
        writes.append(Write(branch, "HEAD", force, delete))
    if maybe_option:  # an unquoted expansion splits into words: options, or refspecs of its own (`:plan/x`)
        return PushRead(unreadable="a remote built by the shell may be options or refspecs")
    return PushRead(writes=tuple(writes), broad=broad, remote=remote, bare=not refspecs, colonless=colonless,
                    head_ref=head_ref, dry=dry)


# ---- the diff shapes ---------------------------------------------------------------------

def _item(branch: str) -> re.Match | None:
    return ITEM_BRANCH.fullmatch(branch)


def _record_path(m: re.Match) -> str:
    return f"{PLANS}progress/{m['plan']}/item-{m['item']}-part{m['part']}.md"


def _register_dir(m: re.Match) -> str:
    return f"{PLANS}unauthorised/{m['plan']}/"


def progress_only(changes: Sequence[Change], branch: str) -> bool:
    """The push touches only this part's progress record (E19's one allowed push)."""
    m = _item(branch)
    if m is None or not changes:
        return False
    record = _record_path(m)
    return all(c.path == record and c.status in ("A", "M") and not c.binary for c in changes)


def register_file(path: str, branch: str) -> bool:
    """Whether a path is a register entry of this branch's own item and part (E7's added files)."""
    m = _item(branch)
    return bool(m) and path.startswith(_register_dir(m)) \
        and bool(REGISTER_FILE.fullmatch(posixpath.basename(path)))


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
            if c.status not in ("A", "M") or c.removed or c.binary:
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


def check_e12(branch: str, head: str, facts: Facts, pr: PrState | None, register: Callable[[], bool],
              conflicted_heads: frozenset[str] = frozenset()) -> None:
    """E12: a push that would make the fourth CI-fix round is refused.

    `conflicted_heads` holds the heads a ledger `conflict` event records (ADR 0063 §5a): a head that
    conflicted with the default branch counts as a round, and the ledger is the only record of one
    past the push that answered it. Empty, as it is for a caller with no ledger, the count is the
    server-side one Amendment 1 shipped, which is weaker by conflict-only rounds."""
    pushes = list(facts.pushes(branch))
    failing = set(facts.failing_heads(branch)) | set(conflicted_heads)
    reviewed = set(facts.facts_run_heads(branch))
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


# ---- E22: the same rows, read from the whole branch's activity on the server --------------------

ZERO = "0" * 40
FORCE_KINDS = frozenset({"force_push"})
DELETE_KINDS = frozenset({"branch_deletion"})


def check_server_history(events: Sequence, *, agent: str, merge_base: str, head: str,
                         on_default: Callable[[str], bool], counted: Collection[str]) -> None:
    """E22's server-side reading of E12, E14, E15 and E16: no hook ran for a push, so the branch-activity events
    (oldest first) and the job records are the whole record (ADR 0063 §7).

    - E16: no event deletes the branch.
    - E14: every event is the agent account's, which is how the server shows a plan run made it.
    - E15: no event is a force push, and the events chain unbroken from the merge-base (or the branch's
      creation) to the head under judgment, so no rewrite hides between two events.
    - E12: no more than `MAX_ROUNDS` heads were each answered by a later push while failing or reviewed;
      `counted` is that set, which the caller computes with `counted_heads`."""
    if any(e.kind in DELETE_KINDS or e.after == ZERO for e in events):
        raise Refusal("E16: the branch's activity holds a deletion (ADR 0063 §5, §7)")
    if any(e.actor.casefold() != agent.casefold() for e in events):
        raise Refusal("E14: a push on the branch was not made by the agent account (ADR 0063 §5, §7)")
    if any(e.kind in FORCE_KINDS for e in events):
        raise Refusal("E15: the branch's activity holds a force push (ADR 0063 §5, §7)")
    if not events:
        raise Refusal("E15: the branch has no activity events to chain (ADR 0063 §5, §7)")
    first = events[0].before
    if first != ZERO and first != merge_base and not on_default(first):
        raise Refusal("E15: the activity chain does not start at the merge-base (ADR 0063 §5, §7)")
    if any(prev.after != nxt.before for prev, nxt in zip(events, events[1:])):
        raise Refusal("E15: the activity chain is broken, so a push is missing or a branch was rewritten "
                      "(ADR 0063 §5, §7)")
    if events[-1].after != head:
        raise Refusal("E15: the activity chain does not end at the head under judgment (ADR 0063 §5, §7)")
    if len(counted) > MAX_ROUNDS:
        raise Refusal(f"E12: the branch holds {len(counted)} CI-fix rounds; the limit is {MAX_ROUNDS} "
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
    check_e19(facts.secret_scan(head, branch), lambda: progress_only(changes(), branch))
    check_e12(branch, head, facts, pr, register)


def judge_close(head: str | None) -> None:
    """E16 for a PR close: a PR whose head is `plan/*` is not closed, nor its branch deleted with it."""
    check_e16(head)


def judge_broad(kind: str, *, plan_run: bool, facts: Facts, force: bool = False, delete: bool = False) -> None:
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
        judge_write(Write(branch, None if delete else branch, force, delete), plan_run=plan_run, facts=facts)
