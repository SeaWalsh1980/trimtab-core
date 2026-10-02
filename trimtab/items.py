"""Harness items: the types every module shares.

Pure data and one protocol. Nothing here reads files or runs processes; the
modules that do (registry discovery, the gh adapter) depend on this, never the
reverse: dependencies point inward.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, Sequence


class Strength(Enum):
    """The strongest norm keyword in an item, from the most binding (REQUIRE) down."""

    REQUIRE = 5
    PROHIBIT = 4
    PREFER = 3
    ALLOW = 2
    AVOID = 1
    NONE = 0

    @property
    def binding(self) -> bool:
        """REQUIRE and PROHIBIT: no project may override, no finding may be graded below HIGH."""
        return self in (Strength.REQUIRE, Strength.PROHIBIT)

    @property
    def label(self) -> str:
        return "—" if self is Strength.NONE else self.name


KEYWORD = re.compile(r"\*\*(REQUIRE|PROHIBIT|PREFER|ALLOW|AVOID)\*\*")

# PREFIX-<local id>. The prefix may carry a project slug (CT-LAYERING); the
# local id is a section number (6, 6.2) for rules or a name for later types.
ITEM_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)*-[A-Za-z0-9][A-Za-z0-9._-]*$")


def strongest(text: str) -> Strength:
    found = [Strength[m] for m in KEYWORD.findall(text)]
    return max(found, key=lambda s: s.value, default=Strength.NONE)


@dataclass(frozen=True)
class Problem:
    """A typed, value-free finding. `where` names a location, never quotes content."""

    code: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.where}: {self.message}"


@dataclass(frozen=True)
class Item:
    id: str
    prefix: str
    local_id: str
    type: str
    source: str
    heading: str
    strength: Strength
    withdrawn: bool = False
    text: str = field(default="", compare=False, repr=False)


@dataclass(frozen=True)
class Discovery:
    items: tuple[Item, ...]
    problems: tuple[Problem, ...]


@dataclass(frozen=True)
class Proposal:
    """What the loop wants changed about one item. Rendered, never applied, in milestone 1."""

    item_id: str
    scope: str  # project | upstream
    kinds: tuple[str, ...]
    evidence: tuple[str, ...]
    prs: tuple[int, ...]


class ItemType(Protocol):
    """The one seam for item types. Milestone 1 has RuleType; skills add SkillType."""

    name: str

    def discover(self, repo) -> Discovery: ...

    def strength(self, item: Item) -> Strength: ...

    def lint(self, item: Item) -> Sequence[Problem]: ...

    def render_change(self, item: Item, proposal: Proposal) -> str: ...
