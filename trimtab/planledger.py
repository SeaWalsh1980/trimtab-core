"""The plan-run ledger (ADR 0063 §1).

`<config dir>/guard-state/plan-runs/<sha256 of owner/name>/<plan>/item-NN-partK.jsonl`, append-only,
written only by hooks, keyed by repository, plan and part, never by session. Machine-local.

Standard library only: the hooks run Python with `-I -S` (ENG-3). Every unreadable, planted or
malformed state raises `LedgerError`; nothing is skipped (SEC-2.5). A line holds a version, a type,
a time and the event's own fields, never a token or an environment value other than the run flag.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable

from trimtab import guardstate

VERSION = 1
SUBDIR = "plan-runs"
PASSES = ("trimtab-review", "security-review", "code-review")
EVENT_TYPES = ("push", "round", "draft", "rerequest", "stop", "conflict")
SHA = re.compile(r"[0-9a-f]{40}")


class LedgerError(Exception):
    """A ledger is absent where it must exist, planted, malformed, or would be started unsafely."""


def _sha(value: str, what: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise LedgerError(f"{what} is not a full commit SHA")
    return value


class Ledger:
    """One part's ledger. `clock` returns an RFC 3339 time with offset (TST-4)."""

    def __init__(self, config: Path, repo: str, plan: str, item: int, part: int, *, clock: Callable[[], str]):
        if not plan or "/" in plan or plan in (".", "..") or plan.startswith("."):
            raise LedgerError("a plan name must be a plain directory name")
        if not (isinstance(item, int) and isinstance(part, int) and item >= 1 and part >= 1):
            raise LedgerError("item and part are positive integers")
        self._config, self._clock = Path(config), clock
        self._repo_key = hashlib.sha256(repo.encode("utf-8")).hexdigest()
        self._plan = plan
        self.path = (self._config / guardstate.ROOT / SUBDIR / self._repo_key / plan
                     / f"item-{item:02d}-part{part}.jsonl")

    def _dir(self, create: bool) -> Path:
        try:
            return guardstate.directory(self._config, SUBDIR, self._repo_key, self._plan, create=create)
        except guardstate.GuardStateError as err:
            raise LedgerError(str(err)) from err

    def exists(self) -> bool:
        """True when the part has a ledger file. Absent is not unreadable: a bad parent raises."""
        try:
            self._dir(create=False)
        except LedgerError:
            if not (self._config / guardstate.ROOT / SUBDIR / self._repo_key / self._plan).exists():
                return False
            raise
        try:
            self.path.lstat()
        except FileNotFoundError:
            return False
        return True

    def start(self, *, remote_branch_exists: bool) -> None:
        """Create the ledger. A part whose branch is already on the remote never starts one: a fresh
        ledger would vouch for pushes it never saw. A ledger that exists is left as it is."""
        if self.exists():
            return
        if remote_branch_exists:
            raise LedgerError("the part's branch is already on the remote; a new ledger would vouch for unseen pushes")
        self._dir(create=True)
        try:
            guardstate.append(self.path, "", create=True)
        except guardstate.GuardStateError as err:
            raise LedgerError(str(err)) from err

    def events(self) -> list[dict]:
        try:
            text = guardstate.read(self.path)
        except guardstate.GuardStateError as err:
            raise LedgerError(str(err)) from err
        if text is None:
            raise LedgerError("the part has no ledger")
        out = []
        for raw in text.splitlines():
            try:
                ev = json.loads(raw)
            except ValueError as err:
                raise LedgerError("a ledger line is not JSON") from err
            if not isinstance(ev, dict) or ev.get("v") != VERSION or ev.get("type") not in EVENT_TYPES \
                    or not isinstance(ev.get("time"), str):
                raise LedgerError("a ledger line has an unknown shape or version")
            out.append(ev)
        return out

    def _append(self, event: dict, *, once: tuple[str, ...] = ()) -> None:
        """Add one event; with `once`, skip it when an earlier event agrees on every named field (REL-1)."""
        existing = self.events()
        if once and any(all(e.get(k) == event.get(k) for k in ("type", *once)) for e in existing):
            return
        line = json.dumps({"v": VERSION, "time": self._clock(), **event}, sort_keys=True) + "\n"
        try:
            guardstate.append(self.path, line)
        except guardstate.GuardStateError as err:
            raise LedgerError(str(err)) from err

    def push(self, branch: str, old: str, new: str, *, run_env: str) -> None:
        self._append({"type": "push", "branch": branch, "old": _sha(old, "old"), "new": _sha(new, "new"),
                      "run": run_env})

    def round(self, head: str, pass_name: str, *, before_draft: bool) -> None:
        if pass_name not in PASSES:
            raise LedgerError("an unknown review pass")
        self._append({"type": "round", "head": _sha(head, "head"), "pass": pass_name,
                      "before_draft": bool(before_draft)}, once=("head", "pass"))

    def draft(self, pr: int, head: str) -> None:
        self._append({"type": "draft", "pr": int(pr), "head": _sha(head, "head")}, once=("pr", "head"))

    def rerequest(self, head: str, reason: str) -> None:
        self._append({"type": "rerequest", "head": _sha(head, "head"), "reason": reason})

    def stop(self, reason: str) -> None:
        self._append({"type": "stop", "reason": reason}, once=("reason",))

    def conflict(self, head: str) -> None:
        """The push in hand answered a head that conflicted with the default branch (§5a, E12)."""
        self._append({"type": "conflict", "head": _sha(head, "head")}, once=("head",))

    def pre_draft_heads(self) -> list[str]:
        """Heads that carry any pass recorded before the draft opened, once each (E23's round count)."""
        return list(dict.fromkeys(e["head"] for e in self.events() if e["type"] == "round" and e["before_draft"]))

    def complete_rounds(self) -> list[str]:
        """Heads carrying all three passes, in the order each head first completed."""
        seen: dict[str, set[str]] = {}
        done: list[str] = []
        for ev in self.events():
            if ev["type"] != "round":
                continue
            passes = seen.setdefault(ev["head"], set())
            passes.add(ev["pass"])
            if len(passes) == len(PASSES) and ev["head"] not in done:
                done.append(ev["head"])
        return done
