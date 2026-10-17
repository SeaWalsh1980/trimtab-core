"""Propose: one draft per ID, never merged.

An `upstream` proposal becomes an issue on Trimtab labelled `harness-feedback`;
a `project` proposal a draft PR in the project. The tool opens issues only, and
only behind the confirmation gate ingest uses: `plan_issues` is the dry run and its
digest is the confirmation; `open_issues` refuses a stale one. A project PR
changes rule text, which is judgement, so the `/trimtab-retro` session writes
it from the rendered draft.

Opening is idempotent, so a re-run duplicates nothing: an ID is skipped when an issue whose title
starts `[<ID>]` is open, or was closed within the evidence window. The second
half matters: an issue the operator closed as declined would otherwise be
re-filed next week from the same evidence. Once that evidence decays, new
reports can file it again.

A project draft is checked against the project's PRs the same way (`plan_project_prs`):
skipped while a `[<ID>]` PR is open or was closed unmerged within the window (unless it
closed before the latest merged fix, which supersedes it), and
resolved by a merged one (by merge time, not PR number): the fix saw the evidence
that merged before it, so only reports since the latest fix count, and the ID is
proposed again once those reach the threshold. Merged within the window was not
enough: ingest labels the fix PR, which bumps its update time, and
evidence that arrived after the fix was blocked for the whole window.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable, ContextManager, Mapping, Protocol, Sequence

from trimtab.items import Item, Proposal
from trimtab.registry.rules import RuleType
from trimtab.score import THRESHOLD

ISSUE_LABEL = "harness-feedback"
ITEM_TYPES = {"rule": RuleType()}


class StaleProposals(Exception):
    """The confirmation does not match the issues about to be opened."""


@dataclass(frozen=True)
class Draft:
    kind: str  # issue | pr
    item_id: str
    title: str
    body: str
    labels: tuple[str, ...]
    prs: tuple[int, ...] = ()  # the evidence PRs
    merged_at: tuple[str, ...] = ()  # when each evidence PR merged, in the same order


def title_prefix(item_id: str) -> str:
    return f"[{item_id}]"


# The ID a title opens with, as `title_prefix` writes it.
LEADING_ID = re.compile(r"\[([^\]]+)\]")


def render(proposal: Proposal, registry: Mapping[str, Item], project: str | None = None) -> Draft:
    item = registry.get(proposal.item_id)
    kind = "issue" if proposal.scope == "upstream" else "pr"
    what = "/".join(proposal.kinds)
    title = f"{title_prefix(proposal.item_id)} reported {what} in {len(proposal.prs)} PRs"
    summary = (ITEM_TYPES[item.type].render_change(item, proposal) if item is not None
               else f"`{proposal.item_id}` is not in the current registry.")
    # Evidence is untrusted PR text: fenced, so it renders as data, not markup.
    evidence = "\n".join(f"- #{n}:\n  ```text\n  {e.replace('```', '` ` `')}\n  ```"
                         for n, e in zip(proposal.prs, proposal.evidence))
    # `project` lets the upstream loop count distinct consumers per ID.
    origin = f"  project: {project}\n" if project else ""
    body = (
        f"{summary}\n\n"
        f"## Evidence\n{evidence}\n\n"
        f"## Harness feedback\n```yaml\n- id: {proposal.item_id}\n  scope: {proposal.scope}\n"
        f"{origin}  prs: [{', '.join(str(n) for n in proposal.prs)}]\n```\n\n"
        "Opened as a draft by the Trimtab loop. Nothing merges without a person."
    )
    return Draft(kind, proposal.item_id, title, body, (ISSUE_LABEL,) if kind == "issue" else (), proposal.prs,
                 proposal.merged_at)


@dataclass(frozen=True)
class Filer:
    """Files one issue and returns what the sink reports for it (its URL); given out only inside `filing`."""

    create: Callable[[str, str, Sequence[str]], str]


class IssueSink(Protocol):
    destination: str  # where `filing` files issues; part of the confirmation

    def recent_titles(self, label: str, closed_since: str | None) -> list[str]:
        """Titles of issues with `label` that are open, or were closed on or after `closed_since`."""
        ...

    def filing(self) -> ContextManager[Filer]:
        """Issues are filed only inside this block; on leaving it, however it is left, every one filed is
        checked, and an error is raised if one is not what was sent."""
        ...


@dataclass(frozen=True)
class IssuePlan:
    destination: str
    to_open: tuple[Draft, ...]
    already_open: tuple[str, ...]  # item IDs skipped: an issue is open or was closed within the window


def plan_issues(drafts: Sequence[Draft], sink: IssueSink, closed_since: str | None = None) -> IssuePlan:
    """Which issue drafts would open. Reads only, and nothing at all when there is no issue draft."""
    issues = [d for d in drafts if d.kind == "issue"]
    if not issues:
        return IssuePlan(sink.destination, (), ())
    titles = sink.recent_titles(ISSUE_LABEL, closed_since)
    skip = {d.item_id for d in issues if any(t.startswith(title_prefix(d.item_id)) for t in titles)}
    return IssuePlan(sink.destination, tuple(d for d in issues if d.item_id not in skip), tuple(sorted(skip)))


@dataclass(frozen=True)
class PullTitle:
    number: int
    title: str
    state: str  # open | merged | closed (closed without merging)
    merged_at: str = ""  # set when state is merged
    closed_at: str = ""  # set when state is closed or merged


class PullSink(Protocol):
    def recent_pulls(self, closed_since: str | None) -> list[PullTitle]:
        """The project's open PRs, and closed ones that merged or closed on or after `closed_since` (a later comment
        does not bring an old fix or decline back into the window)."""
        ...


def _reports_since(fix: PullTitle, draft: Draft) -> list[int] | None:
    """The evidence PRs that merged after `fix`, leaving out `fix` itself if it is evidence; None when it cannot be
    told.

    Merge time, not PR number: a PR is numbered when it opens, and the order PRs open in says nothing about the order
    they merge in. A draft with no evidence merge times, or a fix with none, cannot be told: it is proposed.
    """
    if not fix.merged_at or not draft.prs or len(draft.merged_at) != len(draft.prs):
        return None
    others = [(n, m) for n, m in zip(draft.prs, draft.merged_at) if n != fix.number]
    if not others:
        return [] if fix.number in draft.prs else None  # the fix is the only evidence: it saw all of it
    if not all(m for _, m in others):
        return None
    return [n for n, m in others if m > fix.merged_at]


# Conformance reports that make a candidate (`/trimtab-retro` step 3): an ID raised in this many reports.
CONFORMANCE_THRESHOLD = 2


@dataclass(frozen=True)
class _Standing:
    """What an ID's PRs say, whatever the evidence: an open PR, the merged fixes oldest first, and a decline that still
    stands (closed unmerged, and not before the latest fix merged, which supersedes it; one with no close time
    stands)."""

    open: PullTitle | None
    fixes: tuple[PullTitle, ...]
    decline: PullTitle | None


def _standing(mine: Sequence[PullTitle]) -> _Standing:
    mine = sorted(mine, key=lambda p: p.number)
    fixes = tuple(sorted((p for p in mine if p.state == "merged" and p.merged_at), key=lambda p: p.merged_at))
    decline = next((p for p in mine if p.state == "closed"
                    and not (fixes and p.closed_at and p.closed_at < fixes[-1].merged_at)), None)
    return _Standing(next((p for p in mine if p.state == "open"), None), fixes, decline)


def plan_project_prs(drafts: Sequence[Draft], sink: PullSink, closed_since: str | None = None,
                     threshold: int = THRESHOLD,
                     check_ids: Mapping[str, Sequence[str]] | None = None) -> dict[str, str]:
    """Item ID -> why its project draft is not opened, for each one that is not. Reads nothing when there is no
    project draft and no ID to check.

    A merged `[<ID>]` fix saw the evidence that merged before it, so only reports since the latest fix count: the
    draft is RESOLVED while they stay below `threshold`, and proposed again, marked REOPENED with the reports since,
    once they reach it.

    `check_ids` maps an ID to the days of its conformance reports (step 3's candidates: their evidence is reports,
    not PRs), each day counted once. They are counted the same way against CONFORMANCE_THRESHOLD; a report on the day
    the fix merged counts as after it, since a report has no time of day. An ID with a draft as well is counted on
    both: RESOLVED only while both counts stay below their thresholds, REOPENED once either reaches its own."""
    check_ids = {i: tuple(sorted(set(days))) for i, days in (check_ids or {}).items()}
    if not any(d.kind == "pr" for d in drafts) and not check_ids:
        return {}
    # Grouped once by the ID a title opens with, so each draft is a lookup, not a scan of every PR (ENG-6.2).
    by_id: dict[str, list[PullTitle]] = {}
    for p in sink.recent_pulls(closed_since):
        if (opening := LEADING_ID.match(p.title)) is not None:
            by_id.setdefault(opening.group(1), []).append(p)
    drafted = {d.item_id: d for d in drafts if d.kind == "pr"}
    why: dict[str, str] = {}
    for item_id in [*drafted, *(i for i in check_ids if i not in drafted)]:
        st = _standing(by_id.get(item_id, ()))
        if st.open is not None:
            why[item_id] = f"SKIPPED: #{st.open.number} for this ID is open"
            continue
        counts = []
        if (d := drafted.get(item_id)) is not None:
            since = _reports_since(st.fixes[-1], d) if st.fixes else None
            counts.append(_Since(None if since is None else [f"#{n}" for n in since], threshold, "report(s)"))
        if item_id in check_ids:
            days = [day for day in check_ids[item_id] if st.fixes and day >= st.fixes[-1].merged_at[:10]]
            counts.append(_Since(days if st.fixes else None, CONFORMANCE_THRESHOLD,
                                 "conformance report(s)" if d is not None else "report(s)"))
        if mark := _mark(st, counts):
            why[item_id] = mark
    return why


@dataclass(frozen=True)
class _Since:
    """One kind of evidence for an ID: the reports named since its latest fix (None: no fix, or it cannot be told),
    the threshold they are counted against, and what they are called in the mark."""

    reports: Sequence[str] | None
    threshold: int
    what: str


def _mark(st: _Standing, counts: Sequence[_Since]) -> str | None:
    """The mark for an ID with no open PR, given its evidence since the latest fix, or None for no mark. It is
    RESOLVED only when every count can be told and is below its threshold, and REOPENED when any reaches its own."""
    if all(c.reports is not None and len(c.reports) < c.threshold for c in counts):
        fix = st.fixes[-1].number
        if not any(c.reports for c in counts):
            return f"RESOLVED: #{fix} merged after the evidence"
        return f"RESOLVED: #{fix} merged after the earlier evidence; " + "; ".join(
            f"{len(c.reports)} {c.what} since, below the threshold of {c.threshold}" for c in counts)
    if st.decline is not None:
        return f"SKIPPED: #{st.decline.number} for this ID was closed unmerged within the window"
    if reached := [c for c in counts if c.reports and len(c.reports) >= c.threshold]:
        # Proposed again: say which reports are new, since the draft lists the fix's evidence too.
        return f"REOPENED: #{st.fixes[-1].number} merged; " + "; ".join(
            f"{len(c.reports)} {c.what} since: " + ", ".join(c.reports) for c in reached)
    return None


def confirmation(todo: IssuePlan) -> str:
    """Covers where the issues go as well as what they say: a dry run against one
    repository cannot confirm an apply against another."""
    said = [{"kind": d.kind, "item_id": d.item_id, "title": d.title, "body": d.body, "labels": list(d.labels)}
            for d in todo.to_open]  # the evidence numbers and merge times are not what an issue says
    payload = json.dumps([todo.destination, said], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


class OpeningStopped(Exception):
    """Opening stopped at one draft: `opened` were filed before it, `not_opened` (item IDs) were not tried.

    `cause` is the sink's error; the draft it stopped at may or may not exist, as the cause says. `also` is an
    earlier stop this one followed: a create that failed, then a read-back that failed too. Both are kept, since
    the create's cause may say its issue may have been filed.
    """

    def __init__(self, opened: list[str], stopped_at: str, not_opened: list[str], cause: Exception,
                 also: OpeningStopped | None = None):
        super().__init__(f"opening stopped at {stopped_at}: {cause}")
        self.opened, self.stopped_at, self.not_opened, self.cause = opened, stopped_at, not_opened, cause
        self.also = also


def open_issues(todo: IssuePlan, sink: IssueSink, confirm: str) -> list[str]:
    """Open each planned issue. Returns what the sink reports for each (its URL)."""
    if confirm != confirmation(todo):
        raise StaleProposals("the proposals changed since the dry run; run --dry-run again")
    opened: list[str] = []
    stopped: OpeningStopped | None = None
    try:
        with sink.filing() as filer:  # what was opened is read back on leaving, even part-way
            for i, d in enumerate(todo.to_open):
                try:
                    opened.append(filer.create(d.title, d.body, d.labels))
                except Exception as err:  # raised below with what was opened, so no URL is lost
                    stopped = OpeningStopped(opened, d.item_id, [x.item_id for x in todo.to_open[i + 1:]], err)
                    break
    except Exception as err:
        raise OpeningStopped(opened, "the read-back", stopped.not_opened if stopped else [], err,
                             also=stopped) from err
    if stopped is not None:
        raise stopped
    return opened
