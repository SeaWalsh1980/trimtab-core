"""RuleType: instruction rules as harness items.

A rule file names its prefix in frontmatter (`id_prefix`), and each numbered
section heading (`## 6.`, `### 6.2`) is one item: `PFX-6`, `PFX-6.2`. Unnumbered
sections (a preamble, `## Examples`) are not items. The prefix lives in
frontmatter rather than the filename so a rename changes nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

import yaml

from trimtab.items import Discovery, Item, Problem, Proposal, Strength, strongest

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)^---\s*$\n?", re.S | re.M)
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.+)$")
FENCE = re.compile(r"^\s*(```|~~~)")
PREFIX = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)*$")
WITHDRAWN = re.compile(r"\(withdrawn\b", re.I)
OVERRIDES = "overrides.md"


def frontmatter(text: str) -> dict | None:
    match = FRONTMATTER.match(text)
    if match is None:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, text) for every heading outside a code fence."""
    found, fenced = [], False
    for n, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = HEADING.match(line)
        if m:
            found.append((n, len(m.group(1)), m.group(2)))
    return found


def parse_rule_file(source: str, text: str, prefix: str | None = None) -> tuple[list[Item], list[Problem]]:
    """Items in one rule file. `prefix` overrides frontmatter (never used for base rules)."""
    meta = frontmatter(text)
    if meta is None:
        return [], [Problem("bad-frontmatter", source, "no parseable YAML frontmatter")]
    prefix = prefix or meta.get("id_prefix")
    if not prefix:
        return [], [Problem("missing-prefix", source, "frontmatter has no id_prefix")]
    if not isinstance(prefix, str) or not PREFIX.match(prefix):
        return [], [Problem("bad-prefix", source, "id_prefix must be upper-case letters and digits")]

    lines = text.splitlines(keepends=True)
    heads = _headings(lines)
    items: list[Item] = []
    problems: list[Problem] = []
    seen: set[str] = set()
    for i, (start, level, title) in enumerate(heads):
        m = NUMBERED.match(title)
        if level < 2 or m is None:
            continue
        end = next((s for s, lv, _ in heads[i + 1:] if lv <= level), len(lines))
        body = "".join(lines[start:end])
        local = m.group(1)
        item_id = f"{prefix}-{local}"
        if item_id in seen:
            problems.append(Problem("duplicate-id", f"{source}:{start + 1}", f"{item_id} appears twice"))
            continue
        seen.add(item_id)
        items.append(Item(
            id=item_id, prefix=prefix, local_id=local, type="rule", source=source,
            heading=m.group(2), strength=strongest(body),
            withdrawn=bool(WITHDRAWN.search(m.group(2))), text=body,
        ))
    return items, problems


def clause_strength(item: Item, clause: str) -> Strength | None:
    """The keyword of the line that starts with `clause`, or None if no line does.

    ID-level citation (ADR 0005): when one clause of a section is meant,
    cite the ID plus the clause's first words. Matching ignores emphasis and
    list markers so `PREFER in-memory fakes` finds `- **PREFER** in-memory fakes`.
    """
    line = clause_line(item, clause)
    return None if line is None else strongest(line)


def clause_line(item: Item, clause: str) -> str | None:
    """The first line of the item that starts with `clause`, ignoring emphasis."""
    want = _plain(clause)
    if not want:
        return None
    return next((line for line in item.text.splitlines() if _plain(line).startswith(want)), None)


def _plain(s: str) -> str:
    s = re.sub(r"[*_`>]", "", s)
    s = re.sub(r"^\s*(?:[-+]|\d+\.)\s+", "", s)
    return " ".join(s.split()).lower()


class RuleType:
    name = "rule"

    def __init__(self, rules_dir: str = "rules", project_prefix: str | None = None):
        self.rules_dir = rules_dir
        self.project_prefix = project_prefix

    def discover(self, repo: Path) -> Discovery:
        items: list[Item] = []
        problems: list[Problem] = []
        directory = Path(repo) / self.rules_dir
        for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
            if path.name == OVERRIDES:
                continue  # deviations from base items, not items of its own
            source = path.relative_to(repo).as_posix()
            found, bad = parse_rule_file(source, path.read_text(encoding="utf-8"))
            if self.project_prefix:
                found, more = self._project_scope(source, found)
                bad = bad + more
            items += found
            problems += bad
        return Discovery(tuple(items), tuple(problems))

    def _project_scope(self, source: str, found: list[Item]) -> tuple[list[Item], list[Problem]]:
        """A project rule's prefix must be `<project prefix>-<SLUG>` (CT-LAYERING)."""
        want = self.project_prefix + "-"
        if found and not found[0].prefix.startswith(want):
            return [], [Problem("bad-prefix", source, f"project rule prefix must start with {want}")]
        return found, []

    def strength(self, item: Item) -> Strength:
        return item.strength

    def lint(self, item: Item) -> Sequence[Problem]:
        return []

    def render_change(self, item: Item, proposal: Proposal) -> str:
        kinds = ", ".join(proposal.kinds)
        prs = ", ".join(f"#{n}" for n in proposal.prs)
        return (
            f"Rule `{item.id}` ({item.source}, \"{item.heading}\", {item.strength.label}) "
            f"was reported as {kinds} in {prs}."
        )
