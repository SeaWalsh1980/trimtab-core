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
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Mapping, Protocol, Sequence

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
    return Draft(kind, proposal.item_id, title, body, (ISSUE_LABEL,) if kind == "issue" else ())


class IssueSink(Protocol):
    destination: str  # where `create` files issues; part of the confirmation

    def recent_titles(self, label: str, closed_since: str | None) -> list[str]:
        """Titles of issues with `label` that are open, or were closed on or after `closed_since`."""
        ...

    def create(self, title: str, body: str, labels: Sequence[str]) -> str: ...


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


def confirmation(todo: IssuePlan) -> str:
    """Covers where the issues go as well as what they say: a dry run against one
    repository cannot confirm an apply against another."""
    payload = json.dumps([todo.destination, [asdict(d) for d in todo.to_open]], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def open_issues(todo: IssuePlan, sink: IssueSink, confirm: str) -> list[str]:
    """Open each planned issue. Returns what the sink reports for each (its URL)."""
    if confirm != confirmation(todo):
        raise StaleProposals("the proposals changed since the dry run; run --dry-run again")
    return [sink.create(d.title, d.body, d.labels) for d in todo.to_open]
