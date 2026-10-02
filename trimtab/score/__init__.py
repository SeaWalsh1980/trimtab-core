"""Score: tally evidence per ID, coverage, and the proposal threshold.

Citation *count* is never scored (risk R3): applied citations are reported for
context only. A proposal needs feedback on the same ID and scope from at least
`threshold` distinct PRs (plan section 8a: act only on repeats).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from trimtab.items import Proposal

THRESHOLD = 2
# Evidence older than this no longer counts: items unseen for 8 weeks expire
# (plan section 8a). It is also the bound on the ledger rebuilt from labels.
WINDOW_WEEKS = 8


@dataclass
class Tally:
    total: int = 0
    valid: int = 0
    feedback: dict = field(default_factory=lambda: defaultdict(dict))  # (id, scope) -> {pr: (kind, evidence)}
    applied: Counter = field(default_factory=Counter)

    @property
    def coverage(self) -> tuple[int, int]:
        return self.valid, self.total

    def feedback_prs(self, item_id: str) -> int:
        return len({pr for (i, _), prs in self.feedback.items() if i == item_id for pr in prs})


def tally(records: Iterable, since: str | None = None) -> Tally:
    """Records merged on or after `since` (YYYY-MM-DD); older evidence has decayed (plan section 8a)."""
    t = Tally()
    for r in records:
        if since is not None and r.merged_at[:10] < since:
            continue
        t.total += 1
        t.valid += 1 if r.valid else 0
        t.applied.update(set(r.applied) - {"none"})
        for f in r.feedback:
            t.feedback[(f.id, f.scope)].setdefault(r.number, (f.kind, f.evidence))
    return t


def candidates(t: Tally, threshold: int = THRESHOLD) -> list[Proposal]:
    out = []
    for (item_id, scope), prs in sorted(t.feedback.items()):
        if item_id == "none" or len(prs) < threshold:
            continue
        numbers = tuple(sorted(prs))
        out.append(Proposal(
            item_id=item_id, scope=scope,
            kinds=tuple(sorted({prs[n][0] for n in numbers})),
            evidence=tuple(prs[n][1] for n in numbers),
            prs=numbers,
        ))
    return out
