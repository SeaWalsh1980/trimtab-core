"""Ingestion by watermark (ADR 0005).

A merged PR is recorded in the ledger first and labelled `trimtab-ingested`
second, so a run killed half-way resumes where it stopped. Records are keyed by
PR number, so a PR re-read after a crash between the two steps counts once.

Side effects (the ledger write and the label) are gated: `plan` is a
dry run that changes nothing; `apply` needs the confirmation token that the dry
run printed, and refuses when the plan it is given has changed since.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Protocol

from trimtab.capture.prblock import block_version, check_body
from trimtab.config import ProjectConfig
from trimtab.items import Item

# The only label ingestion may add, and the only write it makes to GitHub.
LABEL = "trimtab-ingested"
LEDGER_VERSION = 1


class StalePlan(Exception):
    """The confirmation does not match the plan being applied."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    body: str
    merged_at: str


@dataclass(frozen=True)
class Unlabelled:
    """One bounded read of merged PRs not yet labelled: the first `limit` oldest merged, numbered above `above`
    when it is given, and what the read left out."""
    pulls: tuple[PullRequest, ...]
    # Numbered at or below `above`: left out before the limit, so they take up none of its places.
    below: tuple[int, ...]
    # Past the limit, for the next run; None when the read stopped at the limit and could not count them.
    # No default: a source states what it cut, never claims by omission that it cut nothing.
    left: int | None


class PullRequestSource(Protocol):
    def unlabelled_merged(self, label: str, merged_since: str | None, limit: int,
                          above: int | None = None) -> Unlabelled: ...

    def labelled_merged(self, label: str, merged_since: str, limit: int) -> list[PullRequest]: ...

    def add_label(self, number: int, label: str) -> None: ...


@dataclass(frozen=True)
class FeedbackRecord:
    id: str
    kind: str
    scope: str
    evidence: str


@dataclass(frozen=True)
class Record:
    number: int
    merged_at: str
    valid: bool
    legacy: bool = False
    applied: tuple[str, ...] = ()
    feedback: tuple[FeedbackRecord, ...] = ()
    problems: int = 0


@dataclass
class Ledger:
    records: dict[int, Record] = field(default_factory=dict)

    def put(self, record: Record) -> None:
        self.records[record.number] = record

    def to_json(self) -> str:
        rows = [asdict(r) for _, r in sorted(self.records.items())]
        return json.dumps({"version": LEDGER_VERSION, "records": rows}, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Ledger":
        data = json.loads(text)
        if data.get("version") != LEDGER_VERSION:
            raise ValueError(f"ledger version {data.get('version')!r}, expected {LEDGER_VERSION}")
        ledger = cls()
        for row in data["records"]:
            row["applied"] = tuple(row["applied"])
            row["feedback"] = tuple(FeedbackRecord(**f) for f in row["feedback"])
            ledger.put(Record(**row))
        return ledger


class LedgerStore(Protocol):
    def load(self) -> Ledger: ...

    def save(self, ledger: Ledger) -> None: ...


class InMemoryLedgerStore:
    def __init__(self):
        self._text = Ledger().to_json()

    def load(self) -> Ledger:
        return Ledger.from_json(self._text)

    def save(self, ledger: Ledger) -> None:
        self._text = ledger.to_json()


class FileLedgerStore:
    """A JSON file, replaced atomically so a crash never leaves half a ledger."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Ledger:
        if not self.path.exists():
            return Ledger()
        return Ledger.from_json(self.path.read_text(encoding="utf-8"))

    def save(self, ledger: Ledger) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(ledger.to_json())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


@dataclass(frozen=True)
class IngestPlan:
    records: tuple[Record, ...]
    baseline_skipped: tuple[int, ...]
    already_recorded: tuple[int, ...]
    # Cut off by the limit and left to the next run; None when the read could not count them.
    left: int | None
    # Written in a block version this Trimtab does not read (ADR 0007): neither
    # recorded nor labelled, so a newer Trimtab still finds and counts them.
    newer_version: tuple[int, ...] = ()


def record_of(pr: PullRequest, registry: Mapping[str, Item], backfill: bool = False) -> Record:
    result = check_body(pr.body, registry, backfill=backfill)
    block = result.block
    if block is None:
        return Record(pr.number, pr.merged_at, valid=False, problems=len(result.problems))
    return Record(
        number=pr.number, merged_at=pr.merged_at, valid=result.ok, legacy=block.legacy,
        applied=tuple(a.id for a in block.applied),
        feedback=tuple(FeedbackRecord(f.id, f.kind, f.scope, f.evidence) for f in block.feedback),
        problems=len(result.problems),
    )


def plan(source: PullRequestSource, ledger: Ledger, config: ProjectConfig,
         registry: Mapping[str, Item], limit: int, backfill: bool = False,
         floor: str | None = None) -> IngestPlan:
    """What a run would record. Reads only.

    PRs merged since the later of the adoption and `floor` (a YYYY-MM-DD day)
    are read; a backfill reads from the start and ignores both.
    """
    bounds = [] if backfill else [d for d in (config.adopted_at[:10] if config.adopted_at else None, floor) if d]
    since = max(bounds) if bounds else None
    # Baseline PRs are left out by number before the limit, so they never take
    # up a place a PR to record needs (a backfill reads them as any other).
    read = source.unlabelled_merged(LABEL, since, limit, above=None if backfill else config.baseline_pr)
    newer = tuple(p.number for p in read.pulls if _too_new(p.body))
    todo = [p for p in read.pulls if p.number not in newer]
    # Recorded but not labelled: a crash between the two steps. Re-recording is
    # idempotent (keyed by number), and it lets the label step finish.
    again = tuple(p.number for p in todo if p.number in ledger.records)
    return IngestPlan(tuple(record_of(p, registry, backfill) for p in todo), baseline_skipped=read.below,
                      already_recorded=again, left=read.left, newer_version=newer)


def _too_new(body: str) -> bool:
    _, refused = block_version(body)
    return refused is not None and refused.code == "unknown-version"


def confirmation(todo: IngestPlan) -> str:
    """A short digest of exactly what `apply` will write."""
    payload = json.dumps([asdict(r) for r in todo.records], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def apply(todo: IngestPlan, ledger: Ledger, store: LedgerStore, source: PullRequestSource,
          confirm: str) -> list[int]:
    """Record then label, one PR at a time. Returns the PR numbers labelled."""
    if confirm != confirmation(todo):
        raise StalePlan("the plan changed since the dry run; run --dry-run again")
    done = []
    for record in todo.records:
        ledger.put(record)
        store.save(ledger)
        source.add_label(record.number, LABEL)
        done.append(record.number)
    return done


def rebuild(pulls: Iterable[PullRequest], registry: Mapping[str, Item], baseline_pr: int = 0,
            backfill: bool = False) -> Ledger:
    """The ledger re-derived from PRs already labelled: their bodies are the durable record (ADR 0006).

    A routine has no durable disk, so in the cloud the ledger is not stored at
    all: the label is the watermark and the merged PR body is the record, read
    again each run within a bounded window (ADR 0006). Keyed by number, as a
    stored ledger is, so the result is the same however often it is rebuilt.
    """
    ledger = Ledger()
    for pr in pulls:
        if pr.number > baseline_pr or backfill:
            ledger.put(record_of(pr, registry, backfill))
    return ledger


def all_records(ledger: Ledger, todo: IngestPlan) -> Iterable[Record]:
    """The ledger as it would stand after `todo` is applied."""
    merged = dict(ledger.records)
    merged.update({r.number: r for r in todo.records})
    return merged.values()
