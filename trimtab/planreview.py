"""The plan-run review, ready and resume rows E5-E10, E13, E18, E23 and K+1 as pure decisions (ADR 0063 §5, §6).

Everything here is a function of values handed in: the E3 facts lines (`Review`, `Thread`), the review's
comments and the agent's replies (`Comment`), the ledger's lists, the branch-activity events (`Activity`)
and the diff (`planrows.Change`). `planfacts.py` reads them from GitHub and git; the subcommands in
`planchecks.py` join the two. A row raises `Refusal` to refuse. A fact that could not be read is the
caller's `FactsError`, never a default here, so a missing input cannot pass a row (SEC-2.5).

Standard library only, and no import of the fact readers or adapters (ARC-3, ENG-3).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from trimtab.planrows import Change, FactsError, Refusal

ZERO = "0" * 40
SHA = re.compile(r"[0-9a-f]{40}")
HIGH_OR_ABOVE = ("critical", "high")
SEVERITIES = ("critical", "high", "medium", "low")
MAX_PRE_DRAFT_ROUNDS = 5
TEST_DEFINITION = re.compile(r"^\s*(?:async\s+def\s+test_|def\s+test_|it\(|test\(|describe\(|func\s+Test|@Test\b)")
TEST_FILE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec)/|(?:^|/)test_[^/]*$|_test\.[a-z]+$|\.(?:test|spec)\.[a-z]+$")
NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Review:
    """One `TRIMTAB-REVIEW v1` line (ADR 0063 §4): an E2 run's head, tree and verdict."""

    run: str
    head: str
    tree: str
    verdict: str  # pass | fail | fail-closed


@dataclass(frozen=True)
class Thread:
    """One `TRIMTAB-THREAD v1` line: a finding, its anchor and its state at a run."""

    run: str
    id: int
    severity: str
    state: str  # open | resolved
    resolved_by: str  # "-" when open
    path: str
    line: int
    category: str
    key: str
    commit: str = ""  # the commit the review anchored the finding on


@dataclass(frozen=True)
class Comment:
    """A review comment on the PR."""

    id: int
    user: str
    body: str
    in_reply_to: int | None = None


@dataclass(frozen=True)
class Activity:
    """A branch-activity event: one push, who made it, and the head before and after."""

    before: str
    after: str
    actor: str


@dataclass(frozen=True)
class LedgerPush:
    """A ledger `push` event, with the commits it introduced (`git rev-list old..new`)."""

    old: str
    new: str
    run: str
    commits: frozenset[str] = frozenset()


_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")
_REVIEW_LINE = re.compile(r"TRIMTAB-REVIEW v1 run=(?P<run>\S+) head=(?P<head>[0-9a-f]{40}) tree=(?P<tree>[0-9a-f]{40}) "
                          r"verdict=(?P<verdict>pass|fail|fail-closed)")
_THREAD_LINE = re.compile(r"TRIMTAB-THREAD v1 run=(?P<run>\S+) id=(?P<id>[0-9]+) "
                          r"severity=(?P<severity>critical|high|medium|low) state=(?P<state>open|resolved) "
                          r"resolved_by=(?P<by>\S+) path=(?P<path>\S+) line=(?P<line>[0-9]+) "
                          r"category=(?P<category>[a-z0-9-]+) key=(?P<key>[0-9a-f]{64})")


def parse_facts(log: str) -> tuple[Review, list[Thread]]:
    """The E3 lines of the facts job's log: exactly one review line and its threads, each in the v1 shape.

    A line that opens with a facts marker but has any other shape, or another version, fails the read; so
    does a missing or repeated review line, or a thread of another run. A marker inside a longer line, a
    model's output echoed in the log for one, is not at a line's start and is not read (SEC-2.1)."""
    reviews: list[Review] = []
    threads: list[Thread] = []
    for raw in log.splitlines():
        line = _STAMP.sub("", raw.rstrip("\r"), count=1)
        if line.startswith("TRIMTAB-REVIEW"):
            m = _REVIEW_LINE.fullmatch(line)
            if m is None:
                raise FactsError("a TRIMTAB-REVIEW line is not in the v1 shape")
            reviews.append(Review(m["run"], m["head"], m["tree"], m["verdict"]))
        elif line.startswith("TRIMTAB-THREAD"):
            m = _THREAD_LINE.fullmatch(line)
            if m is None:
                raise FactsError("a TRIMTAB-THREAD line is not in the v1 shape")
            threads.append(Thread(m["run"], int(m["id"]), m["severity"], m["state"], m["by"], m["path"],
                                  int(m["line"]), m["category"], m["key"]))
    if len(reviews) != 1:
        raise FactsError("the facts log must hold exactly one TRIMTAB-REVIEW line")
    if any(t.run != reviews[0].run for t in threads):
        raise FactsError("a TRIMTAB-THREAD line names another run than the review line")
    return reviews[0], threads


def thread_key(path: str, category: str, line_text: str) -> str:
    """E8's key: SHA-256 of the path, the category and the anchored line with whitespace collapsed."""
    text = " ".join(line_text.split())
    return hashlib.sha256("\x00".join((path, category, text)).encode("utf-8")).hexdigest()


def root_id(comment: Comment) -> int:
    return comment.in_reply_to if comment.in_reply_to is not None else comment.id


def first_word(body: str) -> str:
    words = body.split(None, 1)
    return words[0] if words else ""


# ---- E5, E7 ------------------------------------------------------------------------------------

def check_e5(comments: Sequence[Comment], threads: Sequence[Thread], review_login: str) -> None:
    """E5: a review comment by the review's login whose root is in no E3 line refuses ready."""
    listed = {t.id for t in threads}
    login = review_login.casefold()
    for c in comments:
        if c.user.casefold() == login and root_id(c) not in listed:
            raise Refusal("E5: the review posted a comment that its facts job did not list; ready is refused "
                          "and the part stops (ADR 0063 §5)")


def check_e7(head: str, reviews: Sequence[Review], register_after: Callable[[str], bool]) -> None:
    """E7: ready needs an E2 run on the exact head with a `pass`, or a head that differs from a passed head
    only by the register-commit shape (`register_after(reviewed_head)`)."""
    passed = [r.head for r in reviews if r.verdict == "pass"]
    if head in passed:
        return
    if any(register_after(h) for h in dict.fromkeys(passed)):
        return
    raise Refusal("E7: no review run on this exact head passed, and the head is not a passed head plus a "
                  "register commit (ADR 0063 §5)")


# ---- E8, E9 ------------------------------------------------------------------------------------

def deferral_allowed(thread: Thread, *, outside_diff: bool, merge_base_keys: frozenset[str],
                     recomputed_key: str | None) -> bool:
    """A deferral reply is accepted only below HIGH, for a line outside the head side of the diff from the
    merge-base, whose key this base recomputed and an E2 run on the merge-base's tree also listed."""
    return (thread.severity not in HIGH_OR_ABOVE and outside_diff and recomputed_key is not None
            and thread.key == recomputed_key and thread.key in merge_base_keys)


def accepted_reply(thread: Thread, replies: Sequence[Comment], agent: str, tags: dict[str, str],
                   deferral_ok: Callable[[Thread], bool]) -> str | None:
    """The tag name of the first acceptable agent reply to the thread, else None."""
    by_tag = {v: k for k, v in tags.items()}
    for reply in replies:
        if reply.in_reply_to != thread.id or reply.user.casefold() != agent.casefold():
            continue
        name = by_tag.get(first_word(reply.body))
        if name is None:
            continue
        if thread.severity in HIGH_OR_ABOVE and name != "fixed":
            continue
        if name == "deferred" and not deferral_ok(thread):
            continue
        return name
    return None


def check_e8(threads: Sequence[Thread], replies: Sequence[Comment], agent: str, tags: dict[str, str],
             deferral_ok: Callable[[Thread], bool]) -> None:
    """E8: ready needs every E3 thread answered by the agent with an instance tag; a CRITICAL or HIGH one
    accepts only the fixed tag, and a deferral only where `deferral_ok` holds."""
    for t in threads:
        if accepted_reply(t, replies, agent, tags, deferral_ok) is None:
            raise Refusal(f"E8: a {t.severity} finding has no acceptable reply by the agent with an outcome tag "
                          "(ADR 0063 §5)")


def fixed_commit(body: str) -> str | None:
    """The full SHA a fixed reply cites (the first full 40-hex word after the tag)."""
    for word in body.split()[1:]:
        if SHA.fullmatch(word):
            return word
    return None


def thread_closed(lines: Sequence[Thread], thread_id: int, agent: str) -> bool:
    """A thread is closed only when its latest E3 line says resolved, by someone other than the agent."""
    latest = [t for t in lines if t.id == thread_id]
    return bool(latest) and latest[-1].state == "resolved" and latest[-1].resolved_by.casefold() != agent.casefold()


def check_e9(threads: Sequence[Thread], lines: Sequence[Thread], replies: Sequence[Comment], agent: str,
             tags: dict[str, str], fix_valid: Callable[[Thread, str], bool]) -> None:
    """E9: a fixed reply cites a commit that `fix_valid` accepts (descends from the thread's commit, is on the
    branch, changes a line in its hunk), and the thread is closed by a later E3 line, not by the agent."""
    fixed = tags["fixed"]
    for t in threads:
        for reply in replies:
            if reply.in_reply_to != t.id or reply.user.casefold() != agent.casefold() \
                    or first_word(reply.body) != fixed:
                continue
            sha = fixed_commit(reply.body)
            if sha is None or not fix_valid(t, sha):
                raise Refusal("E9: a fixed reply does not cite a commit that fixes its finding (ADR 0063 §5)")
            if not thread_closed(lines, t.id, agent):
                raise Refusal("E9: a fixed finding is not closed by a later review line; a resolution by the "
                              "agent does not count (ADR 0063 §5)")


# ---- E6, E10 -----------------------------------------------------------------------------------

def check_e6(head: str, reviews: Sequence[Review], reran_heads: Sequence[str]) -> None:
    """E6: a re-request needs a head no E2 run reviewed, except the latest verdict on it being `fail-closed`
    with no earlier `rerequest` for it in the ledger (`reran_heads`)."""
    on_head = [r for r in reviews if r.head == head]
    if not on_head:
        return
    if on_head[-1].verdict == "fail-closed" and head not in reran_heads:
        return
    raise Refusal("E6: this head was already reviewed; a re-request needs a new head (ADR 0063 §5)")


def check_e10(commits: Sequence[str], complete_heads: Sequence[str], last_answer: str | None,
              pushes: Sequence[LedgerPush]) -> None:
    """E10: a re-request needs a complete in-session round on a head H at or after the last commit answering a
    CI review or failing check, and at most one ledger push after H. `commits` run oldest to newest."""
    index = {c: i for i, c in enumerate(commits)}
    floor = index.get(last_answer, -1) if last_answer is not None else -1
    if last_answer is not None and last_answer not in index:
        raise Refusal("E10: the commit answering the CI review is not on the branch (ADR 0063 §5)")
    for h in complete_heads:
        if h in index and index[h] >= floor:
            after = [p for p in pushes if p.new in index and index[p.new] > index[h]]
            if len(after) <= 1:
                return
    raise Refusal("E10: no complete in-session review round covers the work since the last CI answer, or more "
                  "than one push follows it (ADR 0063 §5)")


# ---- E13 ---------------------------------------------------------------------------------------

def check_e13(branch_commits: Sequence[str], pushes: Sequence[LedgerPush], events: Sequence[Activity], head: str,
              merge_base: str, agent: str, on_default: Callable[[str], bool], scope_ok: bool) -> None:
    """E13: a branch is resumed only when every commit lies inside a ledger push made with TRIMTAB_PLAN_RUN=1,
    the activity events chain unbroken from the merge-base to the head each by the agent, ledger and events
    agree, and the diff from the merge-base is in scope. `events` and `pushes` run oldest to newest."""
    covered: set[str] = set()
    for p in pushes:
        if p.run == "1":
            covered |= p.commits
    if not set(branch_commits) <= covered:
        raise Refusal("E13: a commit on the branch was not pushed under a plan run in this ledger (ADR 0063 §5)")
    if not events:
        raise Refusal("E13: the branch has no activity events to chain (ADR 0063 §5)")
    if events[0].before != ZERO and events[0].before != merge_base and not on_default(events[0].before):
        raise Refusal("E13: the activity chain does not start at the merge-base (ADR 0063 §5)")
    for prev, nxt in zip(events, events[1:]):
        if prev.after != nxt.before:
            raise Refusal("E13: the activity chain is broken (ADR 0063 §5)")
    if events[-1].after != head:
        raise Refusal("E13: the activity chain does not end at the branch head (ADR 0063 §5)")
    if any(e.actor.casefold() != agent.casefold() for e in events):
        raise Refusal("E13: a push on the branch was not made by the agent account (ADR 0063 §5)")
    if [(e.before, e.after) for e in events] != [(p.old, p.new) for p in pushes]:
        raise Refusal("E13: the ledger's pushes and the server's events disagree (ADR 0063 §5)")
    if not scope_ok:
        raise Refusal("E13: the branch changes a path outside the plan's scope (ADR 0063 §5)")


# ---- E18 ---------------------------------------------------------------------------------------

def _numbers(line: str) -> list[float]:
    return [float(n) for n in NUMBER.findall(line)]


def check_e18(changes: Sequence[Change], markers: Sequence[str], thresholds: Sequence[str],
              named_lines: frozenset[str]) -> None:
    """E18: a push's own change may not add a marker line, delete a test file or remove more test definitions
    than it adds, or lower a number on a threshold line, unless the plan's item section names that exact line."""
    patterns = [re.compile(p) for p in thresholds]
    for c in changes:
        for line in c.added:
            if any(m in line for m in markers) and line.strip() not in named_lines:
                raise Refusal("E18: the push adds a skip or suppression marker the plan does not name "
                              "(ADR 0063 §5)")
        if TEST_FILE.search(c.path):
            if c.status == "D":
                raise Refusal("E18: the push deletes a test file (ADR 0063 §5)")
            gone = sum(1 for line in c.removed_lines if TEST_DEFINITION.match(line))
            made = sum(1 for line in c.added if TEST_DEFINITION.match(line))
            if gone > made:
                raise Refusal("E18: the push removes more test definitions than it adds (ADR 0063 §5)")
        for pat in patterns:
            old = [x for x in c.removed_lines if pat.search(x) and x.strip() not in named_lines]
            new = [x for x in c.added if pat.search(x)]
            if old and len(new) < len(old):
                raise Refusal("E18: the push removes a threshold line (ADR 0063 §5)")
            for before, after in zip(old, new):
                nb, na = _numbers(before), _numbers(after)
                if len(nb) != len(na) or any(a < b for a, b in zip(na, nb)):
                    raise Refusal("E18: the push lowers a number on a threshold line (ADR 0063 §5)")


# ---- E23 ---------------------------------------------------------------------------------------

def check_e23(head: str, complete_heads: Sequence[str], pre_draft_rounds: int) -> None:
    """E23: opening a PR from `plan/*` needs a complete round on the exact head and at most five pre-draft rounds."""
    if head not in complete_heads:
        raise Refusal("E23: no complete in-session review round on this exact head (ADR 0063 §5)")
    if pre_draft_rounds > MAX_PRE_DRAFT_ROUNDS:
        raise Refusal(f"E23: more than {MAX_PRE_DRAFT_ROUNDS} pre-draft review rounds (ADR 0063 §5)")


def check_round_start(head: str, pre_draft_heads: Sequence[str]) -> None:
    """E23: a sixth pre-draft round refuses at its first pass: a new head when five heads already have rounds."""
    if head not in pre_draft_heads and len(set(pre_draft_heads)) >= MAX_PRE_DRAFT_ROUNDS:
        raise Refusal(f"E23: a sixth pre-draft review round is refused; the limit is {MAX_PRE_DRAFT_ROUNDS} "
                      "(ADR 0063 §5)")


# ---- K+1 ---------------------------------------------------------------------------------------

def parts_in(names: Sequence[str], plan: str, item: int) -> set[int]:
    """The part numbers K in `item-NN-partK` names (a progress record, or a head ref `plan/<plan>/item-NN-partK`)."""
    found = re.compile(rf"(?:^|/){re.escape(plan)}/item-{item:02d}-part([0-9]+)(?:\.md|\.jsonl)?$|^item-{item:02d}-part([0-9]+)(?:\.md|\.jsonl)$")
    out: set[int] = set()
    for name in names:
        m = found.search(name)
        if m:
            out.add(int(m.group(1) or m.group(2)))
    return out


def next_part(record_parts: set[int] | None, ref_parts: set[int] | None, ledger_parts: set[int] | None) -> int:
    """§6: the next part is one past the highest over the progress records on the default branch, the head refs
    of every PR (`state=all`) and the ledger. A source that could not be read (None) refuses a new part."""
    sources = (record_parts, ref_parts, ledger_parts)
    if any(s is None for s in sources):
        raise FactsError("a source of the highest part number could not be read")
    return max((n for s in sources for n in s), default=0) + 1
