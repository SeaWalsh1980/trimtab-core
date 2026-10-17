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
skipped while a `[<ID>]` PR is open or was closed unmerged within the window, and
resolved by one that merged after every other evidence PR merged (by merge time,
not PR number). A fix merged before some evidence did not see it, so the ID is
proposed again. Merged within the
window was not enough: ingest labels the fix PR, which bumps its update time, and
evidence that arrived after the fix was blocked for the whole window.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Callable, ContextManager, Mapping, Protocol, Sequence

from trimtab.items import Item, Proposal
from trimtab.registry.rules import RuleType

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
    """Which issue drafts would open. Reads only."""
    titles = sink.recent_titles(ISSUE_LABEL, closed_since)
    issues = [d for d in drafts if d.kind == "issue"]
    skip = {d.item_id for d in issues if any(t.startswith(title_prefix(d.item_id)) for t in titles)}
    return IssuePlan(sink.destination, tuple(d for d in issues if d.item_id not in skip), tuple(sorted(skip)))


@dataclass(frozen=True)
class PullTitle:
    number: int
    title: str
    state: str  # open | merged | closed (closed without merging)
    merged_at: str = ""  # set when state is merged


class PullSink(Protocol):
    def recent_pulls(self, closed_since: str | None) -> list[PullTitle]:
        """The project's open PRs, and those closed or merged and updated on or after `closed_since`."""
        ...


def _merged_after_evidence(fix: PullTitle, draft: Draft) -> bool:
    """Whether `fix` merged after every evidence PR but itself merged: only then did it see all the evidence.

    Merge time, not PR number: a PR is numbered when it opens, and the order PRs open in says nothing about the order
    they merge in. A draft with no evidence merge times, or a fix with none, is never resolved: it is proposed.
    """
    if not fix.merged_at or len(draft.merged_at) != len(draft.prs):
        return False
    others = [m for n, m in zip(draft.prs, draft.merged_at) if n != fix.number]
    return bool(others) and all(m and fix.merged_at > m for m in others)


def plan_project_prs(drafts: Sequence[Draft], sink: PullSink, closed_since: str | None = None) -> dict[str, str]:
    """Item ID -> why its project draft is not opened, for each one that is not. Reads only."""
    pulls = sink.recent_pulls(closed_since)
    why: dict[str, str] = {}
    for d in drafts:
        if d.kind != "pr":
            continue
        mine = sorted((p for p in pulls if p.title.startswith(title_prefix(d.item_id))), key=lambda p: p.number)
        if (found := next((p for p in mine if p.state == "open"), None)) is not None:
            why[d.item_id] = f"SKIPPED: #{found.number} for this ID is open"
        elif (found := next((p for p in mine if p.state == "merged" and _merged_after_evidence(p, d)), None)):
            why[d.item_id] = f"RESOLVED: #{found.number} merged after the evidence"
        elif (found := next((p for p in mine if p.state == "closed"), None)) is not None:
            why[d.item_id] = f"SKIPPED: #{found.number} for this ID was closed unmerged within the window"
    return why


def confirmation(todo: IssuePlan) -> str:
    """Covers where the issues go as well as what they say: a dry run against one
    repository cannot confirm an apply against another."""
    payload = json.dumps([todo.destination, [asdict(d) for d in todo.to_open]], sort_keys=True)
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
