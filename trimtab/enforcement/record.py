"""The doctrine record's §Enforcement table, parsed into rows E1-E23 (ADR 0063 §2). Pure.

The rows are binding through AGW-1, which names the labels E1-E23, so the table must hold
exactly those rows, in order, each once. A row's text is hashed so that this base's manifest can
say which text it enforces: a changed row reads as a stale entry until this base records it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

ROW_IDS = tuple(f"E{n}" for n in range(1, 24))
HEADING = re.compile(r"^##\s+Enforcement\s*$")
HEADER = ("ID", "Condition", "Fact it rests on", "Owner")
SEPARATOR = re.compile(r"^\|(\s*:?-{3,}:?\s*\|)+\s*$")

BASE, PROJECT, BOTH = "base", "project", "both"
# Owner text, as the record writes it, to whose side shows the row and which hooks may enforce it.
OWNERS = {
    "Guard": (BASE, frozenset({"guard-paths.sh", "guard-github.sh"})),
    "Guard (plan-run layer)": (BASE, frozenset({"guard-paths.sh"})),
    "guard-github": (BASE, frozenset({"guard-github.sh"})),
    "trimtab-core": (BASE, frozenset()),
    "Consuming project": (PROJECT, frozenset()),
    "trimtab-core (job) and the consuming project's approver": (BOTH, frozenset()),
}


class RecordError(Exception):
    """The record has no table this base can read as E1-E23."""


@dataclass(frozen=True)
class Row:
    id: str
    condition: str
    fact: str
    owner: str

    @property
    def side(self) -> str:
        return OWNERS[self.owner][0]

    @property
    def hooks(self) -> frozenset[str]:
        """The hooks that may enforce a base row; empty where a command or job does instead."""
        return OWNERS[self.owner][1]

    @property
    def sha256(self) -> str:
        text = "\x1f".join((self.id, self.condition, self.fact, self.owner))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DerivedRow:
    """A row as the lock's derived field holds it: the owner and the digest of the text as merged, which is all
    `--server` judges a manifest entry by (`entry_problem`). `sha256` is stored, where `Row`'s is computed."""

    id: str
    owner: str
    sha256: str

    @property
    def side(self) -> str:
        return OWNERS[self.owner][0]

    @property
    def hooks(self) -> frozenset[str]:
        return OWNERS[self.owner][1]


def _cells(line: str) -> list[str]:
    body = line.strip()
    if not (body.startswith("|") and body.endswith("|")) or len(body) < 2:
        raise RecordError("a line of the §Enforcement table is not a table row")
    # An escaped pipe stays inside its cell.
    parts = re.split(r"(?<!\\)\|", body[1:-1])
    return [p.strip().replace("\\|", "|") for p in parts]


def parse(text: str) -> tuple[Row, ...]:
    """Rows E1-E23 from the record's §Enforcement table; anything else raises RecordError."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if HEADING.match(line)]
    if len(starts) != 1:
        raise RecordError(f"the record has {len(starts)} §Enforcement headings, not one")
    i = starts[0] + 1
    while i < len(lines) and not lines[i].lstrip().startswith("|"):
        if lines[i].startswith("#"):
            raise RecordError("§Enforcement has no table before the next heading")
        i += 1
    if i + 1 >= len(lines) or tuple(_cells(lines[i])) != HEADER or not SEPARATOR.match(lines[i + 1].strip()):
        raise RecordError("§Enforcement's table does not open with the ID, Condition, Fact, Owner header")
    rows = []
    for line in lines[i + 2:]:
        if not line.lstrip().startswith("|"):
            break
        cells = _cells(line)
        if len(cells) != 4 or not all(cells):
            raise RecordError("a row of the §Enforcement table does not have four non-empty cells")
        rid, condition, fact, owner = cells
        if owner not in OWNERS:
            raise RecordError(f"{rid}: owner {owner!r} is not one this base knows")
        rows.append(Row(rid, condition, fact, owner))
    ids = tuple(r.id for r in rows)
    if ids != ROW_IDS:
        raise RecordError(f"the table's rows are {', '.join(ids) or 'none'}, not E1-E23 in order")
    return tuple(rows)
